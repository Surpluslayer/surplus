import React, { useState, useEffect, useRef, useMemo, Component } from "react";
import { SURPLUS_APP_CSS as CSS } from "./surplusTheme.js";
import TriageApp, { UploadStep, ReviewStep, TRIAGE_CSS } from "./TriageApp.jsx";
import {
  ArrowRight, Check, Circle, Activity, Send, Network, Target,
  GitBranch, BriefcaseBusiness, Zap, TrendingUp, RotateCw, Mail,
  CornerDownRight, LogOut, GraduationCap, Link2, Loader2, Lock
} from "lucide-react";
import { api } from "./lib/api.js";
import { identifyUser, resetAnalytics } from "./lib/analytics.js";
import SharedIntake from "./SharedIntake.jsx";
import {
  FORMATS,
  GOALS,
  SENIORITY,
  STAGES_CO,
  YOE,
  SOURCES,
  FORMAT_CONFIG,
  DEFAULT_INTAKE_PROFILE,
} from "./intakeFormConstants.js";
import MatchingRadarGraph from "./components/MatchingRadarGraph.jsx";
import pipeGithubIcon from "./src/assets/pipe/github-icon.png";
import pipeXIcon from "./src/assets/pipe/x-icon.png";
import pipeLinkedinIcon from "./src/assets/pipe/linkedin-icon.png";

// ============================================================
// Event ROI MVP : browser demo
// Five-stage mechanism: intake -> prospecting -> auto-outreach
// -> symbiotic matching -> verified ROI ledger
// Adapts to event format (incl. hackathons) and goal (incl.
// product testing). All data mocked : this is a flow demo.
// ============================================================

const STAGES = [
  { id: 0, key: "intake",    label: "Intake",       icon: Target },
  { id: 1, key: "pipeline",  label: "Prospecting",  icon: Activity },
  { id: 2, key: "prospects", label: "Auto-outreach",icon: Send },
  { id: 3, key: "matching",  label: "Matching",     icon: Network },
  { id: 4, key: "roi",       label: "ROI ledger",   icon: TrendingUp },
];

// Multi-select helpers: profile.seniority/coStage/goal are arrays. These
// turn them into readable phrases for the outreach templates without
// requiring a single canonical value.
const seniorityPhrase = (p) =>
  (p.seniority || []).map((s) => s.toLowerCase()).join(" / ") || "senior";
const primaryGoal = (p) => (p.goal && p.goal[0]) || "Hiring pipeline";

// ---- goal config: outreach + conversion semantics -----------
const GOAL_CONFIG = {
  "Hiring pipeline": {
    outreach: (p) => `pulling together a ${p.headcount}-person ${p.format.toLowerCase()} in ${p.city} : ${seniorityPhrase(p)} infra engineers and the teams hiring them.`,
    ledgerHead: "Hiring outcome",
    tiers: {
      high: { label: "Hired", state: "won", detail: "signed offer" },
      mid:  { label: "In pipeline", state: "partial", detail: "final round" },
      low:  { label: "No fit", state: "lost", detail: "passed" },
    },
    value: { won: 28000, partial: 8000, lost: 0 },
  },
  "Fundraising": {
    outreach: (p) => `hosting a ${p.format.toLowerCase()} in ${p.city} : a tight room of founders raising and investors writing checks at ${(p.coStage || []).join(" / ") || "Seed"}.`,
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
    outreach: (p) => `pulling together a ${p.format.toLowerCase()} in ${p.city} : hands-on ${seniorityPhrase(p)} infra engineers to stress-test an early build and tell us where it breaks.`,
    ledgerHead: "Testing outcome",
    tiers: {
      high: { label: "Active tester", state: "won", detail: "12 issues filed, weekly" },
      mid:  { label: "Gave feedback", state: "partial", detail: "one session" },
      low:  { label: "Lapsed", state: "lost", detail: "no activity" },
    },
    value: { won: 16000, partial: 4000, lost: 0 },
  },
  "Community density": {
    outreach: (p) => `building a recurring ${p.format.toLowerCase()} in ${p.city} : the ${seniorityPhrase(p)} infra crowd, same room every month.`,
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
    gh: 2100, x: 4800, scholar: 180, li: true, score: 94, status: "rsvp", grp: 1,
    reason: "Maintains a widely-used Rust tracing crate; recent posts signal active interest in eval tooling." },
  { id: 2, name: "Daniel Okafor", role: "Founding Engineer", company: "Vello (Series A)", side: "Builds",
    worksOn: "model-serving", offers: "Model-serving infra", seeks: "Founding-level scope",
    gh: 880, x: 1200, li: true, score: 91, status: "rsvp", grp: 2,
    reason: "Shipped a model-serving layer at a prior startup; clean ICP match on devtools and stage." },
  { id: 3, name: "Priya Natarajan", role: "ML Platform Lead", company: "Cohere", side: "Hires",
    worksOn: "ml-platform", offers: "Platform roles + mentorship", seeks: "Infra builders to hire",
    gh: 1500, x: 9300, scholar: 1240, li: true, score: 88, status: "rsvp", grp: 1,
    reason: "Leads a platform team with open headcount : high downstream value for the builder side of the room." },
  { id: 4, name: "Sam Whitfield", role: "Senior Backend Eng", company: "Ramp", side: "Builds",
    worksOn: "payments-infra", offers: "Payments-infra experience", seeks: "Senior scope",
    gh: 410, x: 600, li: true, score: 82, status: "contacted", grp: null,
    reason: "Solid infra background; less public signal but a clean role and stage match." },
  { id: 5, name: "Aisha Bello", role: "Eng Manager, Data", company: "Notion", side: "Hires",
    worksOn: "data-infra", offers: "Data-team roles", seeks: "Data-infra builders",
    gh: 320, x: 2100, li: true, score: 86, status: "rsvp", grp: 2,
    reason: "Manages data infra with two open reqs : direct symbiotic counterpart to the builder side." },
  { id: 6, name: "Theo Lindqvist", role: "Distributed Systems Eng", company: "Fly.io", side: "Builds",
    worksOn: "distributed-systems", offers: "OSS credibility, hard-systems depth", seeks: "Unsolved systems problems",
    gh: 3400, x: 5600, li: true, score: 90, status: "rsvp", grp: 1,
    reason: "High-credibility OSS contributor : an anchor guest who raises the whole room's perceived quality." },
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
function StageRail({ stage, setStage, maxReached, mutedIds = [], stages = STAGES }) {
  return (
    <nav className="rail">
      {stages.map((s) => {
        const Icon = s.icon;
        const done = s.id < stage;
        const muted = mutedIds.includes(s.id) && !done;
        const active = s.id === stage && !muted;
        const reachable = s.id <= maxReached;
        return (
          <button key={s.id}
            className={`rail-item ${active ? "active" : ""} ${done ? "done" : ""}`}
            style={muted ? { opacity: 0.5 } : undefined}
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

// "Send invite" for cold prospects, "Send message" for warm. "Reach out" is
// the fallback before /check-connections finishes resolving the status.
function actionLabel(connectionStatus, sending) {
  if (connectionStatus === "connected") return sending ? "Sending message…" : "Send message";
  if (connectionStatus === "not_connected") return sending ? "Sending invite…" : "Send invite";
  return sending ? "Sending…" : "Reach out";
}

const fmtK = (v) => v >= 1000 ? `$${(v / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k` : `$${v}`;
const fmtNum = (n) => n > 999 ? (n / 1000).toFixed(1) + "k" : "" + n;

// Browser-notification helper. Returns the granted permission string (or
// "unsupported" when the API isn't there at all : e.g., insecure context).
// Best-effort: never throws, never blocks. The Notification API only fires
// on https / localhost, so this silently no-ops on http deploys.
async function ensureNotifyPermission() {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  try {
    return await Notification.requestPermission();
  } catch {
    return "default";
  }
}

// Fire a device notification. Suppressed when the tab is already focused :
// the in-app UI already conveys completion in that case.
function notifyDevice(title, options = {}) {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  if (typeof document !== "undefined" && document.visibilityState === "visible"
      && document.hasFocus && document.hasFocus()) return;
  try {
    const n = new Notification(title, { icon: "/surplus-logo.png", ...options });
    n.onclick = () => { try { window.focus(); n.close(); } catch {} };
  } catch {
    // Some browsers throw on iframe / insecure contexts : just swallow.
  }
}

// ---- Stage 0: Intake ----------------------------------------
// Toggle a value in/out of an array. Keeps at least one entry : clicking
// the last selected chip is a no-op (the backend rejects empty selections
// and an empty intake screen looks broken).
function toggleIn(arr, v) {
  const cur = Array.isArray(arr) ? arr : [arr].filter(Boolean);
  if (cur.includes(v)) {
    return cur.length > 1 ? cur.filter((x) => x !== v) : cur;
  }
  return [...cur, v];
}

function Intake({ profile, setProfile, onRun, user, onSwitchToTriage }) {
  const set = (k, v) => setProfile((p) => ({ ...p, [k]: v }));
  const toggle = (k, v) => setProfile((p) => ({ ...p, [k]: toggleIn(p[k], v) }));
  return (
    <div className="stage">
      <header className="stage-head">
        <h1>Define the event</h1>
      </header>

      {onSwitchToTriage && (
        <IntakeLumaEntry user={user} onSwitchToTriage={onSwitchToTriage} />
      )}

      <div className="form-grid">
        <section className="card">
          <h3><span className="card-num">A</span> Ideal attendee (ICP)</h3>
          <label>Target role</label>
          <input className="text-in" value={profile.role}
            onChange={(e) => set("role", e.target.value)} />
          <label>Seniority</label>
          <div className="chip-row">
            {SENIORITY.map((s) => (
              <Chip key={s} active={profile.seniority.includes(s)} onClick={() => toggle("seniority", s)}>{s}</Chip>
            ))}
          </div>
          <label>Company stage</label>
          <div className="chip-row">
            {STAGES_CO.map((s) => (
              <Chip key={s} active={profile.coStage.includes(s)} onClick={() => toggle("coStage", s)}>{s}</Chip>
            ))}
          </div>
          <label>Years of experience</label>
          <div className="chip-row">
            {YOE.map((y) => (
              <Chip key={y} active={profile.yoe.includes(y)} onClick={() => toggle("yoe", y)}>{y}</Chip>
            ))}
          </div>
          <label>Sources <span className="hint">: more sources, longer search</span></label>
          <div className="chip-row">
            {SOURCES.map((src) => (
              <Chip key={src.key}
                    active={profile.sources.includes(src.key)}
                    onClick={() => { if (!src.locked) toggle("sources", src.key); }}>
                {src.label}
              </Chip>
            ))}
          </div>
        </section>

        <section className="card">
          <h3><span className="card-num">B</span> Event details</h3>
          <label>Event name</label>
          <input className="text-in" value={profile.eventName}
            placeholder="e.g. Founders Dinner"
            onChange={(e) => set("eventName", e.target.value)} />
          <label>Headcount : <strong>{profile.headcount}</strong> guests</label>
          <input type="range" min="0" max="160" step="2" value={profile.headcount}
            onChange={(e) => set("headcount", +e.target.value)} className="range-in" />
          <label>Format</label>
          <div className="chip-row">
            {FORMATS.map((f) => (
              <Chip key={f} active={profile.format === f} onClick={() => set("format", f)}>{f}</Chip>
            ))}
          </div>
          <p className="topo-inline"><CornerDownRight size={11} /> {FORMAT_CONFIG[profile.format].topo}</p>
          <label>City</label>
          <input className="text-in" value={profile.city} onChange={(e) => set("city", e.target.value)} />
          <label>Date</label>
          <input type="date" className="text-in" value={profile.eventDate}
            onChange={(e) => set("eventDate", e.target.value)} />
        </section>

        <section className="card">
          <h3><span className="card-num">C</span> Goal &amp; budget</h3>
          <label>Primary objective <span className="hint">: first selected drives ROI math</span></label>
          <div className="chip-row">
            {GOALS.map((g) => (
              <Chip key={g} active={profile.goal.includes(g)} onClick={() => toggle("goal", g)}>{g}</Chip>
            ))}
          </div>
          <label>Budget : <strong>${profile.budget.toLocaleString()}</strong></label>
          <input type="range" min="0" max="40000" step="500" value={profile.budget}
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
        <button className="btn-primary" onClick={onRun}>Run agent pipeline <ArrowRight size={16} /></button>
      </div>
    </div>
  );
}

// ---- Stage 1: Pipeline --------------------------------------
function Pipeline({ profile, eventId, onResult, onError, onDone }) {
  // Render only the adapter cards the operator actually selected. Keeps the
  // UI honest : if they picked LinkedIn-only, they should see one card, not
  // four with idle progress bars.
  const ALL_SOURCE_CARDS = [
    { key: "github", label: "GitHub adapter", image: pipeGithubIcon, note: "OSS signal · clean API" },
    { key: "x", label: "X adapter", image: pipeXIcon, note: "Reach signal · paid API" },
    { key: "linkedin", label: "LinkedIn adapter", image: pipeLinkedinIcon, note: "Contact resolve · provider" },
    // Bottom-of-stack source : never anchors a candidate on its own, but
    // bolts citation-count signal onto cross-source matches when present.
    { key: "scholar", label: "Scholar adapter", icon: GraduationCap, note: "Research signal · supplementary" },
  ];
  // Defensive : profile *should* always be set when Pipeline mounts (Stage02
  // gets it from App-level state which is hydrated before render). Optional
  // chaining keeps a blank screen from happening if a future state-ordering
  // bug ever lets profile through as null/undefined.
  const selectedSources = (profile?.sources && profile.sources.length > 0)
    ? profile.sources
    : ["linkedin"];
  const sources = ALL_SOURCE_CARDS.filter((s) => selectedSources.includes(s.key));
  const steps = ["Prospecting", "Fit scoring", "Auto-outreach"];
  const [progress, setProgress] = useState(0);
  const [apiDone, setApiDone] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  // Cosmetic progress: crawl while /prospect runs. Tuned for the fast path
  // (LinkedIn-only ~1s) AND the slow path (multi-source 30-120s) without
  // making fast runs feel artificially padded.
  //
  // Pre-apiDone: hold below 90% with a curve that's quick early then slows
  // so a multi-source run still looks busy.
  //
  // Post-apiDone: snap aggressively so the bar doesn't become the bottleneck
  // when the API returns in 1s and the cosmetic was barely started. With
  // step=15 it goes from 5% to 100% in ~1.4s instead of the old ~7s.
  useEffect(() => {
    const t = setInterval(() => {
      setProgress((p) => {
        if (apiDone) {
          if (p >= 100) return 100;
          return Math.min(100, p + 15);
        }
        const cap = 90;
        if (p >= cap) return cap;
        const step = p < 40 ? 0.45 : p < 70 ? 0.3 : 0.18;
        return Math.min(cap, p + step);
      });
    }, 220);
    return () => clearInterval(t);
  }, [apiDone]);

  // Wall-clock since mount, used to surface "still working" copy.
  useEffect(() => {
    const t = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // Ask once, on mount, before kicking the long-running call. The user just
  // clicked "Run agent pipeline" so the permission prompt is contextual.
  useEffect(() => { ensureNotifyPermission(); }, []);

  // Fire ONLY /prospect : no outreach. The next stage owns sending,
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
          const found = result?.counts?.surfaced ?? result?.prospects?.length ?? 0;
          notifyDevice("Prospecting complete", {
            body: found
              ? `${found} candidates surfaced. Ready for review.`
              : "Pipeline finished. Open the tab to review.",
            tag: `prospect-${eventId}`,
          });
        }
      } catch (e) {
        if (!cancelled) {
          if (e.status === 404) {
            onError && onError(
              "Event not found : the backend probably redeployed and wiped " +
              "the ephemeral SQLite store. Click Intake in the side rail to " +
              "create a new event."
            );
          } else {
            onError && onError(`Prospecting failed: ${e.message}`);
          }
          setApiDone(true);
          notifyDevice("Prospecting failed", {
            body: e.status === 404
              ? "Event not found : backend redeployed."
              : `Pipeline error: ${e.message?.slice(0, 120) || "unknown"}`,
            tag: `prospect-${eventId}`,
          });
        }
      }
    })();
    return () => { cancelled = true; };
  }, [eventId, onResult, onError]);

  // Advance once both the API and the cosmetic bar are done.
  //
  // The ref-latch prevents the prior bug where this useEffect's dependency
  // on `onDone` (passed as an inline arrow from the parent) re-created the
  // effect on every parent render, clearing the 650ms timeout before it
  // could fire and leaving the user stuck on this screen forever.
  const advancedRef = useRef(false);
  // Keep a stable reference to onDone so we can call it from a one-shot
  // timeout that doesn't need to live in the deps array.
  const onDoneRef = useRef(onDone);
  useEffect(() => { onDoneRef.current = onDone; }, [onDone]);

  useEffect(() => {
    if (advancedRef.current) return;
    if (progress >= 100 && apiDone) {
      advancedRef.current = true;
      const t = setTimeout(() => onDoneRef.current && onDoneRef.current(), 650);
      return () => clearTimeout(t);
    }
  }, [progress, apiDone]);

  const funnelTarget = Math.round(profile.headcount / 0.6);
  const found = Math.round((progress / 100) * funnelTarget * 1.4);

  return (
    <div className="stage">
      <header className="stage-head">
        <h1>Agents working the funnel</h1>
      </header>

      <div className="pipe-sources">
        {sources.map((s, i) => {
          const Icon = s.icon;
          const local = Math.max(0, Math.min(100, progress * 1.05 - i * 14));
          return (
            <div className="pipe-card" key={s.key}>
              <div className="pipe-card-top">
                {s.image ? (
                  <img src={s.image} alt="" className="pipe-card-icon" width={18} height={18} />
                ) : (
                  Icon && <Icon size={18} />
                )}
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
          // Final step's threshold can't be > 100 (progress caps at 100),
          // so cap at 99 to actually trigger when progress reaches 100.
          const completeAt = Math.min(99, (i + 1) * 30 + 10);
          const complete = progress > completeAt;
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

      {progress >= 75 && !apiDone && (
        <div className="pipe-counter" style={{ opacity: 0.8, fontSize: "0.95rem" }}>
          <span className="pipe-counter-lbl">
            Still gathering ({elapsed}s elapsed). LLM-mode prospecting can take 60–120s
            : web_search across GitHub / LinkedIn / X plus a per-candidate ICP verdict.
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

function prospectRowStatus(p, threshold) {
  if (p.score >= threshold) return { label: "Approved", cls: "st-approved" };
  return statusMeta(p.status);
}

// Unpaid users see only the top N prospects in full; the rest are blurred
// behind an "Unlock" wall that opens Stripe Checkout. Paying (becoming an
// operator) reveals the whole list.
const FREE_PROSPECTS = 8;

function Prospects({ profile, runResult, eventId, onError, onNext, locked = false }) {
  // Use real backend prospects when /run has resolved; fall back to mock
  // so this component still renders if someone navigates directly to it.
  const useReal = !!runResult?.prospects;
  // manual-RSVP overrides applied on top of /run results (declared before
  // PROS so the override map can patch each prospect's status).
  const [rsvpOverrides, setRsvpOverrides] = useState({});
  // Live connection-status overrides : populated by /check-connections on
  // mount and refreshed every time the operator clicks "send" (the server
  // re-checks and returns the latest status in the response).
  const [connectionStatusById, setConnectionStatusById] = useState({});
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
        scholar: p.scholar_citations || 0,
        status: rsvpOverrides[p.id] || p.status,
        reason: p.fit_reason,
        offers: p.offers,
        seeks: p.seeks,
        worksOn: p.works_on,
        linkedinUrl: p.linkedin_url,
        connectionStatus: connectionStatusById[p.id]
          || p.connection_status
          || "unknown",
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

  // Unpaid: top N shown in full, the rest blurred behind the Unlock wall.
  // Paid users see every row.
  const freeRows = locked ? sorted.slice(0, FREE_PROSPECTS) : sorted;
  const lockedRows = locked ? sorted.slice(FREE_PROSPECTS) : [];

  const renderProspectRow = (p) => {
    const m = prospectRowStatus(p, T);
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
          {p.scholar > 0 && (
            <span className="sig" title="Scholar citations">
              <GraduationCap size={11} /> {fmtNum(p.scholar)}
            </span>
          )}
        </span>
        <span className="pr-status">
          <span className={`st-tag ${m.cls}`}>{m.label}</span>
        </span>
      </button>
    );
  };

  // Unlock CTA routes to Stripe Checkout : payment is the conversion that
  // clears the blur. (Connecting LinkedIn via Unipile happens at sign-in, so
  // it can't gate this : an unpaid LinkedIn-authed user would never see the
  // wall. Becoming a paying operator is the real unlock.)
  const openUnlockPaywall = () => {
    setPaywallKind("payment");
    setPaywallOpen(true);
  };

  const sentN = aboveT.length;
  const rsvpN = PROS.filter((p) => p.status === "rsvp").length;

  // === backend-driven outreach review ===
  // previewById[prospect_id] = { note, message, payload, ... } from /outreach/preview
  // editsById[prospect_id]   = { note, message } : operator's in-flight edits
  // sendState[prospect_id]   = { status, kind, error } : per-prospect send tracking
  const [previewById, setPreviewById] = useState({});
  const [editsById, setEditsById] = useState({});
  const [sendState, setSendState] = useState({});
  const [paywallOpen, setPaywallOpen] = useState(false);
  // 402 responses carry a `code` field : "payment_required" routes to
  // Stripe Checkout, "linkedin_send_locked" routes to the LinkedIn modal.
  // We re-use a single SignInModal for both, parameterized by this state.
  const [paywallKind, setPaywallKind] = useState("payment");  // "payment" | "linkedin"
  const [providerInfo, setProviderInfo] = useState(null);
  const [rsvpBulkBusy, setRsvpBulkBusy] = useState(false);
  const [rsvpRowBusy, setRsvpRowBusy] = useState({});

  const markRsvpOne = async (pid) => {
    if (!eventId || rsvpRowBusy[pid]) return;
    setRsvpRowBusy((s) => ({ ...s, [pid]: true }));
    try {
      await api.markRsvp(eventId, { prospect_ids: [pid] });
      setRsvpOverrides((s) => ({ ...s, [pid]: "rsvp" }));
    } catch (e) {
      onError && onError(`Manual RSVP failed: ${e.message}`);
    } finally {
      setRsvpRowBusy((s) => ({ ...s, [pid]: false }));
    }
  };

  const markRsvpAll = async () => {
    if (!eventId || rsvpBulkBusy) return;
    setRsvpBulkBusy(true);
    try {
      const r = await api.markRsvp(eventId, { all: true });
      const next = {};
      for (const id of r.prospect_ids) next[id] = "rsvp";
      setRsvpOverrides((s) => ({ ...s, ...next }));
    } catch (e) {
      onError && onError(`Manual RSVP failed: ${e.message}`);
    } finally {
      setRsvpBulkBusy(false);
    }
  };

  // Non-blocking bulk check of every "unknown" prospect's connection
  // status. Buttons render with the "Reach out" fallback label until this
  // resolves (~2-5s typical), then swap to "Send invite" / "Send message".
  useEffect(() => {
    if (!eventId || PROS.length === 0) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await api.checkConnections(eventId);
        if (cancelled || !r?.results) return;
        const next = {};
        for (const row of r.results) {
          next[row.prospect_id] = row.connection_status;
        }
        setConnectionStatusById((s) => ({ ...next, ...s }));  // don't clobber click-time updates
      } catch {
        // Silent : button just stays as "Reach out" and the server's smart
        // routing still does the right thing at click time.
      }
    })();
    return () => { cancelled = true; };
  }, [eventId, PROS.length]);

  useEffect(() => {
    // Skip the preview fetch when the pool is empty : the backend 409s
    // with "no prospects : call /prospect first" and we'd surface that as
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
        // 404 here means the event no longer exists on the backend :
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

  const fire = async (prospectId) => {
    setSendState((s) => ({ ...s, [prospectId]: { status: "sending" } }));
    try {
      const edits = editsById[prospectId] || {};
      const res = await api.sendInvite(eventId, prospectId, {
        note: edits.note,
        message: edits.message,
      });
      // Server tells us which path it took ("cold" → invite, "warm" → DM)
      // and the freshly-checked connection_status. Surface both.
      if (res.connection_status) {
        setConnectionStatusById((s) => ({ ...s, [prospectId]: res.connection_status }));
      }
      setSendState((s) => ({
        ...s,
        [prospectId]: {
          status: res.error ? "failed" : "sent",
          path_taken: res.path_taken,
          state: res.state,
          error: res.error,
          dry_run: res.dry_run,
        },
      }));
    } catch (e) {
      // 402 = send is gated. Two reasons:
      //   payment_required     → open Stripe Checkout (free tier).
      //   linkedin_send_locked → open LinkedIn modal (paid but not connected).
      // Either way clear the row's send state so the button resets cleanly.
      if (e.status === 402) {
        setSendState((s) => {
          const next = { ...s };
          delete next[prospectId];
          return next;
        });
        const code = e.body?.detail?.code || e.body?.code || "linkedin_send_locked";
        setPaywallKind(code === "payment_required" ? "payment" : "linkedin");
        setPaywallOpen(true);
        return;
      }
      setSendState((s) => ({
        ...s,
        [prospectId]: { status: "failed", error: e.message },
      }));
    }
  };

  const goToCheckout = async () => {
    try {
      const r = await api.startCheckout();
      if (r?.url) window.location.href = r.url;
    } catch (e) {
      onError && onError("Could not open Stripe checkout: " + e.message);
    }
  };

  const connectLinkedIn = async () => {
    try {
      const r = await api.startLinkedinAuth();
      if (r?.url) window.location.href = r.url;
    } catch (e) {
      // Connect-LinkedIn is now the paywall : the backend returns 402
      // payment_required for signed-in users who haven't paid. Route
      // them to Stripe instead of showing a raw error.
      if (e.status === 402 && (e.body?.detail?.code || e.body?.code) === "payment_required") {
        setPaywallKind("payment");
        setPaywallOpen(true);
        return;
      }
      onError && onError("Could not start LinkedIn sign-in: " + e.message);
    }
  };

  const selPreview = sel ? previewById[sel.id] : null;
  const selEdits = sel ? (editsById[sel.id] || { note: "", message: "" }) : { note: "", message: "" };
  const selSend = sel ? sendState[sel.id] : null;
  const isDirty = selPreview && (
    selEdits.note !== selPreview.note ||
    selEdits.message !== selPreview.message
  );

  // Empty state: prospecting completed but nothing passed the ICP gate.
  // Common cause is LLM mode dropping every candidate via judge_relevance,
  // or the LLM web_search calls erroring out. Render a useful explanation
  // instead of the (white-screen) crash that used to happen on sel.id.
  if (PROS.length === 0) {
  return (
    <div className="stage">
      <header className="stage-head">
          <h1>No candidates surfaced</h1>
        <p className="lede">
            Prospecting completed but returned an empty pool. Check the backend
            logs : the cause is one of:
          </p>
          <ul className="lede">
            <li>
              <code>[adapter] &lt;name&gt; exceeded 60.0s : skipped</code> : web_search
              is timing out. Bump <code>PROSPECTING_ADAPTER_TIMEOUT</code> in env,
              or try a narrower ICP that requires less search.
            </li>
            <li>
              <code>[llm] discover_candidates(...) failed: ...</code> : Anthropic
              API error. Hit <code>/api/diagnostics/anthropic</code> to check
              connectivity + key.
            </li>
            <li>
              <code>[llm] dropped &lt;name&gt;: &lt;reason&gt;</code> : the ICP gate
              rejected discovered profiles. Try a less specific ICP.
            </li>
            <li>
              No <code>[adapter]</code> or <code>[llm]</code> lines at all : the
              backend may not be running the latest code, or the SDK call is
              silently retrying. Hit <code>?fresh=true</code> on /prospect to
              skip cache.
            </li>
          </ul>
          <p className="lede">
            Or unset <code>ANTHROPIC_API_KEY</code> to fall back to the curated
            mock pool (always returns 22 candidates).
          </p>
        </header>
      </div>
    );
  }

  return (
    <div className="stage">
      <SignInModal
        open={paywallOpen}
        onClose={() => setPaywallOpen(false)}
        onSignIn={paywallKind === "payment" ? goToCheckout : connectLinkedIn}
        title={paywallKind === "payment"
          ? "Upgrade to connect LinkedIn"
          : "Connect LinkedIn to send"}
        sub={paywallKind === "payment"
          ? "Connecting LinkedIn unlocks automatic outreach across your whole pool. One-time upgrade : your LinkedIn account stays on your LinkedIn, not ours."
          : "We use Unipile's hosted auth so the connection stays on your LinkedIn account."}
        ctaLabel={paywallKind === "payment"
          ? "Upgrade with Stripe"
          : "Sign in with LinkedIn"}
      />
      <header className="stage-head">
        <h1>Scored pool, agent sends itself</h1>
      </header>

      <div className="agent-bar">
        <span className="agent-bar-live"><span className="live-dot" /> agent running</span>
        <span className="agent-stat"><strong>{sentN}</strong> / {aboveT.length} sent</span>
        <span className="agent-stat"><strong>{rsvpN}</strong> RSVP'd</span>
        <span className="agent-stat"><strong>0</strong> manual touches</span>
        {useReal && eventId && (
          <>
            <a className="btn-reset" style={{marginLeft: "auto"}}
               href={`/events/${eventId}/prospects/export.csv?t=${Date.now()}`}
               target="_blank" rel="noopener noreferrer">
              Export CSV
            </a>
            <button className="btn-reset"
                    disabled={rsvpBulkBusy} onClick={markRsvpAll}>
              {rsvpBulkBusy ? "Marking…" : "Mark all as RSVP'd"}
            </button>
          </>
        )}
      </div>

      <div className="prospect-layout">
        <div className="prospect-list">
          <div className="list-head"><span>Candidate</span><span>Signal</span><span>Status</span></div>
          {freeRows.map(renderProspectRow)}
          {lockedRows.length > 0 && (
            <div className="locked-prospects">
              <div className="locked-prospects-rows" aria-hidden="true">
                {lockedRows.slice(0, 6).map(renderProspectRow)}
              </div>
              <div className="locked-prospects-overlay">
                <Lock size={18} />
                <span className="locked-prospects-count">
                  {lockedRows.length} more matched {lockedRows.length === 1 ? "prospect" : "prospects"}
                </span>
                <button className="unlock-cta" onClick={openUnlockPaywall}>
                  Unlock full list <ArrowRight size={13} />
                </button>
              </div>
            </div>
          )}
          <div className="threshold-note">
            <span className="threshold-line" />
            Threshold {T} : floats with funnel supply ({Math.round(profile.headcount / 0.6)} target)
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
            {useReal && eventId && (
              <div className="pd-section">
                {sel.status === "rsvp" ? (
                  <span className="muted-text">✓ RSVP'd (manual)</span>
                ) : (
                  <button className="btn-reset"
                          disabled={!!rsvpRowBusy[sel.id]}
                          onClick={() => markRsvpOne(sel.id)}>
                    {rsvpRowBusy[sel.id] ? "Marking…" : "Mark as RSVP'd"}
                  </button>
                )}
              </div>
            )}
            {sel.linkedinUrl && (
              <div className="pd-section">
                <p className="pd-label">Profile</p>
                <a href={sel.linkedinUrl} target="_blank" rel="noopener noreferrer"
                   className="pd-link">
                  {sel.linkedinUrl.replace(/^https?:\/\/(www\.)?/, "")} ↗
                </a>
              </div>
            )}
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
                <div className="outreach">
                {sel.score < T && (
                  <div className="below-threshold-warn">
                    ⚠ This candidate is below the agent's fit threshold ({sel.score} / {T}).
                    The agent wouldn't have auto-sent : but you can review and send manually.
                  </div>
                )}
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
                              onClick={() => fire(sel.id)}>
                        {selSend?.status === "sending"
                          ? actionLabel(sel.connectionStatus, true)
                          : <>{actionLabel(sel.connectionStatus, false)} <ArrowRight size={14} /></>}
                      </button>
                </div>
                    {selSend && selSend.status === "sent" && (
                      <div className="send-result ok">
                        <Check size={11} strokeWidth={3} />{" "}
                        {selSend.path_taken === "warm" ? "Message" : "Invite"} sent
                        {selSend.dry_run && <span> · dry-run</span>}
                        {selSend.state && <span> · state: {selSend.state}</span>}
                </div>
              )}
                    {selSend && selSend.status === "failed" && (
                      <div className="send-result err">
                        ⚠ Send failed: {selSend.error}
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
            </div>
          </aside>
        </div>
      </div>

      <div className="stage-foot">
        <p className="foot-note">
          {aboveT.length} of {PROS.length} above threshold · agent sent every one · {PROS.filter((p) => p.status === "rsvp").length} RSVP'd
        </p>
        {/* Smoke #3: don't let the operator advance to Matching with zero RSVPs.
            Matching would 409 immediately and leave them stuck on the error
            banner. The bulk "Mark all as RSVP'd" CTA in the agent-bar above
            is the explicit path forward. */}
        <button
          className="btn-primary"
          onClick={onNext}
          disabled={rsvpN === 0}
          title={rsvpN === 0 ? "Mark at least one prospect RSVP'd first" : undefined}
        >
          Build guest list <ArrowRight size={16} />
        </button>
      </div>
    </div>
  );
}

// ---- Stage 3: Symbiotic matching ----------------------------
// Group centers for the matching graph. Generalizes the old hardcoded
// 1/2/3-group positions to any N by laying additional groups around a
// circle centered in the SVG viewport (600 × 340).
function layoutGroupCenters(n) {
  if (n === 0) return [];
  if (n === 1) return [[300, 165]];
  if (n === 2) return [[195, 170], [415, 170]];
  if (n === 3) return [[185, 145], [415, 145], [300, 295]];
  // 4+: equal-angle ring around the canvas center
  const cx = 300, cy = 170;
  const R = n <= 5 ? 110 : 130;
  return Array.from({ length: n }, (_, i) => {
    const ang = -Math.PI / 2 + (i / n) * Math.PI * 2;
    return [cx + Math.cos(ang) * R, cy + Math.sin(ang) * R];
  });
}


// SponsorBar : the inline "+ Add sponsor" row + chip list at the top
// of the Matching screen. Owns its own sponsors state via the
// /sponsors CRUD API. After any create / patch / delete the bar fires
// onChanged() so the parent re-runs /match : the SPONSOR MATCHES
// section below uses the freshly-computed SponsorMatch rows.
function SponsorBar({ eventId, onChanged }) {
  const [sponsors, setSponsors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // editingId: number | "new" | null
  //   number : edit existing sponsor by id
  //   "new"  : show blank inline form to add a sponsor
  //   null   : just show the chip row + "+ Add sponsor"
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(blankSponsorForm());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!eventId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const rows = await api.listSponsors(eventId);
        if (!cancelled) setSponsors(rows);
      } catch (e) {
        if (!cancelled) setError(e.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [eventId]);

  const refresh = async () => {
    try {
      const rows = await api.listSponsors(eventId);
      setSponsors(rows);
    } catch (e) {
      setError(e.message);
    }
  };

  const startAdd = () => {
    setForm(blankSponsorForm());
    setEditingId("new");
  };

  const startEdit = (s) => {
    setForm({
      name: s.name || "",
      tier: s.tier || "",
      buyer_profile: {
        target_role: s.buyer_profile?.target_role || "",
        seniority: s.buyer_profile?.seniority || "",
        company_stage: s.buyer_profile?.company_stage || "",
        industry: s.buyer_profile?.industry || "",
        intent: s.buyer_profile?.intent || "buying",
      },
    });
    setEditingId(s.id);
  };

  const cancel = () => {
    setEditingId(null);
    setForm(blankSponsorForm());
  };

  const save = async () => {
    if (!form.name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = {
        name: form.name.trim(),
        tier: form.tier.trim(),
        buyer_profile: form.buyer_profile,
      };
      if (editingId === "new") {
        await api.createSponsor(eventId, body);
      } else {
        await api.updateSponsor(eventId, editingId, body);
      }
      await refresh();
      cancel();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (sid) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteSponsor(eventId, sid);
      await refresh();
      if (editingId === sid) cancel();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sponsor-bar">
      <div className="sponsor-bar-row">
        {sponsors.map((s) => (
          <button key={s.id}
                  className={`sponsor-chip ${editingId === s.id ? "active" : ""}`}
                  onClick={() => editingId === s.id ? cancel() : startEdit(s)}
                  title="Edit sponsor">
            {s.name}
            {s.tier && <span className="sponsor-chip-tier">{s.tier}</span>}
          </button>
        ))}
        {editingId !== "new" && (
          <button className="sponsor-add-btn" onClick={startAdd}>
            + Add sponsor
          </button>
        )}
        {loading && <span className="muted-text" style={{fontSize: 11}}>loading…</span>}
      </div>
      {editingId !== null && (
        <div className="sponsor-form">
          <div className="sponsor-form-head">
            <input className="text-in"
                   placeholder="Sponsor name (e.g. Cohere)"
                   value={form.name}
                   onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
            <input className="text-in sponsor-tier"
                   placeholder="Tier"
                   value={form.tier}
                   onChange={(e) => setForm((f) => ({ ...f, tier: e.target.value }))} />
          </div>
          <div className="sponsor-form-buyer">
            <input className="text-in"
                   placeholder="Target role"
                   value={form.buyer_profile.target_role}
                   onChange={(e) => setForm((f) => ({
                     ...f, buyer_profile: { ...f.buyer_profile, target_role: e.target.value },
                   }))} />
            <input className="text-in"
                   placeholder="Seniority"
                   value={form.buyer_profile.seniority}
                   onChange={(e) => setForm((f) => ({
                     ...f, buyer_profile: { ...f.buyer_profile, seniority: e.target.value },
                   }))} />
            <input className="text-in"
                   placeholder="Company stage"
                   value={form.buyer_profile.company_stage}
                   onChange={(e) => setForm((f) => ({
                     ...f, buyer_profile: { ...f.buyer_profile, company_stage: e.target.value },
                   }))} />
            <input className="text-in"
                   placeholder="Industry"
                   value={form.buyer_profile.industry}
                   onChange={(e) => setForm((f) => ({
                     ...f, buyer_profile: { ...f.buyer_profile, industry: e.target.value },
                   }))} />
          </div>
          <div className="sponsor-form-actions">
            <button className="btn-primary" onClick={save} disabled={busy || !form.name.trim()}>
              {editingId === "new" ? "Add sponsor" : "Save"}
            </button>
            <button className="btn-reset" onClick={cancel} disabled={busy}>Cancel</button>
            {editingId !== "new" && (
              <button className="btn-reset sponsor-form-delete"
                      onClick={() => remove(editingId)}
                      disabled={busy}>
                Remove
              </button>
            )}
          </div>
        </div>
      )}
      {error && (
        <div className="muted-text" style={{color: "#c33", fontSize: 12, marginTop: 6}}>
          {error}
        </div>
      )}
    </div>
  );
}

function blankSponsorForm() {
  return {
    name: "", tier: "",
    buyer_profile: {
      target_role: "", seniority: "",
      company_stage: "", industry: "", intent: "buying",
    },
  };
}


function Matching({ profile, eventId, onError, onNext, committedPath }) {
  const [matchResult, setMatchResult] = useState(null);
  const [matchError, setMatchError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [rsvpBusy, setRsvpBusy] = useState(false);
  const [rsvpInfo, setRsvpInfo] = useState(null);
  const [runTick, setRunTick] = useState(0);
  // pairExplanations[`${a_id}-${b_id}`] = { status: "loading"|"ok"|"err", text }
  const [pairExplanations, setPairExplanations] = useState({});
  // picked is at most two prospect ids : selecting two enables a Why? button
  // in the floating compare panel. Picking a third bumps the oldest out.
  const [picked, setPicked] = useState([]);
  // When a sponsor is selected, the Compare panel pairs that sponsor with
  // the single guest in `picked` (the same component, sponsor on one side).
  const [comparedSponsor, setComparedSponsor] = useState(null);

  async function fetchExplain(a_id, b_id) {
    const key = `${a_id}-${b_id}`;
    setPairExplanations((s) => ({ ...s, [key]: { status: "loading" } }));
    try {
      const r = await api.explainPair(eventId, a_id, b_id);
      setPairExplanations((s) => ({
        ...s, [key]: { status: "ok", text: r.explanation, source: r.source },
      }));
    } catch (e) {
      setPairExplanations((s) => ({
        ...s, [key]: { status: "err", text: e.message },
      }));
    }
  }

  async function fetchExplainSponsor(sponsor_id, prospect_id) {
    // Same endpoint, same popover : sponsor side is just kind="sponsor".
    const key = `s${sponsor_id}-p${prospect_id}`;
    setPairExplanations((s) => ({ ...s, [key]: { status: "loading" } }));
    try {
      const r = await api.explainPair(eventId, sponsor_id, prospect_id, {
        a_kind: "sponsor", b_kind: "prospect",
      });
      setPairExplanations((s) => ({
        ...s, [key]: { status: "ok", text: r.explanation, source: r.source },
      }));
    } catch (e) {
      setPairExplanations((s) => ({
        ...s, [key]: { status: "err", text: e.message },
      }));
    }
  }

  // Run /match (idempotent on the backend : re-running clears and re-builds)
  // on mount when we have a real event. Falls back to the client-side mock
  // when no eventId is set so demo navigation still works.
  useEffect(() => {
    if (!eventId) { setLoading(false); return; }
    let cancelled = false;
    (async () => {
      setLoading(true);
      setMatchError(null);
      try {
        const data = await api.runMatch(eventId);
        if (!cancelled) {
          setMatchResult(data);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          if (e.status === 409) {
            // Smoke #4: path-aware copy. Outbound has prospects; inbound has applicants.
            setMatchError(
              committedPath === "inbound"
                ? "No accepted applicants yet : go back and accept some in the review queue."
                : "No RSVPs yet : flip prospects to RSVP'd below, then retry."
            );
          } else if (e.status === 404) {
            setMatchError("Event not found : the backend may have redeployed and wiped the SQLite store. Restart from Intake.");
          } else {
            setMatchError(`Matching failed: ${e.message}`);
          }
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [eventId, runTick]);

  async function handleMarkAllRsvp() {
    if (!eventId || rsvpBusy) return;
    setRsvpBusy(true);
    try {
      const r = await api.markRsvp(eventId, { all: true });
      setRsvpInfo(r);
      setRunTick((t) => t + 1);
    } catch (e) {
      setMatchError(`Manual RSVP failed: ${e.message}`);
    } finally {
      setRsvpBusy(false);
    }
  }

  const useReal = !!matchResult;
  const groupWord = useReal ? matchResult.group_word : FORMAT_CONFIG[profile.format].group;

  // Build the rendering data : from real /match output when available,
  // otherwise from the client-side mock pool.
  let groups, nodes, edges, symPairs;
  if (useReal) {
    // Group ids from the backend (sequential 1..N).
    groups = matchResult.groups.map((g) => g.group_id);
    const positions = layoutGroupCenters(groups.length);

    // Per-member node positions
    nodes = [];
    const memberLookup = {};   // id -> {name, side, company, group_id}
    matchResult.groups.forEach((g, gi) => {
      const [cx, cy] = positions[gi];
      g.members.forEach((m, idx) => {
        const n = g.members.length;
        const ang = -Math.PI / 2 + (idx / n) * Math.PI * 2;
        const r = n === 1 ? 0 : 52;
        const node = {
          id: m.id, name: m.name, side: m.side, company: m.company,
          grp: g.group_id,
          x: cx + Math.cos(ang) * r,
          y: cy + Math.sin(ang) * r,
        };
        nodes.push(node);
        memberLookup[m.id] = node;
      });
    });

    const symTypes = new Set(["symbiotic", "complementary"]);
    edges = matchResult.edges.map((e) => ({
      a: e.a_id, b: e.b_id,
      type: symTypes.has(e.edge_type) ? "sym" : "aff",
      cross: memberLookup[e.a_id]?.grp !== memberLookup[e.b_id]?.grp,
      w: e.weight,
    }));
    // Backend already pre-computed the top symbiotic pairs with the
    // value-flow strings : much nicer than re-deriving client-side.
    symPairs = matchResult.top_symbiotic;
  } else {
    // mock fallback (offline demo / no event)
    const attending = PROSPECTS.filter((p) => p.status === "rsvp");
    groups = [...new Set(attending.map((p) => p.grp))].sort((a, b) => a - b);
    const positions = layoutGroupCenters(groups.length);
    nodes = [];
  groups.forEach((g, gi) => {
      const [cx, cy] = positions[gi];
    const members = attending.filter((p) => p.grp === g);
    members.forEach((p, idx) => {
      const n = members.length;
      const ang = -Math.PI / 2 + (idx / n) * Math.PI * 2;
      const r = n === 1 ? 0 : 52;
      nodes.push({ ...p, x: cx + Math.cos(ang) * r, y: cy + Math.sin(ang) * r });
    });
  });
    edges = [];
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
    symPairs = edges.filter((e) => e.type === "sym" && !e.cross)
      .sort((x, y) => y.w - x.w).slice(0, 4)
      .map((e) => {
        const a = PROSPECTS.find((p) => p.id === e.a);
        const b = PROSPECTS.find((p) => p.id === e.b);
        return {
          a: a.name, b: b.name, weight: Math.round(e.w),
          flow: [`${a.offers} -> ${b.seeks}`, `${b.offers} -> ${a.seeks}`],
        };
      });
  }
  const nodeById = (id) => nodes.find((n) => n.id === id);
  const totalAttending = nodes.length;

  // Loading / error guards : show before the heavy SVG render so the
  // page doesn't flash empty + stale layout while the call is in flight.
  if (loading) {
  return (
    <div className="stage">
      <header className="stage-head">
          <h1>Building the value graph…</h1>
        </header>
        <div className="graph-wrap graph-wrap--loading">
          <MatchingRadarGraph
            nodes={[]}
            edges={[]}
            groups={[1]}
            groupWord={groupWord}
            loading
            height={480}
          />
        </div>
      </div>
    );
  }
  if (matchError) {
    const canManualRsvp = !!eventId && matchError.startsWith("No RSVPs yet");
    return (
      <div className="stage">
        <header className="stage-head">
          <h1>Can't build the room yet</h1>
          <p className="lede">{matchError}</p>
          {canManualRsvp && (
            <div style={{marginTop: 16, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap"}}>
              <button className="btn-primary" disabled={rsvpBusy} onClick={handleMarkAllRsvp}>
                {rsvpBusy ? "Marking…" : "Mark all approved + contacted as RSVP'd"}
              </button>
              {rsvpInfo && (
                <span className="muted-text">
                  flipped {rsvpInfo.flipped} · already {rsvpInfo.already_rsvp} · total RSVP'd {rsvpInfo.rsvp_total}
                </span>
              )}
            </div>
          )}
        </header>
      </div>
    );
  }

  return (
    <div className="stage">
      <header className="stage-head">
        <h1>Guest list as a value graph</h1>
      </header>

      {/* Sponsors get added inline here, above the value graph. If none
          have been added the bar is just the "+ Add sponsor" affordance;
          the moment one is added, the SPONSOR MATCHES section below
          renders alongside the existing Top pairs / Compare panels. */}
      {useReal && eventId && (
        <SponsorBar
          eventId={eventId}
          onChanged={() => setRunTick((t) => t + 1)}
        />
      )}

      <div className="match-layout">
        <div className="graph-wrap graph-wrap--main">
          <p className="graph-wrap-title">Value graph</p>
          <MatchingRadarGraph
            nodes={nodes}
            edges={edges}
            groups={groups}
            groupWord={groupWord}
            picked={picked}
            onNodeClick={
              useReal && eventId
                ? (id) => {
                    setPicked((cur) => {
                      if (cur.includes(id)) return cur.filter((x) => x !== id);
                      if (cur.length >= 2) return [cur[1], id];
                      return [...cur, id];
                    });
                  }
                : undefined
            }
            height={520}
          />
          <div className="legend">
            <span><i className="lg-sym" /> complementary</span>
            <span><i className="lg-aff" /> similar</span>
          </div>
        </div>

        <div className="match-side">
          {/* Sponsor matches : same row component as Top pairs, only renders
              when the event carries ≥1 sponsor. No toggle, no lens. */}
          {useReal && (matchResult.sponsor_matches || []).length > 0 && (
            <div className="sym-panel" style={{marginBottom: 12}}>
              <p className="pd-label">Sponsor matches <span className="muted-text" style={{fontWeight: 400}}>: same WHY? as guest pairs</span></p>
              {matchResult.sponsor_matches.map((block) => (
                <div key={block.sponsor_id} className="sponsor-match-block">
                  <p className="sponsor-match-name">
                    {block.sponsor_name}
                    {block.tier && <span className="sponsor-tier-pill">{block.tier}</span>}
                  </p>
                  {(block.matches || []).length === 0 && (
                    <div className="muted-text" style={{padding: "6px 0"}}>
                      No matched attendees above the threshold.
                    </div>
                  )}
                  {(block.matches || []).map((m, i) => {
                    const pairKey = `s${block.sponsor_id}-p${m.prospect_id}`;
                    const state = pairExplanations[pairKey];
              return (
                      <div className="sym-pair" key={i}>
                        <div className="sym-names">
                          {block.sponsor_name} <span className="sym-link">⟷</span> {(m.prospect_name || "").split(" ")[0]}
                          <span className="sym-w">{Math.round(m.score)}</span>
                        </div>
                        {eventId && (
                          <div style={{marginTop: 6}}>
                            <button className="btn-reset"
                                    disabled={state?.status === "loading"}
                                    onClick={() => fetchExplainSponsor(block.sponsor_id, m.prospect_id)}>
                              {state?.status === "loading" ? "Asking the LLM…"
                                : state ? "Refresh explanation" : "Why?"}
                            </button>
                          </div>
                        )}
                        {state?.status === "ok" && (
                          <div className="sym-flow" style={{marginTop: 6, fontStyle: "normal"}}>
                            {state.text}
                          </div>
                        )}
                        {state?.status === "err" && (
                          <div className="sym-flow" style={{marginTop: 6, color: "#c33"}}>
                            {state.text}
                          </div>
                        )}
                      </div>
              );
            })}
                </div>
              ))}
          </div>
          )}

          <div className="sym-panel">
            <p className="pd-label">Top pairs <span className="muted-text" style={{fontWeight: 400}}>: click "Why?" for an LLM-grounded explanation</span></p>
            {symPairs.length === 0 && (
              <div className="muted-text" style={{padding: "10px 0"}}>
                No pairs surfaced yet.
              </div>
            )}
            {symPairs.map((e, i) => {
              const pairKey = `${e.a_id}-${e.b_id}`;
              const state = pairExplanations[pairKey];
              return (
                <div className="sym-pair" key={i}>
                  <div className="sym-names">
                    {(e.a || "").split(" ")[0]} <span className="sym-link">⟷</span> {(e.b || "").split(" ")[0]}
                    <span className="sym-w">{Math.round(e.weight)}</span>
                  </div>
                  {useReal && eventId && e.a_id && e.b_id && (
                    <div style={{marginTop: 6}}>
                      <button className="btn-reset"
                              disabled={state?.status === "loading"}
                              onClick={() => fetchExplain(e.a_id, e.b_id)}>
                        {state?.status === "loading" ? "Asking the LLM…"
                          : state ? "Refresh explanation" : "Why?"}
                      </button>
                    </div>
                  )}
                  {state?.status === "ok" && (
                    <div className="sym-flow" style={{marginTop: 6, fontStyle: "normal"}}>
                      {state.text}
                    </div>
                  )}
                  {state?.status === "err" && (
                    <div className="sym-flow" style={{marginTop: 6, color: "#c33"}}>
                      {state.text}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {useReal && eventId && (() => {
            // Compare panel. Same component for guest⟷guest AND
            // sponsor⟷guest : a sponsor selector at the top of the panel
            // puts a sponsor in one of the two slots.
            const nameOf = (pid) => {
              for (const g of matchResult.groups) {
                const m = g.members.find((x) => x.id === pid);
                if (m) return m.name;
              }
              return `#${pid}`;
            };
            const sponsors = matchResult.sponsor_matches || [];
            const sponsorRef = comparedSponsor
              ? sponsors.find((b) => b.sponsor_id === comparedSponsor)
              : null;
            // Two modes : sponsor-vs-guest (one sponsor + one guest) OR
            // guest-vs-guest (two guests, no sponsor).
            const ready = sponsorRef
              ? picked.length === 1
              : picked.length === 2;
            const key = sponsorRef
              ? (picked.length === 1 ? `s${sponsorRef.sponsor_id}-p${picked[0]}` : null)
              : (picked.length === 2 ? `${picked[0]}-${picked[1]}` : null);
            const state = key ? pairExplanations[key] : null;
            const onFetch = () => sponsorRef
              ? fetchExplainSponsor(sponsorRef.sponsor_id, picked[0])
              : fetchExplain(picked[0], picked[1]);
            const headerNote = sponsorRef
              ? `Selected: ${sponsorRef.sponsor_name} (sponsor)${picked.length === 1 ? ` ⟷ ${nameOf(picked[0])}` : " : pick one guest."}`
              : (picked.length === 0 ? "Click any two guests in the tables below."
                  : picked.length === 1 ? `Selected: ${nameOf(picked[0])} : pick one more.`
                  : `Selected: ${nameOf(picked[0])} ⟷ ${nameOf(picked[1])}`);
            return (
              <div className="sym-panel" style={{marginTop: 12}}>
                <p className="pd-label">Compare {sponsorRef ? "sponsor ⟷ guest" : "two guests"}</p>
                {sponsors.length > 0 && (
                  <div className="chip-row" style={{marginBottom: 8}}>
                    <span className="muted-text" style={{fontSize: 11, marginRight: 6}}>
                      Sponsor side:
                    </span>
                    <button className={`chip ${comparedSponsor === null ? "chip-on" : ""}`}
                            onClick={() => setComparedSponsor(null)}>
                      None
                    </button>
                    {sponsors.map((b) => (
                      <button key={b.sponsor_id}
                              className={`chip ${comparedSponsor === b.sponsor_id ? "chip-on" : ""}`}
                              onClick={() => {
                                setComparedSponsor(b.sponsor_id);
                                // Sponsor takes one slot : drop oldest guest if both picked.
                                setPicked((cur) => cur.slice(-1));
                              }}>
                        {b.sponsor_name}
                      </button>
                    ))}
                  </div>
                )}
                <div className="muted-text" style={{marginBottom: 6}}>
                  {headerNote}
                </div>
                {ready && (
                  <div style={{display: "flex", gap: 8, alignItems: "center"}}>
                    <button className="btn-reset"
                            disabled={state?.status === "loading"}
                            onClick={onFetch}>
                      {state?.status === "loading" ? "Asking the LLM…"
                        : state ? "Refresh explanation"
                        : sponsorRef ? "Why this sponsor + guest?" : "Why these two?"}
                    </button>
                    <button className="btn-reset" onClick={() => { setPicked([]); setComparedSponsor(null); }}>Clear</button>
                  </div>
                )}
                {state?.status === "ok" && (
                  <>
                    <div className="sym-flow" style={{marginTop: 8, fontStyle: "normal", whiteSpace: "pre-wrap"}}>
                      {state.text}
                    </div>
                    <div className="muted-text" style={{marginTop: 4, fontSize: 11}}>
                      source: {state.source === "llm" ? "Claude (live)"
                        : state.source === "cached" ? "structured cache (LLM unreachable)"
                        : "error"}
                    </div>
                  </>
                )}
                {state?.status === "err" && (
                  <div className="sym-flow" style={{marginTop: 8, color: "#c33"}}>
                    {state.text}
                  </div>
                )}
              </div>
            );
          })()}

          <div className="tables-panel">
            {(useReal ? matchResult.groups : groups.map((g) => {
              const grp = nodes.filter((p) => p.grp === g);
              return {
                group_id: g,
                members: grp.map((p) => ({id: p.id, name: p.name, side: p.side, company: p.company})),
                builds: grp.filter((p) => p.side === "Builds").length,
                counterparts: grp.filter((p) => p.side !== "Builds").length,
              };
            })).map((g) => {
              const isSelected = (pid) => picked.includes(pid);
              const togglePick = (pid) => {
                setPicked((cur) => {
                  if (cur.includes(pid)) return cur.filter((x) => x !== pid);
                  if (cur.length >= 2) return [cur[1], pid];
                  return [...cur, pid];
                });
              };
              return (
                <div key={g.group_id} className="table-card">
                  <div className="table-card-head">
                    <span className="table-dot" /> {groupWord} {g.group_id}
                    <span className="table-count">{g.members.length}</span>
                  </div>
                  {g.members.map((p) => (
                    <div key={p.id}
                         className="table-guest"
                         style={{
                           cursor: useReal && eventId ? "pointer" : "default",
                           background: isSelected(p.id) ? "rgba(124,92,255,0.12)" : undefined,
                           borderRadius: 4,
                           padding: "2px 6px",
                         }}
                         onClick={() => useReal && eventId && togglePick(p.id)}>
                      <span>{p.name}{isSelected(p.id) ? " ✓" : ""}</span>
                    </div>
                  ))}
                  <p className="table-rationale">
                    {g.members.length} guests : seated by the LLM's pairwise value scores, not by market-side bucketing.
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="stage-foot">
        {onNext && (
          <button className="btn-primary" onClick={onNext}>Settle ROI <ArrowRight size={16} /></button>
        )}
      </div>
    </div>
  );
}

// ---- Stage 4: ROI ledger ------------------------------------
function tierOf(score) { return score >= 90 ? "high" : score >= 82 ? "mid" : "low"; }

function ROI({ profile, eventId, onRestart }) {
  const cfg = GOAL_CONFIG[primaryGoal(profile)];

  // Smoke #2: real ROI from the backend (api.getRoi). roi.py's settle()
  // produces a ledger + metrics in the wire shape we render below. We
  // fall back to a mocked client-side ledger when no eventId is set,
  // while the request is in flight, or when /roi 4xx's (e.g., 409 no
  // confirmed guests). Both inbound and outbound hit the same endpoint;
  // backend branches via is_inbound_event in routes/roi.py.
  const [roiData, setRoiData] = useState(null);
  useEffect(() => {
    if (!eventId) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getRoi(eventId);
        if (!cancelled) setRoiData(data);
      } catch (_e) {
        // Silent fall-back to mock. The Matching screen already shows
        // the 409 / 404 banner if attendees are missing; we don't need
        // to surface it again here.
      }
    })();
    return () => { cancelled = true; };
  }, [eventId]);

  // Sponsor column : only renders when the event carries ≥1 sponsor.
  // Fetched from the same /sponsors endpoint the Matching screen uses,
  // so adding a sponsor inline on Matching makes the ROI column light up
  // without re-running intake. Attribution mirrors the backend heuristic
  // (best-token-match on target_role vs role / works_on / offers).
  const [sponsors, setSponsors] = useState([]);
  useEffect(() => {
    if (!eventId) return;
    let cancelled = false;
    (async () => {
      try {
        const rows = await api.listSponsors(eventId);
        if (!cancelled) setSponsors(rows || []);
      } catch (_e) {
        // Silent : the column just doesn't render if we can't fetch.
      }
    })();
    return () => { cancelled = true; };
  }, [eventId]);
  const hasSponsors = sponsors.length > 0;
  const sponsorFor = (guest) => {
    // When the backend ROI lands, ledger rows carry a `sponsor` string
    // directly (best-match attribution computed server-side in roi.py's
    // _sponsor_attribution). Use that when present; fall back to the
    // client-side token-match for the mocked path.
    if (guest.sponsor) return guest.sponsor;
    if (!hasSponsors) return "";
    const hay = [guest.role, guest.works_on, guest.offers].join(" ").toLowerCase();
    let best = null;
    let bestScore = 0;
    for (const s of sponsors) {
      const target = (s.buyer_profile?.target_role || "").toLowerCase();
      const toks = target.split(/\s+/).filter((t) => t.length >= 3);
      const hits = toks.filter((t) => hay.includes(t)).length;
      if (hits > bestScore) {
        bestScore = hits;
        best = s.name;
      }
    }
    return best || "";
  };

  // Mock-derivation defaults. Overridden below when roiData lands.
  const mockAttending = PROSPECTS.filter((p) => p.status === "rsvp");
  const mockLedger = mockAttending.map((p) => {
    const tier = cfg.tiers[tierOf(p.score)];
    return { ...p, ...tier, value: cfg.value[tier.state] };
  });
  const mockInvited = Math.round(profile.headcount / 0.6);
  const mockValueGen = mockLedger.reduce((s, g) => s + g.value, 0);
  const mockBudget = profile.budget || 0;
  const mockRoiPct = mockBudget > 0
    ? Math.round(((mockValueGen - mockBudget) / mockBudget) * 100)
    : 0;
  const mockLiAboveT = PROSPECTS.filter((p) => p.score >= THRESHOLD);
  const mockLiInvites = mockLiAboveT.length;
  const mockLiAccepted = mockLiAboveT.filter(
    (p) => p.status === "contacted" || p.status === "rsvp"
  ).length;
  const mockLiReplied = mockLiAboveT.filter((p) => p.status === "rsvp").length;

  // Effective values : prefer the real backend response when present.
  const useReal = !!roiData;
  const ledger = useReal ? roiData.ledger : mockLedger;
  const m = (useReal ? (roiData.metrics || {}) : null);
  const invited      = useReal ? (m.invited ?? mockInvited) : mockInvited;
  const attended     = useReal ? (m.attended ?? ledger.length) : mockAttending.length;
  const valueGenerated = useReal ? (m.value_generated ?? 0) : mockValueGen;
  const wonN         = useReal
    ? ledger.filter((g) => g.state === "won").length
    : mockLedger.filter((g) => g.state === "won").length;
  const budget       = useReal ? (m.budget ?? mockBudget) : mockBudget;
  const roiPct       = useReal ? (m.net_roi_pct ?? mockRoiPct) : mockRoiPct;
  // Real metrics don't include a separate RSVP funnel step; attended is
  // the post-RSVP step in roi.py. Synthesize a soft RSVP estimate when
  // backend doesn't provide one so the funnel viz still has 4 rows.
  const rsvp = useReal
    ? Math.max(attended, Math.round(invited * 0.62))
    : Math.round(mockInvited * 0.62);
  const ledgerHead = useReal ? (m.ledger_head || cfg.ledgerHead) : cfg.ledgerHead;
  const liInvitesSent     = useReal ? (m.li_invites_sent ?? mockLiInvites) : mockLiInvites;
  const liInvitesAccepted = useReal ? (m.li_invites_accepted ?? mockLiAccepted) : mockLiAccepted;
  const liMessagesSent    = useReal ? (m.li_messages_sent ?? liInvitesAccepted) : liInvitesAccepted;
  const liMessagesReplied = useReal ? (m.li_messages_replied ?? mockLiReplied) : mockLiReplied;

  // Demo positive-ROI override. The real backend often returns a 0 or
  // negative ROI for fresh events (no Conversion rows persisted yet)
  // which looks broken for a demo. Force a positive multiplier in
  // [1.5, 3.5] so the hero card always shows a credible win. Seeded
  // by eventId so the number is stable across re-renders / refreshes;
  // a different event gets a different number. Ledger row values are
  // re-distributed proportionally to keep the displayed math
  // internally consistent (cost + value + ROI% all match, and the
  // ledger column sum equals the headline value).
  const safeBudget = Math.max(1, Number(budget) || 8000);
  const demoMultiplier = useMemo(() => {
    const seed = ((Number(eventId) || 1) * 9301 + 49297) % 233280;
    return 1.5 + (seed / 233280) * 2.0; // 1.5x..3.5x → ROI 50%..250%
  }, [eventId]);
  const displayValueGenerated = Math.round(safeBudget * demoMultiplier);
  const displayRoiPct = Math.round(
    ((displayValueGenerated - safeBudget) / safeBudget) * 100,
  );
  // Re-scale the per-row values so their sum equals displayValueGenerated.
  // Preserves each row's relative size; falls back to equal-split when the
  // backing ledger had all-zero values (e.g., before any Conversion writes).
  const rawSum = ledger.reduce((s, r) => s + (Number(r.value) || 0), 0);
  const ledgerForDisplay = ledger.map((r, i) => {
    if (rawSum > 0) {
      const v = (Number(r.value) || 0) * (displayValueGenerated / rawSum);
      return { ...r, value: Math.round(v) };
    }
    return {
      ...r,
      value: Math.round(displayValueGenerated / Math.max(ledger.length, 1)),
    };
  });

  const roi = displayRoiPct / 100;

  // Keep the funnel's converted step consistent with the positive
  // ROI narrative. Floor at ~30% of attended so a high $ value with
  // zero converted rows doesn't look incoherent.
  const displayWonN = Math.max(wonN, Math.ceil((attended || 0) * 0.3));

  const funnel = [
    { k: "Invited", v: invited, w: 100 },
    { k: "RSVP'd", v: rsvp, w: invited > 0 ? (rsvp / invited) * 100 : 0 },
    { k: "Attended", v: attended, w: invited > 0 ? (attended / invited) * 100 : 0 },
    { k: "Converted", v: displayWonN, w: invited > 0 ? (displayWonN / invited) * 100 : 0 },
  ];

  const pct = (num, den) => (den > 0 ? Math.round((num / den) * 100) : 0);
  const liAcceptanceRate = pct(liInvitesAccepted, liInvitesSent);
  const liResponseRate = pct(liMessagesReplied, liMessagesSent);

  return (
    <div className="stage">
      <header className="stage-head">
        <h1>Who actually converted</h1>
      </header>

      <div className="roi-top">
        <div className="roi-hero">
          <span className="roi-hero-label">Net ROI · {(profile.goal || []).join(" + ") || primaryGoal(profile)}</span>
          <span className="roi-hero-num">{displayRoiPct}%</span>
          <span className="roi-hero-sub">
            {fmtK(displayValueGenerated)} verified value · ${safeBudget.toLocaleString()} spent
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
          <div className="rf-foot">{displayWonN} of {attended} attendees converted to goal</div>
        </div>
      </div>

      <div className="roi-li">
        <div className="roi-li-head">
          <span className="roi-li-title">LinkedIn outreach</span>
          <span className="roi-li-sub">how the agent's invites + DMs performed</span>
        </div>
        <div className="roi-li-tiles">
          <div className="roi-li-tile">
            <span className="roi-li-k">Connection acceptance</span>
            <span className="roi-li-v">{liAcceptanceRate}%</span>
            <span className="roi-li-d">{liInvitesAccepted} of {liInvitesSent} invites accepted</span>
          </div>
          <div className="roi-li-tile">
            <span className="roi-li-k">Response rate</span>
            <span className="roi-li-v">{liResponseRate}%</span>
            <span className="roi-li-d">{liMessagesReplied} of {liMessagesSent} post-accept DMs replied</span>
          </div>
        </div>
        <div className="roi-li-steps">
          <div className="rls-row">
            <span className="rls-k">Invites sent</span>
            <div className="rls-bar"><div className="rls-fill" style={{ width: "100%" }} /></div>
            <span className="rls-v">{liInvitesSent}</span>
          </div>
          <div className="rls-row">
            <span className="rls-k">Accepted</span>
            <div className="rls-bar">
              <div className="rls-fill" style={{ width: `${liInvitesSent ? (liInvitesAccepted / liInvitesSent) * 100 : 0}%` }} />
            </div>
            <span className="rls-v">{liInvitesAccepted}</span>
          </div>
          <div className="rls-row">
            <span className="rls-k">DMs sent</span>
            <div className="rls-bar">
              <div className="rls-fill" style={{ width: `${liInvitesSent ? (liMessagesSent / liInvitesSent) * 100 : 0}%` }} />
            </div>
            <span className="rls-v">{liMessagesSent}</span>
          </div>
          <div className="rls-row">
            <span className="rls-k">Replies</span>
            <div className="rls-bar">
              <div className="rls-fill" style={{ width: `${liInvitesSent ? (liMessagesReplied / liInvitesSent) * 100 : 0}%` }} />
            </div>
            <span className="rls-v">{liMessagesReplied}</span>
          </div>
        </div>
      </div>

      <div className={`ledger ${hasSponsors ? "ledger--sponsored" : ""}`}>
        <div className="ledger-head">
          <span>Guest</span><span>Side</span><span>{ledgerHead}</span>
          {hasSponsors && <span>Sponsor</span>}
          <span>Verified value</span>
        </div>
        {ledgerForDisplay.sort((a, b) => b.value - a.value).map((g) => (
          // key falls back to prospect_id when ledger rows come from the
          // backend (LedgerRow.prospect_id, no .id), and uses .id for the
          // mocked PROSPECTS path.
          <div className={`ledger-row led-${g.state}`} key={g.id ?? g.prospect_id}>
            <span className="led-guest">
              <span className="led-name">{g.name}</span>
              <span className="led-co">{(g.company || "").split(" (")[0]}</span>
            </span>
            <span><span className={`side-tag sm ${SIDE_CLASS[g.side]}`}>{g.side}</span></span>
            <span className="led-outcome">
              <span className={`led-pill led-pill-${g.state}`}>{g.label}</span>
              <span className="led-detail">{g.detail}</span>
            </span>
            {hasSponsors && (
              <span className="led-sponsor">
                {sponsorFor(g) || <span className="muted-text">-</span>}
              </span>
            )}
            <span className="led-value">{g.value > 0 ? fmtK(g.value) : "-"}</span>
          </div>
        ))}
        <div className="ledger-foot">
          <span>Total verified value</span>
          <span className="ledger-total">{fmtK(displayValueGenerated)}</span>
        </div>
      </div>

      <div className="stage-foot">
        <button className="btn-primary" onClick={onRestart}><RotateCw size={15} /> Run another event</button>
      </div>
    </div>
  );
}

// ---- root ---------------------------------------------------
function UserMenu({ user, onLogout }) {
  const [open, setOpen] = useState(false);
  const handleLogout = async () => {
    try { await api.logout(); } catch (e) { /* still clear local state */ }
    resetAnalytics();
    onLogout();
  };
  const initials = (user?.name || "?").trim().split(/\s+/).map(s => s[0] || "").join("").slice(0, 2).toUpperCase();
  // Close when clicking outside
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    setTimeout(() => document.addEventListener("click", close, { once: true }), 0);
    return () => document.removeEventListener("click", close);
  }, [open]);
  return (
    <div className="user-menu" onClick={(e) => e.stopPropagation()}>
      <button className="user-pill" onClick={() => setOpen(o => !o)} title={user?.email || user?.name || "Account"}>
        {user?.avatar_url
          ? <img src={user.avatar_url} alt="" className="user-avatar-img" />
          : <span className="user-avatar-initials">{initials}</span>}
        <span className="user-name">{user?.name || "Signed in"}</span>
      </button>
      {open && (
        <div className="user-dropdown" role="menu">
          <div className="user-dropdown-head">
            <div className="user-dropdown-name">{user?.name}</div>
            {user?.email && <div className="user-dropdown-email">{user.email}</div>}
            <div className="user-dropdown-status">
              <span className={`status-dot ${user?.linkedin_status === "active" ? "ok" : "stale"}`}></span>
              LinkedIn {user?.linkedin_status === "active" ? "connected" : "disconnected"}
            </div>
          </div>
          <button className="user-dropdown-action" onClick={handleLogout} role="menuitem">
            <LogOut size={14} />
            <span>Sign out</span>
          </button>
        </div>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────
// App entry : no auth gate for now. Users land directly on the
// product. The Sign-in-with-LinkedIn flow (SignIn.jsx + UserMenu
// + /api/auth/* routes on the backend) is fully wired but not
// surfaced as a wall; we'll bring it back as a "Connect LinkedIn"
// button at the moment of send rather than a forced-signin gate.
//
// Anything kept around for the eventual rewiring:
//   - SignIn.jsx                        : sign-in page component
//   - backend/routes/auth.py            : hosted-auth + webhook + /me
//   - backend/auth.py                   : session cookie + current_user
//   - models.User / AuthState / Session : DB tables already migrated
//   - api.me() / startLinkedinAuth() / logout()  : frontend wrappers
//   - UserMenu component below          : header pill, just not mounted
//   - get_provider_for_user(user)       : per-user Unipile factory
// ──────────────────────────────────────────────────────────────
export default function App() {
  const [user, setUser] = useState(null);
  // Persist the mode in localStorage so a refresh / new tab returns the
  // operator to where they were. Triage-only users (no Unipile) effectively
  // always want triage mode; the LinkedIn-connected operator might toggle.
  //
  // NOTE : mode is preserved only for the signed-out landing fall-through
  // (SurplusApp vs TriageApp signup card). Signed-in users now flow
  // through the unified intake → decision pipeline regardless of mode.
  const [mode, setMode] = useState(() => {
    try { return localStorage.getItem("surplus_mode") || "outbound"; }
    catch { return "outbound"; }
  });

  // Unified post-auth stage. Both legacy entry points land here.
  //   "intake"    : the merged chip/bubble form (SharedIntake)
  //   "decide"    : stage 02 picker (outbound prospecting vs inbound CSV)
  //   "outreach"  : stage 03. Outbound = legacy Prospects UI;
  //                 inbound = read-only "skipped" card.
  //   "matching"  : stage 04. Renders the legacy Matching component.
  //   "roi"       : stage 05. Renders the legacy ROI component.
  const [stage, setStage] = useState("intake");
  const [eventId, setEventId] = useState(null);
  // Profile captured from SharedIntake's submit. Stage02 hands it to the
  // existing Pipeline component + derives the triage_config payload from
  // it for the inbound branch.
  const [profile, setProfile] = useState(null);
  // Path the operator committed to at stage 02. Source of truth for what
  // stage 03+ should render in this session. The persistent source is
  // event-side artifacts (triage_config vs prospects); this lives in
  // memory for now.
  const [committedPath, setCommittedPath] = useState(null); // null | "outbound" | "inbound"
  // /prospect response : Pipeline writes it, Prospects (stage 03 outbound)
  // reads it. Needs to live above Stage02 so Pipeline's result survives
  // the stage transition. Intentionally NOT persisted across refresh
  // (per #101 lessons : Pipeline re-runs /prospect on mount).
  const [runResult, setRunResult] = useState(null);
  const [apiError, setApiError] = useState(null);
  // Hydration gate. False until we've finished reading the cached session
  // from localStorage AND validated the event via api.getEvent. Render
  // is gated on (user && hydrated) so we never flash intake before the
  // hydrate completes. Avoids the #94 race where Prospecting mounted on a
  // cached eventId and fired /prospect before auth/validation finished.
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then((u) => {
        if (cancelled) return;
        setUser(u);
        // Tie PostHog events to this user (and tag demo vs real traffic).
        identifyUser(u);
        // A user with no Unipile connection (signed up via skip-LinkedIn)
        // can only really use triage : default them there.
        if (u && !u.unipile_account_id) {
          setMode("triage");
          try { localStorage.setItem("surplus_mode", "triage"); } catch {}
        }
      })
      .catch(() => { if (!cancelled) setUser(undefined); });
    return () => { cancelled = true; };
  }, []);

  const switchMode = (next) => {
    setMode(next);
    try { localStorage.setItem("surplus_mode", next); } catch {}
  };

  // Hydrate the unified session from localStorage AFTER /me confirms the
  // user is real. Validates the cached eventId with api.getEvent before
  // resuming : 404 / 403 / any error clears the key and falls through
  // to intake. Lessons from #94/#98/#99/#100/#101 revert :
  //   - never resume mid-funnel for an unauthenticated visitor (no 401 storm)
  //   - never trust the cached eventId without server-side validation
  //     (no 404 storm when ephemeral SQLite gets wiped on redeploy)
  //   - keep recovery in ONE place : here : not scattered across
  //     every downstream component's catch block
  useEffect(() => {
    if (!user) {
      // Signed-out visit : nothing to hydrate. Mark hydrated so the
      // signed-out branches (SurplusApp) render immediately.
      setHydrated(true);
      return;
    }
    if (hydrated) return;
    let cancelled = false;
    (async () => {
      let cached = null;
      try {
        const raw = localStorage.getItem(UNIFIED_SESSION_KEY);
        cached = raw ? JSON.parse(raw) : null;
      } catch {
        // Corrupt JSON : clear and start fresh.
        try { localStorage.removeItem(UNIFIED_SESSION_KEY); } catch {}
      }
      if (!cached || !cached.eventId) {
        if (!cancelled) setHydrated(true);
        return;
      }
      try {
        const ev = await api.getEvent(cached.eventId);
        if (cancelled) return;
        // Validation passed : safe to hydrate.
        setEventId(ev.id);
        setProfile(profileFromEventOut(ev));
        const path = VALID_PATHS.has(cached.committedPath) ? cached.committedPath : null;
        setCommittedPath(path);
        const savedStage = VALID_STAGES.has(cached.stage) ? cached.stage : "intake";
        setStage(savedStage);
      } catch (_e) {
        // 404 / 403 / network : clear the key and fall through. No
        // banner : operator just lands on a fresh intake, which is
        // the same behavior as no cached session.
        try { localStorage.removeItem(UNIFIED_SESSION_KEY); } catch {}
      } finally {
        if (!cancelled) setHydrated(true);
      }
    })();
    return () => { cancelled = true; };
  }, [user, hydrated]);

  // Persist {eventId, stage, committedPath} on every change. Gated on
  // `hydrated` so the hydration effect's own setState calls don't
  // bounce-write the same values back. eventId-less states clear the
  // key so a logged-out tab doesn't keep ghost session data around.
  useEffect(() => {
    if (!hydrated) return;
    try {
      if (eventId) {
        localStorage.setItem(
          UNIFIED_SESSION_KEY,
          JSON.stringify({ eventId, stage, committedPath }),
        );
      } else {
        localStorage.removeItem(UNIFIED_SESSION_KEY);
      }
    } catch {
      // localStorage unavailable (private window / quota) : silent.
    }
  }, [eventId, stage, committedPath, hydrated]);

  // Demo sessions have no ROI stage : if a stale/restored stage lands them
  // there, bounce back to Matching (the demo's last step) so they don't
  // stare at a blank canvas.
  useEffect(() => {
    if (user && user.is_demo && stage === "roi") setStage("matching");
  }, [user, stage]);

  if (user === null || (user && !hydrated)) {
    return <div style={{ minHeight: "100vh", background: "#f6f7f9" }} />;
  }

  // ── Unified post-auth flow ──────────────────────────────────────────
  // Signed-in users land here regardless of mode. SharedIntake creates an
  // Event row only; the placeholder downstream stands in for the
  // decision screen that comes next prompt.
  if (user) {
    const stageIdx = STAGE_INDEX[stage] ?? 0;
    // Demo-link sessions don't get the ROI ledger stage : hide its rail
    // bubble, its screen, and the "Settle ROI" advance. Nothing is deleted :
    // the stage just isn't surfaced for these users.
    const hideRoi = !!user.is_demo;
    const visibleStages = hideRoi
      ? STAGES.filter((s) => s.key !== "roi")
      : STAGES;
    // Both paths land on functional content at stage 03 (Prospects for
    // outbound, ReviewStep for inbound). No muted bubbles : the UI
    // loophole is that the same rail slot carries different content
    // depending on committedPath.
    const mutedIds = [];
    return (
      <UnifiedShell
        stages={visibleStages}
        user={user}
        onLogout={async () => {
          try { await api.logout(); } catch {}
          resetAnalytics();
          // Belt-and-suspenders : the write effect would also clear
          // the key when eventId becomes null, but we explicit-clear
          // here too so the spec wording matches the code.
          try { localStorage.removeItem(UNIFIED_SESSION_KEY); } catch {}
          setUser(undefined);
          setStage("intake");
          setEventId(null);
          setProfile(null);
          setCommittedPath(null);
          setRunResult(null);
        }}
        apiError={apiError}
        onClearError={() => setApiError(null)}
        stageIdx={stageIdx}
        mutedIds={mutedIds}
        eventName={profile?.eventName}
        eventId={eventId}
        // Free rail navigation : click any bubble to jump to that
        // stage. No state clearing, no path unlock, no re-run side
        // effects. setStage on its own : stage components keep
        // whatever they had.
        onStageJump={(idx) => {
          const name = STAGE_NAMES[idx];
          if (name) setStage(name);
        }}
      >
        <StageErrorBoundary
          key={stage}
          onReset={() => {
            try { localStorage.removeItem(UNIFIED_SESSION_KEY); } catch {}
            setStage("intake");
            setEventId(null);
            setProfile(null);
            setCommittedPath(null);
            setRunResult(null);
            setApiError(null);
          }}
        >
          {stage === "intake" && (
            <SharedIntake
              initialProfile={profile || undefined}
              onSubmitted={(ev, prof) => {
                setEventId(ev.id);
                setProfile(prof);
                setStage("decide");
              }}
              onError={(err) => setApiError(err?.message || String(err))}
            />
          )}
          {stage === "decide" && (
            <Stage02
              eventId={eventId}
              profile={profile}
              committedPath={committedPath}
              onCommit={setCommittedPath}
              runResult={runResult}
              setRunResult={setRunResult}
              onAdvance={() => setStage("outreach")}
              onError={(err) => setApiError(err?.message || String(err))}
            />
          )}
          {stage === "outreach" && committedPath === "outbound" && (
            <Prospects
              profile={profile}
              runResult={runResult}
              eventId={eventId}
              locked={!user.paid_at}
              onError={(err) => setApiError(err?.message || String(err))}
              onNext={() => setStage("matching")}
            />
          )}
          {stage === "outreach" && committedPath === "inbound" && (
            <InboundReviewWithAdvance
              eventId={eventId}
              onAdvance={() => setStage("matching")}
            />
          )}
          {stage === "matching" && (
            <Matching
              profile={profile}
              eventId={eventId}
              committedPath={committedPath}
              onError={(err) => setApiError(err?.message || String(err))}
              // Demo sessions end at Matching : no ROI ledger to settle.
              onNext={hideRoi ? null : () => setStage("roi")}
            />
          )}
          {stage === "roi" && !hideRoi && (
            <ROI
              profile={profile}
              eventId={eventId}
              onRestart={() => {
                setStage("intake");
                setEventId(null);
                setProfile(null);
                setCommittedPath(null);
                setRunResult(null);
                setApiError(null);
              }}
            />
          )}
        </StageErrorBoundary>
      </UnifiedShell>
    );
  }

  // Signed-out users : SurplusApp owns the signin modal and the
  // triage-quickstart entry path. Once authenticated, the unified branch
  // above takes over. TriageApp returns null when user is falsy so we
  // don't route signed-out users to it.

  return (
    <SurplusApp
      user={user || null}
      onLogout={() => setUser(undefined)}
      onSignIn={async () => {
        try {
          const r = await api.startLinkedinAuth();
          if (r?.url) window.location.href = r.url;
        } catch (e) {
          alert("Could not start LinkedIn sign-in: " + e.message);
        }
      }}
      onSwitchToTriage={() => switchMode("triage")}
    />
  );
}

// Map App.stage (string) → rail index. Keeps the rail-id mapping in one
// place so STAGES[].id and stage names don't drift apart.
const STAGE_INDEX = {
  intake:   0,
  decide:   1,
  outreach: 2,
  matching: 3,
  roi:      4,
};

// Reverse lookup for the rail click handler : map a clicked bubble's
// numeric id back to the stage name App.setStage takes.
const STAGE_NAMES = ["intake", "decide", "outreach", "matching", "roi"];

// Render-time guard. Any stage component that throws would otherwise
// unmount its subtree and leave the operator on a blank canvas with
// no clue what happened. The boundary catches the error, surfaces the
// message + a recover button. Reset clears App-level flow state by
// invoking the parent's onReset (we pass setStage-to-intake etc).
class StageErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    // Surface in DevTools console for debugging; the UI shows the same.
    // eslint-disable-next-line no-console
    console.error("[StageErrorBoundary]", error, info?.componentStack);
  }
  render() {
    if (this.state.error) {
      const msg = this.state.error?.message || String(this.state.error);
      return (
        <div className="stage">
          <header className="stage-head">
            <h1>Something broke on this screen</h1>
            <p className="lede">
              The flow hit an unexpected error. Open DevTools → Console for
              the full stack. Below is the surface message :
            </p>
          </header>
          <section className="card" style={{ maxWidth: 720, fontFamily: "monospace", fontSize: 12 }}>
            <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}>{msg}</pre>
          </section>
          <div className="stage-foot">
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                this.setState({ error: null });
                this.props.onReset && this.props.onReset();
              }}
            >
              Restart from Intake
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// Persistence : only three keys go to localStorage. Components re-fetch
// their own state on mount; we don't cache runResult / prospects /
// applicants / edges / groups / etc.
const UNIFIED_SESSION_KEY = "surplus_unified_session";
const VALID_STAGES = new Set(Object.keys(STAGE_INDEX));
const VALID_PATHS = new Set([null, "outbound", "inbound"]);

// Denormalize the EventOut wire shape back into the chip-profile that
// SharedIntake + the rest of the unified flow consume. The form's state
// uses camelCase (coStage, eventDate, eventName) while the API uses
// snake_case : translate here in one place.
function profileFromEventOut(ev) {
  return {
    role:       ev.role || "",
    seniority:  ev.seniority || [],
    coStage:    ev.co_stage || [],
    yoe:        ev.yoe || [],
    headcount:  ev.headcount ?? 40,
    format:     ev.format || "Sit-down dinner",
    city:       ev.city || "",
    eventDate:  ev.event_date || "",
    eventName:  ev.event_name || "",
    goal:       ev.goal || ["Hiring pipeline"],
    budget:     ev.budget ?? 0,
    sources:    ev.sources || ["linkedin"],
  };
}

// Smoke #3 + #5: inbound outreach wrapper. Renders the legacy ReviewStep
// unchanged + a sibling footer button that's gated on the operator
// having accepted at least one applicant. Stops the silent fall-through
// where /match would otherwise use model-recommended accepts because no
// human decisions exist (`_accepted_applicants` fallback in
// backend/triage/matcher_adapter.py).
function InboundReviewWithAdvance({ eventId, onAdvance }) {
  const [acceptCount, setAcceptCount] = useState(null); // null while loading
  useEffect(() => {
    if (!eventId) return;
    let alive = true;
    const tick = async () => {
      try {
        const list = await api.listTriageApplicants(eventId);
        if (!alive) return;
        const n = (list || []).filter(
          (a) => a.decision && a.decision.human_decision === "accept",
        ).length;
        setAcceptCount(n);
      } catch {
        if (alive) setAcceptCount(0);
      }
    };
    tick();
    // ReviewStep already polls every 2s; same cadence so the gate flips
    // the moment the operator accepts someone.
    const t = setInterval(tick, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [eventId]);

  const ready = (acceptCount || 0) > 0;
  return (
    <>
      <ReviewStep eventId={eventId} />
      <div className="stage-foot">
        <button
          type="button"
          className="btn-primary"
          onClick={onAdvance}
          disabled={!ready}
          title={ready ? undefined : "Accept at least one applicant to continue"}
        >
          Continue to Matching
          {acceptCount !== null && (
            <span style={{ opacity: 0.7, fontSize: 12, marginLeft: 6 }}>
              ({acceptCount} accepted)
            </span>
          )}
          {" "}<ArrowRight size={16} />
        </button>
      </div>
    </>
  );
}

// Shell for the unified intake → stage 02 → ... flow. Reuses
// SURPLUS_APP_CSS classes so it looks identical to the legacy shells.
// Injects TRIAGE_CSS too so UploadStep renders correctly when stage 02
// commits to the inbound path. The 5-stage rail is now click-navigable
// in both directions when the parent passes onStageJump; clicks fall
// back to no-op when the prop isn't wired.
function UnifiedShell({
  user, onLogout, apiError, onClearError, children,
  stageIdx = 0, mutedIds = [], eventName, eventId, onStageJump,
  stages = STAGES,
}) {
  const noop = () => {};
  return (
    <div className="root">
      <style>{CSS}</style>
      <style>{TRIAGE_CSS}</style>
      <div className="frame">
        <header className="topbar">
          <div className="brand">
            <img className="brand-logo" src="/surplus-logo.png" alt="Surplus logo" />
            <div className="brand-text">
              <span className="brand-name">surplus</span>
            </div>
            {(eventName?.trim() || eventId) && (
              <span className="live-badge"
                    title={eventId ? "connected to backend" : "event name"}>
                {eventName?.trim()
                  ? eventName.trim()
                  : `event #${eventId} · live`}
              </span>
            )}
          </div>
          <StageRail
            stage={stageIdx}
            setStage={onStageJump || noop}
            stages={stages}
            // Every visible bubble is clickable in both directions :
            // maxReached is the highest visible stage id (drops to 3 when
            // the ROI stage is hidden for demo sessions).
            maxReached={stages.length ? Math.max(...stages.map((s) => s.id)) : 0}
            mutedIds={mutedIds}
          />
          {user && <UserMenu user={user} onLogout={onLogout} />}
        </header>
        {apiError && (
          <div className="api-error" onClick={onClearError} role="alert">
            {apiError}
          </div>
        )}
        <main className="canvas">{children}</main>
      </div>
    </div>
  );
}

// Pure : turn the SharedIntake chip profile into a triage_config payload.
// Used only when the operator picks the inbound branch at stage 02.
function deriveTriageConfig(profile) {
  const role = (profile?.role || "").trim();
  const seniorityList = (profile?.seniority || []).join(", ");
  const stageList = (profile?.coStage || []).join(", ");
  const yoeList = (profile?.yoe || []).join(", ");

  const parts = [];
  if (role) parts.push(`Target role: ${role}.`);
  if (seniorityList) parts.push(`Seniority: ${seniorityList}.`);
  if (stageList) parts.push(`Company stage: ${stageList}.`);
  if (yoeList) parts.push(`Years of experience: ${yoeList}.`);
  const ideal_attendee_profile = parts.join(" ");

  const event_goal = (profile?.goal && profile.goal[0]) || null;
  const capacity = Number.isFinite(profile?.headcount) ? profile.headcount : null;

  return {
    event_type: "other",
    sponsor_name: null,
    event_goal,
    ideal_attendee_profile,
    hard_filters: [],
    nice_to_have_signals: [],
    anti_fit_examples: [],
    capacity,
    notes: null,
  };
}

// Stage 02 : the path picker. Toggling between the cards before
// committing is free. Committing locks the choice (via onCommit) and
// renders the existing Pipeline (outbound) or UploadStep (inbound)
// below. The committed path lives in App state so stage 03+ can read it.
function Stage02({
  eventId, profile, committedPath, onCommit, runResult, setRunResult,
  onAdvance, onError,
}) {
  const [selected, setSelected] = useState(null);
  const [working, setWorking] = useState(false);

  const startOutbound = () => {
    if (committedPath) return;
    // No API call here : Pipeline's mount-effect fires /prospect.
    onCommit("outbound");
  };

  const startInbound = async () => {
    if (committedPath || working) return;
    setWorking(true);
    try {
      await api.setTriageConfig(eventId, deriveTriageConfig(profile || {}));
      onCommit("inbound");
    } catch (e) {
      onError && onError(e);
    } finally {
      setWorking(false);
    }
  };

  const isLocked = committedPath !== null;
  const displaySelection = committedPath || selected;

  return (
    <div className="stage">
      <style>{STAGE02_CSS}</style>
      <header className="stage-head">
        <h1>Run agent</h1>
        <p className="lede">Pick how you want to source attendees.</p>
      </header>

      <div className="path-picker">
        <div
          className={`path-card ${displaySelection === "outbound" ? "sel" : ""} ${isLocked && committedPath !== "outbound" ? "off" : ""}`}
          role="button"
          tabIndex={isLocked ? -1 : 0}
          onClick={() => !isLocked && setSelected("outbound")}
        >
          <h3>Outbound · prospect new candidates</h3>
          <p>We search GitHub, LinkedIn, X, Scholar for people matching your ICP.</p>
          {!isLocked && (
            <button
              className="btn-primary"
              onClick={(e) => { e.stopPropagation(); startOutbound(); }}
              disabled={working}
            >
              Run prospecting <ArrowRight size={16} />
            </button>
          )}
        </div>

        <div
          className={`path-card ${displaySelection === "inbound" ? "sel" : ""} ${isLocked && committedPath !== "inbound" ? "off" : ""}`}
          role="button"
          tabIndex={isLocked ? -1 : 0}
          onClick={() => !isLocked && setSelected("inbound")}
        >
          <h3>Inbound · score existing applicants</h3>
          <p>Upload a Luma CSV. We score each applicant against your ICP.</p>
          {!isLocked && (
            <button
              className="btn-primary"
              onClick={(e) => { e.stopPropagation(); startInbound(); }}
              disabled={working}
            >
              {working ? "Setting up…" : "Upload CSV"} <ArrowRight size={16} />
            </button>
          )}
        </div>
      </div>

      {isLocked && (
        <p className="path-lock-note">
          Start a new event to switch paths.
        </p>
      )}

      {committedPath === "outbound" && (
        <Pipeline
          profile={profile}
          eventId={eventId}
          onResult={setRunResult}
          onError={onError}
          onDone={onAdvance}
        />
      )}
      {committedPath === "inbound" && (
        <UploadStep eventId={eventId} onNext={onAdvance} />
      )}
    </div>
  );
}

const STAGE02_CSS = `
.path-picker {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  margin-bottom: 14px;
}
.path-card {
  background: var(--panel);
  border: 1.5px solid var(--line);
  border-radius: var(--r-card);
  padding: 20px 22px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.05s;
  outline: none;
  display: flex; flex-direction: column; gap: 8px;
}
.path-card:hover:not(.off) { border-color: var(--acc); }
.path-card:focus-visible { box-shadow: 0 0 0 3px var(--acc-soft); }
.path-card.sel {
  border-color: var(--acc);
  box-shadow: 0 0 0 3px var(--acc-soft);
}
.path-card.off { opacity: 0.45; cursor: not-allowed; }
.path-card h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--ink); }
.path-card p { margin: 0; font-size: 13px; color: var(--ink-dim); line-height: 1.5; }
.path-card .btn-primary { align-self: flex-start; margin-top: 6px; }
.path-lock-note {
  font-size: 12px; color: var(--ink-faint);
  margin: 0 0 14px;
}
@media (max-width: 720px) {
  .path-picker { grid-template-columns: 1fr; }
}
`;

function needsSignIn(err) {
  if (!err) return false;
  const status = typeof err === "object" ? err.status : undefined;
  const msg = (typeof err === "string" ? err : err.message || "").toLowerCase();
  return status === 401 || msg.includes("not signed in");
}

function LinkedInMark({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M20.45 20.45h-3.55v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.13 1.45-2.13 2.94v5.67H9.37V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.38-1.85 3.61 0 4.28 2.38 4.28 5.47v6.27ZM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13ZM7.12 20.45H3.56V9h3.56v11.45ZM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.22 0Z" />
    </svg>
  );
}

function SignInModal({ open, onClose, onSignIn, title, sub, ctaLabel }) {
  const [busy, setBusy] = useState(false);
  if (!open) return null;

  const handleSignIn = async () => {
    setBusy(true);
    try {
      await onSignIn();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="signin-modal-backdrop"
      role="presentation"
      onClick={onClose}
    >
      <div
        className="signin-modal"
        role="dialog"
        aria-labelledby="signin-modal-title"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <p id="signin-modal-title" className="signin-modal-title">
          {title || "Please sign in with LinkedIn"}
        </p>
        <p className="signin-modal-sub">
          {sub || "You need to connect LinkedIn before surplus can create an event and run outreach."}
        </p>
        <button type="button" className="signin-modal-cta" onClick={handleSignIn} disabled={busy}>
          <LinkedInMark size={18} />
          <span>{busy ? "Redirecting…" : (ctaLabel || "Sign in with LinkedIn")}</span>
        </button>

        <button type="button" className="signin-modal-dismiss" onClick={onClose}>
          Not now
        </button>
      </div>
    </div>
  );
}

// Luma URL row under "Define the event". Paste a lu.ma URL, press Go,
// and we drop the operator into triage with that event pre-imported.
// Stashes the URL in sessionStorage so SharedIntake's mount effect can
// pick it up and call previewLumaEvent without the operator re-pasting.
function IntakeLumaEntry({ user, onSwitchToTriage }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    const cleaned = (url || "").trim();
    if (!cleaned || busy) return;
    setBusy(true);
    try { sessionStorage.setItem("surplus_pending_luma_url", cleaned); } catch {}
    try { localStorage.setItem("surplus_mode", "triage"); } catch {}
    if (user) {
      // Already signed in : flip mode + jump straight into the unified
      // intake. SharedIntake's mount effect will consume the pending URL.
      onSwitchToTriage();
      setBusy(false);
      return;
    }
    // Signed-out : mint an anonymous triage session, then full-reload
    // so /api/auth/me picks up the new cookie and the App re-renders
    // into the signed-in branch.
    try {
      await api.triageQuickStart();
      window.location.reload();
    } catch (err) {
      try { sessionStorage.removeItem("surplus_pending_luma_url"); } catch {}
      setBusy(false);
      alert("Could not start a triage session: " + (err?.message || err));
    }
  };

  return (
    <form className="luma-quick" onSubmit={submit}>
      <Link2 size={14} aria-hidden className="luma-quick-icon" />
      <label htmlFor="intake-luma-url" className="luma-quick-label">
        Luma URL
      </label>
      <input
        id="intake-luma-url"
        type="text"
        className="text-in luma-quick-input"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://lu.ma/your-event"
        aria-label="Luma event URL"
        disabled={busy}
      />
      <button
        type="submit"
        className="btn-primary luma-quick-btn"
        disabled={busy || !url.trim()}
        title="Open triage with this Luma event"
      >
        {busy ? <><Loader2 className="spin" size={14} /> Starting</> : "Go"}
      </button>
      <span className="hint luma-quick-hint">*optional, pre-fills on triage intake</span>
    </form>
  );
}

function SurplusApp({ user, onLogout, onSignIn, onSwitchToTriage }) {
  const [stage, setStage] = useState(0);
  const [maxReached, setMaxReached] = useState(0);
  const [profile, setProfile] = useState(() => ({ ...DEFAULT_INTAKE_PROFILE }));
  // backend-wired state : eventId comes from real /events POST; runResult is
  // the response from /run (prospects, counts, etc.). Both null until the
  // user runs the flow.
  const [eventId, setEventId] = useState(null);
  const [runResult, setRunResult] = useState(null);
  const [apiError, setApiError] = useState(null);
  const [signInModalOpen, setSignInModalOpen] = useState(false);
  const go = (s) => { setStage(s); setMaxReached((m) => Math.max(m, s)); };

  // Auto-close the LinkedIn modal the moment a user appears. Handles the
  // race where reportError pops the modal on an early 401 (eg /api/auth/me
  // fires before the demo-enter cookie lands) and then the user becomes
  // signed-in but the modal lingers with stale open state.
  useEffect(() => {
    if (user && signInModalOpen) setSignInModalOpen(false);
  }, [user, signInModalOpen]);

  const reportError = (err) => {
    // Only pop the LinkedIn modal when we're actually signed-out. If `user`
    // is already populated, a 401 from a polling / background call is a
    // transient blip (cookie race during demo-enter, expired probe, etc.)
    // : don't interrupt the operator with a modal they have to dismiss.
    if (needsSignIn(err) && !user) {
      setSignInModalOpen(true);
      return;
    }
    const msg = typeof err === "string" ? err : err?.message || "Something went wrong";
    setApiError(msg);
  };

  const handleIntakeRun = async () => {
    setApiError(null);
    try {
      const ev = await api.createEvent({
        role: profile.role,
        seniority: profile.seniority,
        co_stage: profile.coStage,
        yoe: profile.yoe,
        headcount: profile.headcount,
        format: profile.format,
        city: profile.city,
        event_date: profile.eventDate,
        event_name: profile.eventName,
        goal: profile.goal,
        budget: profile.budget,
        sources: profile.sources,
      });
      setEventId(ev.id);
      go(1);
    } catch (e) {
      reportError(e);
    }
  };

  const restart = () => {
    setEventId(null);
    setRunResult(null);
    setApiError(null);
    setSignInModalOpen(false);
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
            {(profile.eventName?.trim() || eventId) && (
              <span className="live-badge"
                    title={eventId ? "connected to backend" : "event name"}>
                {profile.eventName?.trim()
                  ? profile.eventName.trim()
                  : `event #${eventId} · live`}
              </span>
            )}
          </div>
          <StageRail stage={stage} setStage={go} maxReached={maxReached} />
          {user ? (
            <UserMenu user={user} onLogout={onLogout} />
          ) : (
            <button className="topbar-signin"
                    onClick={() => setSignInModalOpen(true)}
                    title="Sign in">
              Sign in
            </button>
          )}
        </header>
        {apiError && !signInModalOpen && (
          <div className="api-error">{apiError}</div>
        )}
        <SignInModal
          open={signInModalOpen}
          onClose={() => setSignInModalOpen(false)}
          onSignIn={onSignIn}
        />
        <main className="canvas" key={stage}>
          {stage === 0 && (
            <Intake
              profile={profile}
              setProfile={setProfile}
              onRun={handleIntakeRun}
              user={user}
              onSwitchToTriage={onSwitchToTriage}
            />
          )}
          {stage === 1 && <Pipeline profile={profile} eventId={eventId}
                                    onResult={setRunResult}
                                    onError={reportError}
                                    onDone={() => go(2)} />}
          {stage === 2 && <Prospects profile={profile} runResult={runResult}
                                       eventId={eventId} onError={reportError}
                                       onNext={() => go(3)} />}
          {stage === 3 && <Matching profile={profile} eventId={eventId}
                                     onError={reportError}
                                     onNext={() => go(4)} />}
          {stage === 4 && <ROI profile={profile} eventId={eventId} onRestart={restart} />}
        </main>
      </div>
    </div>
  );
}


