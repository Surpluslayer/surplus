"""agents/relationship/identity.py : contact identity-resolution + merge engine.

The same human can land as several Contact rows -- met on LinkedIn (li:slug),
later emailed (em:hash), saved in Google Contacts with a phone -- with no shared
key at creation time. A fragmented spine means the gather reads half a timeline +
half the facts, and the messaging agent talks to a stranger. This module knows
when two Contact rows are the SAME PERSON and can collapse them into one canonical
contact, reassigning every child row so history is preserved, never dropped.

SAFETY CONTRACT
  - DRY-RUN BY DEFAULT. find_duplicate_clusters is read-only; merge_contacts and
    backfill_merge only WRITE when an explicit apply=True is passed. The default
    computes + returns the plan and changes nothing -- a merge deletes rows, so the
    caller must opt in.
  - STRONG SIGNALS ONLY for an auto-merge. We merge on an exact shared normalized
    email, an exact shared LinkedIn id, an exact shared normalized phone, or a
    BRIDGE record (one contact carrying identities that each match a DIFFERENT
    existing contact). We NEVER auto-merge on a fuzzy signal (same first name) --
    that mis-merges different people. A same-normalized-full-name + same
    company_domain pair is MEDIUM confidence : it is REPORTED for review but never
    auto-merged.

This is the ContactIdentity-backed successor to spine/dedup.py : that one keys
only on a contact's own stored fields and can't see multi-identity / bridge cases
or phone, and misses OutgoingMessage. This module reads the ContactIdentity table.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from ... import models
from ...triage.enrichment_cache import _linkedin_slug

log = logging.getLogger(__name__)

# Scalar fields unioned from a duplicate onto the survivor (prefer survivor's
# existing non-null, else take the duplicate's).
# Contact has no linkedin_provider_id column (that lives on Prospect/User), so it
# is intentionally omitted here -- identities_of_contact still reads it via getattr
# and harmlessly gets None.
_BACKFILL = ("name", "linkedin_url", "linkedin_public_id",
             "email", "phone", "company", "company_domain", "headline", "title",
             "email_thread_id", "preferred_channel")
_MIN = datetime.min.replace(tzinfo=timezone.utc)
_MAX = datetime.max.replace(tzinfo=timezone.utc)


# ── normalization : an identity VALUE only means "same person" if normalized ──

def normalize_email(email: Optional[str]) -> str:
    """Lowercased, trimmed email; '' when not a real address."""
    e = (email or "").strip().lower()
    if not e or "@" not in e or e.startswith("@") or e.endswith("@"):
        return ""
    return e


def normalize_phone(phone: Optional[str]) -> str:
    """Last 10 digits of a phone (so +1-415-555-1234 == 415.555.1234); '' when we
    can't key confidently (< 10 digits). Mirrors enrichment_cache._phone_hash."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        return ""
    return digits[-10:]


def normalize_linkedin(*, public_id: str = "", provider_id: str = "",
                       url: str = "") -> str:
    """A single normalized LinkedIn identity value, preferring an explicit
    public_id, then provider_id, then a slug parsed from a URL. Lowercased. '' when
    none is derivable."""
    pid = (public_id or "").strip().lower()
    if pid:
        return pid
    prov = (provider_id or "").strip().lower()
    if prov:
        return prov
    slug = _linkedin_slug(url) if url else ""
    return (slug or "").strip().lower()


def identities_of_contact(contact) -> list[tuple[str, str]]:
    """Every (kind, normalized_value) strong identity derivable from a Contact's
    OWN stored fields. Used by the backfill + as a fallback. STRONG only -- never
    name/company."""
    out: list[tuple[str, str]] = []
    em = normalize_email(getattr(contact, "email", None))
    if em:
        out.append(("email", em))
    ph = normalize_phone(getattr(contact, "phone", None))
    if ph:
        out.append(("phone", ph))
    li = normalize_linkedin(
        public_id=getattr(contact, "linkedin_public_id", None) or "",
        provider_id=getattr(contact, "linkedin_provider_id", None) or "",
        url=getattr(contact, "linkedin_url", None) or "",
    )
    if li:
        out.append(("linkedin", li))
    return out


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── duplicate detection ──────────────────────────────────────────────────────

def find_duplicate_clusters(db, user) -> list[dict]:
    """Read-only. Cluster the user's Contacts that are the SAME PERSON by STRONG
    shared identities, using the ContactIdentity table (union-find over shared
    (kind, value) keys). Bridge records fall out naturally : a contact whose
    identities span two otherwise-separate contacts pulls them into one cluster.

    Returns a list of clusters, each::

        {
          "contact_ids": [sorted ids],
          "signals": [ {kind, value, contact_ids:[...]} , ... ],
          "bridge": bool,           # a single contact joined >=2 identity-groups
          "confidence": float,      # 1.0 for a single strong key, slightly higher
                                    # when corroborated by multiple shared signals
        }

    Only clusters of size > 1 are returned. MEDIUM (name+company_domain) pairs are
    NOT included here -- see find_review_candidates.
    """
    user_id = getattr(user, "id", user)
    contacts = (db.query(models.Contact)
                .filter(models.Contact.user_id == user_id).all())
    by_id = {c.id: c for c in contacts}
    if not by_id:
        return []

    # (kind, value) -> set of contact_ids carrying it. Prefer ContactIdentity rows;
    # fall back to the contact's own fields so this works pre-backfill too.
    value_to_contacts: dict[tuple[str, str], set] = {}
    contact_to_values: dict[int, set] = {cid: set() for cid in by_id}

    rows = (db.query(models.ContactIdentity)
            .filter(models.ContactIdentity.user_id == user_id).all())
    seen_via_table: set = set()
    for r in rows:
        if r.contact_id not in by_id or not r.value:
            continue
        k = (r.kind, r.value)
        value_to_contacts.setdefault(k, set()).add(r.contact_id)
        contact_to_values[r.contact_id].add(k)
        seen_via_table.add(r.contact_id)
    for cid, c in by_id.items():
        for k in identities_of_contact(c):
            value_to_contacts.setdefault(k, set()).add(cid)
            contact_to_values[cid].add(k)

    # union-find over contacts that share any strong identity value.
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for cid in by_id:
        find(cid)
    shared_signals: list[tuple[str, str, list]] = []
    for (kind, value), cids in value_to_contacts.items():
        if len(cids) > 1:
            ordered = sorted(cids)
            shared_signals.append((kind, value, ordered))
            first = ordered[0]
            for other in ordered[1:]:
                union(first, other)

    clusters: dict = {}
    for cid in by_id:
        clusters.setdefault(find(cid), []).append(cid)

    out: list[dict] = []
    for members in clusters.values():
        if len(members) <= 1:
            continue
        member_set = set(members)
        sigs = [{"kind": k, "value": v,
                 "contact_ids": [c for c in cids if c in member_set]}
                for (k, v, cids) in shared_signals
                if member_set & set(cids)]
        # A bridge cluster : some single contact carries identities that, taken
        # individually, matched two DIFFERENT contacts (i.e. >2 contacts pulled in,
        # or the shared signals don't all share one common contact).
        is_bridge = _is_bridge(member_set, sigs)
        confidence = 1.0 if len(sigs) <= 1 and not is_bridge else min(
            1.0, 0.9 + 0.05 * len(sigs))
        out.append({
            "contact_ids": sorted(members),
            "signals": sorted(sigs, key=lambda s: (s["kind"], s["value"])),
            "bridge": is_bridge,
            "confidence": round(confidence, 3),
        })
    return sorted(out, key=lambda c: c["contact_ids"])


def _is_bridge(member_set: set, sigs: list[dict]) -> bool:
    """True when a SINGLE contact links two otherwise-separate identity groups : it
    appears in >=2 shared signals whose OTHER members don't themselves share a
    signal (e.g. a contact carrying an email that matches contact X and a linkedin
    id that matches a different contact Y, where X and Y share nothing else)."""
    if len(sigs) < 2:
        return False
    for cid in member_set:
        in_sigs = [set(s["contact_ids"]) for s in sigs if cid in s["contact_ids"]]
        if len(in_sigs) < 2:
            continue
        # the co-members this contact bridges, grouped per signal
        co_groups = [ids - {cid} for ids in in_sigs]
        # bridge if at least two of those groups are disjoint (separate people)
        for i in range(len(co_groups)):
            for j in range(i + 1, len(co_groups)):
                if co_groups[i] and co_groups[j] and not (co_groups[i] & co_groups[j]):
                    return True
    return False


def find_review_candidates(db, user) -> list[dict]:
    """MEDIUM-confidence pairs to FLAG FOR REVIEW (never auto-merge) : two contacts
    with the same normalized full-name AND the same company_domain but NO shared
    strong identity. Read-only."""
    user_id = getattr(user, "id", user)
    contacts = (db.query(models.Contact)
                .filter(models.Contact.user_id == user_id).all())
    # Group by (lower full name, lower company_domain).
    buckets: dict[tuple[str, str], list] = {}
    for c in contacts:
        name = (c.name or "").strip().lower()
        dom = (c.company_domain or "").strip().lower()
        if not name or not dom:
            continue
        buckets.setdefault((name, dom), []).append(c)

    # Pre-compute strong-identity sets so we can exclude pairs that ALSO share a
    # strong key (those are handled by find_duplicate_clusters as auto-merge).
    out: list[dict] = []
    for (name, dom), group in buckets.items():
        if len(group) < 2:
            continue
        ids_sets = {c.id: set(identities_of_contact(c)) for c in group}
        # only keep if at least one pair shares NO strong key
        members = sorted(c.id for c in group)
        shares_strong = False
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if ids_sets[group[i].id] & ids_sets[group[j].id]:
                    shares_strong = True
        if shares_strong:
            continue
        out.append({
            "contact_ids": members,
            "signal": {"kind": "name+company_domain",
                       "name": name, "company_domain": dom},
            "confidence": 0.5,
            "auto_merge": False,
            "reason": "medium confidence -- same name + company domain, review before merge",
        })
    return sorted(out, key=lambda c: c["contact_ids"])


# ── survivor selection + merge ───────────────────────────────────────────────

def _data_count(db, contact_id: int) -> int:
    return (db.query(models.Prospect).filter_by(contact_id=contact_id).count()
            + db.query(models.RelationshipInteraction).filter_by(contact_id=contact_id).count()
            + db.query(models.ContactFact).filter_by(contact_id=contact_id).count()
            + db.query(models.OutgoingMessage).filter_by(contact_id=contact_id).count())


def pick_survivor_id(db, contact_ids: list[int]) -> int:
    """The richest contact survives (most linked rows == richest history); tie ->
    oldest, then LOWEST id. Keeping the most-connected row minimizes reassignment."""
    rows = {c.id: c for c in db.query(models.Contact)
            .filter(models.Contact.id.in_(contact_ids)).all()}
    return max(contact_ids, key=lambda cid: (
        _data_count(db, cid),
        -_aware(getattr(rows.get(cid), "created_at", None) or _MAX).timestamp(),
        -cid,
    ))


def merge_contacts(db, *, survivor_id: int, duplicate_id: int,
                   apply: bool = False) -> dict:
    """Merge `duplicate_id` INTO `survivor_id`. Reassigns ALL child rows
    (prospects, relationship_interactions, contact_facts, outgoing_messages),
    moves the duplicate's ContactIdentity rows to the survivor (deduped, keeping a
    single is_primary), unions the scalar fields onto the survivor (prefer
    survivor's existing non-null, else most-complete from the duplicate), deletes
    the duplicate Contact, and records provenance.

    apply=False (DEFAULT) computes + RETURNS the plan and writes NOTHING.
    Idempotent : merging a non-existent / already-merged duplicate is a no-op.
    """
    if survivor_id == duplicate_id:
        return {"applied": False, "noop": True, "reason": "survivor == duplicate",
                "survivor_id": survivor_id, "duplicate_id": duplicate_id}

    survivor = db.get(models.Contact, survivor_id)
    duplicate = db.get(models.Contact, duplicate_id)
    if survivor is None or duplicate is None:
        return {"applied": False, "noop": True,
                "reason": "survivor or duplicate not found",
                "survivor_id": survivor_id, "duplicate_id": duplicate_id}
    if survivor.user_id != duplicate.user_id:
        # SAFETY : never merge across owners.
        return {"applied": False, "noop": True, "reason": "different owners",
                "survivor_id": survivor_id, "duplicate_id": duplicate_id}

    moved = {"prospects": 0, "interactions": 0, "facts": 0, "facts_dropped": 0,
             "outgoing_messages": 0, "identities": 0, "identities_dropped": 0}
    fields_filled: list[str] = []

    # ── child rows ───────────────────────────────────────────────────────────
    prospects = db.query(models.Prospect).filter_by(contact_id=duplicate_id).all()
    moved["prospects"] = len(prospects)
    interactions = db.query(models.RelationshipInteraction).filter_by(
        contact_id=duplicate_id).all()
    moved["interactions"] = len(interactions)
    outgoing = db.query(models.OutgoingMessage).filter_by(
        contact_id=duplicate_id).all()
    moved["outgoing_messages"] = len(outgoing)

    dup_facts = db.query(models.ContactFact).filter_by(contact_id=duplicate_id).all()
    canon_facts = {(f.key, f.dedup_key): f for f in
                   db.query(models.ContactFact).filter_by(contact_id=survivor_id).all()}
    fact_moves: list = []
    fact_drops: list = []
    for f in dup_facts:
        ck = (f.key, f.dedup_key)
        existing = canon_facts.get(ck)
        if existing is None:
            fact_moves.append(f)
            canon_facts[ck] = f
        elif _aware(f.observed_at or _MIN) > _aware(existing.observed_at or _MIN):
            fact_drops.append(existing)   # dup's fact is newer -> it wins
            fact_moves.append(f)
            canon_facts[ck] = f
        else:
            fact_drops.append(f)
    moved["facts"] = len(fact_moves)
    moved["facts_dropped"] = len(fact_drops)

    # ── identities : move the dup's, dedup against survivor's ─────────────────
    surv_idents = db.query(models.ContactIdentity).filter_by(
        contact_id=survivor_id).all()
    surv_keys = {(i.kind, i.value) for i in surv_idents}
    dup_idents = db.query(models.ContactIdentity).filter_by(
        contact_id=duplicate_id).all()
    ident_moves: list = []
    ident_drops: list = []
    for i in dup_idents:
        if (i.kind, i.value) in surv_keys:
            ident_drops.append(i)
        else:
            ident_moves.append(i)
            surv_keys.add((i.kind, i.value))
    moved["identities"] = len(ident_moves)
    moved["identities_dropped"] = len(ident_drops)

    # ── scalar union plan ─────────────────────────────────────────────────────
    for fld in _BACKFILL:
        if not getattr(survivor, fld, None) and getattr(duplicate, fld, None):
            fields_filled.append(fld)

    plan = {
        "applied": False,
        "survivor_id": survivor_id,
        "duplicate_id": duplicate_id,
        "moved": moved,
        "fields_filled": fields_filled,
        "vip_union": bool(getattr(survivor, "vip", False)
                          or getattr(duplicate, "vip", False)),
    }

    if not apply:
        return plan

    # ── WRITE ──────────────────────────────────────────────────────────────────
    for p in prospects:
        p.contact_id = survivor_id
    for it in interactions:
        it.contact_id = survivor_id
    for m in outgoing:
        m.contact_id = survivor_id
    for f in fact_drops:
        db.delete(f)
    db.flush()  # clear the unique (key,dedup) before the moves land
    for f in fact_moves:
        f.contact_id = survivor_id

    for i in ident_drops:
        db.delete(i)
    db.flush()
    for i in ident_moves:
        i.contact_id = survivor_id
        i.is_primary = False  # survivor keeps its own primary

    # scalar union (prefer survivor's existing non-null).
    for fld in fields_filled:
        setattr(survivor, fld, getattr(duplicate, fld))
    survivor.vip = plan["vip_union"]
    # union seen_post_ids
    try:
        seen = set(json.loads(survivor.seen_post_ids or "[]"))
        seen |= set(json.loads(duplicate.seen_post_ids or "[]"))
        survivor.seen_post_ids = json.dumps(sorted(seen)[:200])
    except Exception:  # noqa: BLE001
        pass

    db.flush()
    db.delete(duplicate)
    db.flush()

    plan["applied"] = True
    log.info("identity.merge applied survivor=%s duplicate=%s moved=%s fields=%s",
             survivor_id, duplicate_id, moved, fields_filled)
    return plan


def record_identity(db, *, contact, kind: str, value: str,
                    source: str = "manual", is_primary: bool = False,
                    confidence: float = 1.0):
    """Idempotently attach ONE strong identity to a contact (no commit).

    Returns the existing-or-new ContactIdentity, or None when `value` normalizes to
    empty. Respects the (user_id, kind, value) unique : if the value already exists
    for this OWNER it is returned as-is (it may belong to ANOTHER contact -- that's
    the dedup signal a future creation-path hook should act on, see the TODO at
    link_or_create below). Provided as the building block for that hook; not yet
    wired into the upsert sites."""
    norm = (normalize_email(value) if kind == "email"
            else normalize_phone(value) if kind == "phone"
            else normalize_linkedin(public_id=value))
    if not norm:
        return None
    uid = contact.user_id
    existing = (db.query(models.ContactIdentity)
                .filter_by(user_id=uid, kind=kind, value=norm).first())
    if existing is not None:
        return existing
    row = models.ContactIdentity(
        contact_id=contact.id, user_id=uid, kind=kind, value=norm,
        is_primary=is_primary, source=source, confidence=confidence)
    db.add(row)
    return row


# ── creation-path hook : link to an EXISTING person before forking a dup ──────
#
# The upsert sites (email_sync, linkedin_chat_sync, spine/relationships,
# google_sync) historically keyed ONLY on (user_id, primary_identity_key) -- the
# single STRONGEST key of the incoming source. So an email-sync mint (em:) and a
# LinkedIn mint (li:) for the SAME person never met, forking a duplicate. The two
# helpers below are the fix : each create site normalizes EVERY strong identity it
# knows, looks up an existing contact by ANY of them (not just the primary), and
# registers all of them onto whatever contact wins. All fail-soft : a lookup or
# registration error must never break a sync.


def normalize_identity(kind: str, value: str) -> str:
    """The normalized value for a (kind, raw-value), or '' when it can't key."""
    if kind == "email":
        return normalize_email(value)
    if kind == "phone":
        return normalize_phone(value)
    if kind == "linkedin":
        # accept either a slug/public_id or a full profile URL
        v = (value or "").strip()
        if "/" in v or "linkedin.com" in v.lower():
            return normalize_linkedin(url=v)
        return normalize_linkedin(public_id=v)
    return ""


def strong_identities(*, email: str = "", linkedin_url: str = "",
                      linkedin_public_id: str = "", phone: str = "",
                      ) -> list[tuple[str, str]]:
    """Every (kind, normalized_value) strong identity derivable from the raw
    signals a source knows for one person. STRONG only (email/linkedin/phone);
    de-duplicated; '' values dropped."""
    out: list[tuple[str, str]] = []
    seen: set = set()

    def _add(kind: str, norm: str):
        if norm and (kind, norm) not in seen:
            seen.add((kind, norm))
            out.append((kind, norm))

    _add("email", normalize_email(email))
    _add("linkedin", normalize_linkedin(public_id=linkedin_public_id,
                                        url=linkedin_url))
    _add("phone", normalize_phone(phone))
    return out


def lookup_contact_by_identities(db, *, user_id: int,
                                 identities: list[tuple[str, str]]):
    """The existing Contact for `user_id` carrying ANY of these normalized
    identities, or None. Reads the ContactIdentity table first (the authoritative
    multi-identity index), then falls back to matching contacts' OWN row fields so
    pre-hook rows (which may have no ContactIdentity yet) are still found. Fail-soft
    : any error -> None (the caller falls back to its own primary-key lookup)."""
    if not identities:
        return None
    try:
        wanted = set(identities)
        rows = (db.query(models.ContactIdentity)
                .filter(models.ContactIdentity.user_id == user_id)
                .filter(models.ContactIdentity.kind.in_({k for k, _ in identities}))
                .all())
        for r in rows:
            if (r.kind, r.value) in wanted:
                c = db.get(models.Contact, r.contact_id)
                if c is not None and c.user_id == user_id:
                    return c
        # Fallback : scan the user's contacts' own fields (covers rows not yet
        # mirrored into ContactIdentity). Bounded by the book size.
        contacts = (db.query(models.Contact)
                    .filter(models.Contact.user_id == user_id).all())
        for c in contacts:
            if wanted & set(identities_of_contact(c)):
                return c
    except Exception:  # noqa: BLE001 : a lookup error never breaks a sync
        db.rollback()
        return None
    return None


def register_identities(db, *, contact, identities: list[tuple[str, str]],
                        source: str = "sync",
                        primary_key: str = "") -> int:
    """Attach each strong identity to `contact` (idempotent, no commit). Marks the
    one whose "kind:value" prefix matches `primary_key` as is_primary. Returns the
    count registered/seen. Fail-soft : never raises."""
    n = 0
    pk = (primary_key or "").strip().lower()
    for kind, norm in identities:
        try:
            prefix = {"email": "em:", "linkedin": "li:", "phone": "ph:"}.get(kind, "")
            is_primary = bool(pk) and pk.startswith(prefix) if prefix else False
            # value is already normalized; record_identity re-normalizes harmlessly
            row = record_identity(db, contact=contact, kind=kind, value=norm,
                                  source=source, is_primary=is_primary)
            if row is not None:
                n += 1
        except Exception:  # noqa: BLE001 : one bad identity never sinks a sync
            continue
    return n


def backfill_merge(db, user, *, apply: bool = False) -> dict:
    """Find every duplicate cluster for `user` and merge each. DRY-RUN BY DEFAULT
    (apply=False) : returns a structured report of the clusters, the matching
    signals, and what WOULD move -- writing NOTHING. Pass apply=True to perform the
    merges (commits once at the end).

    Returns::

        {
          "dry_run": bool,
          "user_id": int,
          "clusters": [ {contact_ids, signals, bridge, confidence,
                         survivor_id, merges:[<merge_contacts plan>...]} ],
          "review": [ <name+company_domain medium candidate> ... ],
          "would_merge": int,   # number of duplicate rows that would be removed
        }
    """
    user_id = getattr(user, "id", user)
    clusters = find_duplicate_clusters(db, user)
    review = find_review_candidates(db, user)

    report_clusters: list[dict] = []
    would_merge = 0
    for cl in clusters:
        ids = cl["contact_ids"]
        survivor_id = pick_survivor_id(db, ids)
        merges = []
        for dup_id in ids:
            if dup_id == survivor_id:
                continue
            plan = merge_contacts(db, survivor_id=survivor_id,
                                  duplicate_id=dup_id, apply=apply)
            merges.append(plan)
            if not plan.get("noop"):
                would_merge += 1
        report_clusters.append({**cl, "survivor_id": survivor_id, "merges": merges})

    if apply:
        db.commit()

    return {
        "dry_run": not apply,
        "user_id": user_id,
        "clusters": report_clusters,
        "review": review,
        "would_merge": would_merge,
    }
