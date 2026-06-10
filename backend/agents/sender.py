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

from .. import models
from ..providers import LinkedInProvider, get_provider_for_prospect


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
