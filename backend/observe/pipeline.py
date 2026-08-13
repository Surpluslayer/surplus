"""backend/observe/pipeline.py : the execution-trace view (Observe checklist
section B) -- the one piece the initial Observe build didn't have. Every
adapter in adapters.py answers "why did THIS object turn out this way"; this
module answers the different question "what actually RAN, in order, to
produce this recommendation" -- ingestion -> entity_resolution ->
signal_library -> targeting -> relationship -> ranking -> jurisdiction ->
output, each stage independently timed, versioned, and given an honest
execution status.

`status` is about STAGE HEALTH, never about the decision's polarity: a
jurisdiction stage that correctly BLOCKS an action is `status="success"` (it
ran correctly and produced a real answer) -- `status="warning"` means the
stage hit a REAL, already-documented fallback path elsewhere in this
codebase (unset practice_area -> demo.signal_taxonomy's corporate_ma
default, unset bar_jurisdiction -> solicitation.py's NY default, unset
relationship_type -> PROSPECT default, zero interactions -> cold start), and
`status="failure"` means the
stage's own computation raised an exception -- captured per-stage so one
stage's failure doesn't blank out the rest of the pipeline trace, matching
the checklist's "show fallbacks/errors when something failed".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from . import adapters
from . import versions as ver
from .trace import DEMO, OBSERVED, now as trace_now
from .. import models
from ..agents.relationship import solicitation_signals as sig
from ..demo import ranking_trace as rt
from ..demo import signal_taxonomy as tax

_UTC = timezone.utc

STAGE_ORDER = ("ingestion", "entity_resolution", "signal_library", "targeting",
               "relationship", "ranking", "jurisdiction", "output")


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt


@dataclass
class PipelineStage:
    name: str
    status: str            # "success" | "warning" | "failure"
    latency_ms: float
    version: str | None
    decision: str
    evidence: dict = field(default_factory=dict)
    fallback: str | None = None
    # A stage detail worth stating that is NOT a degradation: a state the
    # pipeline hasn't reached yet, or a default that is the stricter choice
    # rather than a worse one. Distinct from `fallback` because that one
    # renders as a WARN, and a log that warns about ordinary states trains
    # the reader to ignore the warnings that matter.
    note: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status,
            "latency_ms": round(self.latency_ms, 3), "version": self.version,
            "decision": self.decision, "evidence": self.evidence,
            "fallback": self.fallback, "note": self.note, "error": self.error,
        }


@dataclass
class PipelineTrace:
    account_id: int
    object_id: str
    timestamp: datetime
    stages: list
    provenance: str

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id, "object_id": self.object_id,
            "timestamp": self.timestamp.isoformat(),
            "provenance": self.provenance,
            "overall_status": _overall_status(self.stages),
            "total_latency_ms": round(sum(s.latency_ms for s in self.stages), 3),
            "stages": [s.to_dict() for s in self.stages],
        }


def _overall_status(stages: list) -> str:
    statuses = {s.status for s in stages}
    if "failure" in statuses:
        return "failure"
    if "warning" in statuses:
        return "warning"
    return "success"


def _timed(fn):
    start = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - start) * 1000.0


def _run_stage(name: str, version: str, fn) -> PipelineStage:
    """Stages return (status, decision, evidence, fallback) and may append a
    fifth element, `note` -- see PipelineStage.note for the difference."""
    try:
        out, latency_ms = _timed(fn)
        status, decision, evidence, fallback = out[:4]
        note = out[4] if len(out) > 4 else None
        return PipelineStage(name=name, status=status, latency_ms=latency_ms, version=version,
                             decision=decision, evidence=evidence, fallback=fallback,
                             note=note)
    except Exception as exc:  # a real stage failure must not blank the rest of the pipeline
        return PipelineStage(name=name, status="failure", latency_ms=0.0, version=version,
                             decision="stage failed", evidence={}, error=str(exc))


def _ingestion(db, contact) -> tuple:
    signal = rt._latest_signal(db, contact.id)
    if signal is None:
        return ("warning", "no detected signal on this contact", {},
                "no source_type=activity_update interaction found -- downstream stages "
                "have nothing to score against")
    meta = json.loads(signal.meta_json or "{}")
    return ("success", f"detected {meta.get('signal_kind', 'signal')}", {
        "interaction_id": signal.id, "signal_kind": meta.get("signal_kind"),
        "occurred_at": signal.occurred_at.isoformat(), "source_type": signal.source_type,
    }, None)


def _entity_resolution(db, contact) -> tuple:
    rtype = sig._relationship_type(contact)
    raw = (getattr(contact, "relationship_type", None) or "").strip()
    note = None
    if not raw:
        # Not a degradation, so not a `fallback`: PROSPECT is the STRICTER of
        # the two classifications (prospects get the full Rule 7.3 treatment
        # that existing clients are exempt from), and it is the norm for an
        # imported book, where nothing sets relationship_type. Flagging every
        # such contact as a warning made an entire book read as broken while
        # the gate was in fact erring safe on all of it.
        note = ("relationship_type unset -- classified PROSPECT, the stricter "
                "of the two (clients are exempt from the Rule 7.3 gate; "
                "prospects are not), so the gate errs safe here")
    return ("success", f"resolved as {rtype.value}", {
        "contact_id": contact.id, "primary_identity_key": contact.primary_identity_key,
        "relationship_type_raw": raw or None, "resolved_relationship_type": rtype.value,
    }, None, note)


def _signal_library(db, contact) -> tuple:
    from ..demo import signal_taxonomy as tax
    signal = rt._latest_signal(db, contact.id)
    if signal is None:
        return ("warning", "no signal to classify", {}, "no upstream signal from ingestion")
    meta = json.loads(signal.meta_json or "{}")
    category = meta.get("signal_category")
    category_source = "demo_signal_category" if category else None
    if not category:
        category = tax.production_signal_category(signal.interaction_type, meta)
        category_source = "production_interaction_type" if category else None
    if not category:
        # Name the VALUES that failed to map. The old wording said only that
        # there was "no mapping for this interaction_type/milestone_type",
        # which is unactionable -- it took reading updates_engine._POST_SYSTEM
        # to discover that three of production's five milestone types
        # (role/award/milestone) had no bucket at all.
        return ("warning", "unclassified",
                {"interaction_type": signal.interaction_type,
                 "milestone_type": meta.get("milestone_type"),
                 "signal_kind": meta.get("signal_kind")},
                tax.unmapped_reason(signal.interaction_type, meta))
    return ("success", f"classified as {category}",
            {"signal_category": category, "category_source": category_source,
             "signal_kind": meta.get("signal_kind")}, None)


def _targeting(db, user, contact) -> tuple:
    trace = adapters.targeting_trace(db, user, contact)
    fallback = None
    status = "success"
    if not getattr(user, "practice_area", None):
        status, fallback = "warning", (
            "practice_area unset -- defaulting to "
            f"{tax.effective_practice_area(user)!r} (demo.signal_taxonomy._DEFAULT_PRACTICE_AREA)")
    return (status, trace.decision, {"score": trace.score,
            "factors": [f.to_dict() for f in trace.features]}, fallback)


def _relationship(db, user, contact) -> tuple:
    trace = adapters.relationship_trace(db, user, contact)
    total_interactions = next(
        (f.evidence.get("total_interactions") for f in trace.features
         if f.name == "relationship_strength"), 0)
    fallback = None
    status = "success"
    if not total_interactions:
        status, fallback = "warning", "zero prior interactions -- cold start, no relationship evidence"
    return (status, trace.decision, {"score": trace.score,
            "factors": [f.to_dict() for f in trace.features]}, fallback)


def _ranking(db, user, contact, candidates) -> tuple:
    trace = adapters.opportunity_trace(db, user, contact, candidates)
    return ("success", trace.decision,
            {"score": trace.score, "rank": trace.rank,
             "candidates_considered_total": trace.evidence.get("candidates_considered_total")},
            None)


def _jurisdiction(db, user, contact, channel) -> tuple:
    trace = adapters.jurisdiction_trace(db, user, contact, channel)
    fallback = None
    status = "success"
    if not getattr(user, "bar_jurisdiction", None):
        from .. import solicitation as solic
        status, fallback = "warning", (
            f"bar_jurisdiction unset -- defaulting to "
            f"{solic._DEFAULT_JURISDICTION!r} (solicitation.py._DEFAULT_JURISDICTION)")
    return (status, trace.decision, {"policy_result": trace.policy_result}, fallback)


def _output(db, contact) -> tuple:
    signal = rt._latest_signal(db, contact.id)
    if signal is None:
        return ("warning", "no output produced yet", {}, "no drafted signal to report an outcome for")
    trace = adapters.outcome_trace(db, signal)
    note = None
    if trace.outcome and trace.outcome.get("disposition") is None:
        # Nothing was substituted here, so this was never a fallback: a draft
        # the lawyer has not yet sent or snoozed HAS no disposition, and the
        # stage reported that correctly. Warning about it made the expected
        # state of every fresh draft look like a malfunction.
        note = ("awaiting a send/snooze decision -- a disposition is recorded "
                "when the lawyer acts on this draft, and only then does it "
                "count as an observed outcome")
    return ("success", trace.decision, {"outcome": trace.outcome}, None, note)


# Ranking scores EVERY candidate independently, and each score costs several
# queries -- including one in _historical_behavior that reloads the lawyer's
# whole drafted-follow-up history. Against a real book of hundreds of
# contacts that is quadratic and takes minutes, which is exactly how a live
# account hung on "running execution pipeline" with nothing after it.
#
# The candidate set is therefore BOUNDED. This changes what the rank number
# means -- it is a rank within a sampled candidate set, not the whole book --
# so callers surface the bound rather than hiding it (see logstream's
# "ranking against N of M" line). The subject contact is always included, so
# its own factors and score are exact; only its ORDINAL is relative to the
# sample.
MAX_RANKING_CANDIDATES = 60


def resolve_candidates(db, user: models.User, contact: models.Contact,
                        candidates: list | None = None) -> tuple:
    """(bounded_candidates, total_available). Prefers contacts that carry a
    detected signal, since those are the ones a ranking is actually about."""
    if candidates is None:
        candidates = list(db.execute(
            select(models.Contact).where(models.Contact.user_id == user.id)
        ).scalars().all())
    total = len(candidates)
    if total > MAX_RANKING_CANDIDATES:
        # Which contacts carry a detected signal, in one query per chunk
        # instead of one per contact. This used to call rt._latest_signal per
        # contact, so simply deciding WHAT to rank cost a query per contact in
        # the book -- 650 of them on a real book, before any scoring started.
        #
        # Filtered on contact_id + source_type and nothing else, exactly as
        # _latest_signal does. Narrowing by actor_user_id would look
        # equivalent (these are all this user's contacts) but is not: it would
        # drop a signal recorded against the contact by anyone else, silently
        # changing which contacts are considered signal-carrying. Only
        # membership is needed here -- the signal rows themselves are loaded
        # by the ranking prefetch -- so this reads ids, not whole rows.
        ids = [c.id for c in candidates]
        signaled_ids: set = set()
        for i in range(0, len(ids), rt._ID_CHUNK):
            signaled_ids.update(db.execute(
                select(models.RelationshipInteraction.contact_id)
                .where(models.RelationshipInteraction.contact_id.in_(ids[i:i + rt._ID_CHUNK]),
                       models.RelationshipInteraction.source_type == "activity_update")
                .distinct()
            ).scalars().all())
        signaled = [c for c in candidates if c.id in signaled_ids]
        plain = [c for c in candidates if c.id not in signaled_ids]
        candidates = (signaled + plain)[:MAX_RANKING_CANDIDATES]
    if contact not in candidates:
        candidates = candidates + [contact]
    return candidates, total


def iter_stages(db, user: models.User, contact: models.Contact,
                 candidates: list | None = None, channel: str = "linkedin_dm"):
    """Yield each PipelineStage AS IT COMPLETES.

    compute_pipeline_trace() below consumes this, so the two can't drift.
    Streaming callers iterate it directly: running all eight stages and only
    then emitting them is what made a slow ranking stage look like a hang
    instead of like work in progress."""
    candidates, _total = resolve_candidates(db, user, contact, candidates)
    for name, version, fn in (
        ("ingestion", ver.INGESTION_VERSION, lambda: _ingestion(db, contact)),
        ("entity_resolution", ver.ENTITY_RESOLUTION_VERSION, lambda: _entity_resolution(db, contact)),
        ("signal_library", ver.SIGNAL_LIBRARY_VERSION, lambda: _signal_library(db, contact)),
        ("targeting", ver.FEATURE_SCHEMA_VERSION, lambda: _targeting(db, user, contact)),
        ("relationship", ver.RELATIONSHIP_FEATURE_VERSION, lambda: _relationship(db, user, contact)),
        ("ranking", ver.RANKER_VERSION, lambda: _ranking(db, user, contact, candidates)),
        ("jurisdiction", ver.JURISDICTION_POLICY_VERSION,
         lambda: _jurisdiction(db, user, contact, channel)),
        ("output", ver.FEATURE_SCHEMA_VERSION, lambda: _output(db, contact)),
    ):
        yield _run_stage(name, version, fn)


def compute_pipeline_trace(db, user: models.User, contact: models.Contact,
                            candidates: list | None = None, channel: str = "linkedin_dm") -> PipelineTrace:
    stages = list(iter_stages(db, user, contact, candidates, channel))

    provenance = DEMO if getattr(user, "is_demo", False) else OBSERVED
    return PipelineTrace(account_id=user.id, object_id=str(contact.id), timestamp=trace_now(),
                         stages=stages, provenance=provenance)
