import React, { useState, useEffect } from "react";
import {
  ArrowRight, Check, Circle, Activity, Send, Network, Target,
  GitBranch, BriefcaseBusiness, Zap, TrendingUp, RotateCw, Mail,
  CornerDownRight
} from "lucide-react";
import { api } from "./lib/api.js";

// ============================================================
// Event ROI MVP — browser demo
// Five-stage mechanism: intake -> prospecting -> auto-outreach
// -> symbiotic matching -> verified ROI ledger
// Adapts to event format (incl. hackathons) and goal (incl.
// product testing). All data mocked — this is a flow demo.
// ============================================================

const STAGES = [
  { id: 0, key: "intake",    label: "Intake",       icon: Target },
  { id: 1, key: "pipeline",  label: "Prospecting",  icon: Activity },
  { id: 2, key: "prospects", label: "Auto-outreach",icon: Send },
  { id: 3, key: "matching",  label: "Matching",     icon: Network },
  { id: 4, key: "roi",       label: "ROI ledger",   icon: TrendingUp },
];

const FORMATS = ["Sit-down dinner", "Hackathon", "Workshop", "Mixer", "Roundtable"];
const GOALS = ["Hiring pipeline", "Fundraising", "Sales pipeline", "Product testing", "Community density"];
const SENIORITY = ["Mid", "Senior", "Staff+", "Leadership"];
const STAGES_CO = ["Pre-seed", "Seed", "Series A", "Series B+"];

// ---- format config: matching topology -----------------------
const FORMAT_CONFIG = {
  "Sit-down dinner": { group: "Table", topo: "fixed seating — composition locked before doors open" },
  "Hackathon":       { group: "Team",  topo: "team formation — complementary skills balanced per team" },
  "Workshop":        { group: "Breakout", topo: "fluid breakouts — groups regroup between sessions" },
  "Mixer":           { group: "Cluster", topo: "soft clusters — seeded, not enforced" },
  "Roundtable":      { group: "Seat",  topo: "single ring — seating order is the lever" },
};

// ---- goal config: outreach + conversion semantics -----------
const GOAL_CONFIG = {
  "Hiring pipeline": {
    outreach: (p) => `pulling together a ${p.headcount}-person ${p.format.toLowerCase()} in ${p.city} — ${p.seniority.toLowerCase()} infra engineers and the teams hiring them.`,
    ledgerHead: "Hiring outcome",
    tiers: {
      high: { label: "Hired", state: "won", detail: "signed offer" },
      mid:  { label: "In pipeline", state: "partial", detail: "final round" },
      low:  { label: "No fit", state: "lost", detail: "passed" },
    },
    value: { won: 28000, partial: 8000, lost: 0 },
  },
  "Fundraising": {
    outreach: (p) => `hosting a ${p.format.toLowerCase()} in ${p.city} — a tight room of founders raising and investors writing checks at ${p.coStage}.`,
    ledgerHead: "Raise outcome",
    tiers: {
      high: { label: "Term sheet", state: "won", detail: "in diligence" },
      mid:  { label: "Warm intro", state: "partial", detail: "follow-up booked" },
      low:  { label: "Passed", state: "lost", detail: "not a fit" },
    },
    value: { won: 180000, partial: 30000, lost: 0 },
  },
  "Sales pipeline": {
    outreach: (p) => `running a ${p.format.toLowerCase()} in ${p.city} with operators evaluating tools in your space this quarter.`,
    ledgerHead: "Deal outcome",
    tiers: {
      high: { label: "Closed", state: "won", detail: "contract signed" },
      mid:  { label: "Trial", state: "partial", detail: "POC started" },
      low:  { label: "Cold", state: "lost", detail: "no pull" },
    },
    value: { won: 54000, partial: 11000, lost: 0 },
  },
  "Product testing": {
    outreach: (p) => `pulling together a ${p.format.toLowerCase()} in ${p.city} — hands-on ${p.seniority.toLowerCase()} infra engineers to stress-test an early build and tell us where it breaks.`,
    ledgerHead: "Testing outcome",
    tiers: {
      high: { label: "Active tester", state: "won", detail: "12 issues filed, weekly" },
      mid:  { label: "Gave feedback", state: "partial", detail: "one session" },
      low:  { label: "Lapsed", state: "lost", detail: "no activity" },
    },
    value: { won: 16000, partial: 4000, lost: 0 },
  },
  "Community density": {
    outreach: (p) => `building a recurring ${p.format.toLowerCase()} in ${p.city} — the ${p.seniority.toLowerCase()} infra crowd, same room every month.`,
    ledgerHead: "Community outcome",
    tiers: {
      high: { label: "Core member", state: "won", detail: "returning + bringing others" },
      mid:  { label: "Returning", state: "partial", detail: "came back once" },
      low:  { label: "One-off", state: "lost", detail: "no return" },
    },
    value: { won: 6000, partial: 1800, lost: 0 },
  },
};

// ---- mock prospect pool -------------------------------------
// side = market side (drives symbiotic matching)
// offers / seeks = the value vectors the matcher pairs on
const PROSPECTS = [
  { id: 1, name: "Maya Rodriguez", role: "Staff Infra Engineer", company: "Lo91r (Seed)", side: "Builds",
    worksOn: "observability", offers: "Observability depth", seeks: "Staff-scope role",
    gh: 2100, x: 4800, li: true, score: 94, status: "rsvp", grp: 1,
    reason: "Maintains a widely-used Rust tracing crate; recent posts signal active interest in eval tooling." },
  { id: 2, name: "Daniel Okafor", role: "Founding Engineer", company: "Vello (Series A)", side: "Builds",
    worksOn: "model-serving", offers: "Model-serving infra", seeks: "Founding-level scope",
    gh: 880, x: 1200, li: true, score: 91, status: "rsvp", grp: 2,
    reason: "Shipped a model-serving layer at a prior startup; clean ICP match on devtools and stage." },
  { id: 3, name: "Priya Natarajan", role: "ML Platform Lead", company: "Cohere", side: "Hires",
    worksOn: "ml-platform", offers: "Platform roles + mentorship", seeks: "Infra builders to hire",
    gh: 1500, x: 9300, li: true, score: 88, status: "rsvp", grp: 1,
    reason: "Leads a platform team with open headcount — high downstream value for the builder side of the room." },
  { id: 4, name: "Sam Whitfield", role: "Senior Backend Eng", company: "Ramp", side: "Builds",
    worksOn: "payments-infra", offers: "Payments-infra experience", seeks: "Senior scope",
    gh: 410, x: 600, li: true, score: 82, status: "contacted", grp: null,
    reason: "Solid infra background; less public signal but a clean role and stage match." },
  { id: 5, name: "Aisha Bello", role: "Eng Manager, Data", company: "Notion", side: "Hires",
    worksOn: "data-infra", offers: "Data-team roles", seeks: "Data-infra builders",
    gh: 320, x: 2100, li: true, score: 86, status: "rsvp", grp: 2,
    reason: "Manages data infra with two open reqs — direct symbiotic counterpart to the builder side." },
  { id: 6, name: "Theo Lindqvist", role: "Distributed Systems Eng", company: "Fly.io", side: "Builds",
    worksOn: "distributed-systems", offers: "OSS credibility, hard-systems depth", seeks: "Unsolved systems problems",
    gh: 3400, x: 5600, li: true, score: 90, status: "rsvp", grp: 1,
    reason: "High-credibility OSS contributor — an anchor guest who raises the whole room's perceived quality." },
  { id: 7, name: "Grace Liu", role: "Software Engineer", company: "Stripe", side: "Builds",
    worksOn: "web-infra", offers: "Frontend velocity", seeks: "Mentorship",
    gh: 150, x: 90, li: true, score: 61, status: "below", grp: null,
    reason: "Early-career; below the fit threshold for this event's seniority target." },
  { id: 8, name: "Marcus Reed", role: "Product Manager", company: "Figma", side: "Operates",
    worksOn: "product", offers: "Product sense", seeks: "Eng cofounder",
    gh: 40, x: 320, li: true, score: 44, status: "below", grp: null,
    reason: "Role mismatch against an infra-engineer ICP. Held for a future event." },
];

const THRESHOLD = 70;
const SIDE_CLASS = { Builds: "side-build", Hires: "side-hire", Operates: "side-op" };

// ---- shared UI ----------------------------------------------
function StageRail({ stage, setStage, maxReached }) {
  return (
    <nav className="rail">
      {STAGES.map((s) => {
        const Icon = s.icon;
        const done = s.id < stage;
        const active = s.id === stage;
        const reachable = s.id <= maxReached;
        return (
          <button key={s.id}
            className={`rail-item ${active ? "active" : ""} ${done ? "done" : ""}`}
            onClick={() => reachable && setStage(s.id)} disabled={!reachable}>
            <span className="rail-dot">{done ? <Check size={13} strokeWidth={3} /> : <Icon size={13} />}</span>
            <span className="rail-label">{s.label}</span>
            <span className="rail-idx">0{s.id + 1}</span>
          </button>
        );
      })}
    </nav>
  );
}

const Chip = ({ active, onClick, children }) => (
  <button className={`chip ${active ? "chip-on" : ""}`} onClick={onClick}>{children}</button>
);

const fmtK = (v) => v >= 1000 ? `$${(v / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k` : `$${v}`;
const fmtNum = (n) => n > 999 ? (n / 1000).toFixed(1) + "k" : "" + n;

// ---- Stage 0: Intake ----------------------------------------
function Intake({ profile, setProfile, onRun }) {
  const set = (k, v) => setProfile((p) => ({ ...p, [k]: v }));
  return (
    <div className="stage">
      <header className="stage-head">
        <p className="eyebrow">Stage 01 — Elicitation</p>
        <h1>Define the event mechanism</h1>
        <p className="lede">
          Three blocks of private information. The <em>goal</em> becomes the matcher's
          objective function and the conversion definition; the <em>budget</em> is the
          constraint it optimizes against and the denominator for ROI. Works the same
          for a dinner or a hackathon — only the topology changes.
        </p>
      </header>

      <div className="form-grid">
        <section className="card">
          <h3><span className="card-num">A</span> Ideal attendee (ICP)</h3>
          <label>Target role</label>
          <input className="text-in" value={profile.role}
            onChange={(e) => set("role", e.target.value)} />
          <label>Seniority</label>
          <div className="chip-row">
            {SENIORITY.map((s) => (
              <Chip key={s} active={profile.seniority === s} onClick={() => set("seniority", s)}>{s}</Chip>
            ))}
          </div>
          <label>Company stage</label>
          <div className="chip-row">
            {STAGES_CO.map((s) => (
              <Chip key={s} active={profile.coStage === s} onClick={() => set("coStage", s)}>{s}</Chip>
            ))}
          </div>
        </section>

        <section className="card">
          <h3><span className="card-num">B</span> Event shape</h3>
          <label>Headcount — <strong>{profile.headcount}</strong> guests</label>
          <input type="range" min="12" max="160" step="2" value={profile.headcount}
            onChange={(e) => set("headcount", +e.target.value)} className="range-in" />
          <label>Format <span className="hint">— sets the matching topology</span></label>
          <div className="chip-row">
            {FORMATS.map((f) => (
              <Chip key={f} active={profile.format === f} onClick={() => set("format", f)}>{f}</Chip>
            ))}
          </div>
          <p className="topo-inline"><CornerDownRight size={11} /> {FORMAT_CONFIG[profile.format].topo}</p>
          <label>City</label>
          <input className="text-in" value={profile.city} onChange={(e) => set("city", e.target.value)} />
        </section>

        <section className="card">
          <h3><span className="card-num">C</span> Goal &amp; budget</h3>
          <label>Primary objective <span className="hint">— defines "converted"</span></label>
          <div className="chip-row">
            {GOALS.map((g) => (
              <Chip key={g} active={profile.goal === g} onClick={() => set("goal", g)}>{g}</Chip>
            ))}
          </div>
          <label>Budget — <strong>${profile.budget.toLocaleString()}</strong></label>
          <input type="range" min="2000" max="40000" step="500" value={profile.budget}
            onChange={(e) => set("budget", +e.target.value)} className="range-in" />
          <div className="derived">
            <div>
              <span className="derived-k">Funnel target</span>
              <span className="derived-v">{Math.round(profile.headcount / 0.6)} good-fits</span>
            </div>
            <div>
              <span className="derived-k">Cost / seat</span>
              <span className="derived-v">${Math.round(profile.budget / profile.headcount)}</span>
            </div>
          </div>
        </section>
      </div>

      <div className="stage-foot">
        <p className="foot-note">On submit, the agent pipeline fans out concurrently — then auto-sends outreach.</p>
        <button className="btn-primary" onClick={onRun}>Run agent pipeline <ArrowRight size={16} /></button>
      </div>
    </div>
  );
}

// ---- Stage 1: Pipeline --------------------------------------
function Pipeline({ profile, eventId, onResult, onError, onDone }) {
  const sources = [
    { key: "github", label: "GitHub adapter", icon: GitBranch, note: "OSS signal · clean API" },
    { key: "x", label: "X adapter", icon: Send, note: "Reach signal · paid API" },
    { key: "linkedin", label: "LinkedIn adapter", icon: BriefcaseBusiness, note: "Contact resolve · provider" },
  ];
  const steps = ["Prospecting", "Fit scoring", "Auto-outreach"];
  const [progress, setProgress] = useState(0);
  const [apiDone, setApiDone] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  // Cosmetic progress: walks up to 95% in ~2s, then sits there waiting on
  // the real /prospect call. In LLM mode that call can take 60-120s, so
  // jumping to 100% in 2s and looking "done" is misleading.
  useEffect(() => {
    const t = setInterval(() => {
      setProgress((p) => {
        const cap = 95;
        if (p >= cap) { clearInterval(t); return cap; }
        return p + 2;
      });
    }, 45);
    return () => clearInterval(t);
  }, []);

  // Wall-clock since mount, used to surface "still working" copy.
  useEffect(() => {
    const t = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // Snap to 100% the moment the API call returns.
  useEffect(() => {
    if (apiDone) setProgress(100);
  }, [apiDone]);

  // Fire ONLY /prospect — no outreach. The next stage owns sending,
  // per-prospect with explicit clicks. This prevents the old "intake →
  // mass-send" footgun.
  useEffect(() => {
    if (!eventId) { setApiDone(true); return; }
    let cancelled = false;
    (async () => {
      try {
        const result = await api.runProspect(eventId);
        if (!cancelled) {
          onResult && onResult(result);
          setApiDone(true);
        }
      } catch (e) {
        if (!cancelled) {
          if (e.status === 404) {
            onError && onError(
              "Event not found — the backend probably redeployed and wiped " +
              "the ephemeral SQLite store. Click Intake in the side rail to " +
              "create a new event."
            );
          } else {
            onError && onError(`Prospecting failed: ${e.message}`);
          }
          setApiDone(true);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [eventId, onResult, onError]);

  // only advance when BOTH the cosmetic timer and the real API finished
  useEffect(() => {
    if (progress >= 100 && apiDone) {
      const t = setTimeout(onDone, 650);
      return () => clearTimeout(t);
    }
  }, [progress, apiDone, onDone]);

  const funnelTarget = Math.round(profile.headcount / 0.6);
  const found = Math.round((progress / 100) * funnelTarget * 1.4);

  return (
    <div className="stage">
      <header className="stage-head">
        <p className="eyebrow">Stage 02 — Concurrent fan-out</p>
        <h1>Agents working the funnel</h1>
        <p className="lede">
          Per-prospect stages run concurrently across the pool. The final stage hands
          off to the outreach agent, which sends without a human in the loop. Matching
          and ROI wait as barriers until the pool resolves.
        </p>
      </header>

      <div className="pipe-sources">
        {sources.map((s, i) => {
          const Icon = s.icon;
          const local = Math.max(0, Math.min(100, progress * 1.1 - i * 6));
          return (
            <div className="pipe-card" key={s.key}>
              <div className="pipe-card-top">
                <Icon size={18} />
                <div>
                  <p className="pipe-card-label">{s.label}</p>
                  <p className="pipe-card-note">{s.note}</p>
                </div>
                <span className="pipe-pct">{Math.round(local)}%</span>
              </div>
              <div className="bar"><div className="bar-fill" style={{ width: `${local}%` }} /></div>
            </div>
          );
        })}
      </div>

      <div className="pipe-steps">
        {steps.map((st, i) => {
          const active = progress > i * 30;
          const complete = progress > (i + 1) * 30 + 10;
          return (
            <div key={st} className={`pipe-step ${active ? "on" : ""} ${complete ? "complete" : ""}`}>
              <span className="pipe-step-dot">{complete ? <Check size={12} strokeWidth={3} /> : <Circle size={8} />}</span>
              {st}
            </div>
          );
        })}
      </div>

      <div className="pipe-counter">
        <span className="pipe-counter-num">{found}</span>
        <span className="pipe-counter-lbl">candidates surfaced · target {funnelTarget} good-fits</span>
      </div>

      {progress >= 95 && !apiDone && (
        <div className="pipe-counter" style={{ opacity: 0.8, fontSize: "0.95rem" }}>
          <span className="pipe-counter-lbl">
            Still gathering ({elapsed}s elapsed). LLM-mode prospecting can take 60–120s
            — web_search across GitHub / LinkedIn / X plus a per-candidate ICP verdict.
            {elapsed > 90 && " Check the backend logs if this keeps growing."}
          </span>
        </div>
      )}
    </div>
  );
}

// ---- Stage 2: Auto-outreach ---------------------------------
function statusMeta(s) {
  if (s === "rsvp") return { label: "RSVP'd", cls: "st-rsvp" };
  if (s === "contacted") return { label: "Awaiting", cls: "st-contacted" };
  if (s === "below") return { label: "Below threshold", cls: "st-below" };
  return { label: s, cls: "" };
}

function Prospects({ profile, runResult, eventId, onError, onNext }) {
  // Use real backend prospects when /run has resolved; fall back to mock
  // so this component still renders if someone navigates directly to it.
  const useReal = !!runResult?.prospects;
  const PROS = useReal
    ? runResult.prospects.map((p) => ({
        id: p.id,
        name: p.name,
        role: p.role,
        company: p.company,
        side: p.side,
        score: p.fit_score,
        gh: p.gh_stars,
        x: p.x_followers,
        status: p.status,
        reason: p.fit_reason,
        offers: p.offers,
        seeks: p.seeks,
        worksOn: p.works_on,
      }))
    : PROSPECTS;
  const T = useReal ? runResult.event.threshold : THRESHOLD;

  const sorted = [...PROS].sort((a, b) => b.score - a.score);
  const aboveT = sorted.filter((p) => p.score >= T);
  // PROS can be empty when LLM mode is on and the relevance gate drops
  // every candidate. Seed `selected` from an optional chain so the hook
  // doesn't crash; the empty-state early return below catches the rest.
  const [selected, setSelected] = useState(sorted[0]?.id ?? null);
  const sel = PROS.find((p) => p.id === selected) || sorted[0] || null;

  // build the auto-outreach activity feed (interleaved rounds)
  const feed = [];
  aboveT.forEach((p) => feed.push({ t: "sent", p }));
  aboveT.forEach((p) => feed.push({ t: "open", p }));
  aboveT.forEach((p) => feed.push({ t: p.status === "rsvp" ? "rsvp" : "wait", p }));
  const [revealed, setRevealed] = useState(0);
  useEffect(() => {
    if (revealed >= feed.length) return;
    const t = setTimeout(() => setRevealed((r) => r + 1), revealed === 0 ? 250 : 360);
    return () => clearTimeout(t);
  }, [revealed, feed.length]);

  const shown = feed.slice(0, revealed);
  const sentN = shown.filter((f) => f.t === "sent").length;
  const rsvpN = shown.filter((f) => f.t === "rsvp").length;
  const otherRsvps = sel
    ? PROS.filter((x) => x.status === "rsvp" && x.id !== sel.id)
        .slice(0, 2).map((x) => x.name.split(" ")[0]).join(" and ")
    : "";

  // === backend-driven outreach review ===
  // previewById[prospect_id] = { note, message, payload, ... } from /outreach/preview
  // editsById[prospect_id]   = { note, message } — operator's in-flight edits
  // sendState[prospect_id]   = { status, kind, error } — per-prospect send tracking
  const [previewById, setPreviewById] = useState({});
  const [editsById, setEditsById] = useState({});
  const [sendState, setSendState] = useState({});
  const [providerInfo, setProviderInfo] = useState(null);

  useEffect(() => {
    // Skip the preview fetch when the pool is empty — the backend 409s
    // with "no prospects — call /prospect first" and we'd surface that as
    // a spurious error banner on top of the empty-state UI.
    if (!eventId || PROS.length === 0) return;
    let cancelled = false;
    (async () => {
      try {
        const pv = await api.previewOutreach(eventId);
        if (cancelled) return;
        const map = {};
        const edits = {};
        for (const row of pv.prospects) {
          map[row.prospect_id] = row;
          // seed the editable state with the agent's composition
          edits[row.prospect_id] = { note: row.note, message: row.message };
        }
        setPreviewById(map);
        setEditsById((cur) => ({ ...edits, ...cur }));  // don't clobber in-flight edits
        setProviderInfo({ provider: pv.provider, dry_run: pv.dry_run });
      } catch (e) {
        // 404 here means the event no longer exists on the backend —
        // almost always because Railway's container restarted and wiped
        // the ephemeral SQLite file. Tell the operator what to do instead
        // of leaving them staring at a bare red banner.
        if (e.status === 404) {
          onError && onError(
            "This event no longer exists on the server. The backend most " +
            "likely redeployed and wiped its ephemeral SQLite store. Click " +
            "Intake in the side rail and create a new event."
          );
        } else {
          onError && onError(`Couldn't load outreach preview: ${e.message}`);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [eventId, onError, PROS.length]);

  const updateEdit = (prospectId, field, value) => {
    setEditsById((s) => ({
      ...s,
      [prospectId]: { ...s[prospectId], [field]: value },
    }));
  };

  const resetEdit = (prospectId) => {
    const orig = previewById[prospectId];
    if (!orig) return;
    setEditsById((s) => ({
      ...s,
      [prospectId]: { note: orig.note, message: orig.message },
    }));
  };

  const fire = async (kind, prospectId) => {
    setSendState((s) => ({ ...s, [prospectId]: { status: "sending", kind } }));
    try {
      const fn = kind === "invite" ? api.sendInvite : api.sendDirectMessage;
      const edits = editsById[prospectId] || {};
      const res = await fn(eventId, prospectId, {
        note: edits.note,
        message: edits.message,
      });
      setSendState((s) => ({
        ...s,
        [prospectId]: {
          status: res.error ? "failed" : "sent",
          kind,
          state: res.state,
          error: res.error,
          dry_run: res.dry_run,
        },
      }));
    } catch (e) {
      setSendState((s) => ({
        ...s,
        [prospectId]: { status: "failed", kind, error: e.message },
      }));
    }
  };

  const selPreview = sel ? previewById[sel.id] : null;
  const selEdits = sel ? (editsById[sel.id] || { note: "", message: "" }) : { note: "", message: "" };
  const selSend = sel ? sendState[sel.id] : null;
  const isDirty = selPreview && (
    selEdits.note !== selPreview.note ||
    selEdits.message !== selPreview.message
  );

  const feedLabel = { sent: "Sent", open: "Opened", rsvp: "Replied — RSVP", wait: "Opened — awaiting reply" };
  const feedIcon = { sent: <Mail size={11} />, open: <Activity size={11} />, rsvp: <Check size={11} strokeWidth={3} />, wait: <Circle size={7} /> };

  // Empty state: prospecting completed but nothing passed the ICP gate.
  // Common cause is LLM mode dropping every candidate via judge_relevance,
  // or the LLM web_search calls erroring out. Render a useful explanation
  // instead of the (white-screen) crash that used to happen on sel.id.
  if (PROS.length === 0) {
    return (
      <div className="stage">
        <header className="stage-head">
          <p className="eyebrow">Stage 03 — Auto-outreach</p>
          <h1>No candidates surfaced</h1>
          <p className="lede">
            Prospecting completed but the ICP gate dropped every result. If you're
            running with an <code>ANTHROPIC_API_KEY</code> set, the relevance verdict
            judged none of the discovered profiles to be a strong match — try
            broadening the event's ICP (role, seniority, co-stage), or unset the key
            to fall back to the curated mock pool. Check the backend logs for
            per-candidate drop reasons.
          </p>
        </header>
      </div>
    );
  }

  return (
    <div className="stage">
      <header className="stage-head">
        <p className="eyebrow">Stage 03 — Fit scoring &amp; autonomous outreach</p>
        <h1>Scored pool, agent sends itself</h1>
        <p className="lede">
          Fit is a score with reasoning, not a binary — the threshold floats to hit
          funnel supply. Everything above it goes to the outreach agent, which
          personalizes on source signal, reveals composition, and sends. No manual step.
        </p>
      </header>

      <div className="agent-bar">
        <span className="agent-bar-live"><span className="live-dot" /> agent running</span>
        <span className="agent-stat"><strong>{sentN}</strong> / {aboveT.length} sent</span>
        <span className="agent-stat"><strong>{rsvpN}</strong> RSVP'd</span>
        <span className="agent-stat"><strong>0</strong> manual touches</span>
      </div>

      <div className="prospect-layout">
        <div className="prospect-list">
          <div className="list-head"><span>Candidate</span><span>Signal</span><span>Fit</span></div>
          {sorted.map((p) => {
            const m = statusMeta(p.status);
            return (
              <button key={p.id}
                className={`prospect-row ${selected === p.id ? "sel" : ""} ${p.score < T ? "dim" : ""}`}
                onClick={() => setSelected(p.id)}>
                <span className="pr-name">
                  <span className="pr-name-main">{p.name}
                    <span className={`side-tag ${SIDE_CLASS[p.side]}`}>{p.side}</span>
                  </span>
                  <span className="pr-role">{p.role} · {p.company}</span>
                </span>
                <span className="pr-signal">
                  <span className="sig"><GitBranch size={11} /> {fmtNum(p.gh)}</span>
                  <span className="sig"><Send size={11} /> {fmtNum(p.x)}</span>
                </span>
                <span className="pr-score">
                  <span className={`score-num ${p.score >= T ? "ok" : "no"}`}>{p.score}</span>
                  <span className={`st-tag ${m.cls}`}>{m.label}</span>
                </span>
              </button>
            );
          })}
          <div className="threshold-note">
            <span className="threshold-line" />
            Threshold {T} — floats with funnel supply ({Math.round(profile.headcount / 0.6)} target)
          </div>
        </div>

        <div className="prospect-side">
          <aside className="prospect-detail">
            <div className="pd-head">
              <div>
                <h3>{sel.name}</h3>
                <p>{sel.role} · {sel.company}</p>
              </div>
              <span className={`score-badge ${sel.score >= T ? "ok" : "no"}`}>{sel.score}</span>
            </div>
            <div className="pd-section">
              <p className="pd-label">Fit reasoning</p>
              <p className="pd-reason">{sel.reason}</p>
            </div>
            <div className="pd-section">
              <p className="pd-label">Value vectors</p>
              <div className="vectors">
                <div><span className="vec-k">offers</span><span className="vec-v">{sel.offers}</span></div>
                <div><span className="vec-k">seeks</span><span className="vec-v">{sel.seeks}</span></div>
              </div>
            </div>
            <div className="pd-section">
              <p className="pd-label">
                Agent outreach
                {providerInfo && (
                  <span className="prov-tag">
                    {providerInfo.provider}{providerInfo.dry_run ? " · dry-run" : " · LIVE"}
                  </span>
                )}
              </p>
              {sel.score >= T ? (
                <div className="outreach">
                  {selPreview ? (
                    <>
                      <p className="msg-label">
                        Connection note ({selEdits.note?.length || 0} / 300 chars)
                        {selEdits.note?.length > 300 && (
                          <span className="msg-warn"> · over LinkedIn's 300-char limit</span>
                        )}
                      </p>
                      <textarea className="msg-edit"
                                value={selEdits.note || ""}
                                onChange={(e) => updateEdit(sel.id, "note", e.target.value)}
                                rows={4} maxLength={400} />
                      <p className="msg-label">Post-accept DM</p>
                      <textarea className="msg-edit msg-edit-long"
                                value={selEdits.message || ""}
                                onChange={(e) => updateEdit(sel.id, "message", e.target.value)}
                                rows={8} />
                      <span className="outreach-tag">
                        <Zap size={11} /> composed by agent · edit before sending
                        {isDirty && (
                          <button className="btn-reset" onClick={() => resetEdit(sel.id)}>
                            reset to agent text
                          </button>
                        )}
                      </span>
                      <div className="send-row">
                        <button className="btn-send btn-send-invite"
                                disabled={!selPreview.eligible || selSend?.status === "sending"}
                                onClick={() => fire("invite", sel.id)}>
                          {selSend?.kind === "invite" && selSend.status === "sending"
                            ? "Sending invite…"
                            : <>Send invite <ArrowRight size={14} /></>}
                        </button>
                        <button className="btn-send btn-send-dm"
                                disabled={!selPreview.eligible || selSend?.status === "sending"}
                                onClick={() => fire("dm", sel.id)}>
                          {selSend?.kind === "dm" && selSend.status === "sending"
                            ? "Sending DM…"
                            : <>Send DM (if connected) <ArrowRight size={14} /></>}
                        </button>
                      </div>
                      {selSend && selSend.status === "sent" && (
                        <div className="send-result ok">
                          <Check size={11} strokeWidth={3} /> {selSend.kind === "invite" ? "Invite" : "DM"} sent
                          {selSend.dry_run && <span> · dry-run</span>}
                          {selSend.state && <span> · state: {selSend.state}</span>}
                        </div>
                      )}
                      {selSend && selSend.status === "failed" && (
                        <div className="send-result err">
                          ⚠ {selSend.kind === "invite" ? "Invite" : "DM"} failed: {selSend.error}
                        </div>
                      )}
                      {!selPreview.eligible && (
                        <div className="send-result muted">Skipped: {selPreview.skip_reason}</div>
                      )}
                    </>
                  ) : (
                    <p className="muted-text">Loading composed messages…</p>
                  )}
                </div>
              ) : (
                <div className="outreach muted">
                  <p>Held by the agent — below fit threshold for this event. Routed to the
                    pool for a future event with a matching ICP.</p>
                </div>
              )}
            </div>
          </aside>

          <aside className="agent-feed">
            <p className="pd-label">Live agent activity</p>
            <div className="feed-scroll">
              {shown.slice().reverse().map((f, i) => (
                <div className={`feed-row fr-${f.t}`} key={revealed - i}>
                  <span className="feed-icon">{feedIcon[f.t]}</span>
                  <span className="feed-text">{feedLabel[f.t]}</span>
                  <span className="feed-name">{f.p.name}</span>
                </div>
              ))}
              {revealed < feed.length && (
                <div className="feed-row fr-pending">
                  <span className="feed-icon"><Circle size={7} /></span>
                  <span className="feed-text">working…</span>
                </div>
              )}
            </div>
          </aside>
        </div>
      </div>

      <div className="stage-foot">
        <p className="foot-note">
          {aboveT.length} of {PROS.length} above threshold · agent sent every one · {PROS.filter((p) => p.status === "rsvp").length} RSVP'd
        </p>
        <button className="btn-primary" onClick={onNext}>Build guest list <ArrowRight size={16} /></button>
      </div>
    </div>
  );
}

// ---- Stage 3: Symbiotic matching ----------------------------
function Matching({ profile, onNext }) {
  const groupWord = FORMAT_CONFIG[profile.format].group;
  const attending = PROSPECTS.filter((p) => p.status === "rsvp");
  const groups = [...new Set(attending.map((p) => p.grp))].sort((a, b) => a - b);

  const centerFor = (i) => {
    if (groups.length === 1) return [300, 165];
    if (groups.length === 2) return i === 0 ? [195, 170] : [415, 170];
    return [[185, 145], [415, 145], [300, 295]][i];
  };
  const nodes = [];
  groups.forEach((g, gi) => {
    const [cx, cy] = centerFor(gi);
    const members = attending.filter((p) => p.grp === g);
    members.forEach((p, idx) => {
      const n = members.length;
      const ang = -Math.PI / 2 + (idx / n) * Math.PI * 2;
      const r = n === 1 ? 0 : 52;
      nodes.push({ ...p, x: cx + Math.cos(ang) * r, y: cy + Math.sin(ang) * r });
    });
  });
  const nodeById = (id) => nodes.find((n) => n.id === id);

  // edges: symbiotic when sides differ (offer<->seek), affinity when same side
  const edges = [];
  for (let i = 0; i < attending.length; i++) {
    for (let j = i + 1; j < attending.length; j++) {
      const a = attending[i], b = attending[j];
      const sym = a.side !== b.side;
      edges.push({
        a: a.id, b: b.id,
        type: sym ? "sym" : "aff",
        cross: a.grp !== b.grp,
        w: (a.score + b.score) / 2,
      });
    }
  }
  const symPairs = edges.filter((e) => e.type === "sym" && !e.cross)
    .sort((x, y) => y.w - x.w).slice(0, 4);

  return (
    <div className="stage">
      <header className="stage-head">
        <p className="eyebrow">Stage 04 — Symbiotic matching market</p>
        <h1>Guest list as a value graph</h1>
        <p className="lede">
          Edges aren't friendship — they're <em>predicted mutual value</em>. Nodes
          are people; edges split into <em>symbiotic</em> (offer meets seek — a
          builder and someone hiring them, a founder and an investor) and{" "}
          <em>affinity</em> (worked on similar things). The objective is{" "}
          <em>multi-sided</em>: a weighted sum across attendees <em>and</em> the
          host/sponsor side, not just attendee-to-attendee gain.
        </p>
        <p className="lede">
          Two passes. <em>Pre-RSVP</em> — community detection over the value
          graph picks who to even invite. <em>Post-RSVP</em> — given who actually
          said yes, a seating/assignment optimizer decides who sits near whom.
          Different inputs, different timing; the second can't run until RSVPs
          land. {groupWord}s are formed to maximize the multi-sided objective.{" "}
          {FORMAT_CONFIG[profile.format].topo}.
        </p>
      </header>

      <div className="match-layout">
        <div className="graph-wrap">
          <svg viewBox="0 0 600 340" className="graph">
            {groups.map((g, gi) => {
              const [cx, cy] = centerFor(gi);
              return (
                <g key={g}>
                  <circle cx={cx} cy={cy} r="86" className="hull" />
                  <text x={cx} y={cy - 98} className="hull-label">{groupWord} {g}</text>
                </g>
              );
            })}
            {edges.map((e, i) => {
              const a = nodeById(e.a), b = nodeById(e.b);
              return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                className={`edge edge-${e.type} ${e.cross ? "edge-cross" : ""}`} />;
            })}
            {nodes.map((n) => (
              <g key={n.id}>
                <circle cx={n.x} cy={n.y} r="21" className={`node node-${SIDE_CLASS[n.side]}`} />
                <text x={n.x} y={n.y + 1} className="node-init">
                  {n.name.split(" ").map((w) => w[0]).join("")}
                </text>
                <text x={n.x} y={n.y + 35} className="node-name">{n.name.split(" ")[0]}</text>
              </g>
            ))}
          </svg>
          <div className="legend">
            <span><i className="lg-sym" /> symbiotic value</span>
            <span><i className="lg-aff" /> affinity</span>
            <span><i className="lg-build" /> Builds</span>
            <span><i className="lg-hire" /> Hires</span>
          </div>
        </div>

        <div className="match-side">
          <div className="sym-panel">
            <p className="pd-label">Top symbiotic pairs</p>
            {symPairs.map((e, i) => {
              const a = PROSPECTS.find((p) => p.id === e.a);
              const b = PROSPECTS.find((p) => p.id === e.b);
              return (
                <div className="sym-pair" key={i}>
                  <div className="sym-names">
                    {a.name.split(" ")[0]} <span className="sym-link">⟷</span> {b.name.split(" ")[0]}
                    <span className="sym-w">{Math.round(e.w)}</span>
                  </div>
                  <div className="sym-flow">{a.offers} <span>↔</span> {b.seeks}</div>
                  <div className="sym-flow">{b.offers} <span>↔</span> {a.seeks}</div>
                </div>
              );
            })}
          </div>

          <div className="tables-panel">
            {groups.map((g) => {
              const grp = attending.filter((p) => p.grp === g);
              const builds = grp.filter((p) => p.side === "Builds").length;
              const hires = grp.filter((p) => p.side === "Hires").length;
              return (
                <div key={g} className="table-card">
                  <div className="table-card-head">
                    <span className="table-dot" /> {groupWord} {g}
                    <span className="table-count">{grp.length}</span>
                  </div>
                  {grp.map((p) => (
                    <div key={p.id} className="table-guest">
                      <span>{p.name}</span>
                      <span className={`side-tag sm ${SIDE_CLASS[p.side]}`}>{p.side}</span>
                    </div>
                  ))}
                  <p className="table-rationale">
                    {builds} building · {hires} hiring — complementary sides seated together so
                    every offer meets a seek.
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="stage-foot">
        <p className="foot-note">
          {attending.length} confirmed · {groups.length} {groupWord.toLowerCase()}s · objective = weighted Σ over attendees + host side · affinity as tiebreak
        </p>
        <button className="btn-primary" onClick={onNext}>Settle ROI <ArrowRight size={16} /></button>
      </div>
    </div>
  );
}

// ---- Stage 4: ROI ledger ------------------------------------
function tierOf(score) { return score >= 90 ? "high" : score >= 82 ? "mid" : "low"; }

function ROI({ profile, onRestart }) {
  const cfg = GOAL_CONFIG[profile.goal];
  const attending = PROSPECTS.filter((p) => p.status === "rsvp");
  const ledger = attending.map((p) => {
    const tier = cfg.tiers[tierOf(p.score)];
    return { ...p, ...tier, value: cfg.value[tier.state] };
  });

  const invited = Math.round(profile.headcount / 0.6);
  const rsvp = Math.round(invited * 0.62);
  const attended = attending.length;
  const valueGenerated = ledger.reduce((s, g) => s + g.value, 0);
  const roi = (valueGenerated - profile.budget) / profile.budget;
  const wonN = ledger.filter((g) => g.state === "won").length;

  const funnel = [
    { k: "Invited", v: invited, w: 100 },
    { k: "RSVP'd", v: rsvp, w: (rsvp / invited) * 100 },
    { k: "Attended", v: attended, w: (attended / invited) * 100 },
    { k: "Converted", v: wonN, w: (wonN / invited) * 100 },
  ];

  return (
    <div className="stage">
      <header className="stage-head">
        <p className="eyebrow">Stage 05 — Verified settlement</p>
        <h1>Who actually converted</h1>
        <p className="lede">
          ROI settles against the goal set in intake — here, <em>{profile.goal.toLowerCase()}</em>.
          Check-in data confirms attendance; 30/60/90-day follow-up verifies each guest's
          outcome. The ledger is the deliverable: a guest list, scored by what they converted to.
        </p>
      </header>

      <div className="roi-top">
        <div className="roi-hero">
          <span className="roi-hero-label">Net ROI · {profile.goal}</span>
          <span className="roi-hero-num">{(roi * 100).toFixed(0)}%</span>
          <span className="roi-hero-sub">
            {fmtK(valueGenerated)} verified value · ${profile.budget.toLocaleString()} spent
          </span>
        </div>
        <div className="roi-funnel">
          {funnel.map((f) => (
            <div className="rf-row" key={f.k}>
              <span className="rf-k">{f.k}</span>
              <div className="rf-bar"><div className="rf-fill" style={{ width: `${f.w}%` }} /></div>
              <span className="rf-v">{f.v}</span>
            </div>
          ))}
          <div className="rf-foot">{wonN} of {attended} attendees converted to goal</div>
        </div>
      </div>

      <div className="ledger">
        <div className="ledger-head">
          <span>Guest</span><span>Side</span><span>{cfg.ledgerHead}</span><span>Verified value</span>
        </div>
        {ledger.sort((a, b) => b.value - a.value).map((g) => (
          <div className={`ledger-row led-${g.state}`} key={g.id}>
            <span className="led-guest">
              <span className="led-name">{g.name}</span>
              <span className="led-co">{g.company.split(" (")[0]}</span>
            </span>
            <span><span className={`side-tag sm ${SIDE_CLASS[g.side]}`}>{g.side}</span></span>
            <span className="led-outcome">
              <span className={`led-pill led-pill-${g.state}`}>{g.label}</span>
              <span className="led-detail">{g.detail}</span>
            </span>
            <span className="led-value">{g.value > 0 ? fmtK(g.value) : "—"}</span>
          </div>
        ))}
        <div className="ledger-foot">
          <span>Total verified value</span>
          <span className="ledger-total">{fmtK(valueGenerated)}</span>
        </div>
      </div>

      <div className="stage-foot">
        <p className="foot-note">
          Per-guest outcomes feed reputation scores — staking who gets invited to the next event.
        </p>
        <button className="btn-primary" onClick={onRestart}><RotateCw size={15} /> Run another event</button>
      </div>
    </div>
  );
}

// ---- root ---------------------------------------------------
export default function App() {
  const [stage, setStage] = useState(0);
  const [maxReached, setMaxReached] = useState(0);
  const [profile, setProfile] = useState({
    role: "Infrastructure / ML platform engineers",
    seniority: "Staff+",
    coStage: "Seed",
    headcount: 40,
    format: "Sit-down dinner",
    city: "San Francisco",
    goal: "Hiring pipeline",
    budget: 12000,
  });
  // backend-wired state — eventId comes from real /events POST; runResult is
  // the response from /run (prospects, counts, etc.). Both null until the
  // user runs the flow.
  const [eventId, setEventId] = useState(null);
  const [runResult, setRunResult] = useState(null);
  const [apiError, setApiError] = useState(null);
  const go = (s) => { setStage(s); setMaxReached((m) => Math.max(m, s)); };

  const handleIntakeRun = async () => {
    setApiError(null);
    try {
      const ev = await api.createEvent({
        role: profile.role,
        seniority: profile.seniority,
        co_stage: profile.coStage,
        headcount: profile.headcount,
        format: profile.format,
        city: profile.city,
        goal: profile.goal,
        budget: profile.budget,
      });
      setEventId(ev.id);
      go(1);
    } catch (e) {
      setApiError(`Couldn't create event: ${e.message}`);
    }
  };

  const restart = () => {
    setEventId(null);
    setRunResult(null);
    setApiError(null);
    go(0);
  };

  return (
    <div className="root">
      <style>{CSS}</style>
      <div className="frame">
        <header className="topbar">
          <div className="brand">
            <img className="brand-logo" src="/surplus-logo.png" alt="Surplus logo" />
            <div className="brand-text">
              <span className="brand-name">surplus</span>
            </div>
            {eventId && (
              <span className="live-badge" title="connected to backend">
                event #{eventId} · live
              </span>
            )}
          </div>
          <StageRail stage={stage} setStage={go} maxReached={maxReached} />
        </header>
        {apiError && (
          <div className="api-error">{apiError}</div>
        )}
        <main className="canvas" key={stage}>
          {stage === 0 && <Intake profile={profile} setProfile={setProfile} onRun={handleIntakeRun} />}
          {stage === 1 && <Pipeline profile={profile} eventId={eventId}
                                    onResult={setRunResult}
                                    onError={setApiError}
                                    onDone={() => go(2)} />}
          {stage === 2 && <Prospects profile={profile} runResult={runResult}
                                       eventId={eventId} onError={setApiError}
                                       onNext={() => go(3)} />}
          {stage === 3 && <Matching profile={profile} onNext={() => go(4)} />}
          {stage === 4 && <ROI profile={profile} onRestart={restart} />}
        </main>
      </div>
    </div>
  );
}

// ---- styles -------------------------------------------------
const CSS = `
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Plus+Jakarta+Sans:ital,wght@0,400;0,500;0,600;0,700;1,500&display=swap');
* { box-sizing:border-box; margin:0; padding:0; }
.root {
  --bg:#f6f7f9; --panel:#ffffff; --panel-2:#fbfcfd; --panel-3:#f1f3f6;
  --line:#e4e8ee; --line-soft:#edf1f5;
  --ink:#1f1c2e; --ink-dim:#5f5b73; --ink-faint:#9b96ac;
  --acc:#6b46e0; --acc-deep:#5836c6; --acc-soft:#ede9fb; --acc-light:#9d8ae8;
  --ok:#1f9d6b; --ok-soft:#e3f4ec; --no:#d8654f; --no-soft:#fbe9e4;
  --build:#6b46e0; --hire:#3f7fd6; --op:#cf5fa6;
  --shadow:0 8px 30px rgba(76,52,143,0.08); --shadow-sm:0 3px 14px rgba(76,52,143,0.06);
  --r-card:16px; --r-panel:13px; --r-el:10px; --r-pill:999px;
  font-family:'Plus Jakarta Sans',system-ui,sans-serif; background:var(--bg);
  color:var(--ink); min-height:100vh; padding:24px;
  background-image:none;
}
.frame { max-width:1080px; margin:0 auto; }
.topbar {
  display:flex; align-items:center; justify-content:space-between;
  padding:15px 22px; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); box-shadow:var(--shadow-sm);
  flex-wrap:wrap; gap:14px; margin-bottom:18px;
}
.brand { display:flex; align-items:center; gap:12px; isolation:isolate; }
.brand-logo { width:44px; height:44px; display:block; object-fit:contain;
  mix-blend-mode:multiply; filter:drop-shadow(0 8px 18px rgba(108,67,217,0.14)); }
.brand-text { min-height:44px; display:flex; flex-direction:column; justify-content:center; gap:1px; }
.brand-name { font-family:'Inter',system-ui,sans-serif; font-weight:800;
  letter-spacing:-0.05em; font-size:1.85rem; line-height:1; color:var(--ink); }
.brand-sub { font-size:11px; color:var(--ink-faint); line-height:1.2; }
.live-badge { margin-left:14px; padding:4px 10px; border-radius:var(--r-pill);
  font-size:10.5px; font-weight:600; letter-spacing:0.02em; text-transform:uppercase;
  background:var(--acc-soft); color:var(--acc);
  border:1px solid rgba(108,67,217,0.18); }
.api-error { padding:10px 18px; background:#fff5f5; color:#b03030;
  border-bottom:1px solid #f3d6d6; font-size:13px; font-weight:500; }
.rail { display:flex; gap:5px; flex-wrap:wrap; }
.rail-item { display:flex; align-items:center; gap:7px; background:transparent;
  border:1px solid transparent; color:var(--ink-faint); padding:7px 12px; cursor:pointer;
  font-family:inherit; font-size:11.5px; font-weight:500; border-radius:var(--r-pill);
  transition:all 0.18s; }
.rail-item:disabled { cursor:not-allowed; opacity:0.45; }
.rail-item:not(:disabled):hover { color:var(--acc); background:var(--acc-soft); }
.rail-item.active { color:#fff; background:var(--acc); border-color:var(--acc);
  box-shadow:0 4px 12px rgba(108,67,217,0.3); }
.rail-item.done { color:var(--ink-dim); }
.rail-dot { display:flex; }
.rail-idx { font-size:9px; opacity:0.6; }
.canvas { animation:fade 0.4s ease; }
@keyframes fade { from{opacity:0;transform:translateY(6px);} to{opacity:1;transform:none;} }
.stage { display:flex; flex-direction:column; gap:22px; }
.stage-head { max-width:730px; }
.eyebrow { display:inline-block; font-size:10.5px; letter-spacing:0.12em; color:var(--acc);
  text-transform:uppercase; font-weight:700; margin-bottom:14px; background:var(--acc-soft);
  padding:6px 12px; border-radius:var(--r-pill); }
.stage-head h1 { font-family:'Playfair Display',Georgia,serif; font-weight:600; font-size:40px;
  line-height:1.1; letter-spacing:-0.01em; margin-bottom:12px; color:var(--ink); }
.lede { font-size:13.5px; line-height:1.7; color:var(--ink-dim); }
.lede em { color:var(--acc); font-style:normal; font-weight:600; }
.form-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:20px 18px; display:flex; flex-direction:column; gap:11px; box-shadow:var(--shadow-sm); }
.card h3 { font-size:13px; font-weight:700; letter-spacing:-0.01em; display:flex;
  align-items:center; gap:9px; margin-bottom:4px; color:var(--ink); }
.card-num { width:21px; height:21px; background:var(--acc-soft); border-radius:7px;
  color:var(--acc); display:grid; place-items:center; font-size:11px; font-weight:700; }
.card label { font-size:10px; letter-spacing:0.04em; color:var(--ink-faint);
  text-transform:uppercase; font-weight:600; margin-top:4px; }
.card label strong { color:var(--acc); }
.hint { text-transform:none; letter-spacing:0; color:var(--ink-faint); font-weight:400; }
.text-in { background:var(--panel-2); border:1px solid var(--line); border-radius:var(--r-el);
  color:var(--ink); font-family:inherit; font-size:12.5px; padding:10px 12px; }
.text-in:focus { outline:none; border-color:var(--acc); background:#fff;
  box-shadow:0 0 0 3px var(--acc-soft); }
.chip-row { display:flex; flex-wrap:wrap; gap:7px; }
.chip { background:var(--panel-2); border:1px solid var(--line); color:var(--ink-dim);
  font-family:inherit; font-size:11.5px; font-weight:500; padding:7px 11px;
  border-radius:var(--r-pill); cursor:pointer; transition:all 0.15s; }
.chip:hover { border-color:var(--acc-light); color:var(--acc); }
.chip-on { background:var(--acc); border-color:var(--acc); color:#fff; font-weight:600;
  box-shadow:0 3px 10px rgba(108,67,217,0.25); }
.range-in { width:100%; accent-color:var(--acc); cursor:pointer; }
.topo-inline { font-size:10.5px; color:var(--ink-faint); display:flex; align-items:center; gap:5px; }
.derived { display:flex; gap:12px; margin-top:6px; padding-top:13px; border-top:1px dashed var(--line); }
.derived > div { flex:1; display:flex; flex-direction:column; gap:3px; }
.derived-k { font-size:9px; color:var(--ink-faint); text-transform:uppercase;
  letter-spacing:0.04em; font-weight:600; }
.derived-v { font-size:15px; color:var(--acc); font-weight:700; }
.stage-foot { display:flex; align-items:center; justify-content:space-between;
  border-top:1px solid var(--line); padding-top:20px; gap:16px; }
.foot-note { font-size:11.5px; color:var(--ink-faint); }
.btn-primary { background:var(--acc); color:#fff; border:none; font-family:inherit;
  font-size:12.5px; font-weight:700; padding:12px 20px; cursor:pointer; display:flex;
  align-items:center; gap:8px; letter-spacing:-0.01em; border-radius:var(--r-el);
  box-shadow:0 6px 16px rgba(108,67,217,0.3); transition:all 0.16s; white-space:nowrap; }
.btn-primary:hover { background:var(--acc-deep); transform:translateY(-1px);
  box-shadow:0 8px 20px rgba(108,67,217,0.38); }
.pipe-sources { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
.pipe-card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:16px; box-shadow:var(--shadow-sm); }
.pipe-card-top { display:flex; align-items:center; gap:11px; margin-bottom:13px; color:var(--ink-dim); }
.pipe-card-top > div { flex:1; }
.pipe-card-label { font-size:12.5px; color:var(--ink); font-weight:600; }
.pipe-card-note { font-size:10px; color:var(--ink-faint); }
.pipe-pct { font-size:13px; color:var(--acc); font-weight:700; }
.bar { height:5px; background:var(--panel-3); border-radius:var(--r-pill); overflow:hidden; }
.bar-fill { height:100%; background:linear-gradient(90deg,var(--acc-light),var(--acc));
  border-radius:var(--r-pill); transition:width 0.2s linear; }
.pipe-steps { display:flex; gap:10px; flex-wrap:wrap; }
.pipe-step { display:flex; align-items:center; gap:8px; font-size:11.5px; color:var(--ink-faint);
  border:1px solid var(--line); border-radius:var(--r-pill); padding:9px 14px;
  background:var(--panel); font-weight:500; transition:all 0.3s; }
.pipe-step.on { color:var(--ink); border-color:var(--acc-light); }
.pipe-step.complete { color:var(--acc); border-color:var(--acc); background:var(--acc-soft); }
.pipe-counter { display:flex; align-items:baseline; gap:14px; padding:22px;
  background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow-sm); }
.pipe-counter-num { font-size:46px; color:var(--acc); font-weight:800; letter-spacing:-0.02em; }
.pipe-counter-lbl { font-size:12px; color:var(--ink-dim); }
.agent-bar { display:flex; align-items:center; gap:20px; background:var(--panel);
  border:1px solid var(--line); border-radius:var(--r-card); padding:13px 18px;
  box-shadow:var(--shadow-sm); flex-wrap:wrap; }
.agent-bar-live { display:flex; align-items:center; gap:7px; font-size:11px; color:var(--ok);
  text-transform:uppercase; letter-spacing:0.05em; font-weight:700; }
.live-dot { width:7px; height:7px; border-radius:50%; background:var(--ok);
  animation:pulse 1.4s infinite; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.25;} }
.agent-stat { font-size:11.5px; color:var(--ink-faint); }
.agent-stat strong { color:var(--ink); font-size:13px; font-weight:700; }
.prospect-layout { display:grid; grid-template-columns:1.25fr 1fr; gap:16px; }
.prospect-list { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); box-shadow:var(--shadow-sm); align-self:start; overflow:hidden; }
.list-head { display:grid; grid-template-columns:1fr auto auto; gap:14px; padding:13px 16px;
  border-bottom:1px solid var(--line); font-size:9px; letter-spacing:0.06em;
  text-transform:uppercase; color:var(--ink-faint); font-weight:700; }
.prospect-row { display:grid; grid-template-columns:1fr auto auto; gap:14px; align-items:center;
  width:100%; background:transparent; border:none; border-bottom:1px solid var(--line-soft);
  padding:13px 16px; cursor:pointer; font-family:inherit; text-align:left; transition:background 0.14s; }
.prospect-row:hover { background:var(--panel-2); }
.prospect-row.sel { background:var(--acc-soft); box-shadow:inset 3px 0 0 var(--acc); }
.prospect-row.dim { opacity:0.55; }
.pr-name { display:flex; flex-direction:column; gap:3px; }
.pr-name-main { font-size:12.5px; color:var(--ink); font-weight:600; display:flex;
  align-items:center; gap:7px; }
.pr-role { font-size:10px; color:var(--ink-faint); }
.pr-signal { display:flex; gap:9px; }
.sig { font-size:10px; color:var(--ink-dim); display:flex; align-items:center; gap:3px; }
.pr-score { display:flex; flex-direction:column; align-items:flex-end; gap:4px; }
.score-num { font-size:14px; font-weight:800; }
.score-num.ok { color:var(--ok); } .score-num.no { color:var(--no); }
.st-tag { font-size:8px; letter-spacing:0.03em; text-transform:uppercase; padding:3px 7px;
  border-radius:var(--r-pill); font-weight:700; }
.st-rsvp { background:var(--ok-soft); color:var(--ok); }
.st-contacted { background:#e7eefb; color:var(--hire); }
.st-below { background:var(--no-soft); color:var(--no); }
.side-tag { font-size:8px; letter-spacing:0.03em; text-transform:uppercase; padding:3px 7px;
  border-radius:var(--r-pill); font-weight:700; white-space:nowrap; }
.side-build { color:var(--build); background:rgba(107,70,224,0.1); }
.side-hire { color:var(--hire); background:rgba(63,127,214,0.1); }
.side-op { color:var(--op); background:rgba(207,95,166,0.1); }
.threshold-note { font-size:10px; color:var(--ink-faint); padding:13px 16px;
  display:flex; align-items:center; gap:8px; }
.threshold-line { flex:1; height:1px;
  background:repeating-linear-gradient(90deg,var(--line) 0 4px,transparent 4px 8px); }
.prospect-side { display:flex; flex-direction:column; gap:16px; }
.prospect-detail { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); padding:20px; box-shadow:var(--shadow-sm); }
.pd-head { display:flex; justify-content:space-between; align-items:flex-start;
  gap:12px; margin-bottom:15px; }
.pd-head h3 { font-size:18px; font-weight:700; letter-spacing:-0.01em; }
.pd-head p { font-size:11px; color:var(--ink-faint); margin-top:3px; }
.score-badge { font-size:18px; font-weight:800; padding:7px 12px; border-radius:var(--r-el);
  border:1px solid; }
.score-badge.ok { color:var(--ok); border-color:var(--ok); background:var(--ok-soft); }
.score-badge.no { color:var(--no); border-color:var(--no); background:var(--no-soft); }
.pd-section { margin-top:15px; }
.pd-label { font-size:9px; letter-spacing:0.08em; text-transform:uppercase;
  color:var(--ink-faint); margin-bottom:8px; font-weight:700; }
.pd-reason { font-size:12px; line-height:1.65; color:var(--ink-dim); }
.vectors { display:flex; flex-direction:column; gap:7px; }
.vectors > div { display:flex; gap:10px; align-items:baseline; }
.vec-k { font-size:9px; text-transform:uppercase; letter-spacing:0.05em; color:var(--ink-faint);
  width:42px; flex-shrink:0; font-weight:700; }
.vec-v { font-size:11.5px; color:var(--ink); font-weight:500; }
.outreach { background:var(--panel-2); border:1px solid var(--line); border-radius:var(--r-panel);
  padding:14px; display:flex; flex-direction:column; gap:9px; }
.outreach p { font-size:11px; line-height:1.65; color:var(--ink-dim); }
.outreach.muted p { color:var(--ink-faint); }
.outreach-status { font-size:9px; color:var(--ok); display:flex; align-items:center; gap:5px;
  text-transform:uppercase; letter-spacing:0.04em; font-weight:700; }
.outreach-tag { font-size:9px; color:var(--ink-faint); display:flex; align-items:center; gap:5px;
  text-transform:uppercase; letter-spacing:0.03em; margin-top:2px; font-weight:600; }
.msg-label { font-size:9px !important; color:var(--ink-faint) !important; text-transform:uppercase;
  letter-spacing:0.05em; font-weight:700; margin-top:8px; }
.msg-body { font-size:11.5px !important; color:var(--ink) !important; line-height:1.55 !important;
  white-space:pre-wrap; background:#fff; border:1px solid var(--line); border-radius:8px;
  padding:10px 12px; }
.msg-edit { width:100%; font-family:'Inter',system-ui,sans-serif; font-size:11.5px;
  color:var(--ink); line-height:1.55; background:#fff; border:1px solid var(--line);
  border-radius:8px; padding:10px 12px; resize:vertical; box-sizing:border-box;
  transition:border-color 0.15s, box-shadow 0.15s; }
.msg-edit:focus { outline:none; border-color:var(--acc);
  box-shadow:0 0 0 3px rgba(108,67,217,0.12); }
.msg-edit-long { min-height:120px; }
.msg-warn { color:#b03030; font-weight:600; }
.btn-reset { margin-left:auto; background:transparent; border:none; color:var(--acc);
  font-family:inherit; font-size:9px; font-weight:600; cursor:pointer;
  text-transform:uppercase; letter-spacing:0.04em; padding:0; }
.btn-reset:hover { text-decoration:underline; }
.muted-text { color:var(--ink-faint); font-size:11px; font-style:italic; }
.prov-tag { margin-left:8px; padding:2px 7px; background:var(--acc-soft); color:var(--acc);
  border-radius:var(--r-pill); font-size:9px; font-weight:700; letter-spacing:0.04em;
  text-transform:uppercase; }
.send-row { display:flex; gap:8px; margin-top:6px; }
.btn-send { flex:1; padding:9px 12px; border-radius:8px; border:1px solid var(--acc);
  background:var(--acc); color:#fff; font-family:inherit; font-size:11.5px; font-weight:600;
  cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:6px;
  transition:all 0.18s; }
.btn-send:hover:not(:disabled) { transform:translateY(-1px); box-shadow:0 4px 12px rgba(108,67,217,0.25); }
.btn-send:disabled { opacity:0.5; cursor:not-allowed; }
.btn-send-dm { background:#fff; color:var(--acc); }
.send-result { margin-top:6px; font-size:10.5px; padding:7px 10px; border-radius:6px;
  display:flex; align-items:center; gap:6px; font-weight:600; }
.send-result.ok { background:#e9f7ec; color:#1f7a3e; }
.send-result.err { background:#fff5f5; color:#b03030; }
.send-result.muted { background:var(--panel-2); color:var(--ink-faint); font-weight:500; }
.agent-feed { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:18px; box-shadow:var(--shadow-sm); }
.feed-scroll { display:flex; flex-direction:column; gap:6px; max-height:176px; overflow:hidden; }
.feed-row { display:flex; align-items:center; gap:9px; font-size:10px; padding:7px 10px;
  background:var(--panel-2); border-radius:var(--r-el); border-left:2px solid var(--line);
  animation:fade 0.3s ease; }
.feed-icon { display:flex; color:var(--ink-faint); }
.feed-text { color:var(--ink-dim); font-weight:500; }
.feed-name { margin-left:auto; color:var(--ink); font-weight:600; }
.fr-sent { border-left-color:var(--ink-faint); }
.fr-open { border-left-color:var(--hire); }
.fr-open .feed-icon { color:var(--hire); }
.fr-rsvp { border-left-color:var(--ok); }
.fr-rsvp .feed-icon, .fr-rsvp .feed-text { color:var(--ok); }
.fr-wait { border-left-color:var(--ink-faint); opacity:0.7; }
.fr-pending { opacity:0.5; border-left-style:dashed; }
.match-layout { display:grid; grid-template-columns:1.35fr 1fr; gap:16px; }
.graph-wrap { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:12px; box-shadow:var(--shadow-sm); }
.graph { width:100%; height:auto; display:block; }
.hull { fill:rgba(108,67,217,0.035); stroke:var(--line); stroke-dasharray:3 4; }
.hull-label { fill:var(--ink-faint); font-size:9px; letter-spacing:0.1em; text-anchor:middle;
  font-family:'Plus Jakarta Sans',sans-serif; text-transform:uppercase; font-weight:700; }
.edge { stroke-linecap:round; }
.edge-sym { stroke:var(--acc); stroke-width:2; opacity:0.5; }
.edge-aff { stroke:var(--ink-faint); stroke-width:1; opacity:0.35; stroke-dasharray:2 3; }
.edge-cross { opacity:0.13; }
.node { stroke-width:1.5; }
.node-side-build { fill:rgba(107,70,224,0.12); stroke:var(--build); }
.node-side-hire { fill:rgba(63,127,214,0.12); stroke:var(--hire); }
.node-side-op { fill:rgba(207,95,166,0.12); stroke:var(--op); }
.node-init { fill:var(--ink); font-size:10px; font-weight:700; text-anchor:middle;
  font-family:'Plus Jakarta Sans',sans-serif; }
.node-name { fill:var(--ink-faint); font-size:9px; text-anchor:middle;
  font-family:'Plus Jakarta Sans',sans-serif; font-weight:500; }
.legend { display:flex; flex-wrap:wrap; gap:16px; padding:10px 6px 4px; }
.legend span { font-size:9px; color:var(--ink-faint); display:flex; align-items:center; gap:5px;
  text-transform:uppercase; letter-spacing:0.03em; font-weight:600; }
.legend i { width:14px; height:0; display:inline-block; }
.lg-sym { border-top:2px solid var(--acc); }
.lg-aff { border-top:1px dashed var(--ink-faint); }
.lg-build { width:9px; height:9px; border-radius:50%; background:rgba(107,70,224,0.15);
  border:1.5px solid var(--build); }
.lg-hire { width:9px; height:9px; border-radius:50%; background:rgba(63,127,214,0.15);
  border:1.5px solid var(--hire); }
.match-side { display:flex; flex-direction:column; gap:14px; }
.sym-panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:16px; box-shadow:var(--shadow-sm); }
.sym-pair { background:var(--panel-2); border:1px solid var(--line); border-radius:var(--r-panel);
  padding:11px 12px; margin-bottom:8px; }
.sym-pair:last-child { margin-bottom:0; }
.sym-names { font-size:12px; color:var(--ink); font-weight:600; display:flex;
  align-items:center; gap:7px; }
.sym-link { color:var(--acc); }
.sym-w { margin-left:auto; font-size:11px; color:var(--acc); font-weight:800; }
.sym-flow { font-size:9px; color:var(--ink-faint); margin-top:5px; }
.sym-flow span { color:var(--acc); }
.tables-panel { display:flex; flex-direction:column; gap:12px; }
.table-card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:15px; box-shadow:var(--shadow-sm); }
.table-card-head { display:flex; align-items:center; gap:8px; font-size:11.5px; font-weight:700;
  letter-spacing:-0.01em; margin-bottom:10px; }
.table-dot { width:9px; height:9px; border-radius:3px; background:var(--acc); }
.table-count { margin-left:auto; font-size:10px; color:var(--ink-faint);
  border:1px solid var(--line); border-radius:var(--r-pill); padding:2px 8px; font-weight:600; }
.table-guest { display:flex; justify-content:space-between; align-items:center; font-size:11px;
  padding:6px 0; color:var(--ink-dim); border-bottom:1px dotted var(--line); }
.table-rationale { font-size:10px; color:var(--ink-faint); line-height:1.55; margin-top:9px; }
.roi-top { display:grid; grid-template-columns:1fr 1.2fr; gap:16px; }
.roi-hero { background:linear-gradient(145deg,#7d5ae8,#6b46e0); color:#fff; padding:24px;
  border-radius:var(--r-card); display:flex; flex-direction:column; gap:7px;
  box-shadow:0 10px 30px rgba(108,67,217,0.35); }
.roi-hero-label { font-size:11px; letter-spacing:0.05em; text-transform:uppercase;
  font-weight:700; opacity:0.85; }
.roi-hero-num { font-size:62px; font-weight:800; line-height:1; letter-spacing:-0.02em; }
.roi-hero-sub { font-size:11px; opacity:0.85; }
.roi-funnel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:20px; display:flex; flex-direction:column; gap:12px; box-shadow:var(--shadow-sm); }
.rf-row { display:flex; align-items:center; gap:12px; }
.rf-k { font-size:11px; color:var(--ink-dim); width:74px; font-weight:500; }
.rf-bar { flex:1; height:18px; background:var(--panel-3); border-radius:var(--r-pill); overflow:hidden; }
.rf-fill { height:100%; background:linear-gradient(90deg,var(--acc-light),var(--acc));
  border-radius:var(--r-pill); }
.rf-v { font-size:13px; color:var(--ink); width:32px; text-align:right; font-weight:800; }
.rf-foot { font-size:10px; color:var(--ink-faint); margin-top:2px; }
.ledger { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow-sm); overflow:hidden; }
.ledger-head { display:grid; grid-template-columns:1.6fr 0.7fr 1.6fr 0.8fr; gap:12px;
  padding:13px 18px; border-bottom:1px solid var(--line); font-size:9px; letter-spacing:0.06em;
  text-transform:uppercase; color:var(--ink-faint); font-weight:700; }
.ledger-row { display:grid; grid-template-columns:1.6fr 0.7fr 1.6fr 0.8fr; gap:12px;
  padding:14px 18px; border-bottom:1px solid var(--line-soft); align-items:center; }
.led-guest { display:flex; flex-direction:column; gap:2px; }
.led-name { font-size:12.5px; color:var(--ink); font-weight:600; }
.led-co { font-size:10px; color:var(--ink-faint); }
.led-outcome { display:flex; flex-direction:column; gap:4px; }
.led-pill { font-size:9px; text-transform:uppercase; letter-spacing:0.03em; padding:3px 8px;
  width:fit-content; border-radius:var(--r-pill); font-weight:700; }
.led-pill-won { background:var(--ok-soft); color:var(--ok); }
.led-pill-partial { background:var(--acc-soft); color:var(--acc); }
.led-pill-lost { background:var(--no-soft); color:var(--no); }
.led-detail { font-size:10px; color:var(--ink-faint); }
.led-value { font-size:13px; font-weight:800; color:var(--ink); text-align:right; }
.led-lost .led-value { color:var(--ink-faint); }
.led-lost { opacity:0.65; }
.ledger-foot { display:flex; justify-content:space-between; padding:15px 18px;
  font-size:11px; color:var(--ink-dim); text-transform:uppercase; letter-spacing:0.04em; font-weight:600; }
.ledger-total { font-size:17px; font-weight:800; color:var(--acc); letter-spacing:-0.01em; }
@media (max-width:880px) {
  .form-grid, .pipe-sources { grid-template-columns:1fr; }
  .prospect-layout, .match-layout, .roi-top { grid-template-columns:1fr; }
  .ledger-head, .ledger-row { grid-template-columns:1.4fr 1.4fr 0.7fr; }
  .ledger-head span:nth-child(2), .ledger-row > span:nth-child(2) { display:none; }
  .stage-head h1 { font-size:31px; }
}
`;
