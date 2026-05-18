"""
pipeline.py : stage 02-03 orchestrator, split into two halves.

    run_prospect(db, event)        stage 02 + 03a
        fan-out -> persist -> score -> floating threshold ->
        mark each prospect 'below' or 'approved'.

    run_outreach_stage(db, event)  stage 03b
        for every approved prospect:
          1. compose() the (note, message)
          2. provider.send_connection(lead) : DRY_RUN by default
          3. write an OutreachLog row capturing the result
        In DRY_RUN, additionally roll the RNG simulator so /match and /roi
        still have RSVPs to work with. In LIVE mode, status changes come
        only via real webhook events.

    run_pipeline(db, event)        facade : does both, in order.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models, config
from .agents.prospector import prospect
from .agents.scorer import score_prospect, floating_threshold
from .agents.outreach import compose, run_outreach
from .agents.sources import ALL_ADAPTERS
from .providers import get_provider, ProviderResult


def _adapters_for_event(event: models.Event):
    """Filter the global adapter registry to only those the operator
    selected on intake. LinkedIn is always included as a hard guarantee
    (the schema validator should already enforce it, but we double-check
    here so a malformed legacy row can't run a no-anchor pipeline)."""
    enabled = {s.strip().lower()
               for s in (event.enabled_sources or "").split(",")
               if s.strip()}
    if not enabled:
        return list(ALL_ADAPTERS)
    enabled.add("linkedin")
    return [a for a in ALL_ADAPTERS if a.key in enabled]


async def run_prospect(
    db: Session,
    event: models.Event,
    force_fresh: bool = False,
) -> list[models.Prospect]:
    """Fan out, persist, score, set the floating threshold, mark approved/below.

    `force_fresh=True` bypasses the in-memory ICP cache in prospect().
    Use it when the user explicitly asks for new results (e.g. via
    `?fresh=true` on the route); default reuses the cached pool.
    """
    icp = {
        "role": event.role,
        "seniority": event.seniority,
        "co_stage": event.co_stage,
        "city": event.city,
    }

    adapters = _adapters_for_event(event)
    raw = await prospect(icp, adapters=adapters, force_fresh=force_fresh)
    prospects: list[models.Prospect] = []
    for r in raw:
        p = models.Prospect(
            event_id=event.id,
            identity=r["identity"],
            name=r["name"],
            role=r["role"],
            company=r["company"],
            seniority=r["seniority"],
            side=r["side"],
            works_on=r["works_on"],
            offers=r["offers"],
            seeks=r["seeks"],
            gh_stars=r["gh_stars"],
            x_followers=r["x_followers"],
            scholar_citations=r["scholar_citations"],
            li_resolved=r["li_resolved"],
            linkedin_url=r.get("linkedin_url"),
            sources=r["sources"],
            status="surfaced",
        )
        db.add(p)
        prospects.append(p)
    db.flush()

    for p in prospects:
        p.fit_score, p.fit_reason = score_prospect(p, event)

    funnel_target = round(event.headcount / config.FUNNEL_CONVERSION)
    event.threshold = floating_threshold([p.fit_score for p in prospects], funnel_target)

    # Threshold gating intentionally disabled : every discovered prospect is
    # marked "approved" so the user can message anyone, regardless of fit
    # score. The score + computed threshold are still surfaced in the UI for
    # context (sort order, "above threshold" badge), they just don't block
    # outreach. To re-enable the gate, restore:
    #   p.status = "approved" if p.fit_score >= event.threshold else "below"
    for p in prospects:
        p.status = "approved"

    db.commit()
    return prospects


# ---------- outreach -------------------------------------------------------


def _eligible_for_outreach(p: models.Prospect) -> tuple[bool, str | None]:
    """
    Layer-A qualification check. Returns (eligible, skip_reason).
    """
    if p.status not in ("approved", "contacted", "rsvp"):
        return False, f"status={p.status!r} (not approved)"
    if not p.linkedin_url:
        return False, "no linkedin_url"
    return True, None


def _peer_names_for(target: models.Prospect, attending: list[models.Prospect]) -> list[str]:
    """The first names already-confirmed peers for the composition reveal."""
    return [p.name for p in attending if p.id != target.id]


def run_outreach_stage(
    db: Session,
    event: models.Event,
    provider=None,
) -> list[ProviderResult]:
    """
    Provider-backed outreach. Idempotent: wipes prior outreach logs + resets
    contacted/rsvp prospects back to 'approved' before re-running.

    Returns the per-prospect ProviderResult list (also stored in OutreachLog).

    `provider` argument lets the caller pass a per-user provider (the
    LinkedIn account of the signed-in user). If omitted, falls back to the
    env-var operator account : used by the dry-run simulator and by callers
    that don't have a user in scope (e.g. tests).
    """
    if provider is None:
        provider = get_provider()

    # idempotent reset
    targets = [p for p in event.prospects
               if p.status in ("approved", "contacted", "rsvp")]
    for p in targets:
        for o in list(p.outreach):
            db.delete(o)
        p.status = "approved"
    db.flush()

    # personalization peers = the other approved/confirmed prospects for this event
    peers = [p for p in targets if p.fit_score >= event.threshold]

    results: list[ProviderResult] = []
    eligible: list[models.Prospect] = []

    for p in targets:
        ok, skip_reason = _eligible_for_outreach(p)
        if not ok:
            db.add(models.OutreachLog(
                prospect_id=p.id,
                channel="linkedin",
                state="failed",
                body=f"skipped: {skip_reason}",
                provider=provider.name,
            ))
            continue

        peer_names = _peer_names_for(p, peers)
        msg = compose(p, event, peers=peer_names)
        lead = provider.build_lead_payload(p, event, note=msg.note, message=msg.message)
        res = provider.send_connection(lead)
        results.append(res)
        eligible.append(p)

        # cache the provider's internal LinkedIn user id so webhooks can
        # resolve back to this prospect.
        if res.linkedin_provider_id:
            p.linkedin_provider_id = res.linkedin_provider_id

        db.add(models.OutreachLog(
            prospect_id=p.id,
            channel="linkedin",
            state=res.state,
            body=json.dumps(res.payload, default=str)[:8000],
            ts=datetime.now(timezone.utc),
            provider=res.provider,
            provider_lead_id=res.provider_lead_id,
        ))
        # In dry-run we don't update prospect.status here : the simulator
        # below will set it to contacted/rsvp for demo continuity. In live
        # mode, a successful send_connection means we're now waiting on
        # invite_accepted / message_replied webhooks; we update status now
        # so the funnel reflects that.
        if not provider.dry_run and res.state == "invite_sent":
            p.status = "contacted"

    db.flush()

    # DRY_RUN continuity: roll the RNG simulator so /match and /roi keep
    # working end-to-end. We only do this in dry-run because in live mode
    # real webhooks are the source of truth for status.
    if provider.dry_run:
        sim_targets = [p for p in eligible if p.status == "approved"]
        for p, sim_events, status in run_outreach(sim_targets, event):
            p.status = status
            for e in sim_events:
                # skip the redundant 'sent' entry : provider's dry_run_queued
                # log already captures the first touch.
                if e["state"] == "sent":
                    continue
                db.add(models.OutreachLog(
                    prospect_id=p.id,
                    channel="linkedin",
                    state="invite_accepted" if e["state"] == "opened" else
                          "message_replied" if e["state"] == "replied" else
                          e["state"],
                    body=e["body"],
                    ts=e["ts"],
                    provider=provider.name,
                ))

    db.commit()
    return results


async def run_pipeline(db: Session, event: models.Event, provider=None):
    """Facade : run /prospect then /outreach back-to-back. Same provider=
    fallthrough as run_outreach_stage."""
    await run_prospect(db, event)
    run_outreach_stage(db, event, provider=provider)
    return list(event.prospects)
