import React, { useState } from "react";
import {
  ArrowRight, CornerDownRight, Link2, Loader2, AlertCircle, Check,
} from "lucide-react";
import { api } from "./lib/api.js";

// Single intake form shared by outbound (SurplusApp) and inbound (TriageApp).
// UI mirrors the legacy outbound Intake exactly : chips, sliders, three cards.
// The Luma-URL pre-fill row only renders for mode === "inbound".

const FORMATS = ["Sit-down dinner", "Hackathon", "Workshop", "Mixer", "Roundtable"];
const GOALS = ["Hiring pipeline", "Fundraising", "Sales pipeline", "Product testing", "Community density"];
const SENIORITY = ["Student", "New grad", "Junior", "Senior", "Staff+", "Leadership"];
const STAGES_CO = ["Pre-seed", "Seed", "Series A", "Series B+", "Enterprise"];
const YOE = ["0-2", "3-5", "6-10", "10+"];

const SOURCES = [
  { key: "linkedin", label: "LinkedIn", locked: true },
  { key: "github",   label: "GitHub" },
  { key: "scholar",  label: "Scholar" },
];

const FORMAT_CONFIG = {
  "Sit-down dinner": { topo: "fixed seating : composition locked before doors open" },
  "Hackathon":       { topo: "team formation : complementary skills balanced per team" },
  "Workshop":        { topo: "fluid breakouts : groups regroup between sessions" },
  "Mixer":           { topo: "soft clusters : seeded, not enforced" },
  "Roundtable":      { topo: "single ring : seating order is the lever" },
};

const DEFAULT_PROFILE = {
  role: "Infrastructure / ML platform engineers",
  seniority: ["Staff+"],
  coStage: ["Seed"],
  yoe: ["6-10"],
  headcount: 40,
  format: "Sit-down dinner",
  city: "San Francisco",
  eventDate: "",
  eventName: "",
  goal: ["Hiring pipeline"],
  budget: 8000,
  sources: ["linkedin"],
};

const Chip = ({ active, onClick, children }) => (
  <button type="button" className={`chip ${active ? "chip-on" : ""}`} onClick={onClick}>{children}</button>
);

function toggleIn(arr, v) {
  const cur = Array.isArray(arr) ? arr : [arr].filter(Boolean);
  if (cur.includes(v)) {
    return cur.length > 1 ? cur.filter((x) => x !== v) : cur;
  }
  return [...cur, v];
}

// Pure : turn the outbound-shaped chip profile into a triage_config payload.
// Used only when mode === "inbound". Exported for unit testing.
export function deriveTriageConfig(profile) {
  const role = (profile.role || "").trim();
  const seniorityList = (profile.seniority || []).join(", ");
  const stageList = (profile.coStage || []).join(", ");
  const yoeList = (profile.yoe || []).join(", ");

  const parts = [];
  if (role) parts.push(`Target role: ${role}.`);
  if (seniorityList) parts.push(`Seniority: ${seniorityList}.`);
  if (stageList) parts.push(`Company stage: ${stageList}.`);
  if (yoeList) parts.push(`Years of experience: ${yoeList}.`);
  const ideal_attendee_profile = parts.join(" ");

  const event_goal = (profile.goal && profile.goal[0]) || null;
  const capacity = Number.isFinite(profile.headcount) ? profile.headcount : null;

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

export default function SharedIntake({
  mode = "outbound",
  initialProfile,
  onSubmitted,
  onError,
}) {
  const [profile, setProfile] = useState(() => ({ ...DEFAULT_PROFILE, ...(initialProfile || {}) }));
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  // Luma import : inbound-only pre-fill at the top of the form.
  const [lumaUrl, setLumaUrl] = useState("");
  const [lumaLoading, setLumaLoading] = useState(false);
  const [lumaError, setLumaError] = useState(null);
  const [lumaImported, setLumaImported] = useState(null);

  const set = (k, v) => setProfile((p) => ({ ...p, [k]: v }));
  const toggle = (k, v) => setProfile((p) => ({ ...p, [k]: toggleIn(p[k], v) }));

  const handleLumaImport = async () => {
    setLumaError(null);
    const url = (lumaUrl || "").trim();
    if (!url) {
      setLumaError("Paste a Luma event URL (lu.ma/...).");
      return;
    }
    setLumaLoading(true);
    try {
      const res = await api.previewLumaEvent(url);
      const ev = res.event || {};
      const sug = res.suggestions || {};
      setProfile((p) => {
        const next = { ...p };
        if (ev.name && !next.eventName.trim()) next.eventName = ev.name;
        const cap = Number(ev.capacity);
        if (Number.isFinite(cap) && cap > 0) next.headcount = cap;
        if (ev.location && !next.city.trim()) next.city = ev.location;
        if (sug.ideal_attendee_profile && !next.role.trim()) {
          next.role = sug.ideal_attendee_profile;
        }
        return next;
      });
      setLumaImported(ev);
    } catch (err) {
      setLumaError(err.message || "Could not import from Luma.");
    } finally {
      setLumaLoading(false);
    }
  };

  const handleSubmit = async () => {
    if (submitting) return;
    setSubmitError(null);
    setSubmitting(true);
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
      if (mode === "inbound") {
        await api.setTriageConfig(ev.id, deriveTriageConfig(profile));
      }
      onSubmitted && onSubmitted(ev, profile);
    } catch (e) {
      const msg = e?.message || "Could not create event.";
      setSubmitError(msg);
      onError && onError(e);
      setSubmitting(false);
    }
  };

  const submitLabel = mode === "inbound" ? "Continue" : "Run agent pipeline";

  return (
    <div className="stage">
      <header className="stage-head">
        <h1>Define the event</h1>
      </header>

      {mode === "inbound" && (
        <section className="card">
          <h3>
            <span className="card-num"><Link2 size={12} strokeWidth={2.5} aria-hidden /></span>
            Import from Luma <span className="hint">: optional — we&apos;ll pre-fill name + capacity + location</span>
          </h3>
          <div className="luma-import-row">
            <input className="text-in" value={lumaUrl}
              onChange={(e) => setLumaUrl(e.target.value)}
              placeholder="https://lu.ma/your-event"
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); handleLumaImport(); }
              }} />
            <button type="button" className="btn-primary"
              disabled={lumaLoading || !lumaUrl.trim()}
              onClick={handleLumaImport}>
              {lumaLoading ? (
                <><Loader2 className="spin" size={16} /> Importing…</>
              ) : (
                <>Import <ArrowRight size={16} /></>
              )}
            </button>
          </div>
          {lumaError && (
            <div className="api-error" role="alert" style={{ marginTop: 10 }}>
              <AlertCircle size={14} /> {lumaError}
            </div>
          )}
          {lumaImported && !lumaError && (
            <div className="luma-ok-banner">
              <Check size={14} /> Imported &quot;{lumaImported.name || "event"}&quot;
              {lumaImported.location ? ` · ${lumaImported.location}` : ""}
              {lumaImported.capacity ? ` · cap ${lumaImported.capacity}` : ""}
              . Review the chips below before continuing.
            </div>
          )}
        </section>
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
              <span className="derived-v">${Math.round(profile.budget / Math.max(1, profile.headcount))}</span>
            </div>
          </div>
        </section>
      </div>

      {submitError && (
        <div className="api-error" role="alert">
          <AlertCircle size={14} /> {submitError}
        </div>
      )}

      <div className="stage-foot">
        <button type="button" className="btn-primary" onClick={handleSubmit} disabled={submitting}>
          {submitting ? (
            <><Loader2 className="spin" size={16} /> {mode === "inbound" ? "Saving…" : "Starting…"}</>
          ) : (
            <>{submitLabel} <ArrowRight size={16} /></>
          )}
        </button>
      </div>
    </div>
  );
}
