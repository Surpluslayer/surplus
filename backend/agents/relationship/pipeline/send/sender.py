"""
agents/sender.py : the one place a LinkedIn DM goes out.

Three callsites used to duplicate this exact sequence (build a lead → call
provider.send_message → write an OutreachLog row): the cron follow-up,
the AI auto-reply, and the operator approve-pending action. They now all
go through `send_and_log`.

Returns the provider's ProviderResult so callers can pull error / state /
dry_run / provider_lead_id for their own response shapes.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone

from ..... import models
from .....providers import LinkedInProvider, get_provider_for_prospect


def _automation_master_on() -> bool:
    """Env `SURPLUS_AUTOMATED_SENDS`, default FALSE (off for everyone)."""
    return (os.environ.get("SURPLUS_AUTOMATED_SENDS", "false").strip().lower()
            in ("true", "1", "yes", "on"))


def _automated_channels() -> "set[str] | None":
    """The CHANNEL allowlist for auto-fire, from `SURPLUS_AUTOMATED_SEND_CHANNELS`
    (comma list, e.g. "whatsapp,email"). None == unset == all channels auto-fire
    when the master switch is on; a set narrows auto-fire to just those transports
    (others draft-for-review)."""
    raw = (os.environ.get("SURPLUS_AUTOMATED_SEND_CHANNELS") or "").strip()
    if not raw:
        return None
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def automated_sends_enabled() -> bool:
    """MASTER feature flag (channel-agnostic): may the agent DRAFT AND SEND on its
    own at all, with no human per message?

    OFF by default for EVERYONE -- automated messaging is opt-in, never on silently.
    Env `SURPLUS_AUTOMATED_SENDS`, default FALSE. This is the global kill switch;
    per-channel routing lives in `automated_send_enabled(channel)`."""
    return _automation_master_on()


def automated_send_enabled(channel: str = "") -> bool:
    """Should an automated message on THIS channel actually fire?

    Auto-fire is keyed to the TRANSPORT the message arrived/goes on (linkedin /
    email / whatsapp): the master switch must be on AND this channel must be in the
    auto-fire allowlist; channels not allowed STAGE a draft for review instead.
    Allowlist unset -> all channels auto-fire when master is on. Gates every
    fully-automated path (post-accept auto-DM, follow-up cron, AI auto-reply) and
    is layered ABOVE the per-target gates (`auto_dm_after_accept`,
    `auto_followups_enabled`) -- an automated send needs BOTH. MANUAL UI sends
    (send-now, approve-a-draft) never pass through here, so they always work."""
    if not _automation_master_on():
        return False
    allow = _automated_channels()
    if allow is None:
        return True
    return (channel or "").strip().lower() in allow


def _followups_master_on() -> bool:
    """Env `SURPLUS_AUTO_FOLLOWUPS`, default FALSE. Kill switch for the ONE
    built-in automated send: the post-accept first follow-up (the DM that fires
    when an invite is accepted). The later nudge is NOT gated here anymore --
    it is agent autonomy and shares the general-send master with auto-reply."""
    return (os.environ.get("SURPLUS_AUTO_FOLLOWUPS", "false").strip().lower()
            in ("true", "1", "yes", "on"))


def follow_up_send_enabled(channel: str = "") -> bool:
    """Should the BUILT-IN post-accept first follow-up fire on THIS channel?

    Keyed to `SURPLUS_AUTO_FOLLOWUPS` (separate from the general-send master)
    because this one send is pre-authorized by the host's own action (they sent
    the invite) and is a built-in product behavior, on for everyone. The later
    nudge does NOT pass through here -- it shares `automated_send_enabled` with
    the AI auto-reply (agent autonomy, user-decided). Reuses the same channel
    allowlist. MANUAL UI sends never pass through here."""
    if not _followups_master_on():
        return False
    allow = _automated_channels()
    if allow is None:
        return True
    return (channel or "").strip().lower() in allow


def fire_booking_on_send(db, user, contact, booking_payload, *, topic: str = "") -> dict:
    """Fire the actual calendar booking that a SENT meeting-proposal draft carried.

    Booking is a SIDE EFFECT OF SENDING such a draft (manual: host send; auto:
    auto-send), so this is called from every send path AFTER a clean dispatch. It
    is the bridge from the staged `booking_payload` to integrations.booking:

      * mode == "calendly": the self-serve link in the message body IS the booking,
        so there is nothing to create here -> {"booked": False, "mode": "calendly"}.
      * mode == "propose_time": create the event + invite the contact at the
        proposed slot via agent_book_meeting (idempotent, email-required). On
        success -> {"booked": True, ...event...}; if the contact has no email, or
        the slot/calendar is unusable, -> {"booked": False, "reason": ...} (the
        message still sent; only the auto-create was skipped).

    Never raises: a booking miss must NOT unwind or fail a message that already
    went out. `booking_payload` may be a JSON string (as stored on the row) or an
    already-parsed dict."""
    import json
    if not booking_payload:
        return {"booked": False, "reason": "no booking payload"}
    payload = booking_payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return {"booked": False, "reason": "unparseable booking payload"}
    if not isinstance(payload, dict):
        return {"booked": False, "reason": "bad booking payload"}

    mode = (payload.get("mode") or "").strip()
    if mode == "calendly":
        # The link in the body is the booking; nothing to create on send.
        return {"booked": False, "mode": "calendly"}
    if mode != "propose_time":
        return {"booked": False, "reason": f"no actionable mode ({mode or 'none'})"}
    if contact is None:
        return {"booked": False, "reason": "no contact to invite"}

    from .....integrations.booking import _DEFAULT_TZ, agent_book_meeting
    topic = (topic or payload.get("topic") or "Quick chat").strip() or "Quick chat"
    try:
        ev = agent_book_meeting(
            db, user, contact,
            topic=topic,
            start_iso=(payload.get("start_iso") or "").strip(),
            duration_min=int(payload.get("duration_min") or 30),
            tz=(payload.get("tz") or "").strip() or _DEFAULT_TZ,
            with_zoom=payload.get("with_zoom"))
        return {"booked": True, **ev}
    except ValueError as exc:
        # Email-required / no-calendar / bad-time / upstream: the message sent;
        # we just didn't auto-create the event. Surface a clean reason, no stack.
        return {"booked": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"booked": False, "reason": f"{type(exc).__name__}"}


def send_and_log(
    db,
    prospect: models.Prospect,
    text: str,
    *,
    sent_state: str,
    fallback_provider: LinkedInProvider,
    commit: bool = True,
):
    """Send `text` to `prospect` via their owning user's LinkedIn account
    and write an OutreachLog row. `sent_state` is the canonical state to
    record on success (e.g. "follow_up_sent", "auto_reply_sent",
    "message_sent"); failures always record as "failed".

    The caller already has a session; `commit=False` lets the caller
    batch multiple sends into one transaction (the cron does this).
    """
    if prospect.event is None:
        raise ValueError(f"prospect {prospect.id} has no event")

    provider = get_provider_for_prospect(prospect, fallback_provider)
    lead = provider.build_lead_payload(
        prospect, prospect.event, note=text, message=text,
    )
    res = provider.send_message(
        lead, linkedin_provider_id=prospect.linkedin_provider_id,
    )
    # Record the truthful state : clean success -> sent_state, clean failure
    # -> "failed", AMBIGUOUS outcome (request dispatched, response lost — it
    # may have landed) -> "unconfirmed" so send_flow's recent-send guard can
    # hold off blind retries to this person.
    if not res.error:
        log_state = sent_state
    elif res.state == "unconfirmed":
        log_state = "unconfirmed"
    else:
        log_state = "failed"
    if not res.error:
        from ..context.reconcile import clear_prospect_next_step_if_fulfilled
        clear_prospect_next_step_if_fulfilled(prospect, text)
    db.add(models.OutreachLog(
        prospect_id=prospect.id,
        channel="linkedin",
        state=log_state,
        body=text[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    if commit:
        db.commit()
        # Spine: a successful send is a real outbound touch, so ensure the
        # recipient exists as a durable Contact (idempotent, fail-soft, no-op
        # without a strong identity key). Only when commit=True : link_contact
        # commits internally, which would break a caller batching with
        # commit=False (e.g. the cron follow-up).
        if not res.error:
            from ...spine.relationships import link_contact
            owner_id = getattr(prospect.event, "user_id", None)
            if owner_id is not None:
                link_contact(db, prospect, owner_id)
    return res


def send_followup(
    db,
    prospect,
    text: str,
    *,
    channel: str,
    commit: bool = True,
    fallback_provider: "LinkedInProvider | None" = None,
):
    """The ONE transport switch for a follow-up message: dispatch `text` to
    `prospect` over `channel` and write the truthful OutreachLog row, returning
    the ProviderResult. This owns the email-vs-linkedin (and future
    whatsapp/imessage) branch that the send-now / cron / chat callers used to
    copy-paste; each caller keeps its own status/HTTP mapping off the result.

    channel='email' -> send_followup_email (the email path manages its own
    commit). Anything else -> send_and_log over LinkedIn with the standard
    'follow_up_sent' state; `commit` is forwarded so a caller can batch (the
    cron sends with commit=False), and `fallback_provider` defaults to
    get_provider() when the caller does not pre-build one.

    Behavior-preserving: gating stays at the call sites (this never decides
    whether a send is allowed), and which channel a message goes on is the
    caller's choice."""
    if (channel or "linkedin") == "email":
        return send_followup_email(db, prospect, text)
    if fallback_provider is None:
        from .....providers import get_provider
        fallback_provider = get_provider()
    return send_and_log(
        db, prospect, text,
        sent_state="follow_up_sent",
        fallback_provider=fallback_provider,
        commit=commit,
    )


def send_followup_email(db, prospect, text: str):
    """Dispatch one follow-up AS EMAIL from the prospect's owner's mailbox.
    Resolves owner -> mailbox seat, contact -> address + linked thread
    (reply_to + Re: subject keeps Gmail threading). Returns a ProviderResult-
    shaped object; writes the truthful OutreachLog row (channel=email)."""
    from datetime import datetime, timezone
    from ..... import models
    from .....providers import get_provider

    owner = getattr(getattr(prospect, "event", None), "user", None)
    contact = (db.get(models.Contact, prospect.contact_id)
               if getattr(prospect, "contact_id", None) else None)
    to_addr = ((getattr(prospect, "email", None) or "").strip().lower()
               or ((contact.email if contact else "") or "").strip().lower())
    provider = get_provider()
    if not to_addr:
        raise ValueError("no email address on file for this contact")
    acct = getattr(owner, "unipile_email_account_id", None) or ""
    if not provider.dry_run and (
            not acct or getattr(owner, "email_status", "") != "active"):
        raise ValueError("owner has no connected email account")

    subject = "Following up"
    reply_to = None
    thread_id = getattr(contact, "email_thread_id", None) if contact else None
    if thread_id and not provider.dry_run:
        try:
            from ...email_sync import thread_messages
            from .....integrations.unipile_config import unipile_creds
            creds = unipile_creds()
            if not creds:
                raise RuntimeError("unipile not configured")
            dsn, key = creds
            msgs = thread_messages(
                dsn=dsn, api_key=key, account_id=acct, thread_id=thread_id,
                own_address=getattr(owner, "email_account_address", "") or "")
            if msgs:
                last = msgs[-1]
                reply_to = last.get("provider_id")
                orig = (last.get("subject") or "").strip()
                if orig:
                    subject = orig if orig.lower().startswith("re:") else f"Re: {orig}"
        except Exception:  # noqa: BLE001 : fall back to a fresh email
            pass

    from ...email_sync import format_email_html
    to_first = ((getattr(prospect, "name", "") or "").split() or [""])[0]
    host_first = ((getattr(owner, "name", "") or "").split() or [""])[0]
    res = provider.send_email(
        email_account_id=acct, to_address=to_addr,
        to_name=(getattr(prospect, "name", "") or ""),
        subject=subject, body=format_email_html(text, to_first, host_first),
        prospect_id=prospect.id, reply_to=reply_to)
    if not res.error:
        from ..context.reconcile import clear_prospect_next_step_if_fulfilled
        clear_prospect_next_step_if_fulfilled(prospect, text)
    db.add(models.OutreachLog(
        prospect_id=prospect.id, channel="email", state=res.state,
        body=f"[{subject}] {text}"[:8000], ts=datetime.now(timezone.utc),
        provider=res.provider, provider_lead_id=res.provider_lead_id))
    return res
