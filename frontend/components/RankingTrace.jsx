// ── Ranking Trace : product feature + its own technical observability ──────
// Side-by-side view of the demo cohort's opportunity ranking (backend/demo/,
// routes/demo_observability.py): the LEFT panel is the product surface (an
// opportunity card a lawyer would actually see); the RIGHT panel, opened by
// clicking a card, is the decision trace behind its score -- every factor
// that produced it, each expandable into the raw evidence. Below that, the
// real viewed->drafted->edited->sent->outcome stepper for that contact, and
// a cohort-wide eval panel (ranker v0 vs v1, computed live, never hardcoded).
//
// Auth: this surface uses a query-param key (DEMO_ACCESS_TOKEN), not the
// session cookie every other page in this app uses -- it's meant for a
// hand-shared demo link, same posture as routes/demo.py's /enter. Not
// wired into the app shell's normal routing; mount it directly at whatever
// path this gets exposed at.
import React, { useState, useEffect, useCallback } from "react";
import { ChevronDown, ChevronRight, Radar, GitBranch, TrendingUp,
         CheckCircle2, XCircle, Circle, Eye, FileEdit, Send as SendIcon } from "lucide-react";

const C = {
  ink: "#1a1d24", muted: "#6b7280", faint: "#9aa1ad",
  line: "#e6e8ee", card: "#ffffff", bg: "#f4f5f7",
  accent: "#2f6df6", accentBg: "#eaf1fe",
  good: "#1c8c4e", goodBg: "#e7f7ee",
  bad: "#c0432f", badBg: "#fdeaea",
};
const FONT = "'Inter', system-ui, sans-serif";

const FACTOR_LABELS = {
  signal_relevance: "Signal relevance",
  practice_fit: "Practice fit",
  relationship_strength: "Relationship strength",
  relationship_recency: "Relationship recency",
  relationship_trajectory: "Relationship trajectory",
  historical_behavior: "Historical behavior",
};

const STEP_ICON = {
  viewed: Eye, drafted: FileEdit, approved: CheckCircle2,
  discarded: XCircle, edited: FileEdit, sent: SendIcon,
};

function apiKey() {
  return new URLSearchParams(window.location.search).get("key") || "";
}

async function getJSON(path, params) {
  const qs = new URLSearchParams({ ...params, key: apiKey() });
  const res = await fetch(`${path}?${qs.toString()}`);
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

// ── Factor bar (the technical observability unit) ───────────────────────────
function FactorBar({ factor }) {
  const [open, setOpen] = useState(false);
  const pct = Math.round(factor.value * 100);
  return (
    <div style={{ borderBottom: `1px solid ${C.line}`, padding: "10px 0" }}>
      <div
        onClick={() => setOpen(o => !o)}
        style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}
      >
        {open ? <ChevronDown size={14} color={C.faint} /> : <ChevronRight size={14} color={C.faint} />}
        <div style={{ width: 170, fontSize: 13, color: C.ink, fontWeight: 600 }}>
          {FACTOR_LABELS[factor.name] || factor.name}
        </div>
        <div style={{ flex: 1, height: 8, borderRadius: 4, background: C.bg, overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: C.accent, borderRadius: 4 }} />
        </div>
        <div style={{ width: 50, textAlign: "right", fontSize: 12.5, fontFamily: "ui-monospace, monospace",
                      color: C.muted }}>
          {pct}
        </div>
        <div style={{ width: 44, textAlign: "right", fontSize: 10.5, color: C.faint }}>
          x{factor.weight}
        </div>
      </div>
      {open && (
        <div style={{ marginLeft: 24, marginTop: 8, padding: "8px 12px", borderRadius: 8,
                      background: C.bg, fontSize: 12, fontFamily: "ui-monospace, monospace",
                      color: C.muted, lineHeight: 1.7 }}>
          {Object.entries(factor.evidence || {}).map(([k, v]) => (
            <div key={k}>
              <span style={{ color: C.faint }}>{k}:</span>{" "}
              <span style={{ color: C.ink }}>{v === null ? "—" : String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Feedback stepper ──────────────────────────────────────────────────────
function FeedbackStepper({ steps }) {
  if (!steps || !steps.length) {
    return <div style={{ fontSize: 12.5, color: C.faint }}>No recorded activity yet.</div>;
  }
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {steps.map((s, i) => {
        const Icon = STEP_ICON[s.step] || Circle;
        const isTerminal = s.step === "discarded";
        return (
          <div key={i} style={{
            display: "flex", alignItems: "center", gap: 6, padding: "5px 10px",
            borderRadius: 999, fontSize: 12, fontWeight: 600,
            background: isTerminal ? C.badBg : C.accentBg,
            color: isTerminal ? C.bad : C.accent,
          }}>
            <Icon size={13} />
            {s.step}
          </div>
        );
      })}
    </div>
  );
}

// ── Opportunity card (the product feature) ───────────────────────────────
function OpportunityCard({ opp, userEmail, expanded, onToggle }) {
  const [trace, setTrace] = useState(null);
  const [feedback, setFeedback] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!expanded || trace) return;
    setLoading(true);
    Promise.all([
      getJSON(`/api/demo/observability/trace/${opp.contact_id}`, { user_email: userEmail }),
      getJSON(`/api/demo/observability/feedback/${opp.contact_id}`, { user_email: userEmail }),
    ])
      .then(([t, f]) => { setTrace(t); setFeedback(f); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [expanded, trace, opp.contact_id, userEmail]);

  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  marginBottom: 12, overflow: "hidden" }}>
      <div onClick={onToggle} style={{
        display: "flex", alignItems: "center", gap: 12, padding: "14px 16px", cursor: "pointer",
      }}>
        <div style={{
          width: 40, height: 40, borderRadius: 10, background: C.accentBg,
          display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
        }}>
          <Radar size={18} color={C.accent} />
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14.5, fontWeight: 700, color: C.ink }}>{opp.contact_name}</div>
          <div style={{ fontSize: 12, color: C.muted }}>
            Rank #{opp.rank} &middot; opportunity score {(opp.opportunity_score * 100).toFixed(0)}
          </div>
        </div>
        {expanded ? <ChevronDown size={16} color={C.faint} /> : <ChevronRight size={16} color={C.faint} />}
      </div>

      {expanded && (
        <div style={{ borderTop: `1px solid ${C.line}`, padding: "14px 16px", background: C.bg }}>
          {loading && <div style={{ fontSize: 12.5, color: C.faint }}>Loading trace…</div>}
          {trace && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 10,
                            fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                            color: C.muted }}>
                <GitBranch size={13} /> Ranking trace
              </div>
              <div style={{ background: C.card, borderRadius: 10, border: `1px solid ${C.line}`,
                            padding: "4px 14px" }}>
                {trace.factors.map(f => <FactorBar key={f.name} factor={f} />)}
              </div>
            </>
          )}
          {feedback && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 6, margin: "16px 0 10px",
                            fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase",
                            color: C.muted }}>
                <TrendingUp size={13} /> Feedback loop
              </div>
              <FeedbackStepper steps={feedback.steps} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Eval panel (cohort-wide, computed live) ─────────────────────────────
function EvalPanel({ cohortId }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!cohortId) return;
    getJSON("/api/demo/observability/eval", { cohort_id: cohortId }).then(setData).catch(() => {});
  }, [cohortId]);
  if (!data) return null;
  const v0 = data.v0_naive_recency_ranker, v1 = data.v1_full_ranking_trace;
  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 12, background: C.card,
                  padding: "16px 18px", marginBottom: 20 }}>
      <div style={{ fontSize: 12.5, fontWeight: 700, color: C.ink, marginBottom: 4 }}>
        Ranker evaluation
      </div>
      <div style={{ fontSize: 11.5, color: C.faint, marginBottom: 14 }}>{data.caveat}</div>
      <div style={{ display: "flex", gap: 24 }}>
        <div>
          <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase",
                        letterSpacing: 0.4, fontWeight: 700 }}>v0 &middot; naive recency</div>
          <div style={{ fontSize: 24, fontWeight: 800, color: C.ink, fontFamily: "ui-monospace, monospace" }}>
            {(v0.top5_positive_outcome_rate * 100).toFixed(1)}%
          </div>
          <div style={{ fontSize: 11, color: C.faint }}>{v0.opportunities_evaluated} opportunities</div>
        </div>
        <div>
          <div style={{ fontSize: 11, color: C.accent, textTransform: "uppercase",
                        letterSpacing: 0.4, fontWeight: 700 }}>v1 &middot; full ranking trace</div>
          <div style={{ fontSize: 24, fontWeight: 800, color: C.accent, fontFamily: "ui-monospace, monospace" }}>
            {(v1.top5_positive_outcome_rate * 100).toFixed(1)}%
          </div>
          <div style={{ fontSize: 11, color: C.faint }}>{v1.opportunities_evaluated} opportunities</div>
        </div>
        <div style={{ marginLeft: "auto", alignSelf: "center", textAlign: "right" }}>
          <div style={{ fontSize: 11, color: C.good, fontWeight: 700 }}>LIFT</div>
          <div style={{ fontSize: 20, fontWeight: 800, color: C.good, fontFamily: "ui-monospace, monospace" }}>
            +{(data.lift * 100).toFixed(1)}pp
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Top-level: cohort/lawyer picker + opportunity list ──────────────────
export default function RankingTraceDemo() {
  const [cohorts, setCohorts] = useState([]);
  const [cohortId, setCohortId] = useState(null);
  const [userEmail, setUserEmail] = useState("demo-lawyer-000@example.com");
  const [opportunities, setOpportunities] = useState([]);
  const [expandedId, setExpandedId] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    getJSON("/api/demo/observability/cohorts", {})
      .then(d => {
        setCohorts(d.cohorts);
        if (d.cohorts.length) setCohortId(d.cohorts[0].cohort_id);
      })
      .catch(e => setError(String(e)));
  }, []);

  const load = useCallback(() => {
    if (!cohortId) return;
    getJSON("/api/demo/observability/opportunities", { cohort_id: cohortId, user_email: userEmail, limit: 10 })
      .then(d => setOpportunities(d.opportunities))
      .catch(e => setError(String(e)));
  }, [cohortId, userEmail]);

  useEffect(() => { load(); }, [load]);

  return (
    <div style={{ fontFamily: FONT, maxWidth: 720, margin: "0 auto", padding: "32px 20px 80px" }}>
      <div style={{ marginBottom: 4, fontSize: 11, fontWeight: 700, letterSpacing: 0.5,
                    textTransform: "uppercase", color: C.accent }}>
        Demo Environment
      </div>
      <h1 style={{ fontSize: 22, fontWeight: 800, color: C.ink, margin: "2px 0 4px" }}>
        Opportunity ranking &mdash; product + technical observability
      </h1>
      <p style={{ fontSize: 13, color: C.muted, marginBottom: 20, maxWidth: 560 }}>
        Left: the opportunity a lawyer sees. Click it to open the actual decision
        trace behind its score, and the real activity that followed.
      </p>

      {error && (
        <div style={{ padding: 12, borderRadius: 8, background: C.badBg, color: C.bad,
                      fontSize: 12.5, marginBottom: 16 }}>
          {error} &mdash; is DEMO_ACCESS_TOKEN set and does the URL include ?key=...?
        </div>
      )}

      <div style={{ display: "flex", gap: 10, marginBottom: 20 }}>
        <select value={cohortId || ""} onChange={e => setCohortId(e.target.value)}
                style={{ padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.line}`, fontSize: 12.5 }}>
          {cohorts.map(c => <option key={c.cohort_id} value={c.cohort_id}>{c.cohort_id}</option>)}
        </select>
        <input value={userEmail} onChange={e => setUserEmail(e.target.value)}
               style={{ padding: "6px 10px", borderRadius: 8, border: `1px solid ${C.line}`,
                       fontSize: 12.5, flex: 1, fontFamily: "ui-monospace, monospace" }} />
      </div>

      <EvalPanel cohortId={cohortId} />

      {opportunities.map(opp => (
        <OpportunityCard key={opp.contact_id} opp={opp} userEmail={userEmail}
                         expanded={expandedId === opp.contact_id}
                         onToggle={() => setExpandedId(id => id === opp.contact_id ? null : opp.contact_id)} />
      ))}
      {!opportunities.length && !error && (
        <div style={{ fontSize: 12.5, color: C.faint }}>No signaled opportunities for this lawyer.</div>
      )}
    </div>
  );
}
