"""
routes/admin.py : cron / operator-triggered tasks.

    POST /admin/run-followups   shared-secret auth (X-Admin-Token)

Idempotent enough to hit from an external cron (Railway, GitHub Actions)
on a regular schedule. Picks prospects that:
  - have a `message_sent` outreach row (the first post-accept DM landed)
  - have not received a `message_replied` since
  - have fewer than FOLLOWUP_MAX_PER_PROSPECT `follow_up_sent` rows
  - last `message_sent` is older than FOLLOWUP_DELAY_HOURS

For each, composes a follow-up and sends via the prospect's owning user's
LinkedIn account (same per-user routing the webhook auto-DM uses).
"""
from __future__ import annotations
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from .. import config, models
from ..agents.sender import send_and_log
from ..auth import _as_aware_utc
from ..db import get_db
from ..providers import (
    LinkedInProvider,
    get_provider,
    get_provider_for_prospect,
)


class PendingReplyOut(BaseModel):
    id: int
    prospect_id: int
    prospect_name: str
    inbound_body: str
    classification: str
    draft_text: str
    reasoning: str
    status: str
    created_at: datetime


class ApproveBody(BaseModel):
    """Optional edited text : when present, sent instead of the draft."""
    edited_text: Optional[str] = None


class RejectBody(BaseModel):
    reason: Optional[str] = None


class VoiceExamplesBody(BaseModel):
    """Operator's curated outreach exemplars used as voice-matching style
    guides. List of strings, each is one past outreach message."""
    examples: list[str]


class MergeUsersBody(BaseModel):
    """Merge `from_user_id` (the orphaned/duplicate row) INTO `to_user_id`
    (the survivor). Re-points every FK, optionally copies billing forward,
    then deletes the source row. dry_run defaults True : preview the counts
    before committing anything."""
    from_user_id: int
    to_user_id: int
    dry_run: bool = True


router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """Constant-time compare the X-Admin-Token header against ADMIN_TOKEN env.

    Returns 404 (not 401/403) on missing-or-wrong, matching the demo route's
    no-fingerprinting posture : an attacker scanning shouldn't learn this
    endpoint exists.
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(404, "Not Found")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(404, "Not Found")


def _eligible_prospects(db: Session) -> list[models.Prospect]:
    """Find every prospect that's due for a follow-up right now.

    Eager-loads `outreach` so the per-prospect timeline scan doesn't trigger
    one query per row. Legacy email-flavored states (sent/opened/replied)
    coexist with the canonical LinkedIn states here, which is why we walk
    the timeline in Python rather than write a SQL aggregate.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.FOLLOWUP_DELAY_HOURS
    )
    candidates = (db.query(models.Prospect)
                    .filter(models.Prospect.status == "contacted")
                    .options(selectinload(models.Prospect.outreach))
                    .all())

    # Outbound DM states we can both anchor timing on AND prove the thread is
    # live enough to DM. We deliberately anchor on the MOST RECENT outbound
    # touch (a first DM or any prior follow-up), not the first DM, so the
    # cadence stays "time sensitive based on when you last wrote".
    OUTBOUND_DM_STATES = {"message_sent", "follow_up_sent"}
    REPLIED_STATES = {"message_replied", "replied"}  # canonical + legacy

    rows: list[models.Prospect] = []
    for p in candidates:
        if not p.outreach:
            continue
        last_outbound_ts: Optional[datetime] = None  # most recent DM we sent
        has_dm = False                                # at least one real DM out
        replied = False
        followup_count = 0
        for o in p.outreach:
            if o.state in OUTBOUND_DM_STATES:
                if o.state == "message_sent":
                    has_dm = True
                if o.state == "follow_up_sent":
                    followup_count += 1
                ts = _as_aware_utc(o.ts)
                if last_outbound_ts is None or ts > last_outbound_ts:
                    last_outbound_ts = ts
            elif o.state in REPLIED_STATES:
                replied = True

        if replied:
            continue
        if followup_count >= config.FOLLOWUP_MAX_PER_PROSPECT:
            continue
        # DM-able guard : a bare unaccepted invite has no thread to nudge, so
        # an invite_sent-only prospect is never eligible (no message_sent).
        if not has_dm:
            continue
        # Timing : nudge only once the most recent outbound touch has aged past
        # the delay window.
        if last_outbound_ts is None or last_outbound_ts > cutoff:
            continue
        rows.append(p)
    return rows


@router.post("/run-followups", status_code=200)
def run_followups(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Send a follow-up DM to every prospect currently due for one.

    Designed for hourly cron : running it more often is harmless (the
    eligibility window won't shift inside an hour and follow-up rows would
    just exceed FOLLOWUP_MAX_PER_PROSPECT on the second run).
    """
    from ..agents.outreach import compose_followup
    fallback_provider = get_provider()
    eligible = _eligible_prospects(db)

    sent: list[dict] = []
    failed: list[dict] = []

    for prospect in eligible:
        event = prospect.event
        if event is None:
            failed.append({"prospect_id": prospect.id, "error": "no event"})
            continue

        text = compose_followup(prospect, event)

        try:
            res = send_and_log(
                db, prospect, text,
                sent_state="follow_up_sent",
                fallback_provider=fallback_provider,
                commit=False,
            )
        except Exception as exc:  # noqa: BLE001
            failed.append({"prospect_id": prospect.id,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue

        if res.error:
            failed.append({"prospect_id": prospect.id, "error": res.error})
            continue

        sent.append({"prospect_id": prospect.id, "state": res.state,
                     "dry_run": res.dry_run})

    if sent:
        db.commit()

    return {
        "eligible": len(eligible),
        "sent": len(sent),
        "failed": len(failed),
        "delay_hours": config.FOLLOWUP_DELAY_HOURS,
        "max_per_prospect": config.FOLLOWUP_MAX_PER_PROSPECT,
        "results": sent,
        "errors": failed,
    }


class RegisterWebhooksBody(BaseModel):
    """Optional explicit base URL for the callback. Falls back to the
    SURPLUS_BASE_URL env var (the same one the follow-up cron uses)."""
    base_url: Optional[str] = None


@router.post("/register-webhooks", status_code=200)
def register_webhooks(
    body: RegisterWebhooksBody = RegisterWebhooksBody(),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Register the provider's inbound-messaging webhook so auto-reply fires.

    This is the auto-reply analog of the follow-up cron : the
    message_received handler at /webhooks/unipile already exists, but Unipile
    never calls it until a "messaging" webhook is subscribed. Idempotent :
    re-running won't create duplicates. Run once after deploy (or whenever the
    base URL changes).
    """
    base = (body.base_url or os.environ.get("SURPLUS_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            400, "no base_url provided and SURPLUS_BASE_URL is not set")
    provider = get_provider()
    callback_url = f"{base}/webhooks/unipile"
    result = provider.register_inbound_webhook(callback_url)
    return {"provider": provider.name, "callback_url": callback_url, **result}


# ── Billing status : read-only paid-user audit ──────────────────────────
#
# Diagnostic for "are payments landing?". paid_at is stamped ONLY by the
# Stripe checkout.session.completed webhook (routes/billing.py), so this
# answers "who did the app unlock", NOT "who sent money" — if the webhook
# isn't wired, paid users show paid=0 here while Stripe shows real charges.
# That gap is the signal the webhook is misconfigured. Gated by ADMIN_TOKEN.


@router.get("/billing-status")
def billing_status(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Return a roll-up of billing state across all users + the paid rows.

    Read-only. `paid` = rows with paid_at set (app-side unlock). `has_customer`
    = rows with a stripe_customer_id (Stripe round-trip reached us at least
    once). A nonzero gap between Stripe's dashboard and `paid` here means the
    webhook isn't stamping.
    """
    total = db.query(models.User).count()
    paid_rows = (
        db.query(models.User)
        .filter(models.User.paid_at.isnot(None))
        .order_by(models.User.paid_at.desc())
        .all()
    )
    has_customer = (
        db.query(models.User)
        .filter(models.User.stripe_customer_id.isnot(None))
        .count()
    )
    return {
        "total_users": total,
        "paid_count": len(paid_rows),
        "has_stripe_customer_count": has_customer,
        "paid_users": [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "paid_at": u.paid_at.isoformat() if u.paid_at else None,
                "stripe_customer_id": u.stripe_customer_id,
            }
            for u in paid_rows
        ],
    }


class GrantPaidIn(BaseModel):
    email: str


@router.post("/grant-paid")
def grant_paid(
    body: GrantPaidIn,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Stamp paid_at on a user by EMAIL — recovery for payments that Stripe
    confirms but the app DB doesn't reflect (webhook missed it, or the paid
    User row was lost to a DB reset / migration so the webhook's id-based
    lookup can no longer find it).

    Keyed by email rather than user.id precisely because id isn't stable
    across a DB reset. Idempotent : a no-op (returns already_paid) when
    paid_at is already set. Read the current state first via /billing-status.
    """
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email required")
    user = (
        db.query(models.User)
        .filter(models.User.email == email)
        .order_by(models.User.id.desc())
        .first()
    )
    if user is None:
        raise HTTPException(404, f"no user with email {email!r}")
    if user.paid_at is not None:
        return {
            "ok": True,
            "already_paid": True,
            "user_id": user.id,
            "email": user.email,
            "paid_at": user.paid_at.isoformat(),
        }
    user.paid_at = datetime.now(timezone.utc)
    db.commit()
    print(f"  [admin.grant_paid] stamped paid_at on user.id={user.id} email={email}")
    return {
        "ok": True,
        "already_paid": False,
        "user_id": user.id,
        "email": user.email,
        "paid_at": user.paid_at.isoformat(),
    }


# ── Pending AI replies : list, approve, reject ──────────────────────────

@router.get("/pending-replies", response_model=list[PendingReplyOut])
def list_pending_replies(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Return every PendingReply still awaiting a human decision."""
    rows = (db.query(models.PendingReply)
              .filter(models.PendingReply.status == "pending")
              .order_by(models.PendingReply.created_at.asc())
              .all())
    return [
        PendingReplyOut(
            id=r.id,
            prospect_id=r.prospect_id,
            prospect_name=(r.prospect.name if r.prospect else ""),
            inbound_body=r.inbound_body,
            classification=r.classification,
            draft_text=r.draft_text,
            reasoning=r.reasoning,
            status=r.status,
            created_at=r.created_at,
        ) for r in rows
    ]


def _send_pending(db: Session, pending: models.PendingReply, text: str) -> dict:
    prospect = pending.prospect
    if prospect is None or prospect.event is None:
        raise HTTPException(404, "Not Found")
    res = send_and_log(
        db, prospect, text,
        sent_state="message_sent",
        fallback_provider=get_provider(),
        commit=False,
    )
    pending.status = "approved" if not res.error else "rejected"
    pending.final_text = text if not res.error else None
    pending.decided_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": pending.id, "sent": not bool(res.error),
            "dry_run": res.dry_run, "error": res.error}


@router.post("/pending-replies/{pending_id}/approve")
def approve_pending_reply(
    pending_id: int,
    body: Optional[ApproveBody] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    pending = db.get(models.PendingReply, pending_id)
    if pending is None or pending.status != "pending":
        raise HTTPException(404, "Not Found")
    text = (body.edited_text if body and body.edited_text else pending.draft_text).strip()
    if not text:
        raise HTTPException(400, "empty reply text")
    return _send_pending(db, pending, text)


@router.post("/pending-replies/{pending_id}/reject")
def reject_pending_reply(
    pending_id: int,
    body: Optional[RejectBody] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    pending = db.get(models.PendingReply, pending_id)
    if pending is None or pending.status != "pending":
        raise HTTPException(404, "Not Found")
    pending.status = "rejected"
    pending.decided_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": pending.id, "status": "rejected",
            "reason": (body.reason if body else None)}


# ── Voice-matching examples : per-operator style guide ──────────────────
#
# These get injected into compose()'s system prompt as <style_examples>
# so Claude mirrors the operator's voice when writing outreach. Stored
# JSON-encoded on User.voice_examples. Resolution order in compose() is:
# event.user.voice_examples → OPERATOR_VOICE_EXAMPLES env var → none.


def _operator_user(db: Session) -> Optional[models.User]:
    """Look up the User whose unipile_account_id matches the env var."""
    account_id = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    if not account_id:
        return None
    return db.query(models.User).filter(
        models.User.unipile_account_id == account_id
    ).first()


@router.get("/voice-examples")
def get_voice_examples(
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Return the operator's current voice-matching examples + which source
    they're coming from (DB row vs env-var fallback)."""
    import json as _json
    user = _operator_user(db)
    db_raw = (user.voice_examples if user else "") or ""
    env_raw = (os.environ.get("OPERATOR_VOICE_EXAMPLES") or "").strip()

    examples: list[str] = []
    source = "none"
    if db_raw.strip():
        try:
            parsed = _json.loads(db_raw)
            if isinstance(parsed, list):
                examples = [str(s).strip() for s in parsed if str(s).strip()]
                source = "user_row"
        except _json.JSONDecodeError:
            pass
    elif env_raw:
        try:
            parsed = _json.loads(env_raw)
            if isinstance(parsed, list):
                examples = [str(s).strip() for s in parsed if str(s).strip()]
                source = "env_var"
        except _json.JSONDecodeError:
            pass
    return {
        "source": source,
        "count": len(examples),
        "examples": examples,
    }


@router.post("/voice-examples")
def set_voice_examples(
    body: VoiceExamplesBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Set the operator's voice-matching examples. Persists to the operator
    User row (User.voice_examples) as JSON-encoded list."""
    import json as _json
    user = _operator_user(db)
    if user is None:
        raise HTTPException(404, "operator User row not found")
    cleaned = [s.strip() for s in body.examples if s and s.strip()]
    user.voice_examples = _json.dumps(cleaned)
    db.commit()
    # Bust the compose cache so subsequent composes pick up the new voice
    from ..agents.outreach import reset_compose_cache
    reset_compose_cache()
    return {"saved": len(cleaned), "examples": cleaned}


# ── User lookup + merge : un-orphan events after a re-auth duplicate ─────
#
# Background: a LinkedIn re-auth can mint a NEW Unipile account_id AND a NEW
# User row when dedup misses (old row had NULL linkedin_provider_id, so the
# provider-id join couldn't match). The new empty row owns nothing, so the
# operator's real Events 404 ("Event not found") because get_owned_event
# filters Event.user_id == user.id. These two endpoints let an operator
# (1) confirm the duplicate-row state read-only, then (2) merge the orphaned
# row into the survivor, re-pointing every FK. See routes/auth.py dedup.


def _user_fk_counts(db: Session, user_id: int) -> dict:
    """Count every row that points at this user, across all FK tables.
    Read-only : used by both the lookup (display) and merge (preview)."""
    return {
        "events": db.query(models.Event).filter(
            models.Event.user_id == user_id).count(),
        "contacts": db.query(models.Contact).filter(
            models.Contact.user_id == user_id).count(),
        "interactions": db.query(models.RelationshipInteraction).filter(
            models.RelationshipInteraction.actor_user_id == user_id).count(),
        "sessions": db.query(models.Session).filter(
            models.Session.user_id == user_id).count(),
    }


def _user_summary(db: Session, u: models.User) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "unipile_account_id": u.unipile_account_id,
        "linkedin_provider_id": u.linkedin_provider_id,
        "linkedin_public_id": u.linkedin_public_id,
        "linkedin_status": u.linkedin_status,
        "paid_at": u.paid_at.isoformat() if u.paid_at else None,
        "stripe_customer_id": u.stripe_customer_id,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "owns": _user_fk_counts(db, u.id),
    }


@router.get("/users")
def lookup_users(
    identity: Optional[str] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Read-only. List users matching `identity` (substring match against
    unipile_account_id / linkedin_provider_id / linkedin_public_id / email /
    name), each with a count of the rows that FK to them. Omit `identity`
    to list every user (capped at 200). Use this to confirm a duplicate /
    orphaned row before calling /admin/merge-users."""
    q = db.query(models.User)
    if identity and identity.strip():
        term = f"%{identity.strip()}%"
        q = q.filter(
            (models.User.unipile_account_id.ilike(term))
            | (models.User.linkedin_provider_id.ilike(term))
            | (models.User.linkedin_public_id.ilike(term))
            | (models.User.email.ilike(term))
            | (models.User.name.ilike(term))
        )
    rows = q.order_by(models.User.id.asc()).limit(200).all()
    return {"count": len(rows), "users": [_user_summary(db, u) for u in rows]}


@router.post("/merge-users")
def merge_users(
    body: MergeUsersBody,
    db: Session = Depends(get_db),
    _: None = Depends(_require_admin_token),
):
    """Merge the orphaned/duplicate `from_user_id` INTO the survivor
    `to_user_id`. Re-points events / contacts / interactions / sessions,
    copies billing forward when the survivor lacks it, then deletes the
    source row. dry_run=True (default) previews the move without writing.

    Idempotent-ish: re-pointing is an UPDATE keyed on the source id, so a
    second non-dry run after the source is deleted is a no-op."""
    if body.from_user_id == body.to_user_id:
        raise HTTPException(400, "from_user_id and to_user_id are identical")

    src = db.get(models.User, body.from_user_id)
    dst = db.get(models.User, body.to_user_id)
    if src is None or dst is None:
        raise HTTPException(404, "Not Found")

    before = {
        "from": _user_summary(db, src),
        "to": _user_summary(db, dst),
    }

    # Billing: only copy forward when the survivor has none and the source does.
    billing_copied = False
    if dst.paid_at is None and src.paid_at is not None:
        billing_copied = True

    # Dedup-key heal : the whole point of recovery. Fill any NULL dedup key on
    # the survivor from the source so the NEXT logged-out re-auth matches by
    # linkedin_provider_id (re-points onto this row) instead of minting yet
    # another duplicate. Gap-fill only : never clobber a value the survivor
    # already has. (Common case: survivor is the new live row WITH keys and
    # src is the legacy NULL row, so this is a no-op : but when the operator
    # keeps the legacy row as survivor, this is what stops re-orphaning.)
    keys_to_backfill = [
        attr for attr in ("linkedin_provider_id", "linkedin_public_id", "email")
        if getattr(dst, attr) is None and getattr(src, attr) is not None
    ]

    moved = dict(before["from"]["owns"])  # counts that WILL move

    if body.dry_run:
        return {
            "dry_run": True,
            "would_move": moved,
            "would_copy_billing": billing_copied,
            "would_backfill_keys": keys_to_backfill,
            "from": before["from"],
            "to": before["to"],
        }

    # ── Commit path : re-point every FK, then delete the source row. ──
    db.query(models.Event).filter(
        models.Event.user_id == src.id).update(
        {models.Event.user_id: dst.id}, synchronize_session=False)
    db.query(models.Contact).filter(
        models.Contact.user_id == src.id).update(
        {models.Contact.user_id: dst.id}, synchronize_session=False)
    db.query(models.RelationshipInteraction).filter(
        models.RelationshipInteraction.actor_user_id == src.id).update(
        {models.RelationshipInteraction.actor_user_id: dst.id},
        synchronize_session=False)
    db.query(models.Session).filter(
        models.Session.user_id == src.id).update(
        {models.Session.user_id: dst.id}, synchronize_session=False)
    # AuthState is ephemeral, but re-point any dangling pre-tags so a stale
    # in-flight flow can't resurrect the deleted row.
    db.query(models.AuthState).filter(
        models.AuthState.user_id == src.id).update(
        {models.AuthState.user_id: dst.id}, synchronize_session=False)

    if billing_copied:
        dst.paid_at = src.paid_at
        if dst.stripe_customer_id is None:
            dst.stripe_customer_id = src.stripe_customer_id

    # Heal the survivor's NULL dedup keys from the source (gap-fill only).
    for attr in keys_to_backfill:
        setattr(dst, attr, getattr(src, attr))

    db.delete(src)
    db.commit()

    return {
        "dry_run": False,
        "moved": moved,
        "billing_copied": billing_copied,
        "keys_backfilled": keys_to_backfill,
        "survivor": _user_summary(db, dst),
    }
