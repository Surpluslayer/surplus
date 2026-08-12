// ── Surplus Observe : one page, real numbers, nothing hidden behind clicks ──
// PURELY REACTIVE -- no navigation of its own. Its subject is whatever was
// last clicked in the real Book product to its left (main-inperson.jsx's
// ObserveSplitView owns the click delegation and passes the result down as
// `selection`).
//
// Layout, top to bottom:
//   1. SYSTEM STATUS -- all 6 harnesses, visible on load, no clicks needed.
//   2. The selected person -- ONE flat block, every computed number visible
//      at once (pipeline stages, signal, targeting, relationship, ranking,
//      jurisdiction, outcome). An earlier version nested each of these in
//      its own collapsed accordion, so nothing was visible without working
//      through every section by hand -- replaced with a single dense panel,
//      closer to how a real trace waterfall reads.
//   3. Observation log -- every person looked at this session; click one to
//      re-render it in the same flat panel above.
//
// Every trace comes from a real backend/observe DecisionTrace -- never an
// LLM-invented rationale (see backend/observe/trace.py's own docstring: the
// structured trace is the source of truth).
//
// Auth: real signed-in Surplus session (cookie, same as every other page in
// this app) via backend.auth.current_user. backend/routes/observe.py's own
// authorization rule: DEMO-provenance data is inspectable by any signed-in
// account; real data is strictly self-scoped.
import React, { useState, useEffect } from "react";
import {
  Radar, GitBranch, Scale, Activity, TrendingUp,
  CheckCircle2, XCircle, Layers, FlaskConical, Beaker, AlertTriangle, Workflow,
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

// Gateway/edge failures (a slow trace computation timing out at Cloudflare,
// a deploy in flight) ship a full HTML error page as the body -- same set
// lib/api.js's _friendly() already treats as transient elsewhere in this
// app. Dumping that raw markup into a trace's error text (as happened once
// here: a 524 gateway timeout rendered as if it were real classification
// output) is actively misleading, not just ugly -- an observability tool
// that shows a lie is worse than one that shows nothing.
const _TRANSIENT_STATUS = new Set([502, 503, 504, 524]);

function _friendlyError(status, text) {
  const isHtml = /^\s*</.test(text || "");
  if (_TRANSIENT_STATUS.has(status)) {
    return `${status}: gateway timeout -- didn't finish in time. Try again.`;
  }
  if (isHtml) return `${status}: request failed (non-JSON response).`;
  try {
    const j = JSON.parse(text);
    const d = j && (typeof j.detail === "string" ? j.detail : j.message);
    if (d) return `${status}: ${d}`;
  } catch { /* not JSON -- fall through */ }
  return `${status}: ${(text || "").slice(0, 200)}`;
}

async function getJSON(path, params) {
  const qs = params && Object.keys(params).length ? `?${new URLSearchParams(params)}` : "";
  const res = await fetch(`${path}${qs}`, { credentials: "same-origin" });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    const err = new Error(_friendlyError(res.status, body));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// Resolves once, never rejects -- lets Promise.all wait on every fetch even
// when some 404/524/etc, so one failing section doesn't blank the rest of
// the flat panel.
function safeGet(path, params) {
  return getJSON(path, params).then(v => ({ ok: true, v })).catch(e => ({ ok: false, error: String(e) }));
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

function fmtVal(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "number") return v.toFixed(3);
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

// One label:value row -- the base unit of the whole flat panel.
function Row({ label, value, color, indent = 0 }) {
  return (
    <div style={{ display: "flex", gap: 8, padding: "2px 0", fontFamily: MONO, fontSize: 11.5,
                  paddingLeft: indent }}>
      <span style={{ color: C.faint, minWidth: 190, flexShrink: 0 }}>{label}</span>
      <span style={{ color: color || C.ink, wordBreak: "break-word" }}>{value}</span>
    </div>
  );
}

function SectionHead({ icon: Icon, title, right }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 16, marginBottom: 6 }}>
      <Icon size={13} color={C.muted} />
      <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: 0.5, textTransform: "uppercase",
                    color: C.ink }}>
        {title}
      </span>
      {right}
    </div>
  );
}

// Which harnesses need a demo cohort_id to run vs. which are self-contained
// (jurisdiction fixtures / synthetic scenarios build their own data).
const HARNESS_NEEDS_COHORT = {
  historical_replay: true, ablation: true, relationship_evaluation: true,
  signal_library_evaluation: true, jurisdiction_regression: false, synthetic_scenarios: false,
};

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

// ── System status: every harness, visible the moment the page loads ──────
function SystemStatus({ cohortId, cohortsLoaded }) {
  const [results, setResults] = useState({});
  useEffect(() => {
    if (!cohortsLoaded) return;
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
  }, [cohortId, cohortsLoaded]);

  const passing = Object.values(results).filter(r => r && !r.error && !r.unavailable && r.cases_failed === 0).length;
  const total = HARNESS_CATALOG.length;

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "14px 16px", marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <Layers size={13} color={C.muted} />
        <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: 0.5, textTransform: "uppercase",
                      color: C.ink }}>
          System status -- every harness
        </span>
        <span style={{ marginLeft: "auto", fontSize: 11, fontWeight: 700, fontFamily: MONO,
                      color: passing === total ? C.good : C.muted }}>
          {passing}/{total} clean
        </span>
      </div>
      {HARNESS_CATALOG.map(({ id, label, proves }) => {
        const r = results[id];
        const ok = r && !r.error && !r.unavailable && r.cases_failed === 0;
        return (
          <div key={id} style={{ display: "flex", alignItems: "baseline", gap: 10,
                                  padding: "5px 0", borderTop: `1px solid ${C.line}` }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: C.ink, width: 170, flexShrink: 0 }}>
              {label}
            </span>
            <span style={{ fontSize: 10.5, color: C.faint, flex: 1 }}>{proves}</span>
            {!r && <span style={{ fontSize: 11, color: C.faint, fontFamily: MONO }}>running…</span>}
            {r && r.unavailable && (
              <span style={{ fontSize: 10.5, color: C.faint, fontFamily: MONO, flexShrink: 0 }}>
                needs a seeded demo cohort
              </span>
            )}
            {r && r.error && <span style={{ fontSize: 11, color: C.faint, fontFamily: MONO }}>n/a</span>}
            {r && !r.error && !r.unavailable && (
              <span style={{ fontSize: 11.5, fontWeight: 700, fontFamily: MONO, flexShrink: 0,
                            color: ok ? C.good : C.bad }}>
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

// ── Ablate action: full system vs without-<lever> ─────────────────────────
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
    <div style={{ marginTop: 6 }}>
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
          <div key={group} style={{ marginTop: 6, fontFamily: MONO, fontSize: 11.5, color: C.ink }}>
            <span style={{ fontWeight: 700 }}>{label}:</span>{" "}
            full #{result.full_system.rank} ({result.full_system.score}) → without #{result.without_group.rank}{" "}
            ({result.without_group.score}){" "}
            <span style={{ color: result.rank_delta > 0 ? C.good : C.faint }}>
              contribution {result.rank_delta > 0 ? "+" : ""}{result.rank_delta} positions
              ({result.score_delta > 0 ? "+" : ""}{result.score_delta} score)
            </span>
          </div>
        );
      })}
    </div>
  );
}

function EvalRefs({ refs, cohortId }) {
  const [results, setResults] = useState({});
  if (!refs || !refs.length) return null;
  const run = (harnessId) => {
    const needsCohort = HARNESS_NEEDS_COHORT[harnessId];
    if (needsCohort && !cohortId) {
      setResults(prev => ({ ...prev, [harnessId]: { unavailable: true } }));
      return;
    }
    setResults(prev => ({ ...prev, [harnessId]: { loading: true } }));
    getJSON(`/api/observe/harness/${harnessId}`, needsCohort ? { cohort_id: cohortId } : {})
      .then(r => setResults(prev => ({ ...prev, [harnessId]: r })))
      .catch(e => setResults(prev => ({ ...prev, [harnessId]: { error: String(e) } })));
  };
  return (
    <div style={{ fontSize: 10.5, color: C.faint, marginTop: 2 }}>
      evaluated by:{" "}
      {refs.map(r => {
        const res = results[r.harness_id];
        return (
          <span key={r.harness_id} style={{ marginRight: 8 }}>
            <button onClick={() => run(r.harness_id)} style={{
              fontSize: 10.5, fontWeight: 700, color: C.accent, background: "none", border: "none",
              textDecoration: "underline", cursor: "pointer", padding: 0,
            }}>
              {r.harness_id}
            </button>
            {res && res.loading && "…"}
            {res && res.unavailable && " (needs demo cohort)"}
            {res && res.error && " (n/a)"}
            {res && !res.loading && !res.unavailable && !res.error && (
              <span style={{ fontWeight: 700, color: res.cases_failed === 0 ? C.good : C.bad }}>
                {" "}{res.cases_passed}/{res.cases_total}
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}

const STAGE_STATUS_ICON = { success: CheckCircle2, warning: AlertTriangle, failure: XCircle };
const STAGE_STATUS_COLOR = { success: C.good, warning: C.amber, failure: C.bad };

function PipelineRows({ trace }) {
  if (!trace) return <Row label="pipeline" value="loading…" color={C.faint} />;
  if (trace.error) return <Row label="pipeline" value={trace.error} color={C.bad} />;
  return trace.stages.map(s => {
    const color = STAGE_STATUS_COLOR[s.status] || C.faint;
    return (
      <div key={s.name} style={{ padding: "2px 0" }}>
        <div style={{ display: "flex", gap: 8, fontFamily: MONO, fontSize: 11.5 }}>
          <span style={{ color: C.faint, minWidth: 190, flexShrink: 0, textTransform: "capitalize" }}>
            {s.name.replace(/_/g, " ")}
          </span>
          <span style={{ color, fontWeight: 700, minWidth: 70 }}>{s.status}</span>
          <span style={{ color: C.faint, minWidth: 60 }}>{s.latency_ms}ms</span>
          <span style={{ color: C.ink }}>{s.decision}</span>
        </div>
        {s.fallback && (
          <div style={{ fontSize: 10.5, color: C.amber, fontFamily: MONO, paddingLeft: 198 }}>
            ⚠ {s.fallback}
          </div>
        )}
        {s.error && (
          <div style={{ fontSize: 10.5, color: C.bad, fontFamily: MONO, paddingLeft: 198 }}>
            ✕ {s.error}
          </div>
        )}
      </div>
    );
  });
}

function FeatureRows({ features }) {
  return (features || []).map(f => (
    <Row key={f.name} label={f.name + (f.weight != null ? ` (×${f.weight})` : "")} value={fmtVal(f.value)} />
  ));
}

// ── One flat panel: every real computed number for one person, at once ───
function PersonPanel({ selection, cohortId }) {
  const [data, setData] = useState(null);
  const { contactId, name, type, id: selectedId } = selection;
  const draftInteractionId = type === "signal" ? selectedId : null;

  useEffect(() => {
    let cancelled = false;
    setData(null);
    const calls = {
      pipeline: safeGet(`/api/observe/trace/pipeline/${contactId}`),
      targeting: safeGet(`/api/observe/trace/lawyer/${contactId}`),
      relationship: safeGet(`/api/observe/trace/relationship/${contactId}`),
      ranking: safeGet(`/api/observe/trace/opportunity/${contactId}`),
      jurisdiction: safeGet(`/api/observe/trace/jurisdiction/${contactId}`),
    };
    if (draftInteractionId != null) {
      calls.signal = safeGet(`/api/observe/trace/signal/${draftInteractionId}`);
      calls.outcome = safeGet(`/api/observe/trace/outcome/${draftInteractionId}`);
    }
    Promise.all(Object.values(calls)).then(results => {
      if (cancelled) return;
      const keys = Object.keys(calls);
      const out = {};
      keys.forEach((k, i) => { out[k] = results[i]; });
      setData(out);
    });
    return () => { cancelled = true; };
  }, [contactId, draftInteractionId]);

  const get = (key) => data && data[key] && data[key].ok ? data[key].v : null;
  const getErr = (key) => data && data[key] && !data[key].ok ? data[key].error : null;
  const pipeline = data && data.pipeline ? (data.pipeline.ok ? data.pipeline.v : { error: data.pipeline.error }) : null;
  const targeting = get("targeting"), relationship = get("relationship"), ranking = get("ranking");
  const jurisdiction = get("jurisdiction"), signal = get("signal"), outcome = get("outcome");
  const provenance = targeting && targeting.provenance;

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "16px 18px", marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{ fontSize: 10.5, fontWeight: 800, letterSpacing: 0.6, color: C.accent,
                      textTransform: "uppercase" }}>
          Surplus Observe
        </div>
        {provenance && <ProvenanceBadge provenance={provenance} />}
      </div>
      <div style={{ fontSize: 18, fontWeight: 800, color: C.ink, marginTop: 2 }}>
        {name || `contact ${contactId}`}
      </div>

      <SectionHead icon={Workflow} title="Pipeline"
        right={pipeline && !pipeline.error && (
          <span style={{ marginLeft: "auto", fontSize: 11, fontWeight: 700,
                        color: STAGE_STATUS_COLOR[pipeline.overall_status] }}>
            {pipeline.overall_status} · {pipeline.total_latency_ms}ms total
          </span>
        )} />
      <PipelineRows trace={pipeline} />

      {draftInteractionId != null && (
        <>
          <SectionHead icon={Radar} title="Signal" />
          {getErr("signal") ? <Row label="signal" value={getErr("signal")} color={C.bad} /> : (
            signal ? (
              <>
                <Row label="decision" value={signal.decision} />
                <FeatureRows features={signal.features} />
                <EvalRefs refs={signal.evaluation_references} cohortId={cohortId} />
              </>
            ) : <Row label="signal" value="loading…" color={C.faint} />
          )}
        </>
      )}

      <SectionHead icon={GitBranch} title="Targeting" />
      {getErr("targeting") ? <Row label="targeting" value={getErr("targeting")} color={C.bad} /> : (
        targeting ? (
          <>
            <Row label="decision" value={`${targeting.decision}  (score ${fmtVal(targeting.score)})`} />
            <FeatureRows features={targeting.features} />
            <EvalRefs refs={targeting.evaluation_references} cohortId={cohortId} />
          </>
        ) : <Row label="targeting" value="loading…" color={C.faint} />
      )}

      <SectionHead icon={Activity} title="Relationship" />
      {getErr("relationship") ? <Row label="relationship" value={getErr("relationship")} color={C.bad} /> : (
        relationship ? (
          <>
            <Row label="decision" value={relationship.decision} />
            <FeatureRows features={relationship.features} />
            {relationship.evidence && (
              <>
                <Row label="relationship_duration_days" value={fmtVal(relationship.evidence.relationship_duration_days)} />
                <Row label="shared_entities" value={fmtVal(relationship.evidence.shared_entities)} />
              </>
            )}
            <EvalRefs refs={relationship.evaluation_references} cohortId={cohortId} />
          </>
        ) : <Row label="relationship" value="loading…" color={C.faint} />
      )}

      <SectionHead icon={TrendingUp} title="Ranking" />
      {getErr("ranking") ? <Row label="ranking" value={getErr("ranking")} color={C.bad} /> : (
        ranking ? (
          <>
            <Row label="decision" value={`${ranking.decision}  (score ${fmtVal(ranking.score)})`} />
            <FeatureRows features={ranking.features} />
            {ranking.evidence && (
              <Row label="candidates_considered" value={ranking.evidence.candidates_considered_total} />
            )}
            {ranking.evidence && ranking.evidence.comparison && (
              <Row label={ranking.evidence.comparison.this_beat_alternative ? "beat" : "lost to"}
                   value={`#${ranking.evidence.comparison.alternative_rank} `
                          + `${ranking.evidence.comparison.alternative_contact_name} `
                          + `(score ${ranking.evidence.comparison.alternative_score}, `
                          + `delta ${ranking.evidence.comparison.score_delta}) `
                          + `-- largest factor: ${ranking.evidence.comparison.largest_contributing_factor}`} />
            )}
            <AblateButton contactId={contactId} />
            <EvalRefs refs={ranking.evaluation_references} cohortId={cohortId} />
          </>
        ) : <Row label="ranking" value="loading…" color={C.faint} />
      )}

      <SectionHead icon={Scale} title="Jurisdiction" />
      {getErr("jurisdiction") ? <Row label="jurisdiction" value={getErr("jurisdiction")} color={C.bad} /> : (
        jurisdiction ? (
          <>
            <Row label="decision" value={jurisdiction.decision}
                 color={jurisdiction.decision === "PASS" ? C.good : C.bad} />
            <Row label="bar_jurisdiction (this account)"
                 value={jurisdiction.inputs && jurisdiction.inputs.jurisdiction
                        ? jurisdiction.inputs.jurisdiction
                        : "not set on this account -- defaulting to the fail-closed unknown-jurisdiction rule"}
                 color={jurisdiction.inputs && jurisdiction.inputs.jurisdiction ? C.ink : C.amber} />
            <Row label="channel" value={jurisdiction.inputs && jurisdiction.inputs.channel} />
            <Row label="relationship_type" value={jurisdiction.inputs && jurisdiction.inputs.relationship_type} />
            {jurisdiction.policy_result && (
              <Row label="reason" value={jurisdiction.policy_result.reason} />
            )}
            <EvalRefs refs={jurisdiction.evaluation_references} cohortId={cohortId} />
          </>
        ) : <Row label="jurisdiction" value="loading…" color={C.faint} />
      )}

      {draftInteractionId != null && (
        <>
          <SectionHead icon={CheckCircle2} title="Outcome" />
          {getErr("outcome") ? <Row label="outcome" value={getErr("outcome")} color={C.bad} /> : (
            outcome ? (
              <>
                <Row label="decision" value={outcome.decision} />
                <Row label="outcome" value={JSON.stringify(outcome.outcome)} />
                <EvalRefs refs={outcome.evaluation_references} cohortId={cohortId} />
              </>
            ) : <Row label="outcome" value="loading…" color={C.faint} />
          )}
        </>
      )}

      {targeting && targeting.versions && (
        <div style={{ marginTop: 14, paddingTop: 8, borderTop: `1px dashed ${C.line}`,
                      fontSize: 9.5, color: C.faint, fontFamily: MONO }}>
          {Object.entries(targeting.versions).map(([k, v]) => `${k}=${v}`).join("  ·  ")}
        </div>
      )}
    </div>
  );
}

// ── Observation log: everything looked at this session ───────────────────
const TYPE_LABEL = { lawyer: "Contact", signal: "Signal", draft: "Draft" };

function ObservationLog({ entries, activeAt, onSelect }) {
  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "14px 16px" }}>
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
      {entries.map(e => (
        <div key={e.at} onClick={() => onSelect(e)} style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          borderTop: `1px solid ${C.line}`, padding: "7px 2px", cursor: "pointer",
          background: e.at === activeAt ? C.accentBg : "transparent",
        }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: C.ink }}>
            {TYPE_LABEL[e.type] || e.type}{e.name ? ` · ${e.name}` : ""}
          </span>
          <span style={{ fontSize: 10, color: C.faint, fontFamily: MONO }}>
            {new Date(e.at).toLocaleTimeString()}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Top level ──────────────────────────────────────────────────────────
export default function ObservePanel({ selection }) {
  const [cohortId, setCohortId] = useState(null);
  const [cohortsLoaded, setCohortsLoaded] = useState(false);
  const [authError, setAuthError] = useState(false);
  const [log, setLog] = useState([]);
  const [active, setActive] = useState(null);

  useEffect(() => {
    getJSON("/api/observe/cohorts")
      .then(d => { if (d.cohorts.length) setCohortId(d.cohorts[0].cohort_id); })
      .catch(e => { if (String(e).startsWith("401")) setAuthError(true); })
      .finally(() => setCohortsLoaded(true));
  }, []);

  useEffect(() => {
    if (!selection) return;
    setLog(prev => [{ ...selection, at: Date.now() }, ...prev].slice(0, 50));
    setActive({ ...selection, at: Date.now() });
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
        Every harness below, and its live pass/fail, is running now -- no clicks needed.
        Click a contact, update, or signal in Surplus to see every real number behind its decision.
      </p>

      {authError ? (
        <div style={{ padding: 14, borderRadius: 10, background: C.badBg, color: C.bad, fontSize: 13 }}>
          Sign in to a Surplus account to use Observe.
        </div>
      ) : (
        <>
          <SystemStatus cohortId={cohortId} cohortsLoaded={cohortsLoaded} />

          {active ? (
            <PersonPanel key={`${active.type}:${active.id}`} selection={active} cohortId={cohortId} />
          ) : (
            <div style={{ fontSize: 12.5, color: C.faint, padding: "40px 20px", textAlign: "center",
                          border: `1px dashed ${C.line}`, borderRadius: 12, marginBottom: 16 }}>
              Select a contact, update, or signal to see its decision trace.
            </div>
          )}

          <ObservationLog entries={log} activeAt={active && active.at} onSelect={setActive} />
        </>
      )}
    </div>
  );
}
