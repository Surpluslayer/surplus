"""agents/relationship/contact_dedup.py : collapse duplicate Contacts.

The same person can land as separate Contact rows -- met on LinkedIn (li:slug) and
later emailed (em:hash) with no shared key at creation time. A fragmented spine means
the gather reads half a timeline + half the facts. This finds contacts that share a
STRONG identity key and merges them into ONE canonical row, reassigning every linked
prospect, interaction, and fact (history is preserved, never dropped).

Deterministic, owner-scoped, and dry_run by DEFAULT -- a merge deletes rows, so the
caller must opt into applying it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .... import models
from ..enrichment_cache import identity_keys

# Scalar fields copied from a dup into the canonical when the canonical's is empty.
_BACKFILL = ("name", "linkedin_url", "email", "company", "company_domain",
             "headline", "title", "email_thread_id", "preferred_channel")
_MIN = datetime.min.replace(tzinfo=timezone.utc)
_MAX = datetime.max.replace(tzinfo=timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _keys_for(c) -> set:
    """Every strong identity key a contact carries: derived from its linkedin_url +
    email, plus its stored primary_identity_key (an email-only capture's key isn't
    re-derivable if the email field is blank)."""
    keys = set(identity_keys(email=c.email or "", linkedin_url=c.linkedin_url or ""))
    if c.primary_identity_key:
        keys.add(c.primary_identity_key.strip().lower())
    return keys


def find_duplicate_groups(db, user_id: int) -> list[list]:
    """Groups (size > 1) of the user's Contacts that are the same person -- linked
    by a shared strong identity key via union-find. Read-only."""
    contacts = (db.query(models.Contact)
                .filter(models.Contact.user_id == user_id).all())
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:        # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_id = {c.id: c for c in contacts}
    key_owner: dict = {}
    for c in contacts:
        find(c.id)
        for k in _keys_for(c):
            if k in key_owner:
                union(c.id, key_owner[k])
            else:
                key_owner[k] = c.id

    groups: dict = {}
    for cid in by_id:
        groups.setdefault(find(cid), []).append(by_id[cid])
    return [g for g in groups.values() if len(g) > 1]


def _data_count(db, contact_id: int) -> int:
    return (db.query(models.Prospect).filter_by(contact_id=contact_id).count()
            + db.query(models.RelationshipInteraction).filter_by(contact_id=contact_id).count()
            + db.query(models.ContactFact).filter_by(contact_id=contact_id).count())


def _pick_canonical(db, group: list):
    """The richest contact wins (most linked rows); tie -> oldest, then lowest id.
    Keeping the most-connected row minimizes the reassignment + churn."""
    return max(group, key=lambda c: (
        _data_count(db, c.id),
        -_aware(c.created_at or _MAX).timestamp(),
        -c.id,
    ))


def merge_group(db, group: list, *, commit: bool = True) -> dict:
    """Merge a duplicate group into one canonical Contact. Reassigns prospects,
    interactions, and facts (newer wins on a (key,dedup_key) clash), backfills empty
    canonical scalars, unions seen_post_ids/vip, then deletes the dups."""
    import json
    canonical = _pick_canonical(db, group)
    dups = [c for c in group if c.id != canonical.id]
    moved = {"prospects": 0, "interactions": 0, "facts": 0, "facts_dropped": 0}

    canon_facts = {(f.key, f.dedup_key): f for f in
                   db.query(models.ContactFact).filter_by(contact_id=canonical.id).all()}
    seen: set = set()
    try:
        seen = set(json.loads(canonical.seen_post_ids or "[]"))
    except Exception:  # noqa: BLE001
        seen = set()

    for d in dups:
        for p in db.query(models.Prospect).filter_by(contact_id=d.id).all():
            p.contact_id = canonical.id
            moved["prospects"] += 1
        for it in db.query(models.RelationshipInteraction).filter_by(contact_id=d.id).all():
            it.contact_id = canonical.id
            moved["interactions"] += 1
        for f in db.query(models.ContactFact).filter_by(contact_id=d.id).all():
            ck = (f.key, f.dedup_key)
            existing = canon_facts.get(ck)
            if existing is None:
                f.contact_id = canonical.id
                canon_facts[ck] = f
                moved["facts"] += 1
            elif _aware(f.observed_at or _MIN) > _aware(existing.observed_at or _MIN):
                # dup's fact is newer -> it wins. Delete the loser FIRST and flush so
                # the UPDATE that follows can't collide on the unique (key,dedup).
                db.delete(existing)
                db.flush()
                f.contact_id = canonical.id
                canon_facts[ck] = f
                moved["facts"] += 1
                moved["facts_dropped"] += 1
            else:
                db.delete(f)
                moved["facts_dropped"] += 1
        for fld in _BACKFILL:
            if not getattr(canonical, fld, None) and getattr(d, fld, None):
                setattr(canonical, fld, getattr(d, fld))
        canonical.vip = bool(getattr(canonical, "vip", False) or getattr(d, "vip", False))
        try:
            seen |= set(json.loads(d.seen_post_ids or "[]"))
        except Exception:  # noqa: BLE001
            pass

    canonical.seen_post_ids = json.dumps(sorted(seen)[:200])
    db.flush()                       # persist all FK moves before deleting the dups
    for d in dups:
        db.delete(d)
    if commit:
        db.commit()
    return {"canonical_id": canonical.id, "merged_ids": [d.id for d in dups], **moved}


def dedup_user(db, user_id: int, *, dry_run: bool = True) -> dict:
    """Find + (optionally) merge all duplicate groups for one user. dry_run=True (the
    default) only REPORTS what it would merge; pass dry_run=False to apply."""
    groups = find_duplicate_groups(db, user_id)
    if dry_run:
        return {"dry_run": True,
                "groups": [sorted(c.id for c in g) for g in groups],
                "would_merge": sum(len(g) - 1 for g in groups)}
    detail = [merge_group(db, g, commit=False) for g in groups]
    db.commit()
    return {"dry_run": False, "merged_groups": len(detail), "detail": detail}
