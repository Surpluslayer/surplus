"""
pipeline.py — stage 02-03 orchestrator.

run_pipeline() is the barrier flow behind `POST /events/{id}/run`:

    fan-out  ->  persist surfaced prospects
             ->  score every prospect
             ->  set the floating threshold on the event
             ->  mark below-threshold prospects
             ->  autonomous outreach for everyone above it
             ->  commit

Matching (stage 04) and ROI (stage 05) are deliberately *not* here — they are
separate barriers that only make sense once the pool has resolved, and they're
triggered by their own endpoints.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import models, config
from .agents.prospector import prospect
from .agents.scorer import score_prospect, floating_threshold
from .agents.outreach import run_outreach


async def run_pipeline(db: Session, event: models.Event) -> list[models.Prospect]:
    """Run fan-out, scoring, thresholding, and autonomous outreach for an event."""
    icp = {"role": event.role, "seniority": event.seniority, "co_stage": event.co_stage}

    # --- stage 02: concurrent fan-out, persist what surfaced --------------
    raw = await prospect(icp)
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
            li_resolved=r["li_resolved"],
            sources=r["sources"],
            status="surfaced",
        )
        db.add(p)
        prospects.append(p)
    db.flush()  # assign ids

    # --- stage 03a: score, then float the threshold to hit funnel supply --
    for p in prospects:
        p.fit_score, p.fit_reason = score_prospect(p, event)

    funnel_target = round(event.headcount / config.FUNNEL_CONVERSION)
    event.threshold = floating_threshold([p.fit_score for p in prospects], funnel_target)

    for p in prospects:
        if p.fit_score < event.threshold:
            p.status = "below"

    # --- stage 03b: autonomous outreach for everyone above the line ------
    for p, events, status in run_outreach(prospects, event):
        p.status = status
        for e in events:
            db.add(models.OutreachLog(
                prospect_id=p.id, state=e["state"], body=e["body"], ts=e["ts"],
            ))

    db.commit()
    return prospects
