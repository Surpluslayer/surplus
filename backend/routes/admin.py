"""
routes/admin.py : cron / operator-triggered tasks.

    POST /admin/run-followups   shared-secret auth (X-Admin-Token)

Idempotent enough to hit from an external cron (Railway, GitHub Actions)
on a regular schedule. Dispatches the "Gmail Schedule Send" follow-up queue:
every ScheduledFollowup row that is still `scheduled` and whose host-chosen
`send_at` has arrived. Each row flips to sent/cancelled/failed as it's
processed, so overlapping cron runs can't double-send.

Rows are staged at first-DM time by agents/followup_scheduler.stage_followup
(drafted body + suggested time the host can edit) and auto-cancelled on reply
by the webhook. Sends go via the prospect's owning user's LinkedIn account
(same per-user routing the webhook auto-DM uses).
"""
from __future__ import annotations
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from .. import audit, models
from ..agents.relationship.pipeline.send.sender import send_and_log, send_followup
from ..auth import _as_aware_utc, user_may_send
from ..db import ENGINE, get_service_db
from ..providers import (
    LinkedInProvider,
    get_provider,
    get_provider_for_prospect,
)

# How long a due-but-unsent follow-up stays sendable. Past this, the dispatch
# expires it (cancel_reason="stale") instead of firing a weeks-late nudge.
# Env-tunable for ops; 7 days matches a reasonable "still natural" window.
_FOLLOWUP_STALE_DAYS = int(os.environ.get("SURPLUS_FOLLOWUP_STALE_DAYS", "7"))


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


class DedupContactsBody(BaseModel):
    """Merge same-person duplicate Contacts. `user_id` scopes to one owner (omit
    to sweep EVERY user). dry_run defaults True : preview the groups + a name
    sample before anything is merged. Only STRONG-identity duplicates (shared
    normalized email / linkedin / phone) are auto-merged; name-only collisions are
    reported separately for review, never merged."""
    user_id: Optional[int] = None
    dry_run: bool = True


class CleanupEmailNoiseBody(BaseModel):
    """Remove the one-way email-sync 'contacts' the OLD gate created (anyone you
    ever emailed once). `user_id` scopes to one owner (omit = every owner).
    dry_run defaults True : preview the names before anything is touched."""
    user_id: Optional[int] = None
    dry_run: bool = True


router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_role(token: Optional[str]) -> Optional[str]:
    """Resolve an X-Admin-Token to a ROLE (constant-time), or None if it matches
    no configured token. This is the least-privilege split (checklist: RBAC):

      ADMIN_TOKEN           -> "admin"     (full: read + every mutating op)
      ADMIN_READONLY_TOKEN  -> "readonly"  (read-only endpoints ONLY)

    So a high-frequency, low-trust consumer (an uptime monitor, a status
    dashboard) can carry a token that is mechanically unable to hit a
    destructive endpoint — losing it can't delete a user or purge data.
    ADMIN_READONLY_TOKEN is optional; unset means only the full token exists.
    """
    if not token:
        return None
    full = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if full and hmac.compare_digest(token, full):
        return "admin"
    ro = (os.environ.get("ADMIN_READONLY_TOKEN") or "").strip()
    if ro and hmac.compare_digest(token, ro):
        return "readonly"
    return None


def _check_admin(*, need_write: bool, x_admin_token: Optional[str],
                 request: Optional[Request], db: Optional[Session]) -> str:
    """Shared admin gate for both privilege levels. Enforces, in order:

      1. Some admin token is configured at all (else 404, no fingerprint).
      2. The presented token maps to a role (else denied).
      3. Write endpoints require the FULL "admin" role (readonly is rejected).
      4. Optional IP allowlist (network second factor) — see backend.audit.

    Every outcome, allowed or DENIED, is written to the audit log (who / what /
    when / from where), then a denial raises 404 — same no-fingerprinting
    posture as before, now observable. Returns the resolved role on success.
    """
    ip = audit.client_ip(request)
    action = (f"{request.method} {request.url.path}" if request is not None
              else "admin")
    role = _admin_role(x_admin_token)

    any_configured = bool((os.environ.get("ADMIN_TOKEN") or "").strip()
                          or (os.environ.get("ADMIN_READONLY_TOKEN") or "").strip())

    detail = ""
    allowed = True
    if not any_configured:
        # No admin surface configured: behave as if the route doesn't exist and
        # don't bother auditing (nothing to protect / a fresh dev box).
        raise HTTPException(404, "Not Found")
    if role is None:
        allowed, detail = False, "bad_token"
    elif need_write and role != "admin":
        allowed, detail = False, "insufficient_role"
    elif not audit.ip_allowed(ip):
        allowed, detail = False, "ip_not_allowlisted"

    audit.record(
        db,
        actor=f"admin:{role}" if role else "anon",
        action=action,
        outcome="allowed" if allowed else "denied",
        source_ip=ip,
        detail=detail,
    )
    if not allowed:
        raise HTTPException(404, "Not Found")
    return role or "admin"


def _require_admin_token(
    x_admin_token: Optional[str] = Header(default=None),
    request: Request = None,
    db: Session = Depends(get_service_db),
) -> None:
    """Full-admin (write) gate for the mutating admin endpoints.

    Constant-time compares X-Admin-Token against ADMIN_TOKEN and requires the
    full "admin" role; readonly tokens are rejected here. Returns 404 (not
    401/403) on missing-or-wrong, matching the demo route's no-fingerprinting
    posture : an attacker scanning shouldn't learn this endpoint exists. Every
    access (allowed or denied) is audited.

    Backward-compatible: still importable/callable with just the token (as
    main.py does); when called outside a request the audit write no-ops.
    """
    _check_admin(need_write=True, x_admin_token=x_admin_token,
                 request=request, db=db)


def _require_admin_readonly(
    x_admin_token: Optional[str] = Header(default=None),
    request: Request = None,
    db: Session = Depends(get_service_db),
) -> str:
    """Read-only admin gate (least privilege). Accepts EITHER the full admin
    token or the read-only token, so a monitoring/dashboard consumer provisioned
    with ADMIN_READONLY_TOKEN can reach observability endpoints (e.g.
    GET /admin/audit-log) but nothing mutating. Returns the resolved role."""
    return _check_admin(need_write=False, x_admin_token=x_admin_token,
                        request=request, db=db)


def _due_followups(db: Session) -> list[models.ScheduledFollowup]:
    """Every staged follow-up whose user-chosen send_at has arrived.

    The host controls timing now : we send a ScheduledFollowup row when it's
    still `scheduled` AND its send_at is in the past. A reply already flips
    pending rows to `cancelled` via the webhook, so a row reaching this query
    is one the host scheduled and the recipient hasn't answered.

    Eager-loads the prospect (+ its outreach) so the dispatch loop and the
    defensive reply re-check don't fan out into per-row queries.
    """
    now = datetime.now(timezone.utc)
    q = (db.query(models.ScheduledFollowup)
           .filter(models.ScheduledFollowup.status == "scheduled",
                   models.ScheduledFollowup.send_at <= now)
           .options(
               selectinload(models.ScheduledFollowup.prospect)
               .selectinload(models.Prospect.outreach),
               selectinload(models.ScheduledFollowup.prospect)
               .selectinload(models.Prospect.event)
               .selectinload(models.Event.user)))
    # Cross-replica claim: on Postgres, SELECT ... FOR UPDATE SKIP LOCKED lets
    # each replica lock a DISJOINT set of due rows, so two overlapping dispatch
    # passes never both pick the same row. SQLite has no such lock (single-writer
    # anyway), so the FOR UPDATE is Postgres-only; the real double-send guard is
    # the status flip to "sending" committed BEFORE the network send (below).
    if ENGINE.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    rows = q.all()
    due: list[models.ScheduledFollowup] = []
    for r in rows:
        send_at = _as_aware_utc(r.send_at)
        # Defensive : the DB filter already applied send_at <= now, but a naive
        # stored value could round-trip oddly, so keep the explicit guard.
        if send_at is None or send_at > now:
            continue
        due.append(r)
    return due


def _replied_since_staging(prospect: models.Prospect) -> bool:
    """Defensive guard against a reply that raced past the webhook cancel."""
    return any(o.state in ("message_replied", "replied") for o in prospect.outreach)


def _auto_send_enabled(prospect: models.Prospect, channel: str = "linkedin") -> bool:
    """Whether the dispatcher should auto-send this prospect's NUDGE on `channel`.

    Product taxonomy (2026-07-01): the nudge ("checking in" after no reply) is
    agent-initiated autonomy, NOT a built-in -- so it shares ONE gate stack with
    the AI auto-reply. An unattended send needs BOTH layers:

      1. the env master (SURPLUS_AUTOMATED_SENDS + channel allowlist), the
         ops kill switch, AND
      2. the OWNING USER's autonomy_mode == 'auto' (per-user opt-in; 'off'
         and 'ask' both hold -- 'ask' just surfaces the held queue in the UI
         for a one-tap confirm).

    Only the post-accept FIRST follow-up stays built-in (SURPLUS_AUTO_FOLLOWUPS,
    in webhooks._trigger_auto_dm). Gate closed -> due nudges HOLD in the queue
    for a manual send-now. A reply still cancels; stale rows expire (dispatch
    loop).
    """
    from ..agents.relationship.pipeline.send.sender import (
        automated_send_enabled,
        owner_autonomy_mode,
    )
    if not automated_send_enabled(channel):
        return False
    owner = getattr(getattr(prospect, "event", None), "user", None)
    return owner_autonomy_mode(owner) == "auto"


def _fire_followup_booking(db, prospect, booking_payload, text: str) -> None:
    """Fire the calendar booking a SENT meeting-proposal follow-up carries, in the
    cron (auto-send) path. Resolves the host (prospect.event.user) and the durable
    Contact from the prospect, then delegates to fire_booking_on_send. Never raises:
    a booking miss must never fail or unwind a follow-up that already sent."""
    if not booking_payload:
        return
    try:
        from ..agents.relationship.pipeline.send.sender import fire_booking_on_send
        owner = getattr(getattr(prospect, "event", None), "user", None)
        contact = (db.get(models.Contact, prospect.contact_id)
                   if getattr(prospect, "contact_id", None) else None)
        if owner is None or contact is None:
            return
        topic = (text or "Quick chat").strip().split("\n", 1)[0][:80] or "Quick chat"
        fire_booking_on_send(db, owner, contact, booking_payload, topic=topic)
    except Exception:  # noqa: BLE001
        pass


@router.post("/run-followups", status_code=200)
def run_followups(
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Dispatch every scheduled follow-up whose send time has arrived.

    Thin admin-token wrapper around dispatch_due_followups so an external
    cron (GitHub Actions) can still fire it; the PRIMARY dispatcher is the
    in-process scheduler thread (updates_scheduler), which calls the core
    directly every minute for punctual sends. Idempotent either way: each
    row flips to `sent`/`cancelled`/`failed` the moment it's processed, so
    overlapping runs never double-send.
    """
    return dispatch_due_followups(db)


@router.post("/run-retention-purge", status_code=200)
def run_retention_purge(
    dry_run: bool = True,
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Purge ephemeral rows (expired sessions, old finished jobs) past their
    category TTL. No-op unless SURPLUS_RETENTION_ENABLED. `dry_run=true` (the
    default) only reports what WOULD be purged, so this is safe to poke."""
    from .. import retention
    return retention.run_purge_sweep(db, dry_run=dry_run)


@router.post("/run-content-retention", status_code=200)
def run_content_retention(
    dry_run: bool = True,
    user_id: int | None = None,
    contact_limit: int = 200,
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Summarize-then-expire aged-out message BODIES (the metadata skeleton
    stays). A body expires only when it is BOTH older than
    SURPLUS_CONTENT_RETENTION_DAYS and beyond the contact's most recent
    SURPLUS_CONTENT_KEEP_LAST_N messages, and only after the contact's rolling
    thread_summary fact verifiably exists. Writes additionally require the
    master SURPLUS_RETENTION_ENABLED switch; `dry_run=true` (default) reports
    what a real pass would expire. Scope with `user_id` to trial one book."""
    from .. import retention
    return retention.run_content_retention(
        db, user_id=user_id, dry_run=dry_run, contact_limit=contact_limit)


@router.post("/delete-user", status_code=200)
def admin_delete_user(
    user_id: int,
    reason: str = "",
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
) -> dict:
    """Admin-initiated full deletion (support handling a customer's delete
    request). Returns per-category counts as the deletion confirmation, and
    writes a metadata-only DeletionAudit row. Irreversible."""
    from .. import retention
    return retention.delete_user_data(db, user_id, actor="admin", reason=reason)


def dispatch_due_followups(db: Session) -> dict:
    """Core dispatch pass: send every scheduled follow-up whose send_at has
    arrived. Callable from the route (external cron) AND the in-process
    scheduler thread.

    Double-send safe. Two passes:

      PASS 1 (claim, one commit, still under the FOR UPDATE SKIP LOCKED row
      locks from _due_followups): classify each due row. Terminal non-send
      outcomes (no-prospect, replied, stale, held, empty-body) get their final
      status now; rows that WILL send are flipped "scheduled" -> "sending".
      The single commit persists all of this AND releases the row locks. After
      it, every to-send row is claimed as "sending", so no overlapping dispatch
      (cron re-fire, second replica) and no racing manual send-now can re-pick
      it : both only ever act on status=="scheduled".

      PASS 2 (send, commit per row): the actual network send for each claimed
      row, flipping "sending" -> "sent" | "failed". A crash between the two
      passes leaves the row "sending" (never "scheduled"), so it is never
      resent -- it is safely inspectable/recoverable, not silently double-fired.
    """
    fallback_provider = get_provider()
    due = _due_followups(db)
    now = datetime.now(timezone.utc)

    sent: list[dict] = []
    failed: list[dict] = []
    cancelled: list[dict] = []
    held: list[dict] = []

    # ── PASS 1 : classify + claim, then one commit (releases the row locks). ──
    to_send: list[models.ScheduledFollowup] = []
    for row in due:
        prospect = row.prospect
        if prospect is None or prospect.event is None:
            row.status = "failed"
            row.cancel_reason = "no_prospect"
            row.updated_at = now
            failed.append({"followup_id": row.id, "error": "no prospect/event"})
            continue

        # A reply that beat the webhook cancel : drop the nudge, don't send.
        if _replied_since_staging(prospect):
            row.status = "cancelled"
            row.cancel_reason = "replied"
            row.updated_at = now
            cancelled.append({"followup_id": row.id, "prospect_id": prospect.id})
            continue

        # Staleness guard: a "just checking in" nudge fired way past its slot
        # reads as broken automation, not attentiveness. If the row sat in the
        # queue (kill switch off / cron down) long enough to go stale, expire
        # it instead of sending. Guards the backlog the moment the built-in
        # dispatch opens after an outage.
        send_at = _as_aware_utc(row.send_at)
        if send_at is not None and (now - send_at) > timedelta(days=_FOLLOWUP_STALE_DAYS):
            row.status = "cancelled"
            row.cancel_reason = "stale"
            row.updated_at = now
            cancelled.append({"followup_id": row.id, "prospect_id": prospect.id,
                              "reason": "stale"})
            continue

        # Auto-send gate : the draft is staged regardless, but the dispatcher
        # only fires it when the general-send master (SURPLUS_AUTOMATED_SENDS +
        # channel allowlist) is on. Off -> leave it `scheduled` so it waits for
        # a manual send-now. Don't cancel : automation may come on, or the host
        # may send it themselves, later.
        if not _auto_send_enabled(prospect, (getattr(row, "channel", "") or "linkedin")):
            held.append({"followup_id": row.id, "prospect_id": prospect.id})
            continue

        # Send paywall (queued path): a row scheduled while paid must NOT fire
        # later if the owner has since lapsed to free. HOLD (leave "scheduled"),
        # never cancel -- it fires whenever they pay again. Bypasses unlimited.
        owner = getattr(getattr(prospect, "event", None), "user", None)
        if owner is None or not user_may_send(owner):
            held.append({"followup_id": row.id, "prospect_id": prospect.id,
                         "reason": "unpaid"})
            continue

        text = (row.body or "").strip()
        if not text:
            row.status = "failed"
            row.cancel_reason = "empty_body"
            row.updated_at = now
            failed.append({"followup_id": row.id, "error": "empty body"})
            continue

        # Claim: flip "scheduled" -> "sending" so nothing else re-picks it once
        # the commit below lands. The network send happens in PASS 2.
        row.status = "sending"
        row.updated_at = now
        to_send.append(row)

    # Single commit: persists the terminal transitions AND the "sending" claims,
    # and releases the FOR UPDATE SKIP LOCKED locks. From here the claimed rows
    # are off-limits to any other dispatch / manual send-now (they filter on
    # status=="scheduled").
    db.commit()

    # ── PASS 2 : network send for each claimed row (commit per row). ──────────
    for row in to_send:
        prospect = row.prospect
        text = (row.body or "").strip()
        try:
            res = send_followup(
                db, prospect, text,
                channel=(getattr(row, "channel", "") or "linkedin"),
                commit=False,
                fallback_provider=fallback_provider,
            )
        except Exception as exc:  # noqa: BLE001
            row.status = "failed"
            row.cancel_reason = f"{type(exc).__name__}"
            row.updated_at = now
            db.commit()
            failed.append({"followup_id": row.id, "prospect_id": prospect.id,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue

        if res.error:
            row.status = "failed"
            row.cancel_reason = "send_error"
            row.updated_at = now
            db.commit()
            failed.append({"followup_id": row.id, "prospect_id": prospect.id,
                           "error": res.error})
            continue

        row.status = "sent"
        row.sent_at = now
        row.updated_at = now
        # Auto-send equivalent of manual approve: if this draft carried a meeting
        # booking payload, the SEND fires the calendar event + invite now. Gated
        # implicitly by reaching here (auto-send is ON). Never fails the send.
        _fire_followup_booking(db, prospect, getattr(row, "booking_payload", None),
                               text)
        db.commit()
        sent.append({"followup_id": row.id, "prospect_id": prospect.id,
                     "state": res.state, "dry_run": res.dry_run})

    return {
        "due": len(due),
        "sent": len(sent),
        "failed": len(failed),
        "cancelled": len(cancelled),
        "held": len(held),
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
    # Also register the account-status webhook: the circuit breaker that halts a
    # host the instant LinkedIn pushes back (creds/captcha/checkpoint). Best
    # effort -- if the provider lacks the method, skip it.
    status_result = getattr(provider, "register_account_status_webhook",
                            lambda _u: {"ok": False, "reason": "unsupported"})(callback_url)
    return {"provider": provider.name, "callback_url": callback_url,
            "messaging": result, "account_status": status_result}


# ── Billing status : read-only paid-user audit ──────────────────────────
#
# Diagnostic for "are payments landing?". paid_at is stamped ONLY by the
# Stripe checkout.session.completed webhook (routes/billing.py), so this
# answers "who did the app unlock", NOT "who sent money" — if the webhook
# isn't wired, paid users show paid=0 here while Stripe shows real charges.
# That gap is the signal the webhook is misconfigured. Gated by ADMIN_TOKEN.


@router.get("/billing-status")
def billing_status(
    db: Session = Depends(get_service_db),
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
    db: Session = Depends(get_service_db),
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
    db: Session = Depends(get_service_db),
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
    db: Session = Depends(get_service_db),
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
    db: Session = Depends(get_service_db),
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
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
):
    """Return the operator's current voice-matching examples + which source
    they're coming from (DB row vs env-var fallback)."""
    from ..agents import voice
    user = _operator_user(db)
    db_raw = (user.voice_examples if user else "") or ""
    env_raw = (os.environ.get("OPERATOR_VOICE_EXAMPLES") or "").strip()

    # parse_voice_examples handles BOTH the legacy plain-string form and the
    # richer {"text", "channel", ...} provenance form, returning just the text —
    # so a tagged example never leaks as a stringified dict into the admin UI.
    examples: list[str] = []
    source = "none"
    if db_raw.strip():
        examples = voice.parse_voice_examples(db_raw, env_fallback=False, limit=100)
        if examples:
            source = "user_row"
    elif env_raw:
        examples = voice.parse_voice_examples(env_raw, env_fallback=False, limit=100)
        if examples:
            source = "env_var"
    return {
        "source": source,
        "count": len(examples),
        "examples": examples,
    }


@router.post("/voice-examples")
def set_voice_examples(
    body: VoiceExamplesBody,
    db: Session = Depends(get_service_db),
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
    db: Session = Depends(get_service_db),
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





# ── Gathering : LinkedIn chat sync trigger + prospect->contact backfill ──


class LinkedInSyncBody(BaseModel):
    """Omit user_id to dispatch for EVERY user with an active LinkedIn seat.
    incremental=False forces a full re-scan (dedup makes it write-idempotent)."""
    user_id: Optional[int] = None
    incremental: bool = True


@router.post("/sync-linkedin-chats")
def sync_linkedin_chats_route(
    body: Optional[LinkedInSyncBody] = None,
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
):
    """On-demand LinkedIn DM sync. Dispatches DURABLY (jobs.run_detached: Modal
    run_detached_job when USE_MODAL, else a local daemon thread that owns its
    session) -- never inside this request's lifecycle. Idempotent by Unipile
    message id, incremental by users.linkedin_chat_synced_at."""
    from ..agents.relationship.linkedin_chat_sync import dispatch_linkedin_chat_sync

    body = body or LinkedInSyncBody()
    if body.user_id is not None:
        user = db.get(models.User, body.user_id)
        if user is None:
            raise HTTPException(404, "Not Found")
        users = [user]
    else:
        users = (db.query(models.User)
                 .filter(models.User.unipile_account_id.isnot(None),
                         models.User.linkedin_status == "active")
                 .order_by(models.User.id.asc())
                 .all())
    dispatched = []
    for u in users:
        runner = dispatch_linkedin_chat_sync(u.id, incremental=body.incremental)
        dispatched.append({"user_id": u.id, "runner": runner})
    return {"dispatched": dispatched, "count": len(dispatched)}




class CleanupEmailContactsBody(BaseModel):
    """Delete contacts whose ONLY footprint is the email-sync import :
    inbound-only promotional/newsletter senders that were minted as contacts
    before the two-way filter existed. dry_run defaults True so the operator
    previews the counts + a name sample before anything is touched."""
    dry_run: bool = True





# ─── Access audit log (Phase 4: monitoring) ─────────────────────────────

class AuditLogOut(BaseModel):
    """One metadata-only audit row (see backend.audit / models.AuditLog)."""
    id: int
    actor: str
    action: str
    target: str
    outcome: str
    source_ip: str
    detail: str
    created_at: datetime


@router.get("/audit-log", response_model=list[AuditLogOut], tags=["admin"])
def admin_audit_log(
    limit: int = 100,
    outcome: Optional[str] = None,
    role: str = Depends(_require_admin_readonly),
    db: Session = Depends(get_service_db),
) -> list[models.AuditLog]:
    """Recent access-audit rows, newest first — "who accessed what and when".

    Read-only: reachable with the full admin token OR the least-privilege
    read-only token, so an operator dashboard can surface the trail (and the
    `denied` probe signal) without carrying a token that could mutate anything.
    Filter with `?outcome=denied` to see just refused attempts. Metadata only;
    there is no content to leak here by construction.
    """
    q = db.query(models.AuditLog)
    if outcome in ("allowed", "denied"):
        q = q.filter(models.AuditLog.outcome == outcome)
    limit = max(1, min(limit, 1000))
    return (q.order_by(models.AuditLog.id.desc()).limit(limit).all())
@router.post("/backfill-accounts")
def backfill_accounts(
    user_id: Optional[int] = None,
    execute: bool = False,
    allow_llm: bool = False,
    limit: Optional[int] = 100,
    offset: int = 0,
    db: Session = Depends(get_service_db),
    _: None = Depends(_require_admin_token),
):
    """Run the account-layer backfill (docs/accounts-architecture.md §3) from
    INSIDE the deployment, where Postgres is milliseconds away — running the
    CLI script over the public DB URL takes 10+ minutes of WAN round-trips for
    one user's book, which is how this endpoint earned its existence.

    Default is a DRY RUN (reports what it would do, then rolls back) with the
    LLM disabled (deterministic paths only — domain keys, exact names, headline
    regex). Pass execute=true to write, allow_llm=true to let ambiguous names
    hit the disambiguator (slower; background-gated). Gated by ADMIN_TOKEN like
    every ops verb here."""
    from ..agents.relationship import company_resolve

    if not allow_llm:
        # The resolver checks availability per call; masking the key for this
        # request's duration keeps the run deterministic without env surgery.
        import backend.agents.relationship.company_resolve as cr
        orig = cr._anthropic_available
        cr._anthropic_available = lambda: False
        try:
            return company_resolve.backfill(db, user_id=user_id,
                                            dry_run=not execute,
                                            limit=limit, offset=offset)
        finally:
            cr._anthropic_available = orig
    return company_resolve.backfill(db, user_id=user_id, dry_run=not execute,
                                    limit=limit, offset=offset)
