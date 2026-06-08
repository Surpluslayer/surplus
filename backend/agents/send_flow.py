"""
agents/send_flow.py : the ONE warm/cold LinkedIn send routing.

Extracted verbatim from routes/pipeline.py:/invite so the prospecting /invite
route and the in-person capture routes share a single code path for "reach out
to this one prospect":

    1. live-check the relation (is_relation) and stamp connection_status
    2. compose the note + DM (operator overrides win when provided)
    3. route WARM  (already a 1st-degree connection) -> send_message (first DM)
       or  COLD  (not connected)                     -> send_connection (invite)
    4. cache linkedin_provider_id, write the OutreachLog row, flip status

Dry-run is respected throughout : status only flips on a real (non-dry-run)
send, mirroring the original /invite behavior so demos stay non-destructive.
"""
from __future__ import annotations
import json as _json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from .. import models
from ..providers.base import ProviderResult
from .outreach import Message, compose


def _refresh_connection_status(provider, prospect: models.Prospect) -> str:
    """Live-check the provider, write the result to the Prospect row, return the
    new status. Moved here from routes/pipeline.py so the send helper and the
    bulk /check-connections endpoint share one implementation.

    Don't fail the action just because the provider is flaky : keep the last
    known status on error so the caller sees the unchanged value and proceeds.
    """
    try:
        connected = provider.is_relation(prospect.linkedin_url or "")
    except Exception:  # noqa: BLE001
        return prospect.connection_status or "unknown"
    new_status = "connected" if connected else "not_connected"
    prospect.connection_status = new_status
    prospect.connection_checked_at = datetime.now(timezone.utc)
    return new_status


@dataclass
class SendOutcome:
    """Everything a caller needs to build its response after a routed send."""
    path_taken: str           # "warm" | "cold"
    connection_status: str    # the (possibly refreshed) connection_status
    res: ProviderResult
    final_note: str
    final_message: str
    draft: Message            # the composed draft (pre-override), for reference


def route_and_send(
    db,
    prospect: models.Prospect,
    provider,
    event=None,
    *,
    note: Optional[str] = None,
    message: Optional[str] = None,
    draft: Optional[Message] = None,
    refresh_connection: bool = True,
    commit: bool = True,
) -> SendOutcome:
    """Route ONE prospect through warm vs cold, log it, flip status.

    `note` / `message` override the composed draft when non-None (operator
    edits from /invite's OutreachOverride or the in-person /send body).
    `draft` lets a caller pass a pre-composed Message (e.g. the in-person warm
    framing) instead of the default event compose. `refresh_connection=False`
    trusts the already-stored connection_status (skips the live Unipile call).
    """
    ev = event or prospect.event
    if ev is None:
        raise ValueError(f"prospect {prospect.id} has no event")

    status = (
        _refresh_connection_status(provider, prospect)
        if refresh_connection
        else (prospect.connection_status or "unknown")
    )

    if draft is None:
        peers = [q.name for q in ev.prospects
                 if q.id != prospect.id
                 and q.status in ("approved", "contacted", "rsvp")]
        draft = compose(prospect, ev, peers=peers)
    final_note = (note if note is not None else draft.note).strip()
    final_message = (message if message is not None else draft.message).strip()

    if status == "connected":
        # Warm path: skip the invite, send the first DM directly. Resolve the
        # provider_id if we don't have it cached (or it's a stale dry-run id).
        if not prospect.linkedin_provider_id or (
            not provider.dry_run and prospect.linkedin_provider_id.startswith("dry_")
        ):
            try:
                li_id = provider.resolve_linkedin_user(prospect.linkedin_url)
                if li_id:
                    prospect.linkedin_provider_id = li_id
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(502, f"linkedin lookup failed: {exc}")
        lead = provider.build_lead_payload(
            prospect, ev, note=draft.note, message=final_message)
        res = provider.send_message(
            lead, linkedin_provider_id=prospect.linkedin_provider_id)
        if not provider.dry_run and res.state == "message_sent":
            prospect.status = "contacted"
        path_taken = "warm"
    else:
        # Cold path (the historical default). LinkedIn caps notes at 300.
        if len(final_note) > 300:
            raise HTTPException(
                400, f"note exceeds LinkedIn's 300-char limit ({len(final_note)})")
        lead = provider.build_lead_payload(
            prospect, ev, note=final_note, message=final_message)
        res = provider.send_connection(lead)
        if res.linkedin_provider_id:
            prospect.linkedin_provider_id = res.linkedin_provider_id
        if not provider.dry_run and res.state == "invite_sent":
            prospect.status = "contacted"
        path_taken = "cold"

    db.add(models.OutreachLog(
        prospect_id=prospect.id,
        channel="linkedin",
        state=res.state,
        body=_json.dumps(res.payload, default=str)[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    if commit:
        db.commit()

    return SendOutcome(
        path_taken=path_taken,
        connection_status=status,
        res=res,
        final_note=final_note,
        final_message=final_message,
        draft=draft,
    )
