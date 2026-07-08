"""retention.py : data-retention + offboarding (Phase 3 of the security checklist).

Three capabilities, one module:

  export_user_data(db, user_id)   -> a full, secret-free dump of a user's data
  delete_user_data(db, user_id)   -> full hard-delete + subprocessor revocation +
                                      a metadata-only DeletionAudit row
  run_purge_sweep(db, dry_run)    -> category-TTL purge of EPHEMERAL rows

Design decisions:
  - **Offboarding** builds on the existing FK-CASCADE deletes (the same pattern
    the demo-user purge uses). It records only METADATA in `DeletionAudit`
    (who/when/counts) — never deleted content — and best-effort revokes the
    user's live subprocessor access (Unipile) so deletion propagates outward.
  - **Category TTL purge is OFF by default and only touches ephemeral rows**
    (expired sessions, old finished jobs). Crown-jewel *content* (contacts,
    messages, notes) is intentionally NOT time-purged here: it's retained while
    the account is active and removed via `delete_user_data` at offboarding.
    Enable + set periods via env once a content retention schedule exists; until
    then `run_purge_sweep` reports what it WOULD do (dry-run) and deletes
    nothing. This avoids destroying real customer data on a guessed TTL.

Env config (all optional; purge stays off until explicitly enabled):
  SURPLUS_RETENTION_ENABLED     "1" to let run_purge_sweep actually delete
  SURPLUS_RETENTION_SESSION_DAYS  expired/revoked sessions older than N (default 90)
  SURPLUS_RETENTION_JOB_DAYS      finished jobs older than N days (default 30)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import inspect as _sa_inspect

from . import models

log = logging.getLogger("surplus.retention")

# Column names never included in an export, matched by suffix/exact. Secrets and
# key material must not leave via the data-export path.
_SECRET_SUFFIXES = ("_token", "_secret", "_hash", "_dek")
_SECRET_EXACT = {"access_token", "refresh_token", "password_hash",
                 "wrapped_dek", "session_token"}


def _is_secret_col(name: str) -> bool:
    return name in _SECRET_EXACT or name.endswith(_SECRET_SUFFIXES)


def _row_to_dict(row) -> dict:
    """Serialize an ORM row's columns to JSON-able values, dropping secrets."""
    out: dict[str, Any] = {}
    for col in _sa_inspect(row).mapper.column_attrs:
        name = col.key
        if _is_secret_col(name):
            continue
        val = getattr(row, name, None)
        out[name] = val.isoformat() if isinstance(val, datetime) else val
    return out


# ── export ──────────────────────────────────────────────────────────────────
def export_user_data(db, user_id: int) -> dict:
    """A full, secret-free export of everything owned by this user, for the
    offboarding "give me my data" right. Excludes OAuth tokens, password hash,
    session tokens, and wrapped DEKs by construction (`_is_secret_col`)."""
    user = db.get(models.User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")

    contacts = db.query(models.Contact).filter_by(user_id=user_id).all()
    contact_ids = [c.id for c in contacts]

    def _by_user(model, col="user_id"):
        return db.query(model).filter(getattr(model, col) == user_id).all()

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": _row_to_dict(user),
        "contacts": [_row_to_dict(c) for c in contacts],
        "interactions": [_row_to_dict(r) for r in
                         _by_user(models.RelationshipInteraction, "actor_user_id")],
        "outgoing_messages": [_row_to_dict(m) for m in _by_user(models.OutgoingMessage)],
        "events": [_row_to_dict(e) for e in _by_user(models.Event)],
    }
    # Contact identities/facts, if those models exist, keyed by the user's contacts.
    for attr, model_name in (("contact_identities", "ContactIdentity"),
                             ("contact_facts", "ContactFact")):
        model = getattr(models, model_name, None)
        if model is not None and contact_ids:
            rows = db.query(model).filter(model.contact_id.in_(contact_ids)).all()
            export[attr] = [_row_to_dict(r) for r in rows]
    return export


# ── delete (offboarding) ──────────────────────────────────────────────────────
def _revoke_subprocessors(db, user) -> list[str]:
    """Best-effort: cut the user's live subprocessor access so deletion
    propagates outward. Failures are logged, never fatal to the delete."""
    revoked: list[str] = []
    acct_ids = {getattr(user, a, None) for a in (
        "unipile_account_id", "unipile_email_account_id",
        "unipile_whatsapp_account_id")}
    acct_ids.discard(None)
    if acct_ids:
        try:
            from .integrations import linkedin_cookie
            for aid in acct_ids:
                try:
                    if linkedin_cookie.delete_account(str(aid)):
                        revoked.append(f"unipile:{aid}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("unipile revoke failed for %s: %s", aid, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("subprocessor revoke unavailable: %s", exc)
    return revoked


def delete_user_data(db, user_id: int, *, actor: str = "self",
                     reason: str = "") -> dict:
    """Hard-delete a user and everything they own, revoke subprocessor access,
    and write a metadata-only audit row. Returns per-category counts (the
    deletion confirmation). Idempotent-safe: a missing user returns not_found."""
    user = db.get(models.User, user_id)
    if user is None:
        return {"status": "not_found", "user_id": user_id}

    contacts = db.query(models.Contact).filter_by(user_id=user_id).all()
    events = db.query(models.Event).filter_by(user_id=user_id).all()
    counts: dict[str, int] = {
        "contacts": len(contacts),
        "events": len(events),
        "interactions": db.query(models.RelationshipInteraction).filter(
            models.RelationshipInteraction.actor_user_id == user_id).count(),
        "outgoing_messages": db.query(models.OutgoingMessage).filter_by(
            user_id=user_id).count(),
    }

    revoked = _revoke_subprocessors(db, user)

    # Delete the direct per-user rows explicitly — don't rely on DB-level CASCADE
    # (some of those FKs only get ON DELETE CASCADE via a runtime migration, and
    # TenantKey.tenant_id isn't a FK at all). (model, ownership column):
    direct = [
        (models.RelationshipInteraction, "actor_user_id"),
        (models.OutgoingMessage, "user_id"),
        (models.Session, "user_id"),
        (models.Job, "user_id"),
        (models.ConnectedAccount, "user_id"),
    ]
    if getattr(models, "TenantKey", None) is not None:
        direct.append((models.TenantKey, "tenant_id"))
    for model, col in direct:
        db.query(model).filter(getattr(model, col) == user_id).delete(
            synchronize_session=False)
    # Tree parents via ORM cascade (Python-side, dialect-independent): Contact ->
    # identities/facts, Event -> prospects -> edges/conversions/followups.
    for c in contacts:
        db.delete(c)
    for ev in events:
        db.delete(ev)
    db.delete(user)

    audit = models.DeletionAudit(
        subject_user_id=user_id, actor=actor, reason=(reason or "")[:500],
        counts_json=_json_counts({**counts, "subprocessors_revoked": len(revoked)}))
    db.add(audit)
    db.commit()

    log.info("deleted user %s (actor=%s) counts=%s revoked=%s",
             user_id, actor, counts, revoked)
    return {"status": "deleted", "user_id": user_id,
            "deleted_counts": counts, "subprocessors_revoked": revoked}


def _json_counts(counts: dict) -> str:
    import json
    return json.dumps(counts, default=str)


# ── category-TTL purge (ephemeral rows only; OFF by default) ───────────────────
def purge_enabled() -> bool:
    return (os.environ.get("SURPLUS_RETENTION_ENABLED") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _days(env: str, default: int) -> int:
    try:
        return max(1, int((os.environ.get(env) or "").strip()))
    except ValueError:
        return default


# ── content retention: summarize-then-expire message bodies ─────────────────
# The product's hot window is the last KEEP_LAST_N messages per contact (the
# drafter reads ~5 verbatim; N gives headroom) plus everything newer than the
# retention window. Older message BODIES are pure risk: the drafter only ever
# consumes them as the rolling thread_summary ContactFact. So: refresh that
# summary from the real thread, then blank the aged-out bodies, keeping the
# metadata skeleton (occurred_at / direction / channel / type) that cadence
# and health scoring read. Voice is untouched (users.voice_examples is
# derived, refreshed live from the provider — never from this archive).
#
# INVARIANT: a body is only expired after the contact's thread_summary fact
# demonstrably exists — a summarizer failure skips the contact, never drops
# content that was not first compressed.
#
# Tombstone shape: summary="" (the thread builder, signals, and re-summaries
# all skip empty-text rows already), title="", meta_json marks provenance.
_EXPIRED_META = '{"expired": true}'


def content_retention_days() -> int:
    """Days a message body is kept verbatim. 0 / unset = content expiry OFF."""
    try:
        return max(0, int((os.environ.get("SURPLUS_CONTENT_RETENTION_DAYS")
                           or "0").strip()))
    except ValueError:
        return 0


def content_keep_last_n() -> int:
    """Most-recent messages per contact that never expire, regardless of age
    (re-engaging a years-stale contact needs the actual last exchange)."""
    try:
        return max(1, int((os.environ.get("SURPLUS_CONTENT_KEEP_LAST_N")
                           or "20").strip()))
    except ValueError:
        return 20


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _expirable_rows(db, contact_id: int, *, cutoff: datetime,
                    keep_n: int) -> list:
    """Message rows past BOTH guards (older than cutoff AND beyond the last
    keep_n by recency) that still carry a body."""
    rows = (db.query(models.RelationshipInteraction)
            .filter(models.RelationshipInteraction.contact_id == contact_id,
                    models.RelationshipInteraction.interaction_type == "message")
            .order_by(models.RelationshipInteraction.occurred_at.desc())
            .all())
    return [r for r in rows[keep_n:]
            if (r.summary or "").strip()
            and r.occurred_at is not None
            and _aware(r.occurred_at) < cutoff]


def _refresh_thread_summary(db, contact, keep_n: int) -> bool:
    """Compress the contact's full real thread into the rolling thread_summary
    fact. True only when the fact verifiably exists afterwards."""
    from .agents.relationship.pipeline.context.gather import thread_from_timeline
    from .agents.relationship.pipeline.context.summary import window_and_summarize
    from .agents.relationship.spine import relationships as _rel
    from .agents.relationship.spine.memory import get_facts
    timeline = _rel.contact_timeline(db, contact)
    prior_full = thread_from_timeline(timeline)
    window_and_summarize(prior_full, keep_n, db=db,
                         user_id=contact.user_id, contact=contact)
    return bool(get_facts(db, contact.id, key="thread_summary"))


def expire_contact_message_bodies(db, contact, *, cutoff: datetime,
                                  keep_n: int, dry_run: bool = True) -> dict:
    """Summarize-then-expire ONE contact's aged-out message bodies."""
    expirable = _expirable_rows(db, contact.id, cutoff=cutoff, keep_n=keep_n)
    if not expirable or dry_run:
        return {"contact_id": contact.id, "expirable": len(expirable),
                "expired": 0}
    try:
        summarized = _refresh_thread_summary(db, contact, keep_n)
    except Exception as exc:  # noqa: BLE001
        log.warning("content retention: summary refresh failed for contact %s "
                    "(%s: %s) -> skipping expiry", contact.id,
                    type(exc).__name__, exc)
        summarized = False
    if not summarized:
        return {"contact_id": contact.id, "expirable": len(expirable),
                "expired": 0, "skipped": "no thread_summary"}
    for r in expirable:
        r.summary = ""
        r.title = ""
        r.meta_json = _EXPIRED_META
    db.commit()
    return {"contact_id": contact.id, "expirable": len(expirable),
            "expired": len(expirable)}


def run_content_retention(db, *, user_id: int | None = None,
                          dry_run: bool = True,
                          contact_limit: int = 200) -> dict:
    """One bounded content-retention pass. Requires BOTH the master retention
    switch (SURPLUS_RETENTION_ENABLED) and a nonzero
    SURPLUS_CONTENT_RETENTION_DAYS before it writes; dry_run reports what a
    real pass would expire. Per-contact commit so a long pass neither pins a
    pooled connection nor loses progress on a crash."""
    days = content_retention_days()
    if days <= 0:
        return {"enabled": False,
                "note": "set SURPLUS_CONTENT_RETENTION_DAYS to activate"}
    write = (not dry_run) and purge_enabled()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    keep_n = content_keep_last_n()

    q = (db.query(models.RelationshipInteraction.contact_id)
         .filter(models.RelationshipInteraction.interaction_type == "message",
                 models.RelationshipInteraction.contact_id.isnot(None),
                 models.RelationshipInteraction.occurred_at < cutoff,
                 models.RelationshipInteraction.summary != ""))
    if user_id is not None:
        q = (q.join(models.Contact,
                    models.Contact.id
                    == models.RelationshipInteraction.contact_id)
             .filter(models.Contact.user_id == user_id))
    contact_ids = [cid for (cid,) in q.distinct().limit(contact_limit).all()]

    checked = expirable = expired = skipped = 0
    for cid in contact_ids:
        contact = db.get(models.Contact, cid)
        if contact is None:
            continue
        checked += 1
        try:
            res = expire_contact_message_bodies(
                db, contact, cutoff=cutoff, keep_n=keep_n,
                dry_run=not write)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.warning("content retention: contact %s failed: %s: %s",
                        cid, type(exc).__name__, exc)
            continue
        expirable += res.get("expirable", 0)
        expired += res.get("expired", 0)
        if res.get("skipped"):
            skipped += 1
    result = {"enabled": True, "dry_run": not write,
              "retention_days": days, "keep_last_n": keep_n,
              "contacts_checked": checked, "bodies_expirable": expirable,
              "bodies_expired": expired, "contacts_skipped_no_summary": skipped}
    log.info("content retention (dry_run=%s): %s", not write, result)
    return result


def run_purge_sweep(db, *, dry_run: bool = True) -> dict:
    """Purge ephemeral rows past their category TTL. No-op unless
    SURPLUS_RETENTION_ENABLED. dry_run counts what WOULD be purged without
    deleting — the safe default so a misconfigured TTL can't destroy data."""
    if not purge_enabled():
        return {"enabled": False, "note": "set SURPLUS_RETENTION_ENABLED=1 to activate"}

    now = datetime.now(timezone.utc)
    session_cut = now - timedelta(days=_days("SURPLUS_RETENTION_SESSION_DAYS", 90))
    job_cut = now - timedelta(days=_days("SURPLUS_RETENTION_JOB_DAYS", 30))

    # Expired or long-revoked sessions (pure auth ephemera).
    sessions_q = db.query(models.Session).filter(
        (models.Session.expires_at < session_cut)
        | (models.Session.revoked_at.isnot(None)
           & (models.Session.revoked_at < session_cut)))
    # Finished jobs (done/failed/error) older than the job TTL.
    jobs_q = db.query(models.Job).filter(
        models.Job.status.in_(("done", "failed", "error", "cancelled")),
        models.Job.created_at < job_cut)

    result = {"enabled": True, "dry_run": dry_run,
              "sessions": sessions_q.count(), "jobs": jobs_q.count()}
    if not dry_run:
        sessions_q.delete(synchronize_session=False)
        jobs_q.delete(synchronize_session=False)
        db.commit()
    log.info("retention purge (dry_run=%s): %s", dry_run, result)
    return result
