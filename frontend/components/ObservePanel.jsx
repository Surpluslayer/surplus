// ── Surplus Observe : a debugger for the intelligence system ────────────────
// PURELY REACTIVE -- no navigation of its own. Its subject is whatever was
// last clicked in the real Book product to its left (main-inperson.jsx's
// ObserveSplitView owns the click delegation and passes the result down as
// `selection`). An earlier version of this panel had its own cohort/lawyer
// picker; that made Observe a second, disconnected app sharing a screen
// with the real product instead of a panel that explains it -- removed.
//
// Panel: SIGNAL / TARGETING / RELATIONSHIP / BEHAVIOR / RANKING (+ Ablate) /
// JURISDICTION / OUTCOME / EVALUATION, each "Inspect ->" pulling a real
// backend/observe DecisionTrace -- never an LLM-invented rationale (see
// backend/observe/trace.py's own docstring: the structured trace is the
// source of truth). Below that: a standing harness catalog, and an
// append-only observation log of everything clicked this session.
//
// Auth: real signed-in Surplus session (cookie, same as every other page in
// this app) via backend.auth.current_user -- NOT the ?key= demo-token
// pattern RankingTrace.jsx uses. This is the account-aware "add-on to the
// existing Surplus application" the Observe spec's success criterion #1
// requires. backend/routes/observe.py's own authorization rule: DEMO-
// provenance data is inspectable by any signed-in account; real data is
// strictly self-scoped.
import React, { useState, useEffect } from "react";
import {
  ChevronDown, ChevronRight, Radar, GitBranch, Scale, Activity, TrendingUp,
  CheckCircle2, XCircle, Circle, Layers, FlaskConical, Beaker, AlertTriangle, Workflow,
} from "lucide-react";

const C = {
  ink: "#1a1d24", muted: "#6b7280", faint: "#9aa1ad",
  line: "#e6e8ee", card: "#ffffff", bg: "#f4f5f7",
  accent: "#2f6df6", accentBg: "#eaf1fe",
  good: "#1c8c4e", goodBg: "#e7f7ee",
  bad: "#c0432f", badBg: "#fdeaea",
  amber: "#a06a00", amberBg: "#fdf3e0",
};
const FONT = "'Inter', system-ui, sans-serif";
const MONO = "ui-monospace, monospace";

const PROVENANCE_LABEL = { observed: "OBSERVED", demo: "DEMO", synthetic: "SYNTHETIC" };
const PROVENANCE_COLOR = { observed: C.good, demo: C.accent, synthetic: C.amber };

async function getJSON(path, params) {
  const qs = params && Object.keys(params).length ? `?${new URLSearchParams(params)}` : "";
  const res = await fetch(`${path}${qs}`, { credentials: "same-origin" });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    const err = new Error(`${res.status}: ${body.slice(0, 200)}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function ProvenanceBadge({ provenance }) {
  if (!provenance) return null;
  const color = PROVENANCE_COLOR[provenance] || C.faint;
  return (
    <span style={{
      fontSize: 9.5, fontWeight: 800, letterSpacing: 0.5, color,
      border: `1px solid ${color}`, borderRadius: 999, padding: "1px 7px",
    }}>
      {PROVENANCE_LABEL[provenance] || provenance.toUpperCase()}
    </span>
  );
}

// Which harnesses need a demo cohort_id to run vs. which are self-contained
// (jurisdiction fixtures / synthetic scenarios build their own data). Used
// both by the standing catalog and by each trace's clickable
// evaluation_references.
const HARNESS_NEEDS_COHORT = {
  historical_replay: true, ablation: true, relationship_evaluation: true,
  signal_library_evaluation: true, jurisdiction_regression: false, synthetic_scenarios: false,
};

// ── one evaluation_references entry: click to actually run that harness ──
function EvalRefLink({ harnessId, cohortId }) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const needsCohort = HARNESS_NEEDS_COHORT[harnessId];

  const run = () => {
    if (needsCohort && !cohortId) { setResult({ unavailable: true }); return; }
    setLoading(true);
    getJSON(`/api/observe/harness/${harnessId}`, needsCohort ? { cohort_id: cohortId } : {})
      .then(setResult).catch(e => setResult({ error: String(e) })).finally(() => setLoading(false));
  };

  return (
    <span style={{ marginRight: 10 }}>
      <button onClick={run} disabled={loading} style={{
        fontSize: 10.5, fontWeight: 700, color: C.accent, background: "none",
        border: "none", textDecoration: "underline", cursor: "pointer", padding: 0,
      }}>
        {harnessId}{loading ? "…" : ""}
      </button>
      {result && !result.unavailable && !result.error && (
        <span style={{ marginLeft: 4, fontSize: 10.5, fontWeight: 700,
                       color: result.cases_failed === 0 ? C.good : C.bad }}>
          {result.cases_passed}/{result.cases_total}
        </span>
      )}
      {result && result.unavailable && (
        <span style={{ marginLeft: 4, fontSize: 10.5, color: C.faint }}>(no demo cohort)</span>
      )}
      {result && result.error && (
        <span style={{ marginLeft: 4, fontSize: 10.5, color: C.bad }}>error</span>
      )}
    </span>
  );
}

// ── one collapsible "Inspect ->" trace section ───────────────────────────
function TraceSection({ title, icon: Icon, statusLabel, statusColor, fetchTrace, extra, cohortId }) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const toggle = () => {
    setOpen(o => !o);
    if (!trace && !loading) {
      setLoading(true);
      fetchTrace().then(setTrace).catch(e => setError(String(e))).finally(() => setLoading(false));
    }
  };

  return (
    <div style={{ borderBottom: `1px solid ${C.line}` }}>
      <div onClick={toggle} style={{
        display: "flex", alignItems: "center", gap: 10, padding: "12px 4px", cursor: "pointer",
      }}>
        {open ? <ChevronDown size={14} color={C.faint} /> : <ChevronRight size={14} color={C.faint} />}
        <Icon size={15} color={C.muted} />
        <div style={{ fontSize: 12.5, fontWeight: 700, color: C.ink, flex: 1 }}>{title}</div>
        {statusLabel && (
          <span style={{ fontSize: 11, fontWeight: 700, color: statusColor || C.muted }}>
            {statusLabel}
          </span>
        )}
      </div>
      {open && (
        <div style={{ marginLeft: 24, marginBottom: 12, padding: "10px 12px", borderRadius: 8,
                      background: C.bg, fontSize: 12, color: C.muted }}>
          {loading && "Loading trace…"}
          {error && <span style={{ color: C.bad }}>{error}</span>}
          {trace && (
            <>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                <div style={{ fontWeight: 700, color: C.ink }}>{trace.decision}</div>
                <ProvenanceBadge provenance={trace.provenance} />
              </div>
              {(trace.features || []).map(f => (
                <div key={f.name} style={{ display: "flex", gap: 8, padding: "3px 0",
                                            fontFamily: MONO, fontSize: 11.5 }}>
                  <span style={{ color: C.faint, minWidth: 160 }}>{f.name}</span>
                  <span style={{ color: C.ink }}>
                    {typeof f.value === "number" ? f.value.toFixed(3) : String(f.value)}
                  </span>
                  {f.weight != null && <span style={{ color: C.faint }}>x{f.weight}</span>}
                </div>
              ))}
              {trace.policy_result && (
                <div style={{ marginTop: 6, fontFamily: MONO, fontSize: 11.5 }}>
                  reason: {trace.policy_result.reason}
                </div>
              )}
              {trace.outcome && (
                <div style={{ marginTop: 6, fontFamily: MONO, fontSize: 11.5 }}>
                  outcome: {JSON.stringify(trace.outcome)}
                </div>
              )}
              {trace.versions && (
                <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px dashed ${C.line}`,
                              fontSize: 10, color: C.faint }}>
                  {Object.entries(trace.versions).map(([k, v]) => `${k}=${v}`).join("  ·  ")}
                </div>
              )}
              {trace.evaluation_references && trace.evaluation_references.length > 0 && (
                <div style={{ marginTop: 8, paddingTop: 6, borderTop: `1px dashed ${C.line}` }}>
                  <div style={{ fontSize: 9.5, fontWeight: 700, color: C.faint,
                                textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 4 }}>
                    Evaluated by
                  </div>
                  {trace.evaluation_references.map(r => (
                    <EvalRefLink key={r.harness_id} harnessId={r.harness_id} cohortId={cohortId} />
                  ))}
                </div>
              )}
            </>
          )}
          {extra}
        </div>
      )}
    </div>
  );
}

// ── Ablate action: full system vs without-<lever>, for one contact ───────
// Four independently-selectable levers (Observe checklist H). "relationship"
// and "timing" overlap on purpose (timing is the time-based half of
// relationship) -- see ranking_trace.FACTOR_GROUPS' own docstring; this UI
// exposes both because they answer different questions ("what if there were
// no relationship at all" vs "what if the relationship existed but had no
// recency/momentum signal").
const ABLATION_LEVERS = [
  ["relationship", "Remove relationship"],
  ["behavior", "Remove behavior"],
  ["signal_affinity", "Remove signal affinity"],
  ["timing", "Remove timing"],
];

function AblateButton({ contactId }) {
  const [results, setResults] = useState({});
  const [loading, setLoading] = useState(null);
  const run = (group) => {
    setLoading(group);
    getJSON(`/api/observe/ablate/${contactId}`, { remove_group: group })
      .then(r => setResults(prev => ({ ...prev, [group]: r })))
      .catch(() => {}).finally(() => setLoading(null));
  };
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {ABLATION_LEVERS.map(([group, label]) => (
          <button key={group} onClick={() => run(group)} disabled={loading === group} style={{
            fontSize: 11, fontWeight: 700, color: C.accent, background: C.accentBg,
            border: "none", borderRadius: 6, padding: "5px 9px", cursor: "pointer",
          }}>
            <Beaker size={10} style={{ verticalAlign: -1, marginRight: 4 }} />
            {loading === group ? "…" : label}
          </button>
        ))}
      </div>
      {ABLATION_LEVERS.map(([group, label]) => {
        const result = results[group];
        if (!result) return null;
        return (
          <div key={group} style={{ marginTop: 8, fontFamily: MONO, fontSize: 11.5, color: C.ink }}>
            <div style={{ fontWeight: 700 }}>{label}:</div>
            Full system: rank #{result.full_system.rank} (score {result.full_system.score})<br />
            Without: rank #{result.without_group.rank} (score {result.without_group.score})<br />
            <span style={{ color: result.rank_delta > 0 ? C.good : C.faint }}>
              Contribution: {result.rank_delta > 0 ? "+" : ""}{result.rank_delta} ranking positions
              ({result.score_delta > 0 ? "+" : ""}{result.score_delta} score)
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ── Standing harness catalog: all 6 harnesses, what each proves, live status ─
const HARNESS_CATALOG = [
  { id: "historical_replay", label: "Historical Replay",
    proves: "Reconstructs what Surplus knew at a past timestamp with no future-data leakage, "
            + "and compares the prediction to the real outcome." },
  { id: "ablation", label: "Feature Ablation",
    proves: "Removes one feature group at a time and re-ranks the same candidates, to show "
            + "which pieces of the score actually move it." },
  { id: "relationship_evaluation", label: "Relationship Evaluation",
    proves: "Compares signal-only vs relationship-aware ranking against observed lawyer "
            + "behavior -- the core moat claim." },
  { id: "signal_library_evaluation", label: "Signal Library Evaluation",
    proves: "Classification accuracy on labeled fixtures, plus lawyer-conditioned engagement "
            + "by signal category." },
  { id: "jurisdiction_regression", label: "Jurisdiction Regression",
    proves: "Runs the Rule 7.3 solicitation gate against its adversarial fixture matrix; "
            + "flags any regression." },
  { id: "synthetic_scenarios", label: "Synthetic Scenarios",
    proves: "Known-answer adversarial ranking scenarios, each with a pre-declared expected "
            + "outcome -- tests the system's reasoning, not customer evidence." },
];

function HarnessCatalog({ cohortId }) {
  const [results, setResults] = useState({});
  useEffect(() => {
    HARNESS_CATALOG.forEach(({ id }) => {
      const needsCohort = HARNESS_NEEDS_COHORT[id];
      if (needsCohort && !cohortId) {
        setResults(prev => ({ ...prev, [id]: { unavailable: true } }));
        return;
      }
      getJSON(`/api/observe/harness/${id}`, needsCohort ? { cohort_id: cohortId } : {})
        .then(r => setResults(prev => ({ ...prev, [id]: r })))
        .catch(e => setResults(prev => ({ ...prev, [id]: { error: String(e) } })));
    });
  }, [cohortId]);

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "14px 16px", marginTop: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 10,
                    fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                    color: C.muted }}>
        <Layers size={13} /> Harness catalog
      </div>
      {HARNESS_CATALOG.map(({ id, label, proves }) => {
        const r = results[id];
        const ok = r && !r.error && !r.unavailable && r.cases_failed === 0;
        return (
          <div key={id} style={{ padding: "8px 0", borderTop: `1px solid ${C.line}` }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: C.ink }}>{label}</span>
              {!r && <span style={{ fontSize: 11, color: C.faint }}>…</span>}
              {r && r.unavailable && <span style={{ fontSize: 11, color: C.faint }}>no demo cohort</span>}
              {r && r.error && <span style={{ fontSize: 11, color: C.faint }}>n/a</span>}
              {r && !r.error && !r.unavailable && (
                <span style={{ fontSize: 11.5, fontWeight: 700, fontFamily: MONO,
                              color: ok ? C.good : C.bad }}>
                  {ok ? <CheckCircle2 size={12} style={{ verticalAlign: -2 }} />
                      : <XCircle size={12} style={{ verticalAlign: -2 }} />}{" "}
                  {r.cases_passed}/{r.cases_total}
                </span>
              )}
            </div>
            <div style={{ fontSize: 10.5, color: C.faint, marginTop: 2 }}>{proves}</div>
          </div>
        );
      })}
    </div>
  );
}

const STAGE_STATUS_ICON = { success: CheckCircle2, warning: AlertTriangle, failure: XCircle };
const STAGE_STATUS_COLOR = { success: C.good, warning: C.amber, failure: C.bad };

// ── Pipeline trace: what actually ran, stage by stage ────────────────────
function PipelineSection({ contactId }) {
  const [trace, setTrace] = useState(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setTrace(null);
    getJSON(`/api/observe/trace/pipeline/${contactId}`).then(setTrace).catch(e => setError(String(e)));
  }, [contactId]);

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "14px 16px", marginBottom: 16 }}>
      <div onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 8, cursor: "pointer",
      }}>
        {open ? <ChevronDown size={14} color={C.faint} /> : <ChevronRight size={14} color={C.faint} />}
        <Workflow size={14} color={C.muted} />
        <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                      color: C.muted, flex: 1 }}>
          Pipeline
        </div>
        {trace && (
          <span style={{ fontSize: 11, fontWeight: 700, color: STAGE_STATUS_COLOR[trace.overall_status] }}>
            {trace.overall_status} · {trace.total_latency_ms}ms
          </span>
        )}
      </div>
      {open && (
        <div style={{ marginTop: 10 }}>
          {error && <div style={{ fontSize: 12, color: C.bad }}>{error}</div>}
          {!trace && !error && <div style={{ fontSize: 12, color: C.faint }}>Loading…</div>}
          {trace && trace.stages.map(s => {
            const Icon = STAGE_STATUS_ICON[s.status] || Circle;
            const color = STAGE_STATUS_COLOR[s.status] || C.faint;
            return (
              <div key={s.name} style={{ display: "flex", alignItems: "flex-start", gap: 8,
                                          padding: "6px 0", borderTop: `1px solid ${C.line}` }}>
                <Icon size={13} color={color} style={{ marginTop: 2, flexShrink: 0 }} />
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: C.ink, textTransform: "capitalize" }}>
                      {s.name.replace(/_/g, " ")}
                    </span>
                    <span style={{ fontSize: 10.5, color: C.faint, fontFamily: MONO }}>
                      {s.latency_ms}ms · {s.version}
                    </span>
                  </div>
                  <div style={{ fontSize: 11.5, color: C.muted }}>{s.decision}</div>
                  {s.fallback && (
                    <div style={{ fontSize: 10.5, color: C.amber, marginTop: 2 }}>⚠ {s.fallback}</div>
                  )}
                  {s.error && (
                    <div style={{ fontSize: 10.5, color: C.bad, marginTop: 2 }}>✕ {s.error}</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Ranking comparison: full candidate list + why it beat the alternative ─
function RankingComparison({ contactId }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    getJSON(`/api/observe/trace/opportunity/${contactId}`).then(setData).catch(() => {});
  }, [contactId]);
  if (!data) return null;
  const { candidates_considered, candidates_considered_total, comparison } = data.evidence || {};
  return (
    <div style={{ marginTop: 10 }}>
      {candidates_considered && (
        <div style={{ fontSize: 11, color: C.faint, marginBottom: 6 }}>
          {candidates_considered_total} candidates considered
          {candidates_considered.length < candidates_considered_total
            ? ` (showing top ${candidates_considered.length})` : ""}
        </div>
      )}
      {comparison && (
        <div style={{ fontFamily: MONO, fontSize: 11.5, color: C.ink }}>
          {comparison.this_beat_alternative ? "Beat" : "Lost to"} #{comparison.alternative_rank}{" "}
          {comparison.alternative_contact_name} (score {comparison.alternative_score},{" "}
          delta {comparison.score_delta > 0 ? "+" : ""}{comparison.score_delta})
          <div style={{ marginTop: 4, color: C.muted }}>
            Largest contributing factor: {comparison.largest_contributing_factor}
          </div>
        </div>
      )}
    </div>
  );
}

// ── The full Observe panel for one selected opportunity ─────────────────
function ObserveDebugger({ contactId, contactName, draftInteractionId, cohortId }) {
  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "16px 18px" }}>
      <div style={{ fontSize: 10.5, fontWeight: 800, letterSpacing: 0.6, color: C.accent,
                    textTransform: "uppercase", marginBottom: 2 }}>
        Surplus Observe
      </div>
      <div style={{ fontSize: 16, fontWeight: 800, color: C.ink, marginBottom: 12 }}>
        {contactName}
      </div>

      <PipelineSection contactId={contactId} />

      {draftInteractionId != null && (
        <TraceSection title="Signal" icon={Radar} cohortId={cohortId}
          fetchTrace={() => getJSON(`/api/observe/trace/signal/${draftInteractionId}`)} />
      )}
      <TraceSection title="Targeting" icon={GitBranch} cohortId={cohortId}
        fetchTrace={() => getJSON(`/api/observe/trace/lawyer/${contactId}`)} />
      <TraceSection title="Relationship" icon={Activity} cohortId={cohortId}
        fetchTrace={() => getJSON(`/api/observe/trace/relationship/${contactId}`)} />
      <TraceSection title="Behavior" icon={Layers} cohortId={cohortId}
        fetchTrace={() => getJSON(`/api/observe/trace/opportunity/${contactId}`)
          .then(t => ({ ...t, features: t.features.filter(f => f.name === "historical_behavior"),
                       decision: "empirical engagement history" }))} />
      <TraceSection title="Ranking" icon={TrendingUp} cohortId={cohortId}
        fetchTrace={() => getJSON(`/api/observe/trace/opportunity/${contactId}`)}
        extra={<>
          <RankingComparison contactId={contactId} />
          <AblateButton contactId={contactId} />
        </>} />
      <TraceSection title="Jurisdiction" icon={Scale} cohortId={cohortId}
        fetchTrace={() => getJSON(`/api/observe/trace/jurisdiction/${contactId}`)} />
      {draftInteractionId != null && (
        <TraceSection title="Outcome" icon={CheckCircle2} cohortId={cohortId}
          fetchTrace={() => getJSON(`/api/observe/trace/outcome/${draftInteractionId}`)} />
      )}
    </div>
  );
}

// ── Observation log: append-only record of everything clicked this session ─
const TYPE_LABEL = { lawyer: "Contact", signal: "Signal", draft: "Draft" };

function ObservationLogEntry({ entry, expanded, onToggle }) {
  return (
    <div style={{ borderTop: `1px solid ${C.line}`, padding: "8px 2px", cursor: "pointer" }}
         onClick={onToggle}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: C.ink }}>
          {TYPE_LABEL[entry.type] || entry.type}
          {entry.name ? ` · ${entry.name}` : ""}
        </span>
        <span style={{ fontSize: 10, color: C.faint, fontFamily: MONO }}>
          {new Date(entry.at).toLocaleTimeString()}
        </span>
      </div>
      {expanded && (
        <div style={{ marginTop: 4, fontSize: 10.5, color: C.muted, fontFamily: MONO }}>
          object_type={entry.type} object_id={entry.id} contact_id={entry.contactId}
        </div>
      )}
    </div>
  );
}

function ObservationLog({ entries }) {
  const [expandedAt, setExpandedAt] = useState(null);
  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "14px 16px", marginTop: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4,
                    fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                    color: C.muted }}>
        <FlaskConical size={13} /> Observation log ({entries.length})
      </div>
      {entries.length === 0 && (
        <div style={{ fontSize: 12, color: C.faint, padding: "8px 2px" }}>
          Nothing observed yet this session -- click a contact, update, or signal on the left.
        </div>
      )}
      {entries.map((e, i) => (
        <ObservationLogEntry key={e.at} entry={e} expanded={expandedAt === e.at}
                             onToggle={() => setExpandedAt(x => x === e.at ? null : e.at)} />
      ))}
    </div>
  );
}

// ── Top-level: purely reactive to `selection` (main-inperson.jsx's click
// delegation), a standing harness catalog, and the observation log ────────
export default function ObservePanel({ selection }) {
  const [cohortId, setCohortId] = useState(null);
  const [authError, setAuthError] = useState(false);
  const [log, setLog] = useState([]);

  useEffect(() => {
    getJSON("/api/observe/cohorts")
      .then(d => { if (d.cohorts.length) setCohortId(d.cohorts[0].cohort_id); })
      .catch(e => { if (String(e).startsWith("401")) setAuthError(true); });
  }, []);

  useEffect(() => {
    if (!selection) return;
    setLog(prev => [{ ...selection, at: Date.now() }, ...prev].slice(0, 50));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection && selection.at]);

  return (
    <div style={{ fontFamily: FONT, padding: "24px 24px 80px", background: C.bg,
                  minHeight: "100%" }}>
      <div style={{ marginBottom: 4, fontSize: 11, fontWeight: 700, letterSpacing: 0.5,
                    textTransform: "uppercase", color: C.accent }}>
        <FlaskConical size={12} style={{ verticalAlign: -2, marginRight: 5 }} />
        Surplus Observe
      </div>
      <h1 style={{ fontSize: 22, fontWeight: 800, color: C.ink, margin: "2px 0 4px" }}>
        Universal intelligence observability
      </h1>
      <p style={{ fontSize: 13, color: C.muted, marginBottom: 20, maxWidth: 640 }}>
        Click a contact, update, or signal in Surplus to open its decision trace: what
        the system saw, what it inferred, what it recommended, which constraints
        applied, and how that decision is evaluated.
      </p>

      {authError ? (
        <div style={{ padding: 14, borderRadius: 10, background: C.badBg, color: C.bad, fontSize: 13 }}>
          Sign in to a Surplus account to use Observe.
        </div>
      ) : (
        <>
          {selection ? (
            <ObserveDebugger key={`${selection.type}:${selection.id}`}
                             contactId={selection.contactId} contactName={selection.name || selection.contactId}
                             draftInteractionId={selection.type === "signal" ? selection.id : null}
                             cohortId={cohortId} />
          ) : (
            <div style={{ fontSize: 12.5, color: C.faint, padding: "60px 20px", textAlign: "center",
                          border: `1px dashed ${C.line}`, borderRadius: 12 }}>
              Select a contact, update, or signal to inspect its decision trace.
            </div>
          )}

          <HarnessCatalog cohortId={cohortId} />
          <ObservationLog entries={log} />
        </>
      )}
    </div>
  );
}
