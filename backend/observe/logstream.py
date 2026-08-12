"""backend/observe/logstream.py : Observe's execution log.

Every line this module emits corresponds to REAL work that just ran -- a
harness that actually executed, a pipeline stage that actually computed, a
jurisdiction rule that was actually checked against real values. Nothing
here is decorative filler printed to look busy: if a line says a step took
12.4ms, that is a measured `time.perf_counter()` delta around the real
call, and if it prints a score, that score came back from the real scorer.

`src` on each line is the actual module:function that produced it, so a
reader can go straight to the code that ran.

Emits plain dicts; routes/observe.py wraps them as SSE frames.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

_UTC = timezone.utc

# Log levels, mapped to how the UI colors a line.
INFO, OK, WARN, ERR, STEP = "info", "ok", "warn", "error", "step"


def _now_iso() -> str:
    return datetime.now(_UTC).isoformat()


def line(level: str, src: str, msg: str, **extra) -> dict:
    return {"ts": _now_iso(), "level": level, "src": src, "msg": msg, **extra}


def _timed(fn):
    t0 = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t0) * 1000.0


def _is_missing_table(exc: Exception) -> bool:
    """True when `exc` is specifically a "relation/table does not exist" error.

    Matched on the driver's message rather than an exception class because the
    two backends disagree: SQLite raises OperationalError("no such table: x")
    and Postgres raises ProgrammingError wrapping UndefinedTable. Both surface
    as sqlalchemy.exc.DatabaseError, so the class alone can't distinguish a
    not-yet-created table from a genuinely broken query.
    """
    msg = str(getattr(exc, "orig", exc)).lower()
    return "no such table" in msg or "does not exist" in msg or "undefinedtable" in msg


# ── boot: run every harness, one line per real execution ─────────────────

# (harness_id, needs_evaluation_dataset, counts_are_pass_fail)
#
# The last flag matters for honest reporting. Most harnesses use
# cases_passed/cases_total as genuine pass/fail. ablation and
# relationship_evaluation do NOT: there, cases_total is "lawyers considered"
# and cases_passed is "lawyers that had a resolved outcome to evaluate
# against" -- so 9/10 means one lawyer's data wasn't evaluable, not that a
# case failed. Rendering that as a red FAILED line (as an earlier version
# did) reports a healthy run as broken.
#
# synthetic_scenarios is NOT in this boot list -- it's a correctness check
# against four hand-built known-answer fixtures (see evaluation_cohort_id's
# own docstring on why those must never become the evaluation dataset), not
# a read of this account's real state, so it doesn't belong in "what has
# Surplus actually been doing" the way the other five do. Still runnable
# on demand via GET /api/observe/harness/synthetic_scenarios.
_HARNESS_ORDER = (
    ("jurisdiction_regression", False, True),
    ("historical_replay", True, True),
    ("ablation", True, False),
    ("relationship_evaluation", True, False),
    ("signal_library_evaluation", True, True),
)


def evaluation_cohort_id(db):
    """The cohort the data-driven harnesses should evaluate against.

    Restricted to prov.EVALUATION_VALUES on purpose. Picking the plain newest
    tag instead (as an earlier version did) selected
    `synthetic-scenarios-v1` -- the four hand-built known-answer fixtures
    that synthetic_scenarios.setup() writes lazily DURING the harness run,
    which makes them the newest rows in the table by construction. The
    aggregate harnesses then reported real arithmetic over a 4-contact
    fixture set instead of the generated cohort, so replay/ablation/
    relationship numbers came back as "3/3" and "1/1" and looked broken.
    Synthetic fixtures are a correctness check, never the evaluation
    dataset.

    The set (rather than BASELINE alone) is what lets an OBSERVED cohort --
    a real account's own data, referenced in by backend/demo/seed_operator.py
    -- actually become the evaluation dataset. Matching only BASELINE would
    silently ignore it, which is the failure mode this whole predicate exists
    to prevent."""
    from sqlalchemy import select
    from .. import models
    from ..demo import provenance as prov
    return db.execute(
        select(models.DemoProvenance.cohort_id)
        .where(models.DemoProvenance.data_provenance.in_(tuple(prov.EVALUATION_VALUES)))
        .order_by(models.DemoProvenance.id.desc()).limit(1)
    ).scalar_one_or_none()


def boot_events(db, user=None):
    """On page load: report the live product pipeline first, then run all five
    account-state harnesses for real, streaming a line per execution.
    synthetic_scenarios is deliberately excluded -- see _HARNESS_ORDER's own
    comment; it's still reachable on demand via GET /api/observe/harness/
    synthetic_scenarios.

    Order is deliberate. "What has the system actually been doing" (the
    updates pipeline that produced the feed on screen) comes before "does it
    hold up under evaluation" (the harnesses) -- reading the harness results
    first invites treating them as the whole story, when they are the
    evaluation OF the pipeline above them."""
    from .harnesses import (ablation, jurisdiction_regression, relationship_eval,
                             signal_library_eval)
    from .harnesses import replay as replay_harness
    from . import versions as ver

    yield line(INFO, "backend.observe.logstream:boot_events", "Surplus Observe starting")
    yield line(INFO, "backend.observe.versions",
               " · ".join(f"{k}={v}" for k, v in ver.VERSIONS.items()))

    for ev in updates_pipeline_events(db, user):
        yield ev

    cohort_id = evaluation_cohort_id(db)
    if cohort_id:
        yield line(INFO, "backend.observe.logstream:evaluation_cohort_id",
                   f"evaluation dataset resolved: cohort_id={cohort_id}")
    else:
        yield line(WARN, "backend.observe.logstream:evaluation_cohort_id",
                   "no demo cohort in this database -- the four data-driven harnesses "
                   "have nothing to evaluate against (run: python -m backend.demo.cohort)")

    runners = {
        "jurisdiction_regression": lambda: jurisdiction_regression.run(),
        "historical_replay": lambda: replay_harness.run(db, cohort_id),
        "ablation": lambda: ablation.run(db, cohort_id),
        "relationship_evaluation": lambda: relationship_eval.run(db, cohort_id),
        "signal_library_evaluation": lambda: signal_library_eval.run(db, cohort_id),
    }
    modules = {
        "jurisdiction_regression": "backend.observe.harnesses.jurisdiction_regression:run",
        "historical_replay": "backend.observe.harnesses.replay:run",
        "ablation": "backend.observe.harnesses.ablation:run",
        "relationship_evaluation": "backend.observe.harnesses.relationship_eval:run",
        "signal_library_evaluation": "backend.observe.harnesses.signal_library_eval:run",
    }

    total_pass = total_cases = 0
    for harness_id, needs_cohort, pass_fail in _HARNESS_ORDER:
        src = modules[harness_id]
        if needs_cohort and not cohort_id:
            yield line(WARN, src, f"{harness_id}: SKIPPED (needs an evaluation dataset)",
                       harness=harness_id)
            continue
        yield line(STEP, src, f"running {harness_id}", harness=harness_id)
        try:
            result, ms = _timed(runners[harness_id])
        except Exception as exc:  # noqa: BLE001
            yield line(ERR, src, f"{harness_id}: FAILED {type(exc).__name__}: {exc}",
                       harness=harness_id)
            continue

        if pass_fail:
            total_pass += result.cases_passed
            total_cases += result.cases_total
            lvl = OK if result.cases_failed == 0 else ERR
            yield line(lvl, src,
                       f"{harness_id}: {result.cases_passed}/{result.cases_total} passed "
                       f"in {ms:.1f}ms",
                       harness=harness_id, passed=result.cases_passed,
                       total=result.cases_total, kind="pass_fail")
        else:
            yield line(OK, src,
                       f"{harness_id}: evaluated {result.cases_passed}/{result.cases_total} "
                       f"lawyers with a resolved outcome in {ms:.1f}ms",
                       harness=harness_id, passed=result.cases_passed,
                       total=result.cases_total, kind="coverage")

        for detail in _harness_detail_lines(harness_id, result):
            yield detail

        for f in (result.failures or [])[:5]:
            yield line(ERR, src, f"  failure: {f.get('scenario_id') or f}", harness=harness_id)

    yield line(OK if total_pass == total_cases else WARN,
               "backend.observe.logstream:boot_events",
               f"harness suite complete -- {total_pass}/{total_cases} pass/fail cases passed")


def updates_pipeline_events(db, user=None):
    """Where the Updates feed actually comes from -- the detect → target →
    draft flow, reported from real state.

    The harness suite answers "does the intelligence hold up under
    evaluation". It does NOT answer "what has the product actually been
    doing", which is the question a feed of N updates raises. This reads the
    real pipeline state: the sweep's configured cadence, when it last
    claimed a run (scheduler_claims -- a real row, not an assumption), how
    many contacts are due for a re-check right now, the
    detected → drafted → resolved funnel, and the most recent signals with
    the actual elapsed time between detection and drafting.

    It deliberately does NOT trigger a sweep. A page load must not fire
    BrightData/Exa fetches or LLM autodrafts -- that would spend money and
    mutate data as a side effect of opening a debugger. Everything here is a
    read of work that already happened, and cached//unchanged rows are
    reported as such rather than re-derived."""
    import json
    from sqlalchemy import func, select, text
    from .. import models
    from ..agents.relationship import updates_engine as ue
    from ..agents.relationship import updates_scheduler as us

    src_sched = "backend.agents.relationship.updates_scheduler"
    yield line(STEP, "backend.observe.logstream:updates_pipeline_events",
               "updates pipeline -- detect → target → draft")

    yield line(INFO, f"{src_sched}:_gap_seconds",
               f"  cadence: sweep every {us._gap_seconds()}s · tick {us._tick_seconds()}s · "
               f"batch limit {us._limit()} · re-check vip every {ue._VIP_DAYS}d, "
               f"standard every {ue._STD_DAYS}d · enabled={us._enabled()}")

    # When each sweep last actually ran -- a real row, not an inference.
    #
    # Two failure modes are distinct and must not be collapsed: the table is
    # created lazily by the first sweep, so its ABSENCE really does mean "no
    # sweep has run here". Any other error means the query itself broke, and
    # reporting that as "no sweep has run" would state something false in a
    # panel whose whole point is that its lines are checkable.
    try:
        rows = db.execute(text(
            "SELECT name, last_run_at FROM scheduler_claims ORDER BY name")).all()
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            yield line(WARN, f"{src_sched}:_ensure_claim_table",
                       "  scheduler_claims table does not exist yet -- it is created "
                       "by the first sweep, so no sweep has ever run in this database")
        else:
            yield line(ERR, f"{src_sched}:_claim",
                       f"  could not read scheduler_claims ({type(exc).__name__}: "
                       f"{str(exc)[:120]}) -- sweep cadence below is UNVERIFIED")
        rows = None

    if rows is not None:
        if not rows:
            yield line(WARN, f"{src_sched}:_claim",
                       "  scheduler_claims exists but is empty -- the table was created "
                       "and no sweep has claimed a run since")
        now_s = time.time()
        for name, last_run_at in rows or ():
            if not last_run_at:
                yield line(WARN, f"{src_sched}:_claim", f"  {name}: never run")
                continue
            age = now_s - float(last_run_at)
            yield line(INFO, f"{src_sched}:_claim",
                       f"  {name}: last ran {_ago(age)} ago")

    # The funnel, scoped to this account when we have one.
    cq = select(func.count(models.Contact.id))
    iq = select(func.count(models.RelationshipInteraction.id)).where(
        models.RelationshipInteraction.source_type == "activity_update")
    if user is not None:
        cq = cq.where(models.Contact.user_id == user.id)
        iq = iq.where(models.RelationshipInteraction.actor_user_id == user.id)
    n_contacts = db.execute(cq).scalar_one()
    n_signals = db.execute(iq).scalar_one()

    due = ue.due_contacts(db, user_id=(user.id if user is not None else None), limit=10_000)
    yield line(INFO, "backend.agents.relationship.updates_engine:due_contacts",
               f"  book: {n_contacts} contacts · {len(due)} due for re-check now · "
               f"{n_contacts - len(due)} still inside their tier window (cached, not re-fetched)")

    # detected → drafted → resolved, from the rows themselves.
    sig_q = select(models.RelationshipInteraction).where(
        models.RelationshipInteraction.source_type == "activity_update")
    if user is not None:
        sig_q = sig_q.where(models.RelationshipInteraction.actor_user_id == user.id)
    sigs = db.execute(sig_q.order_by(
        models.RelationshipInteraction.occurred_at.desc()).limit(400)).scalars().all()

    drafted = resolved = 0
    by_kind: dict = {}
    for s in sigs:
        try:
            meta = json.loads(s.meta_json or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        kind = meta.get("signal_kind") or s.interaction_type or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if meta.get("draft"):
            drafted += 1
        if meta.get("disposition"):
            resolved += 1

    yield line(INFO, "backend.observe.logstream:updates_pipeline_events",
               f"  detected: {n_signals} signals (inspecting newest {len(sigs)}) → "
               f"{drafted} carry a stored autodraft → {resolved} have a recorded outcome")
    if by_kind:
        yield line(INFO, "backend.agents.relationship.updates_engine:_DRAFTWORTHY_KINDS",
                   "  by kind: " + " · ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
                   + f"  (draftworthy: {sorted(ue._DRAFTWORTHY_KINDS)})")

    # The newest few, with the REAL detect→draft gap per signal.
    for s in sigs[:5]:
        try:
            meta = json.loads(s.meta_json or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        contact = db.get(models.Contact, s.contact_id) if s.contact_id else None
        who = (contact.name if contact else None) or f"contact {s.contact_id}"
        age = _ago((datetime.now(_UTC) - _aware_dt(s.occurred_at)).total_seconds())
        bits = [f"detected {age} ago", f"kind={meta.get('signal_kind') or s.interaction_type}"]
        if meta.get("signal_category"):
            bits.append(f"category={meta['signal_category']}")
        if meta.get("draft"):
            bits.append(f"drafted {len(meta['draft'])} chars")
        else:
            bits.append("no draft")
        if meta.get("disposition"):
            bits.append(f"outcome={meta['disposition']}")
        yield line(OK if meta.get("draft") else INFO,
                   "backend.agents.relationship.relationship_watch:_emit",
                   f"    #{s.id} {who[:26]:<26} " + " · ".join(bits))

    # In-process caches: what a page load reuses instead of recomputing.
    try:
        from ..agents.relationship import book as book_agent
        yield line(INFO, "backend.agents.relationship.book",
                   f"  caches: assess {len(book_agent._assess_cache)} entries · "
                   f"draft {len(book_agent._draft_cache)} entries "
                   f"(TTL {book_agent._ASSESS_TTL // 3600}h, per-process) -- a reload reuses "
                   f"these instead of re-scoring or re-drafting")
    except Exception:  # noqa: BLE001
        pass


def _aware_dt(dt):
    if dt is None:
        return datetime.now(_UTC)
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _harness_detail_lines(harness_id: str, result):
    """The two or three numbers per harness that actually carry the claim --
    pulled from the real HarnessResult.metrics, not restated prose."""
    m = result.metrics or {}
    src = f"backend.observe.harnesses.{harness_id}"

    if harness_id == "historical_replay":
        yield line(INFO, src,
                   f"  temporal_leakage_violations={m.get('temporal_leakage_violations')} "
                   f"· reconstruction_failures={m.get('reconstruction_failures')} "
                   f"· prediction/outcome agreement={m.get('prediction_outcome_agreement_rate')}")
    elif harness_id == "ablation":
        a = m.get("A_signal_practice") or {}
        c = m.get("C_plus_relationship") or {}
        yield line(INFO, src,
                   f"  ndcg@5  signal+practice={a.get('ndcg_at_k')} "
                   f"→ +behavior+relationship={c.get('ndcg_at_k')} "
                   f"· mean rank delta={m.get('mean_rank_delta_A_to_C')}")
    elif harness_id == "relationship_evaluation":
        if m.get("sufficient_data_for_aggregate_claim"):
            yield line(INFO, src,
                       f"  relationship NDCG@5 lift vs baseline="
                       f"{m.get('ndcg_lift_relationship_vs_baseline')} over {m.get('n_cases_total')} cases")
        else:
            yield line(WARN, src,
                       f"  only {m.get('n_cases_total')} resolved cases -- below the "
                       f"threshold to claim an aggregate lift; "
                       f"relationship-sensitive cases={m.get('relationship_sensitive_cases')}, "
                       f"aligned with observed action={m.get('cases_where_change_aligned_with_observed_action')}")
    elif harness_id == "signal_library_evaluation":
        cls = m.get("classification") or {}
        yield line(INFO, src,
                   f"  classifier accuracy={cls.get('overall_accuracy')} "
                   f"over {cls.get('n_fixtures')} labeled fixtures ({cls.get('classifier')})")
    elif harness_id == "jurisdiction_regression":
        by_cat = m.get("by_category") or {}
        yield line(INFO, src,
                   f"  rule_version={m.get('rule_version')} · categories: "
                   + ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in by_cat.items()))
    elif harness_id == "synthetic_scenarios":
        for case in (m.get("cases") or []):
            lvl = OK if case.get("passed") else ERR
            yield line(lvl, src, f"  {case.get('scenario_id')}: "
                                  f"expected {case.get('expected')}")


# ── per-contact: the agent's actual run, stage by stage ──────────────────

def contact_events(db, user, contact, channel: str = "linkedin_dm"):
    """Stream the real computation for one contact: every pipeline stage,
    the ranking factors and how they combine into the score, then the
    jurisdiction rule set applied line by line to the real drafted message."""
    from . import adapters, pipeline
    from .harnesses import ablation
    from ..demo import ranking_trace as rt

    yield line(INFO, "backend.observe.logstream:contact_events",
               f"trace requested · contact_id={contact.id} name={contact.name!r} "
               f"account={user.email}")

    # ── pipeline: emit each stage the moment it finishes ──
    candidates, total_available = pipeline.resolve_candidates(db, user, contact, None)
    if total_available > len(candidates):
        yield line(WARN, "backend.observe.pipeline:resolve_candidates",
                   f"ranking against {len(candidates)} of {total_available} contacts "
                   f"(capped at {pipeline.MAX_RANKING_CANDIDATES}, signal-carrying first) -- "
                   f"scoring every contact costs several queries each, so rank here is an "
                   f"ordinal within this sample; this contact's own factors and score are exact")
    else:
        yield line(INFO, "backend.observe.pipeline:resolve_candidates",
                   f"ranking against all {len(candidates)} contacts in this book")

    yield line(STEP, "backend.observe.pipeline:iter_stages", "running execution pipeline")
    t_pipe = time.perf_counter()
    statuses = []
    for s in pipeline.iter_stages(db, user, contact, candidates, channel):
        statuses.append(s.status)
        lvl = {"success": OK, "warning": WARN, "failure": ERR}.get(s.status, INFO)
        yield line(lvl, f"backend.observe.pipeline:_{s.name}",
                   f"{s.name}: {s.decision} ({s.latency_ms:.2f}ms, {s.version})")
        if s.fallback:
            yield line(WARN, f"backend.observe.pipeline:_{s.name}", f"  fallback: {s.fallback}")
        if s.error:
            yield line(ERR, f"backend.observe.pipeline:_{s.name}", f"  error: {s.error}")
    overall = ("failure" if "failure" in statuses
               else "warning" if "warning" in statuses else "success")
    yield line(INFO, "backend.observe.pipeline:iter_stages",
               f"pipeline complete: {overall} in {(time.perf_counter() - t_pipe) * 1000:.1f}ms")

    # ── ranking: show the arithmetic, not just the result ──
    yield line(STEP, "backend.demo.ranking_trace:compute_trace",
               "scoring opportunity -- factor × weight breakdown")
    rtrace, rms = _timed(lambda: rt.compute_trace(db, user, contact))
    running = 0.0
    for f in rtrace.factors:
        w = rt.FACTOR_WEIGHTS[f.name]
        contribution = f.value * w
        running += contribution
        ev = ", ".join(f"{k}={v}" for k, v in (f.evidence or {}).items() if v is not None)
        yield line(INFO, f"backend.demo.ranking_trace:_{f.name}",
                   f"  {f.name:<24} {f.value:.3f} × {w:.2f} = {contribution:.4f}"
                   + (f"   [{ev}]" if ev else ""))
    yield line(OK, "backend.demo.ranking_trace:compute_trace",
               f"opportunity_score = {running:.4f}  (sum of weighted factors, {rms:.1f}ms)")

    # ── ablation: a real re-ranking loop, one full pass per feature group ──
    # Reuses the SAME bounded candidate set the ranking stage used, so the
    # before/after ranks are comparable and the loop stays bounded.
    groups = ("relationship", "behavior", "signal_affinity", "timing")
    if len(candidates) > 1:
        # Every lever re-ranks the SAME candidates from the SAME rows, so the
        # data is loaded once for the whole loop. The scoring arithmetic still
        # runs in full for each pass -- only the repeated fetching is removed.
        abl_data, pf_ms = _timed(lambda: rt.prefetch(db, user, candidates))
        yield line(STEP, "backend.observe.harnesses.ablation:ablate_one",
                   f"ablation loop: {len(groups)} iterations, each re-ranking all "
                   f"{len(candidates)} candidates from scratch "
                   f"({len(groups) * len(candidates) * 2} scoring passes total, "
                   f"one bulk load of {len(candidates)} candidates in {pf_ms:.0f}ms "
                   f"shared across them)")
        for i, group in enumerate(groups, start=1):
            try:
                res, ams = _timed(
                    lambda g=group: ablation.ablate_one(db, user, contact, candidates, g,
                                                        data=abl_data))
            except Exception as exc:  # noqa: BLE001
                yield line(ERR, "backend.observe.harnesses.ablation:ablate_one",
                           f"  [{i}/{len(groups)}] {group}: {type(exc).__name__}: {exc}")
                continue
            delta = res["rank_delta"]
            yield line(OK if delta else INFO, "backend.observe.harnesses.ablation:ablate_one",
                       f"  [{i}/{len(groups)}] without {group:<16} rank "
                       f"#{res['full_system']['rank']} → #{res['without_group']['rank']}  "
                       f"(score {res['full_system']['score']} → {res['without_group']['score']}, "
                       f"{delta:+d} positions, {ams:.0f}ms)")

    # ── jurisdiction: the real rule set, check by check, on the real draft ──
    for ev in jurisdiction_events(db, user, contact, channel):
        yield ev

    for ev in feedback_loop_status(db, user):
        yield ev

    yield line(OK, "backend.observe.logstream:contact_events", "trace complete")


def feedback_loop_status(db, user):
    """State plainly which parts of the outcome→ranking loop are actually
    wired, because "the system learns from outcomes" is the single easiest
    thing to imply and not have.

    What is closed today: outcomes are RECORDED (draft dispositions land in
    meta_json) and READ back by ranking_trace._historical_behavior, so a
    lawyer's own past engagement per signal category does move their next
    score. That is a real, live loop and the log says so.

    What is NOT closed: signal_taxonomy.affinity() still returns SEED values.
    update_affinity_from_outcomes() exists and is tested, but nothing in
    production calls it -- verified by grep, not assumed. So the cross-lawyer
    taxonomy does not improve with usage yet, and any claim that it does
    would be false."""
    import json
    from sqlalchemy import select
    from .. import models
    from ..demo import signal_taxonomy as tax

    src = "backend.demo.signal_taxonomy"
    yield line(STEP, "backend.observe.logstream:feedback_loop_status",
               "outcome → ranking loop status")

    rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.actor_user_id == user.id,
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
    ).scalars().all()
    resolved = 0
    for r in rows:
        try:
            if json.loads(r.meta_json or "{}").get("disposition"):
                resolved += 1
        except Exception:  # noqa: BLE001
            pass

    yield line(OK if resolved else WARN, "backend.demo.ranking_trace:_historical_behavior",
               f"  CLOSED: {resolved} resolved outcomes for this lawyer feed "
               f"historical_behavior on their next score")
    yield line(WARN, src,
               "  OPEN:   signal_taxonomy.affinity() returns SEED priors -- "
               "update_affinity_from_outcomes() is implemented and tested but is not "
               "called anywhere in production, so the cross-lawyer taxonomy does not "
               "yet improve with usage")
    yield line(INFO, src,
               f"  affinity table version={tax.__name__} seed · "
               f"{len(tax.SIGNAL_AFFINITY_SEED)} practice areas × "
               f"{len(tax.ALL_SIGNAL_CATEGORIES)} signal categories")


def _stored_draft(db, contact) -> tuple:
    """The pre-written follow-up production's updates_engine.autodraft()
    stashes on the detected-signal interaction's meta_json. There is no
    drafts table -- that key IS the store."""
    import json
    from sqlalchemy import select
    from .. import models
    rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact.id,
               models.RelationshipInteraction.source_type == "activity_update")
        .order_by(models.RelationshipInteraction.occurred_at.desc()).limit(5)
    ).scalars().all()
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:  # noqa: BLE001
            continue
        if meta.get("draft"):
            return meta["draft"], r.id
    return None, None


# How often to emit a progress line while tokens stream in. Every delta would
# be hundreds of lines for one short message; this keeps the log readable
# while still moving continuously, the way a build step's output does.
_STREAM_PROGRESS_EVERY_CHARS = 60


def draft_events(db, user, contact, out: dict, channel: str = "linkedin_dm"):
    """Resolve the drafted message, STREAMING what the composer actually does.

    Drafting is the slowest and least deterministic thing in this trace -- an
    LLM call in the host's voice -- and it was previously invisible: the log
    jumped from the ranking straight to a finished draft, so the one part
    genuinely working in real time was the one part with no output. Now each
    real step reports itself: which source resolved the draft, whether a model
    key exists at all, which model, when the first token landed, and progress
    as the body materializes.

    Writes the result into `out` ("text", "source", "interaction_id") because
    a generator's return value isn't reachable from a `yield from`.
    """
    stored, iid = _stored_draft(db, contact)
    if stored:
        yield line(OK, "backend.agents.relationship.updates_engine:autodraft",
                   f"draft: reusing stored autodraft ({len(stored)} chars, "
                   f"interaction_id={iid}) -- no model call needed")
        out.update(text=stored, source="meta_json.draft (updates_engine.autodraft)",
                   interaction_id=iid)
        return

    from ..agents.relationship.book import _anthropic_available
    if not _anthropic_available():
        yield line(WARN, "backend.agents.relationship.book:_anthropic_available",
                   "ANTHROPIC_API_KEY is not set -- no model call is possible in this "
                   "environment; falling back to the deterministic heuristic drafter")
    else:
        from ..agents import llm
        from ..agents.relationship.pipeline.compose import drafting
        yield line(STEP, "backend.agents.relationship.pipeline.compose.drafting:compose_stream",
                   f"composing draft live · model={llm.MODEL} · channel={channel} "
                   f"· streaming token-by-token")
        t0 = time.perf_counter()
        first_ms = None
        chunks = 0
        buf = []
        emitted_at = 0
        try:
            for delta in drafting.compose_stream(db, user.id, contact,
                                                  reason="catching up", channel=channel):
                if first_ms is None:
                    first_ms = (time.perf_counter() - t0) * 1000.0
                    yield line(INFO, "backend.agents.relationship.book:stream_text",
                               f"  first token after {first_ms:.0f}ms")
                chunks += 1
                buf.append(delta)
                n = sum(len(b) for b in buf)
                if n - emitted_at >= _STREAM_PROGRESS_EVERY_CHARS:
                    emitted_at = n
                    yield line(INFO, "backend.agents.relationship.book:stream_text",
                               f"  streaming… {n} chars / {chunks} deltas")
            body = "".join(buf).strip()
        except Exception as exc:  # noqa: BLE001 -- streaming is best-effort
            body = ""
            yield line(ERR, "backend.agents.relationship.book:stream_text",
                       f"  stream failed: {type(exc).__name__}: {exc}")

        total_ms = (time.perf_counter() - t0) * 1000.0
        if body:
            yield line(OK, "backend.agents.relationship.pipeline.compose.drafting:compose_stream",
                       f"draft composed: {len(body)} chars in {total_ms:.0f}ms "
                       f"({chunks} deltas"
                       + (f", first token {first_ms:.0f}ms)" if first_ms else ")"))
            out.update(text=body, source=f"drafting:compose_stream (live, {llm.MODEL})",
                       interaction_id=None)
            return
        yield line(WARN, "backend.agents.relationship.pipeline.compose.drafting:compose_stream",
                   f"model returned nothing after {total_ms:.0f}ms -- falling back to the "
                   "deterministic heuristic drafter")

    # Same last resort routes/book.py's /draft endpoint uses when the shared
    # composer returns nothing (no key, or a miss): the heuristic drafter.
    try:
        from ..agents.relationship import book as book_agent
        from ..routes.book import _find_contact_fast
        t0 = time.perf_counter()
        cdict = _find_contact_fast(db, user, str(contact.id))
        if cdict:
            msg = book_agent.draft_message_cached(
                cdict, "catching up", channel=channel,
                user_name=(getattr(user, "name", None) or "").split(" ")[0] or "there")
            if msg and msg.get("body"):
                yield line(OK, "backend.agents.relationship.book:draft_message_cached",
                           f"draft: heuristic drafter produced {len(msg['body'])} chars "
                           f"in {(time.perf_counter() - t0) * 1000:.0f}ms (no model call)")
                out.update(text=msg["body"],
                           source="book:draft_message_cached (heuristic, deterministic)",
                           interaction_id=None)
                return
    except Exception as exc:  # noqa: BLE001
        yield line(ERR, "backend.agents.relationship.book:draft_message_cached",
                   f"heuristic drafter failed: {type(exc).__name__}: {exc}")
    out.update(text=None, source=None, interaction_id=None)


def jurisdiction_events(db, user, contact, channel: str = "linkedin_dm"):
    """Walk backend/solicitation.py's real rule set over this account's real
    jurisdiction and this contact's real drafted message.

    The per-check lines below mirror `solicitation.evaluate()`'s documented
    precedence (exemption → real-time channel → sensitive-matter cooldown →
    volume cap) using the SAME rule table and the SAME resolved context, and
    the authoritative verdict at the end comes from calling the real
    `evaluate()` -- the trace explains the decision, it never replaces it.
    """
    from ..agents.relationship import solicitation_signals as sig
    from ..solicitation import (JURISDICTION_RULES, _EXEMPT_RELATIONSHIP_TYPES,
                                _UNKNOWN_JURISDICTION_RULE, evaluate)

    src = "backend.solicitation:evaluate"
    yield line(STEP, "backend.agents.relationship.solicitation_signals:resolve_context",
               "resolving jurisdiction context from account + contact")

    ctx = sig.resolve_context(db, user, contact, channel)
    raw_jur = (getattr(user, "bar_jurisdiction", None) or "").strip()
    rule = JURISDICTION_RULES.get(ctx.jurisdiction.upper(), _UNKNOWN_JURISDICTION_RULE)

    if raw_jur:
        yield line(INFO, src, f"jurisdiction: {rule.state} (users.bar_jurisdiction={raw_jur!r})")
    else:
        yield line(WARN, src,
                   "jurisdiction: users.bar_jurisdiction is NOT SET on this account -- "
                   "falling back to the fail-closed unknown-jurisdiction rule")
    yield line(INFO, src,
               f"rule set: realtime_barred={sorted(rule.prohibited_realtime_channels)} "
               f"· disclosure_required={rule.requires_disclosure_label}"
               + (f" ({rule.disclosure_text!r})" if rule.disclosure_text else "")
               + f" · cooldown_days={rule.sensitive_matter_cooldown_days}"
               f" · volume_cap={rule.max_solicitations_per_window}")
    if rule.citation:
        yield line(INFO, src, f"modeled on: {rule.citation}")
    else:
        yield line(WARN, src,
                   f"NO CITATION recorded for the {rule.state} entry -- these values are "
                   "illustrative and were never verified against current bar text. "
                   "Not usable as a compliance record until counsel fills this in "
                   "(backend/solicitation.py JurisdictionRule.citation)")
    yield line(INFO, src,
               f"context: channel={ctx.channel!r} relationship_type={ctx.relationship_type.value!r} "
               f"recent_solicitations_in_window={ctx.recent_solicitation_count_in_window}")

    # check 1 -- exemption
    exempt = ctx.relationship_type in _EXEMPT_RELATIONSHIP_TYPES
    yield line(OK if exempt else INFO, src,
               f"  [1/4] exemption          relationship_type={ctx.relationship_type.value} → "
               + ("EXEMPT (rule 7.3 solicitation limits do not apply)" if exempt else "not exempt, continue"))

    if not exempt:
        # check 2 -- real-time channel
        barred = ctx.channel in rule.prohibited_realtime_channels
        yield line(ERR if barred else OK, src,
                   f"  [2/4] realtime channel   {ctx.channel!r} in barred set → "
                   + ("BLOCK" if barred else "permitted (not a real-time channel)"))

        # check 3 -- sensitive-matter cooldown
        if rule.sensitive_matter_cooldown_days > 0:
            yield line(INFO, src,
                       f"  [3/4] cooldown           {rule.state} requires "
                       f"{rule.sensitive_matter_cooldown_days}d; matter_is_sensitive="
                       f"{ctx.matter_is_sensitive}")
        else:
            yield line(OK, src,
                       f"  [3/4] cooldown           {rule.state} configures no sensitive-matter "
                       f"cooldown → n/a")

        # check 4 -- volume cap
        if rule.max_solicitations_per_window is None:
            yield line(OK, src,
                       f"  [4/4] volume cap         {rule.state} configures no cap → n/a")
        else:
            cap, window = rule.max_solicitations_per_window
            over = ctx.recent_solicitation_count_in_window >= cap
            yield line(ERR if over else OK, src,
                       f"  [4/4] volume cap         {ctx.recent_solicitation_count_in_window}/{cap} "
                       f"per {window}d → " + ("BLOCK" if over else "under cap"))

    verdict = evaluate(ctx)
    yield line(OK if verdict.allowed else ERR, src,
               f"VERDICT: {'PASS' if verdict.allowed else 'BLOCK'} -- {verdict.reason}")

    # ── the disclosure check, run against the REAL drafted message ──
    if not rule.requires_disclosure_label:
        yield line(INFO, src,
                   f"{rule.state} requires no advertising label on written solicitation -- "
                   "no message-body check needed")
        return

    # Resolving the draft is itself real work (often a live model call), so it
    # streams its own progress rather than appearing as a finished string.
    resolved: dict = {}
    for ev in draft_events(db, user, contact, resolved, channel):
        yield ev
    draft = resolved.get("text")
    draft_src, interaction_id = resolved.get("source"), resolved.get("interaction_id")

    if not draft:
        yield line(WARN, "backend.observe.logstream:draft_events",
                   f"{rule.state} requires a {rule.disclosure_text!r} label, but no drafted "
                   "message could be resolved for this contact (no stored draft, and no "
                   "composer produced one)")
        return

    yield line(STEP, "backend.observe.logstream:jurisdiction_events",
               f"checking drafted message against {rule.state}'s {rule.disclosure_text!r} "
               f"labeling requirement · source={draft_src}"
               + (f" interaction_id={interaction_id}" if interaction_id else ""))
    label = (rule.disclosure_text or "").lower()
    # Wrap to readable widths so a one-paragraph draft still reads line by
    # line rather than as a single truncated blob.
    import textwrap
    lines = []
    for para in draft.splitlines():
        if not para.strip():
            continue
        lines.extend(textwrap.wrap(para.strip(), width=96) or [para.strip()])

    hit_index = None
    for i, ln in enumerate(lines, start=1):
        hit = bool(label) and label in ln.lower()
        if hit and hit_index is None:
            hit_index = i
        yield line(OK if hit else INFO, "backend.observe.logstream:jurisdiction_events",
                   f"  L{i:<3} {ln}" + ("   ← required label appears here" if hit else ""))

    present = bool(label) and label in draft.lower()
    if present:
        yield line(OK, src,
                   f"disclosure label {rule.disclosure_text!r}: PRESENT (line L{hit_index}) "
                   f"→ satisfies {rule.state}'s labeling requirement")
    else:
        yield line(ERR, src,
                   f"disclosure label {rule.disclosure_text!r}: MISSING from all "
                   f"{len(lines)} lines → {rule.state} requires this label on written "
                   f"solicitation; this draft needs REVIEW before send")
        yield line(WARN, src,
                   f"  first line as sent would be: {lines[0][:96]!r}"
                   if lines else "  draft is empty")
