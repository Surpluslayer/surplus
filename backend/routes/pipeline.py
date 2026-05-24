"""routes/pipeline.py : stage 02-03. Prospecting and outreach, split.

Multi-tenant: every route requires a signed-in user (via current_user dep)
and resolves the event through get_owned_event (404s if the event isn't
the user's). Send paths use get_provider_for_user(user) so DMs go from the
signed-in user's connected LinkedIn : NOT the env-var operator account.

The env-var operator account remains the fallback for webhook handlers
(see routes/webhooks.py : webhooks have no session cookie, so they trace
the owning user via Prospect → Event → User).
"""
from __future__ import annotations
import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import current_user, get_owned_event, require_linkedin_send
from ..db import get_db
from ..pipeline import run_prospect, run_outreach_stage, run_pipeline
from ..agents.outreach import compose
from ..agents.prospector import prospect as run_discovery
from ..agents import llm
from ..providers import get_provider_for_user, get_preview_provider

router = APIRouter(prefix="/events", tags=["02-03 · pipeline"])


def _wipe_prior_prospects(db: Session, ev: models.Event) -> None:
    """Cascade-delete every prospect (and their outreach + conversions)."""
    for p in list(ev.prospects):
        db.delete(p)
    db.commit()


@router.post("/{event_id}/prospect", response_model=schemas.PipelineResult)
async def prospect_only(
    event_id: int,
    fresh: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Stage 02 + 03a only: fan-out + score + threshold. No outreach.

    Marks every prospect 'approved' or 'below'. Idempotent : wipes prior
    prospects (and their outreach + conversions) first.

    Pass `?fresh=true` to bypass the in-memory ICP cache and force a
    real web_search round. By default the prospector reuses the cached
    pool for the same ICP, which makes iterating on UI / outreach copy
    essentially instant.
    """
    ev = get_owned_event(event_id, user, db)
    _wipe_prior_prospects(db, ev)
    prospects = await run_prospect(db, ev, force_fresh=fresh)
    return schemas.PipelineResult.build(ev, prospects)


@router.post("/{event_id}/outreach", response_model=schemas.OutreachRunResult)
def outreach_only(
    event_id: int,
    confirm_live_batch: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Stage 03b only: provider-backed outreach for everyone 'approved'.

    SAFETY: in LIVE mode this fires real LinkedIn invites to every approved
    prospect at once, FROM THE SIGNED-IN USER'S LINKEDIN. We require
    ?confirm_live_batch=true as an explicit second-step opt-in to prevent
    the entire mock pool from getting blasted by accident. DRY_RUN calls
    bypass the guard.

    Idempotent: wipes prior outreach logs and resets contacted/rsvp back
    to approved before re-running.
    """
    ev = get_owned_event(event_id, user, db)
    if not ev.prospects:
        raise HTTPException(409, "no prospects : call /prospect first")
    targets = [p for p in ev.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    if not targets:
        raise HTTPException(409, "no approved prospects to contact : "
                                 "threshold may be too high for the pool")

    require_linkedin_send(user)
    provider = get_provider_for_user(user)
    if not provider.dry_run and not confirm_live_batch:
        raise HTTPException(
            400,
            f"refusing to fire {len(targets)} real LinkedIn invites in one batch "
            f"without explicit confirmation. Re-call with ?confirm_live_batch=true, "
            f"or use POST /events/{ev.id}/prospects/<pid>/dm for one-at-a-time sends.",
        )

    # run_outreach_stage internally builds its own provider instance. To make
    # it use the per-user one we pass it explicitly via the same call site.
    results = run_outreach_stage(db, ev, provider=provider)
    return schemas.OutreachRunResult.build(ev, list(ev.prospects), results)


@router.post("/{event_id}/run", response_model=schemas.PipelineResult)
async def run(
    event_id: int,
    confirm_live_batch: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Convenience: /prospect + /outreach back-to-back. Idempotent.

    Same SAFETY guard as /outreach : in LIVE mode this fires real invites
    to every approved prospect at once. Pass ?confirm_live_batch=true to
    proceed, or use the per-prospect /dm endpoint for safer one-at-a-time.
    """
    ev = get_owned_event(event_id, user, db)

    require_linkedin_send(user)
    provider = get_provider_for_user(user)
    if not provider.dry_run and not confirm_live_batch:
        raise HTTPException(
            400,
            "refusing to run the full pipeline in LIVE mode without "
            "?confirm_live_batch=true. This endpoint fires real LinkedIn "
            "invites for every approved prospect at once.",
        )

    _wipe_prior_prospects(db, ev)
    prospects = await run_pipeline(db, ev, provider=provider)
    return schemas.PipelineResult.build(ev, prospects)


@router.get("/{event_id}/prospects", response_model=schemas.PipelineResult)
def get_prospects(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Read the resolved pool without re-running the pipeline."""
    ev = get_owned_event(event_id, user, db)
    if not ev.prospects:
        raise HTTPException(409, "pipeline has not been run for this event yet")
    return schemas.PipelineResult.build(ev, ev.prospects)


@router.get("/{event_id}/prospects/export.csv")
def export_prospects_csv(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Stream the post-prospecting candidate pool as a CSV. Read-only :
    does not mutate prospect state or kick off any pipeline work."""
    ev = get_owned_event(event_id, user, db)
    if not ev.prospects:
        raise HTTPException(409, "pipeline has not been run for this event yet")

    rows = sorted(ev.prospects, key=lambda p: (-p.fit_score, p.id))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "prospect_id", "name", "role", "company", "seniority",
        "side", "works_on", "offers", "seeks",
        "linkedin_url", "sources",
        "gh_stars", "x_followers", "scholar_citations",
        "fit_score", "fit_reason", "status",
    ])
    for p in rows:
        writer.writerow([
            p.id, p.name or "", p.role or "", p.company or "", p.seniority or "",
            p.side or "", p.works_on or "", p.offers or "", p.seeks or "",
            p.linkedin_url or "", p.sources or "",
            p.gh_stars or 0, p.x_followers or 0, p.scholar_citations or 0,
            p.fit_score or 0, p.fit_reason or "", p.status or "",
        ])
    buf.seek(0)
    filename = f"prospects-event-{ev.id}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{event_id}/prospect/preview", response_model=schemas.ProspectingPreview)
async def prospect_preview(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Run discovery + LLM ICP gate WITHOUT persisting anything.

    Read-only: no DB writes, no provider calls. Safe to hit before
    committing to /prospect.
    """
    from types import SimpleNamespace

    ev = get_owned_event(event_id, user, db)

    icp = {"role": ev.role, "seniority": ev.seniority, "co_stage": ev.co_stage}
    candidates = await run_discovery(icp)

    rows: list[schemas.ProspectingPreviewCandidate] = []
    all_names = [c["name"] for c in candidates]
    for c in candidates:
        fake_prospect = SimpleNamespace(
            name=c["name"],
            works_on=c.get("works_on", "general"),
            offers=c.get("offers", ""),
        )
        peers = [n for n in all_names if n != c["name"]]
        msg = compose(fake_prospect, ev, peers=peers)

        rows.append(schemas.ProspectingPreviewCandidate(
            identity=c["identity"],
            name=c["name"],
            role=c.get("role", "Unknown"),
            company=c.get("company", "Unknown"),
            seniority=c.get("seniority", "Mid"),
            side=c.get("side", "Builds"),
            works_on=c.get("works_on", "general"),
            offers=c.get("offers", ""),
            seeks=c.get("seeks", ""),
            gh_stars=int(c.get("gh_stars") or 0),
            x_followers=int(c.get("x_followers") or 0),
            scholar_citations=int(c.get("scholar_citations") or 0),
            li_resolved=bool(c.get("li_resolved", False)),
            linkedin_url=c.get("linkedin_url"),
            sources=c.get("sources", ""),
            llm_verdict=c.get("llm_verdict"),
            note=msg.note,
            note_chars=len(msg.note),
            message=msg.message,
        ))

    return schemas.ProspectingPreview(
        event_id=ev.id,
        mode="llm" if llm.llm_available() else "mock",
        count=len(rows),
        candidates=rows,
    )


@router.get("/{event_id}/outreach/preview", response_model=schemas.OutreachPreview)
def outreach_preview(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Show exactly what would be sent for each approved prospect, WITHOUT
    invoking the provider or mutating state.

    Provider preview is built with a dry-run instance of the SAME provider
    class as the real one, configured with the signed-in user's account_id.
    """
    ev = get_owned_event(event_id, user, db)
    if not ev.prospects:
        raise HTTPException(409, "no prospects : call /prospect first")

    provider = get_preview_provider(user)
    targets = [p for p in ev.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    peers = targets

    # The prefetch kicked off by run_prospect usually has the cache fully
    # populated by the time we get here. Cache hits return in <100ms;
    # misses fall through to live compose (slower but still works).
    from ..agents.outreach import get_cached_compose

    rows: list[schemas.OutreachPreviewRow] = []
    for p in targets:
        eligible = True
        skip_reason = None
        if not p.linkedin_url:
            eligible = False
            skip_reason = "no linkedin_url"
        msg = get_cached_compose(p.id, ev.id) or compose(
            p, ev, peers=[q.name for q in peers if q.id != p.id],
        )
        lead = provider.build_lead_payload(p, ev, note=msg.note, message=msg.message)
        payload = None
        if eligible:
            preview_provider = provider
            if not provider.dry_run:
                preview_provider = type(provider)(
                    dsn=provider.dsn,
                    api_key=provider.api_key,
                    account_id=provider.account_id,
                    dry_run=True,
                )
            result = preview_provider.send_connection(lead)
            payload = result.payload
        rows.append(schemas.OutreachPreviewRow(
            prospect_id=p.id,
            name=p.name,
            company=p.company,
            linkedin_url=p.linkedin_url,
            fit_score=p.fit_score,
            eligible=eligible,
            skip_reason=skip_reason,
            note=msg.note,
            note_chars=len(msg.note),
            message=msg.message,
            payload=payload,
        ))

    return schemas.OutreachPreview(
        event_id=ev.id,
        provider=provider.name,
        dry_run=provider.dry_run,
        count_eligible=sum(1 for r in rows if r.eligible),
        count_skipped=sum(1 for r in rows if not r.eligible),
        prospects=rows,
    )


def _refresh_connection_status(provider, prospect: models.Prospect) -> str:
    """Live-check Unipile, write the result to the Prospect row, return the
    new status. Called by anything that needs the freshest connection state
    (the smart /invite endpoint, the bulk /check-connections endpoint)."""
    from datetime import datetime, timezone
    try:
        connected = provider.is_relation(prospect.linkedin_url or "")
    except Exception:
        # Don't fail the action just because Unipile is flaky : keep the
        # last known status. Caller sees the unchanged value and proceeds.
        return prospect.connection_status or "unknown"
    new_status = "connected" if connected else "not_connected"
    prospect.connection_status = new_status
    prospect.connection_checked_at = datetime.now(timezone.utc)
    return new_status


@router.post("/{event_id}/prospects/{prospect_id}/invite")
def send_connection_invite(
    event_id: int,
    prospect_id: int,
    override: schemas.OutreachOverride = schemas.OutreachOverride(),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    "Reach out" to ONE prospect from the signed-in user's LinkedIn :
    smart-routes between cold (send_connection) and warm (send_message)
    based on a live Unipile relation check.

    The route name stays `/invite` for frontend compatibility, but it now
    handles both paths transparently. Updates Prospect.connection_status
    on every call so the UI label can re-render.
    """
    import json as _json
    from datetime import datetime, timezone
    ev = get_owned_event(event_id, user, db)
    p = db.get(models.Prospect, prospect_id)
    if not p or p.event_id != event_id:
        raise HTTPException(404, "prospect not found on this event")
    if not p.linkedin_url:
        raise HTTPException(409, "prospect has no linkedin_url")

    require_linkedin_send(user)
    provider = get_provider_for_user(user)
    status = _refresh_connection_status(provider, p)

    peers = [q.name for q in ev.prospects if q.id != p.id and
             q.status in ("approved", "contacted", "rsvp")]
    msg = compose(p, ev, peers=peers)
    final_note = (override.note or msg.note).strip()
    final_message = (override.message or msg.message).strip()

    if status == "connected":
        # Warm path: skip the invite, send the first DM directly. Resolve
        # the provider_id if we don't have it cached (warm prospects often
        # don't, since send_connection is where we usually cache it).
        if not p.linkedin_provider_id or (
            not provider.dry_run and p.linkedin_provider_id.startswith("dry_")
        ):
            try:
                li_id = provider.resolve_linkedin_user(p.linkedin_url)
                if li_id:
                    p.linkedin_provider_id = li_id
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(502, f"linkedin lookup failed: {exc}")

        lead = provider.build_lead_payload(p, ev, note=msg.note, message=final_message)
        res = provider.send_message(lead, linkedin_provider_id=p.linkedin_provider_id)
        if not provider.dry_run and res.state == "message_sent":
            p.status = "contacted"
        path_taken = "warm"
    else:
        # Cold path (the historical default). LinkedIn caps notes at 300.
        if len(final_note) > 300:
            raise HTTPException(400, f"note exceeds LinkedIn's 300-char limit ({len(final_note)})")
        lead = provider.build_lead_payload(p, ev, note=final_note, message=final_message)
        res = provider.send_connection(lead)
        if res.linkedin_provider_id:
            p.linkedin_provider_id = res.linkedin_provider_id
        if not provider.dry_run and res.state == "invite_sent":
            p.status = "contacted"
        path_taken = "cold"

    db.add(models.OutreachLog(
        prospect_id=p.id,
        channel="linkedin",
        state=res.state,
        body=_json.dumps(res.payload, default=str)[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    db.commit()

    return {
        "prospect_id": p.id,
        "prospect_name": p.name,
        "linkedin_url": p.linkedin_url,
        "provider": res.provider,
        "dry_run": res.dry_run,
        "state": res.state,
        "provider_lead_id": res.provider_lead_id,
        "error": res.error,
        "note_preview": final_note if path_taken == "cold" else None,
        "message_preview": final_message,
        "connection_status": status,
        "path_taken": path_taken,
    }


@router.post("/{event_id}/prospects/{prospect_id}/dm")
def send_direct_message(
    event_id: int,
    prospect_id: int,
    override: schemas.OutreachOverride = schemas.OutreachOverride(),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Send a direct LinkedIn DM to ONE prospect, FROM THE SIGNED-IN USER'S
    LINKEDIN account. Skips the connection-invite step. Use when already
    connected to the recipient.

    Accepts optional `message` in the request body to override the agent
    composition (the `note` field is ignored here).
    """
    import json as _json
    from datetime import datetime, timezone
    ev = get_owned_event(event_id, user, db)
    p = db.get(models.Prospect, prospect_id)
    if not p or p.event_id != event_id:
        raise HTTPException(404, "prospect not found on this event")
    if not p.linkedin_url:
        raise HTTPException(409, "prospect has no linkedin_url")

    require_linkedin_send(user)
    provider = get_provider_for_user(user)

    # Resolve & cache the linkedin provider id. Re-resolve when going live if
    # the cached id is a leftover dry-run placeholder.
    needs_resolve = (
        not p.linkedin_provider_id
        or (not provider.dry_run and p.linkedin_provider_id.startswith("dry_"))
    )
    if needs_resolve:
        try:
            li_id = provider.resolve_linkedin_user(p.linkedin_url)
            if li_id:
                p.linkedin_provider_id = li_id
                db.commit()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"linkedin lookup failed: {exc}")

    peers = [q.name for q in ev.prospects if q.id != p.id and
             q.status in ("approved", "contacted", "rsvp")]
    msg = compose(p, ev, peers=peers)
    final_message = (override.message or msg.message).strip()
    lead = provider.build_lead_payload(p, ev, note=msg.note, message=final_message)

    res = provider.send_message(lead, linkedin_provider_id=p.linkedin_provider_id)

    db.add(models.OutreachLog(
        prospect_id=p.id,
        channel="linkedin",
        state=res.state,
        body=_json.dumps(res.payload, default=str)[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    if not provider.dry_run and res.state == "message_sent":
        p.status = "contacted"
    db.commit()

    return {
        "prospect_id": p.id,
        "prospect_name": p.name,
        "linkedin_url": p.linkedin_url,
        "provider": res.provider,
        "dry_run": res.dry_run,
        "state": res.state,
        "provider_lead_id": res.provider_lead_id,
        "error": res.error,
        "message_preview": final_message,
        "payload": res.payload,
    }


@router.post("/{event_id}/check-connections")
def check_connections(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Bulk-refresh connection_status for every prospect in this event whose
    status is currently "unknown". Designed to be called once when the
    auto-outreach screen loads so button labels render correctly.

    Re-checks are NOT free : one Unipile API call per prospect : so already-
    classified rows are skipped. To force a recheck, hit /invite which always
    calls _refresh_connection_status.
    """
    ev = get_owned_event(event_id, user, db)
    provider = get_preview_provider(user)

    updated: list[dict] = []
    skipped = 0
    for p in ev.prospects:
        if p.connection_status != "unknown":
            skipped += 1
            continue
        if not p.linkedin_url:
            continue
        status = _refresh_connection_status(provider, p)
        updated.append({
            "prospect_id": p.id,
            "name": p.name,
            "connection_status": status,
        })
    db.commit()
    return {
        "event_id": event_id,
        "checked": len(updated),
        "skipped": skipped,
        "results": updated,
    }


@router.get("/{event_id}/outreach/log", response_model=schemas.OutreachLogResult)
def outreach_log(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Per-event outreach timeline. One entry per OutreachLog row, sorted by
    (prospect, ts). The dashboard renders this as the per-prospect funnel.
    """
    ev = get_owned_event(event_id, user, db)
    return schemas.OutreachLogResult.build(ev)
