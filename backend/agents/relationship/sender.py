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
from datetime import datetime, timezone

from ... import models
from ...providers import LinkedInProvider, get_provider_for_prospect


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
            from .relationships import link_contact
            owner_id = getattr(prospect.event, "user_id", None)
            if owner_id is not None:
                link_contact(db, prospect, owner_id)
    return res


def send_followup_email(db, prospect, text: str):
    """Dispatch one follow-up AS EMAIL from the prospect's owner's mailbox.
    Resolves owner -> mailbox seat, contact -> address + linked thread
    (reply_to + Re: subject keeps Gmail threading). Returns a ProviderResult-
    shaped object; writes the truthful OutreachLog row (channel=email)."""
    from datetime import datetime, timezone
    from ... import models
    from ...providers import get_provider

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
            import os
            from .email_sync import thread_messages
            dsn = (os.environ.get("UNIPILE_DSN", "") or "").strip().rstrip("/")
            if dsn and not dsn.startswith(("http://", "https://")):
                dsn = f"https://{dsn}"
            key = (os.environ.get("UNIPILE_API_KEY", "") or "").strip()
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

    from .email_sync import format_email_html
    to_first = ((getattr(prospect, "name", "") or "").split() or [""])[0]
    host_first = ((getattr(owner, "name", "") or "").split() or [""])[0]
    res = provider.send_email(
        email_account_id=acct, to_address=to_addr,
        to_name=(getattr(prospect, "name", "") or ""),
        subject=subject, body=format_email_html(text, to_first, host_first),
        prospect_id=prospect.id, reply_to=reply_to)
    db.add(models.OutreachLog(
        prospect_id=prospect.id, channel="email", state=res.state,
        body=f"[{subject}] {text}"[:8000], ts=datetime.now(timezone.utc),
        provider=res.provider, provider_lead_id=res.provider_lead_id))
    return res
