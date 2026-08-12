"""backend/observe/harnesses/synthetic_scenarios.py : SYNTHETIC DATA HARNESS
(Observe spec section 10) -- structured, known-answer scenarios that test the
system's REASONING, not customer evidence. Every scenario has an
expected_behavior declared in code before anything runs; a case "passes" when
ranking_trace's actual output matches that pre-declared expectation.

Provenance is always SYNTHETIC (backend/demo/provenance.SYNTHETIC) -- never
conflated with demo-cohort-generated rows (BASELINE/EXTENDED) or real
observed data (see trace.py's PROVENANCE_VALUES and Observe spec section 11:
"Never silently mix them"). Rows are written through the SAME
tag/cohort_row_counts/delete_cohort machinery backend/demo/cohort.py already
uses, under a fixed cohort_id, so this harness leaves no untracked rows and
can be cleaned up with prov.delete_cohort(db, COHORT_ID) exactly like any
other cohort.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..harness import HarnessResult, now as harness_now
from ...demo import provenance as prov
from ...demo import ranking_trace as rt
from ... import models

_UTC = timezone.utc
COHORT_ID = "synthetic-scenarios-v1"

HARNESS_ID = "synthetic_scenarios"
HARNESS_NAME = "Synthetic Scenario Harness"
HARNESS_VERSION = "v1"


def _touch(db, user, contact, occurred_at, *, meta=None, source_type="manual_note",
           title="Note") -> models.RelationshipInteraction:
    row = models.RelationshipInteraction(
        actor_user_id=user.id, contact_id=contact.id, source_type=source_type,
        interaction_type="note", direction="none", occurred_at=occurred_at,
        title=title, summary="synthetic scenario fixture",
        meta_json=json.dumps(meta or {}),
    )
    db.add(row)
    db.flush()
    prov.tag(db, row, provenance=prov.SYNTHETIC, cohort_id=COHORT_ID)
    return row


def setup(db):
    """Builds ONE synthetic lawyer + 4 candidate contacts embodying the
    adversarial scenarios below. Callers who want a clean rebuild should
    prov.delete_cohort(db, COHORT_ID) first."""
    now = datetime.now(_UTC)
    user = models.User(email="synthetic-scenario-lawyer@example.com", name="Synthetic Lawyer",
                        is_demo=True, bar_jurisdiction="NY", practice_area="corporate_ma")
    db.add(user)
    db.flush()
    prov.tag(db, user, provenance=prov.SYNTHETIC, cohort_id=COHORT_ID)

    def make_contact(name: str) -> models.Contact:
        c = models.Contact(user_id=user.id, primary_identity_key=f"synthetic:{name}",
                            name=name, watched_at=now)
        db.add(c)
        db.flush()
        prov.tag(db, c, provenance=prov.SYNTHETIC, cohort_id=COHORT_ID)
        return c

    c1 = make_contact("Strong Relevant")     # strong relationship + relevant signal
    c2 = make_contact("Strong Irrelevant")   # strong relationship + irrelevant signal
    c3 = make_contact("Weak Relevant")       # weak relationship + relevant signal
    c4 = make_contact("Old Stale")           # high-volume but genuinely stale relationship

    engaged_meta = {"engaged": True, "disposition": "approved_as_is"}

    for i in range(14):
        _touch(db, user, c1, now - timedelta(days=13 - i))
    _touch(db, user, c1, now - timedelta(hours=6), source_type="activity_update",
           title="Drafted follow-up (synthetic)",
           meta={"signal_kind": "new_post", "signal_category": "acquisition_announced", **engaged_meta})

    for i in range(14):
        _touch(db, user, c2, now - timedelta(days=13 - i))
    # A DIFFERENT category from c1/c3's acquisition_announced, and one with a
    # low corporate_ma affinity in the seed table (0.20, vs
    # acquisition_announced's 0.95) -- otherwise this scenario's whole point
    # (irrelevant signal should drag the score down despite a strong
    # relationship) has no real gap to detect.
    _touch(db, user, c2, now - timedelta(hours=6), source_type="activity_update",
           title="Drafted follow-up (synthetic)",
           meta={"signal_kind": "new_post", "signal_category": "litigation_filed", **engaged_meta})

    _touch(db, user, c3, now - timedelta(days=20))
    _touch(db, user, c3, now - timedelta(hours=6), source_type="activity_update",
           title="Drafted follow-up (synthetic)",
           meta={"signal_kind": "new_post", "signal_category": "acquisition_announced", **engaged_meta})

    # Old Stale gets ONLY old touches, nothing in the last 30+ days -- no
    # drafted signal at all. Earlier drafts attached a "resolvable outcome"
    # touch dated `now` to every contact, which itself counted as the most
    # recent relationship interaction and made every contact look equally
    # fresh regardless of its actual history -- exactly the bug this
    # scenario exists to catch, so it must not repeat it on itself.
    for i in range(20):
        _touch(db, user, c4, now - timedelta(days=120 - i))

    db.commit()
    return user, [c1, c2, c3, c4]


def _load_existing(db):
    user_tag = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == COHORT_ID,
        models.DemoProvenance.table_name == "users").limit(1)).scalar_one_or_none()
    user = db.get(models.User, user_tag.row_id)
    contact_tags = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == COHORT_ID,
        models.DemoProvenance.table_name == "contacts")).scalars().all()
    contacts = [db.get(models.Contact, t.row_id) for t in contact_tags]
    contacts.sort(key=lambda c: c.id)
    return user, contacts


def run(db) -> HarnessResult:
    existing = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == COHORT_ID).limit(1)).scalar_one_or_none()
    user, contacts = _load_existing(db) if existing is not None else setup(db)

    ranked = rt.rank_opportunities(db, user, contacts)
    by_name = {t.contact_name: t for t in ranked}

    cases = []
    failures = []

    top = ranked[0]
    ok1 = top.contact_name == "Strong Relevant"
    cases.append({"scenario_id": "strong_relationship_relevant_signal",
                  "expected": "ranks #1",
                  "actual": f"#{top.rank} was {top.contact_name!r}", "passed": ok1})
    if not ok1:
        failures.append(cases[-1])

    irrelevant = by_name.get("Strong Irrelevant")
    practice_fit_val = (next((f.value for f in irrelevant.factors if f.name == "practice_fit"), None)
                         if irrelevant else None)
    ok2 = practice_fit_val is not None and practice_fit_val < 0.5
    cases.append({"scenario_id": "strong_relationship_irrelevant_signal",
                  "expected": "practice_fit < 0.5 despite strong relationship",
                  "actual": {"practice_fit": practice_fit_val}, "passed": ok2})
    if not ok2:
        failures.append(cases[-1])

    weak = by_name.get("Weak Relevant")
    ok3 = weak is not None and irrelevant is not None and weak.rank < irrelevant.rank
    cases.append({"scenario_id": "weak_relationship_high_practice_fit",
                  "expected": "ranks above the irrelevant-signal strong relationship",
                  "actual": {"weak_relevant_rank": weak.rank if weak else None,
                             "strong_irrelevant_rank": irrelevant.rank if irrelevant else None},
                  "passed": ok3})
    if not ok3:
        failures.append(cases[-1])

    stale = by_name.get("Old Stale")
    recency_val = (next((f.value for f in stale.factors if f.name == "relationship_recency"), None)
                   if stale else None)
    signal_val = (next((f.value for f in stale.factors if f.name == "signal_relevance"), None)
                  if stale else None)
    # NOT a trajectory check: trend=0 (no recent activity, no prior-recent
    # activity either) maps to the NEUTRAL midpoint 0.5 by
    # ranking_trace._relationship_trajectory's own formula -- "flat" isn't
    # "penalized". What actually reflects staleness here is recency (near 0,
    # last touch 100+ days ago) and the complete absence of a current signal.
    ok4 = (recency_val is not None and recency_val < 0.3
           and signal_val is not None and signal_val == 0.0)
    cases.append({"scenario_id": "old_relationship_vs_recent_alternative",
                  "expected": "recency near 0 and no detected signal, despite high historical volume",
                  "actual": {"relationship_recency": recency_val, "signal_relevance": signal_val},
                  "passed": ok4})
    if not ok4:
        failures.append(cases[-1])

    passed = sum(1 for c in cases if c["passed"])
    return HarnessResult(
        harness_id=HARNESS_ID, name=HARNESS_NAME, version=HARNESS_VERSION,
        run_timestamp=harness_now(),
        cases_total=len(cases), cases_passed=passed, cases_failed=len(cases) - passed,
        metrics={"cases": cases, "full_ranking": [
            {"contact_name": t.contact_name, "rank": t.rank, "score": round(t.opportunity_score, 4)}
            for t in ranked
        ]},
        failures=failures, provenance="synthetic",
    )
