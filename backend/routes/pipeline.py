"""routes/pipeline.py — stage 02-03. Prospecting and outreach, split."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..pipeline import run_prospect, run_outreach_stage, run_pipeline
from ..agents.outreach import compose
from ..agents.prospector import prospect as run_discovery
from ..agents import llm
from ..providers import get_provider

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
):
    """
    Stage 02 + 03a only: fan-out + score + threshold. No outreach.

    Marks every prospect 'approved' or 'below'. Idempotent — wipes prior
    prospects (and their outreach + conversions) first.

    Pass `?fresh=true` to bypass the in-memory ICP cache and force a
    real web_search round. By default the prospector reuses the cached
    pool for the same ICP, which makes iterating on UI / outreach copy
    essentially instant.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    _wipe_prior_prospects(db, ev)
    prospects = await run_prospect(db, ev, force_fresh=fresh)
    return schemas.PipelineResult.build(ev, prospects)


@router.post("/{event_id}/outreach", response_model=schemas.OutreachRunResult)
def outreach_only(event_id: int, db: Session = Depends(get_db),
                  confirm_live_batch: bool = False):
    """
    Stage 03b only: provider-backed outreach for everyone 'approved'.

    SAFETY: in LIVE mode this fires real LinkedIn invites to every approved
    prospect at once. We require ?confirm_live_batch=true as an explicit
    second-step opt-in to prevent the entire mock pool from getting blasted
    by accident. DRY_RUN calls bypass the guard.

    Idempotent: wipes prior outreach logs and resets contacted/rsvp back
    to approved before re-running.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "no prospects — call /prospect first")
    targets = [p for p in ev.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    if not targets:
        raise HTTPException(409, "no approved prospects to contact — "
                                 "threshold may be too high for the pool")

    provider = get_provider()
    if not provider.dry_run and not confirm_live_batch:
        raise HTTPException(
            400,
            f"refusing to fire {len(targets)} real LinkedIn invites in one batch "
            f"without explicit confirmation. Re-call with ?confirm_live_batch=true, "
            f"or use POST /events/{ev.id}/prospects/<pid>/dm for one-at-a-time sends.",
        )

    results = run_outreach_stage(db, ev)
    return schemas.OutreachRunResult.build(ev, list(ev.prospects), results)


@router.post("/{event_id}/run", response_model=schemas.PipelineResult)
async def run(event_id: int, db: Session = Depends(get_db),
              confirm_live_batch: bool = False):
    """
    Convenience: /prospect + /outreach back-to-back. Idempotent.

    Same SAFETY guard as /outreach — in LIVE mode this fires real invites
    to every approved prospect at once. Pass ?confirm_live_batch=true to
    proceed, or use the per-prospect /dm endpoint for safer one-at-a-time.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    provider = get_provider()
    if not provider.dry_run and not confirm_live_batch:
        raise HTTPException(
            400,
            "refusing to run the full pipeline in LIVE mode without "
            "?confirm_live_batch=true. This endpoint fires real LinkedIn "
            "invites for every approved prospect at once.",
        )

    _wipe_prior_prospects(db, ev)
    prospects = await run_pipeline(db, ev)
    return schemas.PipelineResult.build(ev, prospects)


@router.get("/{event_id}/prospects", response_model=schemas.PipelineResult)
def get_prospects(event_id: int, db: Session = Depends(get_db)):
    """Read the resolved pool without re-running the pipeline."""
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "pipeline has not been run for this event yet")
    return schemas.PipelineResult.build(ev, ev.prospects)


@router.get("/{event_id}/prospect/preview", response_model=schemas.ProspectingPreview)
async def prospect_preview(event_id: int, db: Session = Depends(get_db)):
    """
    Run discovery + LLM ICP gate WITHOUT persisting anything.

    In LLM mode (ANTHROPIC_API_KEY set), this fires real web_search calls
    plus a relevance verdict per merged candidate. In mock mode it reads
    the hand-curated prospect_pool.json. In both modes, every surfaced
    candidate is run through compose() so the response shows the exact
    LinkedIn connection note + post-accept DM that would land in their
    inbox — proving the LLM-extracted profile fields (works_on, offers,
    seeks) actually feed the outreach personalization.

    Read-only: no DB writes, no provider calls. Safe to hit before
    committing to /prospect.
    """
    from types import SimpleNamespace

    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    icp = {"role": ev.role, "seniority": ev.seniority, "co_stage": ev.co_stage}
    candidates = await run_discovery(icp)

    rows: list[schemas.ProspectingPreviewCandidate] = []
    all_names = [c["name"] for c in candidates]
    for c in candidates:
        # compose() reads .name, .works_on, .offers — a SimpleNamespace is
        # enough, we don't need an ORM row. peers come from the other
        # surfaced candidates (the same logic the live pipeline uses).
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
def outreach_preview(event_id: int, db: Session = Depends(get_db)):
    """
    Show exactly what would be sent for each approved prospect, WITHOUT
    invoking the provider or mutating state.

    For each approved prospect: composed note + message, eligibility flag,
    and (if eligible) the full provider payload that would be POSTed.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    if not ev.prospects:
        raise HTTPException(409, "no prospects — call /prospect first")

    provider = get_provider()
    targets = [p for p in ev.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    peers = targets  # composition reveal uses everyone passing the threshold

    rows: list[schemas.OutreachPreviewRow] = []
    for p in targets:
        eligible = True
        skip_reason = None
        if not p.linkedin_url:
            eligible = False
            skip_reason = "no linkedin_url"
        msg = compose(p, ev, peers=[q.name for q in peers if q.id != p.id])
        lead = provider.build_lead_payload(p, ev, note=msg.note, message=msg.message)
        # Provider-agnostic preview: dry-run is forced to true to guarantee no
        # network call, and the payload that *would* be POSTed is captured on
        # the ProviderResult. Works for any provider.
        payload = None
        if eligible:
            # If the live provider isn't already in dry-run, use a throwaway
            # dry-run instance of the SAME class for the preview.
            preview_provider = provider
            if not provider.dry_run:
                preview_provider = type(provider)(dry_run=True)  # type: ignore[call-arg]
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


@router.post("/{event_id}/prospects/{prospect_id}/invite")
def send_connection_invite(event_id: int, prospect_id: int,
                           override: schemas.OutreachOverride = schemas.OutreachOverride(),
                           db: Session = Depends(get_db)):
    """
    Send a LinkedIn connection request to ONE prospect.

    Accepts optional `note` and `message` in the request body — if provided,
    those override the agent-composed text. Lets the operator review/edit
    the messages in the UI before firing.
    """
    import json as _json
    from datetime import datetime, timezone
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    p = db.get(models.Prospect, prospect_id)
    if not p or p.event_id != event_id:
        raise HTTPException(404, "prospect not found on this event")
    if not p.linkedin_url:
        raise HTTPException(409, "prospect has no linkedin_url")

    provider = get_provider()
    peers = [q.name for q in ev.prospects if q.id != p.id and
             q.status in ("approved", "contacted", "rsvp")]
    msg = compose(p, ev, peers=peers)
    final_note = (override.note or msg.note).strip()
    final_message = (override.message or msg.message).strip()
    if len(final_note) > 300:
        raise HTTPException(400, f"note exceeds LinkedIn's 300-char limit ({len(final_note)})")
    lead = provider.build_lead_payload(p, ev, note=final_note, message=final_message)
    res = provider.send_connection(lead)

    if res.linkedin_provider_id:
        p.linkedin_provider_id = res.linkedin_provider_id

    db.add(models.OutreachLog(
        prospect_id=p.id,
        channel="linkedin",
        state=res.state,
        body=_json.dumps(res.payload, default=str)[:8000],
        ts=datetime.now(timezone.utc),
        provider=res.provider,
        provider_lead_id=res.provider_lead_id,
    ))
    if not provider.dry_run and res.state == "invite_sent":
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
        "note_preview": final_note,
        "message_preview": final_message,
    }


@router.post("/{event_id}/prospects/{prospect_id}/dm")
def send_direct_message(event_id: int, prospect_id: int,
                        override: schemas.OutreachOverride = schemas.OutreachOverride(),
                        db: Session = Depends(get_db)):
    """
    Send a direct LinkedIn DM to ONE prospect, skipping the connection-invite
    step. Use this when you're already connected to the recipient.

    Accepts optional `message` in the request body to override the agent
    composition (the `note` field is ignored here — connection requests
    use that, DMs use `message`).
    """
    import json as _json
    from datetime import datetime, timezone
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    p = db.get(models.Prospect, prospect_id)
    if not p or p.event_id != event_id:
        raise HTTPException(404, "prospect not found on this event")
    if not p.linkedin_url:
        raise HTTPException(409, "prospect has no linkedin_url")

    provider = get_provider()

    # Resolve & cache the linkedin provider id. We re-resolve when going
    # live if the cached id is a leftover dry-run placeholder ("dry_li_..."),
    # since those aren't valid Unipile ids.
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


@router.get("/{event_id}/outreach/log", response_model=schemas.OutreachLogResult)
def outreach_log(event_id: int, db: Session = Depends(get_db)):
    """
    Per-event outreach timeline. One entry per OutreachLog row, sorted by
    (prospect, ts). The dashboard renders this as the per-prospect funnel.
    """
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "event not found")

    return schemas.OutreachLogResult.build(ev)
