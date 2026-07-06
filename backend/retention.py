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
