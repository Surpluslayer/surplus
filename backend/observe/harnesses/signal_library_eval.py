"""backend/observe/harnesses/signal_library_eval.py : HARNESS #4 -- two
separate questions, kept separate (Observe spec section 7).

Part A (classification): "did we correctly identify the signal?" -- a small
hand-labeled fixture set run through a reference rule-based classifier. This
is NOT a trained model and does not claim to be one (section 15): production's
real, deterministic signal detection only distinguishes job_change/new_post
(backend/agents/relationship/updates_engine._DRAFTWORTHY_KINDS); the finer
categories below (gc_appointment, acquisition_announced, ...) come from
backend/demo/signal_taxonomy.py and have no production classifier at all yet.
Part A evaluates a REFERENCE classifier against fixtures so this harness's own
machinery is real and rerunnable -- not a claim about production
classification quality.

Part B (lawyer-conditioned utility): "was the signal actually useful to the
lawyer?" -- P(action | signal, lawyer), computed from real generated cohort
data, broken down by practice_area x signal_category. "Targeted" and
"detected" collapse to one number in the current data model: every generated
signal is immediately drafted against exactly one lawyer in the same row
(backend/demo/cohort.py's _emit_session) -- there's no separate
detected-but-not-yet-targeted state to report yet. Documented here rather
than inventing a funnel stage the data doesn't have.
"""
from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select

from .. import cohort_query as cq
from ..harness import HarnessResult, now as harness_now
from ... import models

HARNESS_ID = "signal_library_evaluation"
HARNESS_NAME = "Signal Library Evaluation Harness"
HARNESS_VERSION = "v1"


# ── Part A: reference classifier + labeled fixtures ─────────────────────────

def _reference_classify(event: dict) -> str:
    """A deterministic keyword/kind rule -- a REFERENCE classifier built to
    exercise this harness, not the production algorithm (see module
    docstring)."""
    kind = event.get("kind")
    title = (event.get("title") or "").lower()
    text = (event.get("text") or "").lower()
    if kind == "job_change":
        if "general counsel" in title:
            return "gc_appointment"
        if "founder" in title and "departure" in text:
            return "founder_departure"
        return "exec_appointment"
    if kind == "new_post":
        if "acqui" in text:
            return "acquisition_announced"
        if any(w in text for w in ("raised", "funding", "series")):
            return "funding_announcement"
        if any(w in text for w in ("lawsuit", "litigation")):
            return "litigation_filed"
        if any(w in text for w in ("regulat", "compliance", "investigation")):
            return "regulatory_action"
        if "launch" in text:
            return "product_launch"
    return "unclassified"


_FIXTURES = (
    ({"kind": "job_change", "title": "General Counsel"}, "gc_appointment"),
    ({"kind": "job_change", "title": "Chief Financial Officer"}, "exec_appointment"),
    ({"kind": "job_change", "title": "Co-Founder", "text": "announced their departure from the company"},
     "founder_departure"),
    ({"kind": "new_post", "text": "announced the acquisition of a competitor"}, "acquisition_announced"),
    ({"kind": "new_post", "text": "closed a $40M Series B funding round"}, "funding_announcement"),
    ({"kind": "new_post", "text": "named in a new litigation filing"}, "litigation_filed"),
    ({"kind": "new_post", "text": "confirmed it is under regulatory investigation"}, "regulatory_action"),
    ({"kind": "new_post", "text": "announced the launch of a new product line"}, "product_launch"),
)


def _classification_eval() -> dict:
    per_category = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    correct = 0
    for event, expected in _FIXTURES:
        predicted = _reference_classify(event)
        if predicted == expected:
            correct += 1
            per_category[expected]["tp"] += 1
        else:
            per_category[expected]["fn"] += 1
            per_category[predicted]["fp"] += 1

    per_category_metrics = {}
    for cat, c in per_category.items():
        precision = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else None
        recall = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else None
        f1 = (round(2 * precision * recall / (precision + recall), 4)
              if precision and recall and (precision + recall) else None)
        per_category_metrics[cat] = {
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": f1,
        }

    return {
        "n_fixtures": len(_FIXTURES),
        "correct": correct,
        "overall_accuracy": round(correct / len(_FIXTURES), 4),
        "per_category": per_category_metrics,
        "classifier": "reference_rule_based_v1 (NOT production -- see module docstring)",
    }


# ── Part B: lawyer-conditioned utility, from real cohort data ───────────────

def library_funnel(db, cohort_id: str) -> dict:
    """detected(=targeted) / engaged / sent counts per signal_category, from
    real generated draft rows -- see module docstring for why
    detected==targeted in the current data model."""
    pairs = cq.users_and_contacts(db, cohort_id)
    funnel = defaultdict(lambda: {"detected": 0, "engaged": 0, "sent": 0})
    for _user, contacts in pairs:
        contact_ids = [c.id for c in contacts]
        if not contact_ids:
            continue
        rows = db.execute(
            select(models.RelationshipInteraction)
            .where(models.RelationshipInteraction.contact_id.in_(contact_ids),
                   models.RelationshipInteraction.title.like("Drafted follow-up%"))
        ).scalars().all()
        for row in rows:
            meta = json.loads(row.meta_json or "{}")
            cat = meta.get("signal_category")
            if not cat:
                continue
            funnel[cat]["detected"] += 1
            if meta.get("engaged"):
                funnel[cat]["engaged"] += 1
            if meta.get("disposition") in ("approved_as_is", "edited_then_sent"):
                funnel[cat]["sent"] += 1
    return dict(funnel)


def lawyer_conditioned_performance(db, cohort_id: str) -> dict:
    """detected/engaged/sent + action_rate broken down by (practice_area,
    signal_category) -- P(action | signal, lawyer), not P(correct signal)."""
    pairs = cq.users_and_contacts(db, cohort_id)
    perf: dict = defaultdict(lambda: defaultdict(lambda: {"detected": 0, "engaged": 0, "sent": 0}))
    for user, contacts in pairs:
        pa = getattr(user, "practice_area", None) or "unknown"
        contact_ids = [c.id for c in contacts]
        if not contact_ids:
            continue
        rows = db.execute(
            select(models.RelationshipInteraction)
            .where(models.RelationshipInteraction.contact_id.in_(contact_ids),
                   models.RelationshipInteraction.title.like("Drafted follow-up%"))
        ).scalars().all()
        for row in rows:
            meta = json.loads(row.meta_json or "{}")
            cat = meta.get("signal_category")
            if not cat:
                continue
            bucket = perf[pa][cat]
            bucket["detected"] += 1
            if meta.get("engaged"):
                bucket["engaged"] += 1
            if meta.get("disposition") in ("approved_as_is", "edited_then_sent"):
                bucket["sent"] += 1

    out: dict = {}
    for pa, cats in perf.items():
        out[pa] = {}
        for cat, bucket in cats.items():
            action_rate = bucket["engaged"] / bucket["detected"] if bucket["detected"] else None
            out[pa][cat] = {**bucket,
                             "action_rate": round(action_rate, 4) if action_rate is not None else None}
    return out


def run(db, cohort_id: str) -> HarnessResult:
    classification = _classification_eval()
    funnel = library_funnel(db, cohort_id)
    conditioned = lawyer_conditioned_performance(db, cohort_id)

    cases_total = classification["n_fixtures"]
    cases_passed = classification["correct"]

    return HarnessResult(
        harness_id=HARNESS_ID, name=HARNESS_NAME, version=HARNESS_VERSION,
        run_timestamp=harness_now(),
        cases_total=cases_total, cases_passed=cases_passed,
        cases_failed=cases_total - cases_passed,
        metrics={
            "classification": classification,
            "library_funnel_by_category": funnel,
            "lawyer_conditioned_performance": conditioned,
        },
        failures=[], provenance="demo",
    )
