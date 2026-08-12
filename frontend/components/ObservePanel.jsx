// ── Surplus Observe : a debugger for the intelligence system ────────────────
// Left: the object list (an account's/demo lawyer's ranked opportunities --
// the same product surface RankingTrace.jsx already renders). Click one to
// open the right-side Observe panel: SIGNAL / TARGETING / RELATIONSHIP /
// BEHAVIOR / RANKING (+ Ablate) / JURISDICTION / OUTCOME / EVALUATION, each
// "Inspect ->" pulling a real backend/observe DecisionTrace -- never an
// LLM-invented rationale (see backend/observe/trace.py's own docstring: the
// structured trace is the source of truth).
//
// Auth: real signed-in Surplus session (cookie, same as every other page in
// this app) via backend.auth.current_user -- NOT the ?key= demo-token
// pattern RankingTrace.jsx uses. This is the account-aware "add-on to the
// existing Surplus application" the Observe spec's success criterion #1
// requires. backend/routes/observe.py's own authorization rule: DEMO-
// provenance data is inspectable by any signed-in account; real data is
// strictly self-scoped.
import React, { useState, useEffect, useCallback } from "react";
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

// ── one collapsible "Inspect ->" trace section ───────────────────────────
function TraceSection({ title, icon: Icon, statusLabel, statusColor, fetchTrace, extra }) {
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
                <div style={{ marginTop: 6, fontSize: 10.5, color: C.faint }}>
                  evaluated by: {trace.evaluation_references.map(r => r.harness_id).join(", ")}
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

// ── Evaluation strip: harness pass/fail summary for the current cohort ───
function EvaluationSection({ cohortId }) {
  const [results, setResults] = useState({});
  const harnesses = [
    ["ablation", "Ablation"], ["relationship_evaluation", "Relationship Eval"],
    ["signal_library_evaluation", "Signal Library Eval"],
    ["jurisdiction_regression", "Jurisdiction Regression"],
    ["historical_replay", "Historical Replay"], ["synthetic_scenarios", "Synthetic Scenarios"],
  ];
  useEffect(() => {
    harnesses.forEach(([id]) => {
      const params = ["jurisdiction_regression", "synthetic_scenarios"].includes(id)
        ? {} : { cohort_id: cohortId };
      if (!cohortId && Object.keys(params).length) return;
      getJSON(`/api/observe/harness/${id}`, params)
        .then(r => setResults(prev => ({ ...prev, [id]: r })))
        .catch(e => setResults(prev => ({ ...prev, [id]: { error: String(e) } })));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cohortId]);

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "14px 16px", marginTop: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 10,
                    fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                    color: C.muted }}>
        <Layers size={13} /> Evaluation
      </div>
      {harnesses.map(([id, label]) => {
        const r = results[id];
        const ok = r && !r.error && r.cases_failed === 0;
        return (
          <div key={id} style={{ display: "flex", justifyContent: "space-between",
                                  padding: "4px 0", fontSize: 12 }}>
            <span style={{ color: C.ink }}>{label}</span>
            {!r && <span style={{ color: C.faint }}>…</span>}
            {r && r.error && <span style={{ color: C.faint }}>n/a</span>}
            {r && !r.error && (
              <span style={{ color: ok ? C.good : C.bad, fontWeight: 700, fontFamily: MONO }}>
                {ok ? <CheckCircle2 size={12} style={{ verticalAlign: -2 }} />
                    : <XCircle size={12} style={{ verticalAlign: -2 }} />}{" "}
                {r.cases_passed}/{r.cases_total}
              </span>
            )}
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
        <TraceSection title="Signal" icon={Radar}
          fetchTrace={() => getJSON(`/api/observe/trace/signal/${draftInteractionId}`)} />
      )}
      <TraceSection title="Targeting" icon={GitBranch}
        fetchTrace={() => getJSON(`/api/observe/trace/lawyer/${contactId}`)} />
      <TraceSection title="Relationship" icon={Activity}
        fetchTrace={() => getJSON(`/api/observe/trace/relationship/${contactId}`)} />
      <TraceSection title="Behavior" icon={Layers}
        fetchTrace={() => getJSON(`/api/observe/trace/opportunity/${contactId}`)
          .then(t => ({ ...t, features: t.features.filter(f => f.name === "historical_behavior"),
                       decision: "empirical engagement history" }))} />
      <TraceSection title="Ranking" icon={TrendingUp}
        fetchTrace={() => getJSON(`/api/observe/trace/opportunity/${contactId}`)}
        extra={<>
          <RankingComparison contactId={contactId} />
          <AblateButton contactId={contactId} />
        </>} />
      <TraceSection title="Jurisdiction" icon={Scale}
        fetchTrace={() => getJSON(`/api/observe/trace/jurisdiction/${contactId}`)} />
      {draftInteractionId != null && (
        <TraceSection title="Outcome" icon={CheckCircle2}
          fetchTrace={() => getJSON(`/api/observe/trace/outcome/${draftInteractionId}`)} />
      )}

      <EvaluationSection cohortId={cohortId} />
    </div>
  );
}

// ── Top-level: cohort/lawyer picker + object list + Observe panel ───────
export default function ObservePanel() {
  const [cohorts, setCohorts] = useState([]);
  const [cohortId, setCohortId] = useState(null);
  const [userEmail, setUserEmail] = useState("demo-lawyer-000@example.com");
  const [opportunities, setOpportunities] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    getJSON("/api/observe/cohorts")
      .then(d => {
        setCohorts(d.cohorts);
        if (d.cohorts.length) setCohortId(d.cohorts[0].cohort_id);
      })
      .catch(e => setError(String(e)));
  }, []);

  const load = useCallback(() => {
    if (!cohortId) return;
    getJSON("/api/observe/book", { cohort_id: cohortId, user_email: userEmail })
      .then(d => setOpportunities(d.opportunities))
      .catch(e => setError(String(e)));
  }, [cohortId, userEmail]);

  useEffect(() => { load(); }, [load]);

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
        Click an opportunity to open its decision trace: what the system saw, what it
        inferred, what it recommended, which constraints applied, and how that decision
        is evaluated.
      </p>

      {error === "401: {\"detail\":\"Not signed in\"}" ? (
        <div style={{ padding: 14, borderRadius: 10, background: C.badBg, color: C.bad, fontSize: 13 }}>
          Sign in to a Surplus account to use Observe.
        </div>
      ) : (
        <>
          {error && (
            <div style={{ padding: 12, borderRadius: 8, background: C.badBg, color: C.bad,
                          fontSize: 12.5, marginBottom: 16 }}>
              {error}
            </div>
          )}

          <div style={{ display: "flex", gap: 10, marginBottom: 20 }}>
            <select value={cohortId || ""} onChange={e => setCohortId(e.target.value)}
                    style={{ padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.line}`,
                            fontSize: 12.5 }}>
              {cohorts.map(c => <option key={c.cohort_id} value={c.cohort_id}>{c.cohort_id}</option>)}
            </select>
            <input value={userEmail} onChange={e => setUserEmail(e.target.value)}
                   style={{ padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.line}`,
                           fontSize: 12.5, flex: 1, fontFamily: MONO }} />
          </div>

          <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
            <div style={{ flex: 1, minWidth: 260 }}>
              {opportunities.map(opp => (
                <div key={opp.contact_id} onClick={() => setSelected(opp)}
                     style={{
                       border: `1px solid ${selected && selected.contact_id === opp.contact_id ? C.accent : C.line}`,
                       borderRadius: 10, padding: "10px 14px", marginBottom: 8, cursor: "pointer",
                       background: selected && selected.contact_id === opp.contact_id ? C.accentBg : C.card,
                     }}>
                  <div style={{ fontSize: 13.5, fontWeight: 700, color: C.ink }}>{opp.contact_name}</div>
                  <div style={{ fontSize: 11.5, color: C.muted }}>
                    Rank #{opp.rank} · score {opp.score}
                  </div>
                </div>
              ))}
              {!opportunities.length && !error && (
                <div style={{ fontSize: 12.5, color: C.faint }}>No signaled opportunities for this lawyer.</div>
              )}
            </div>
            <div style={{ flex: 1, minWidth: 340, position: "sticky", top: 20 }}>
              {selected ? (
                <ObserveDebugger contactId={selected.contact_id} contactName={selected.contact_name}
                                 draftInteractionId={selected.latest_draft_interaction_id}
                                 cohortId={cohortId} />
              ) : (
                <div style={{ fontSize: 12.5, color: C.faint, padding: "40px 0", textAlign: "center" }}>
                  Select an opportunity to inspect its decision trace.
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
