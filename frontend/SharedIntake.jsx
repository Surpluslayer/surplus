import React, { useState, useEffect } from "react";
import { ArrowRight, CornerDownRight, Loader2, AlertCircle, Link2, Check } from "lucide-react";
import { api } from "./lib/api.js";

// Unified intake form for the merged app. Mode-less : both downstream
// branches (outbound prospecting, inbound triage) start from this same
// screen. Submitting creates an Event row via api.createEvent and nothing
// else: no triage_config write, no prospecting kickoff. The downstream
// decision happens on the next screen.

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

export default function SharedIntake({ initialProfile, onSubmitted, onError }) {
  const [profile, setProfile] = useState(() => ({ ...DEFAULT_PROFILE, ...(initialProfile || {}) }));
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  // Luma import : optional pre-fill at the bottom of the form. Re-uses
  // the existing backend scraper (api.previewLumaEvent → /events/triage/
  // luma-preview → backend/triage/luma.py). Never overwrites operator
  // input: only fills empty fields via the (prev) => prev || ... pattern.
  // suggestions.hard_filters / anti_fit_examples are intentionally NOT
  // mapped to chip groups (too risky to auto-toggle).
  const [lumaUrl, setLumaUrl] = useState("");
  const [lumaLoading, setLumaLoading] = useState(false);
  const [lumaError, setLumaError] = useState(null);
  const [lumaImported, setLumaImported] = useState(null);

  const set = (k, v) => setProfile((p) => ({ ...p, [k]: v }));
  const toggle = (k, v) => setProfile((p) => ({ ...p, [k]: toggleIn(p[k], v) }));

  const handleLumaImport = async (maybeUrl) => {
    setLumaError(null);
    // Accept an explicit URL so the topbar entry path can hand us a
    // pending URL without round-tripping through React state. Button
    // onClick passes a SyntheticEvent : ignore non-strings.
    const explicit = typeof maybeUrl === "string" ? maybeUrl : null;
    const url = ((explicit ?? lumaUrl) || "").trim();
    if (!url) {
      setLumaError("Paste an event URL (lu.ma/... or partiful.com/e/...).");
      return;
    }
    if (explicit && lumaUrl !== explicit) setLumaUrl(explicit);
    setLumaLoading(true);
    try {
      const res = await api.previewLumaEvent(url);
      const ev = res?.event || {};
      const sug = res?.suggestions || {};
      setProfile((prev) => {
        const next = { ...prev };
        // Only fill empty fields. Skip everything if the operator
        // already typed something. NB: city/format have non-empty seed
        // defaults, so "untouched" means "still equals the seed" (same
        // rule as headcount below), not "falsy".
        if (ev.name) next.eventName = next.eventName || ev.name;
        if (ev.location && next.city === DEFAULT_PROFILE.city) {
          next.city = ev.location;
        }
        // ev.starts_at is ISO-8601 per LumaEvent; slice gives YYYY-MM-DD.
        if (ev.starts_at) {
          next.eventDate = next.eventDate || String(ev.starts_at).slice(0, 10);
        }
        // event_format is snapped to our FORMATS taxonomy server-side.
        if (sug.event_format && FORMATS.includes(sug.event_format)
            && next.format === DEFAULT_PROFILE.format) {
          next.format = sug.event_format;
        }
        // Headcount has a slider min=0 max=160, clamp before assigning.
        // We treat the default 40 as "not yet set by the operator" for
        // the purpose of the empty-only rule : that's the seed value
        // and the only way it survives intake is if the operator never
        // touched the slider. Conservative: only fill when the current
        // value equals the seed default.
        const cap = Number(ev.capacity);
        if (Number.isFinite(cap) && cap > 0) {
          const clamped = Math.max(0, Math.min(160, Math.round(cap)));
          if (next.headcount === DEFAULT_PROFILE.headcount) {
            next.headcount = clamped;
          }
        }
        return next;
      });
      setLumaImported(ev);
    } catch (err) {
      setLumaError(err?.message || "Could not import from that event URL.");
    } finally {
      setLumaLoading(false);
    }
  };

  // Auto-consume a pending Luma URL left in sessionStorage by the
  // landing intake's IntakeLumaEntry (signed-out). Pop-and-fire-once : remove the
  // key before kicking off the import so a refresh doesn't re-import.
  // Empty deps : runs exactly once on mount, intentional.
  useEffect(() => {
    let pending = null;
    try { pending = sessionStorage.getItem("surplus_pending_luma_url"); } catch {}
    if (!pending) return;
    try { sessionStorage.removeItem("surplus_pending_luma_url"); } catch {}
    handleLumaImport(pending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        // Gap #4: roi.goal_cfg keys on the literal string; a CSV-joined
        // multi-goal silently misses the dict. Send only the primary.
        goal: profile.goal.slice(0, 1),
        budget: profile.budget,
        sources: profile.sources,
      });
      onSubmitted && onSubmitted(ev, profile);
    } catch (e) {
      const msg = e?.message || "Could not create event.";
      setSubmitError(msg);
      onError && onError(e);
      setSubmitting(false);
    }
  };

  return (
    <div className="stage">
      <header className="stage-head">
        <h1>Define the event</h1>
      </header>

      {/* One-line Luma pre-fill row. Sits above the form so the three
          A/B/C cards stay on screen without extra scrolling. Styled as
          a subtle card via .luma-quick so the row is unmistakably
          visible and doesn't blend into the page header. */}
      <div className="luma-quick">
        <Link2 size={14} aria-hidden className="luma-quick-icon" />
        <label htmlFor="luma-url" className="luma-quick-label">
          Event URL
        </label>
        <input
          id="luma-url"
          type="text"
          className="text-in luma-quick-input"
          value={lumaUrl}
          onChange={(e) => setLumaUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); handleLumaImport(); }
          }}
          placeholder="https://lu.ma/your-event or partiful.com/e/..."
        />
        <button
          type="button"
          className="btn-primary luma-quick-btn"
          onClick={handleLumaImport}
          disabled={lumaLoading || !lumaUrl.trim()}
        >
          {lumaLoading ? (
            <><Loader2 className="spin" size={14} /> Importing</>
          ) : (
            "Import"
          )}
        </button>
        <span className="hint luma-quick-hint">*optional, pre-fills name + date + capacity</span>
      </div>
      {lumaError && (
        <div className="api-error" role="alert" style={{ marginTop: 4 }}>
          <AlertCircle size={14} /> {lumaError}
        </div>
      )}
      {lumaImported && !lumaError && (
        <div className="luma-ok-banner" style={{ marginTop: 4 }}>
          <Check size={14} /> Imported &quot;{lumaImported.name || "event"}&quot;
        </div>
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
          <label>Sources</label>
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
          <label>Primary objective</label>
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
            <><Loader2 className="spin" size={16} /> Creating event…</>
          ) : (
            <>Continue <ArrowRight size={16} /></>
          )}
        </button>
      </div>
    </div>
  );
}
