"""backend/demo/extended.py : the layer beyond current tracked usage.

cohort.py's baseline reproduces what Surplus already does today: capture,
notes, drafts. This module adds the capabilities the product is BUILDING
TOWARD but doesn't have production telemetry for yet -- the "NEXT SURPLUS"
half of the brief's own diagram (signal x relationship matching -> opportunity
ranking -> jurisdiction-aware action -> outcome feedback). Every row here is
tagged provenance.EXTENDED, never BASELINE -- the distinction the brief asks
to preserve is exactly "this part is a scaled reproduction of something real"
versus "this part demonstrates where the architecture goes next", and
collapsing that into one tag would erase the one thing this module exists to
keep visible.

Two of the three sub-layers reuse REAL production logic rather than
reimplementing an approximation of it:
  - match/ranking reuses book._score_health_heuristic (the actual
    deterministic scorer -- same function production calls when no LLM
    judge is configured, not a demo-only lookalike).
  - jurisdiction-aware action reuses solicitation_signals.check() (built
    earlier this session) against each generated draft, for real -- so the
    cohort's allow/block mix is a genuine consequence of the real gate
    running against a real multi-jurisdiction population, not a fabricated
    distribution of outcomes.

Outcome feedback (the third sub-layer) has no real function to call yet --
it's the thing the moat memo describes as not-yet-collected, so this layer
can only model its SHAPE (an edit occurred, tagged with a reason category),
never fabricate example draft text and imply it came from the model.
"""
from __future__ import annotations

from datetime import timezone
from typing import Optional

from . import distributions as dist
from . import provenance as prov
from .. import models
from ..agents.relationship.book import _score_health_heuristic
from ..agents.relationship import solicitation_signals as sig

_UTC = timezone.utc


def _aware(dt):
    """SQLite hands back naive values even when we stored tz-aware ones
    (same normalization used in accounts_read.py) -- mixing the two in
    subtraction raises TypeError."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt


def compute_match_scores(db, cohort_id: str) -> list[dict]:
    """Signal x relationship matching -> opportunity ranking. For every
    generated contact, score it with the REAL heuristic
    (book._score_health_heuristic) and combine with whether it carries a
    draftworthy signal (job_change/new_post, per updates_engine's real
    taxonomy) into a rank. Returns the ranking; does not write new DB rows
    -- ranking is a read-model in production too (book.py computes it on
    read, never stores it), so this layer matches that shape rather than
    inventing a stored-score table production doesn't have."""
    from sqlalchemy import select
    tags = db.execute(
        select(models.DemoProvenance)
        .where(models.DemoProvenance.cohort_id == cohort_id,
               models.DemoProvenance.table_name == "contacts")
    ).scalars().all()
    contact_ids = [t.row_id for t in tags]
    if not contact_ids:
        return []
    contacts = db.execute(
        select(models.Contact).where(models.Contact.id.in_(contact_ids))
    ).scalars().all()

    now = __import__("datetime").datetime.now(_UTC)
    ranked = []
    for c in contacts:
        watched_at = _aware(c.watched_at)
        days_since = (now - watched_at).days if watched_at else 90
        health = _score_health_heuristic({
            "days_since": days_since, "cadence_days": 7 if c.vip else 30,
            "tier": "key" if c.vip else "",
        })
        has_signal = db.execute(
            select(models.RelationshipInteraction.id)
            .where(models.RelationshipInteraction.contact_id == c.id,
                   models.RelationshipInteraction.source_type == "activity_update")
            .limit(1)
        ).first() is not None
        signal_boost = 1.5 if has_signal else 1.0
        warmth_weight = {"active": 1.0, "warm": 0.8, "cooling": 0.5, "dormant": 0.2}
        score = warmth_weight.get(health["status"], 0.5) * signal_boost
        ranked.append({"contact_id": c.id, "name": c.name, "status": health["status"],
                        "has_signal": has_signal, "opportunity_score": round(score, 3)})
    ranked.sort(key=lambda r: r["opportunity_score"], reverse=True)
    return ranked


def run_jurisdiction_gate(db, cohort_id: str) -> str:
    """For every generated draft interaction in the cohort, resolve the
    REAL user + contact and run it through solicitation_signals.check() --
    the exact gate wired into pipeline/send/sender.py earlier this session.
    Writes one RelationshipInteraction per draft recording the real verdict
    (allowed/blocked + reason), tagged EXTENDED. Returns the new cohort_id
    for this layer's own rows (kept separate from the baseline cohort_id so
    a delete of one doesn't require re-deriving which rows belong to it)."""
    from sqlalchemy import select
    ext_cohort_id = f"{cohort_id}-jurisdiction"

    draft_tags = db.execute(
        select(models.DemoProvenance)
        .where(models.DemoProvenance.cohort_id == cohort_id,
               models.DemoProvenance.table_name == "relationship_interactions")
    ).scalars().all()
    draft_ids = [t.row_id for t in draft_tags]
    drafts = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.id.in_(draft_ids),
               models.RelationshipInteraction.interaction_type == "message",
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
    ).scalars().all()

    for draft in drafts:
        user = db.get(models.User, draft.actor_user_id)
        contact = db.get(models.Contact, draft.contact_id)
        if user is None or contact is None:
            continue
        verdict = sig.check(db, user, contact, "linkedin_dm")
        row = models.RelationshipInteraction(
            actor_user_id=user.id, contact_id=contact.id,
            source_type="manual_note", interaction_type="note", direction="none",
            occurred_at=draft.occurred_at,
            title=f"Jurisdiction gate: {'allowed' if verdict.allowed else 'blocked'}",
            summary=verdict.reason,
        )
        db.add(row)
        db.flush()
        prov.tag(db, row, provenance=prov.EXTENDED, cohort_id=ext_cohort_id)

    db.commit()
    return ext_cohort_id


def outcome_feedback(db, cohort_id: str) -> str:
    """Model the SHAPE of the draft-edit-preference flywheel the moat memo
    describes -- an edit happened, tagged with a reason category -- without
    fabricating example draft text and implying it's real model output.
    This is intentionally the thinnest of the three sub-layers: it's a
    placeholder for a data source that doesn't exist yet, not a simulation
    of one."""
    from sqlalchemy import select
    ext_cohort_id = f"{cohort_id}-outcomes"

    edit_tags = db.execute(
        select(models.DemoProvenance)
        .where(models.DemoProvenance.cohort_id == cohort_id,
               models.DemoProvenance.table_name == "relationship_interactions")
    ).scalars().all()
    edit_ids = [t.row_id for t in edit_tags]
    edits = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.id.in_(edit_ids),
               models.RelationshipInteraction.title == "Edited draft before sending")
    ).scalars().all()

    reason_categories = ("tone_too_formal", "added_specific_detail",
                          "shortened", "changed_ask", "fixed_fact")
    rng_seed = 0
    for edit in edits:
        rng_seed += 1
        category = reason_categories[rng_seed % len(reason_categories)]
        row = models.RelationshipInteraction(
            actor_user_id=edit.actor_user_id, contact_id=edit.contact_id,
            source_type="manual_note", interaction_type="note", direction="none",
            occurred_at=edit.occurred_at,
            title="Outcome feedback signal (shape only, not real edit content)",
            summary=f"edit_category={category}",
        )
        db.add(row)
        db.flush()
        prov.tag(db, row, provenance=prov.EXTENDED, cohort_id=ext_cohort_id)

    db.commit()
    return ext_cohort_id


def run_all(db, cohort_id: str) -> dict:
    """Run every extended sub-layer against an already-generated baseline
    cohort. Returns the sub-cohort ids plus the (in-memory, not persisted)
    ranking, so a caller can inspect all three without re-querying."""
    ranking = compute_match_scores(db, cohort_id)
    jurisdiction_cohort_id = run_jurisdiction_gate(db, cohort_id)
    outcomes_cohort_id = outcome_feedback(db, cohort_id)
    return {
        "ranking_top5": ranking[:5],
        "jurisdiction_cohort_id": jurisdiction_cohort_id,
        "outcomes_cohort_id": outcomes_cohort_id,
    }
