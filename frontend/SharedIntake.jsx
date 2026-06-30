import React, { useState, useEffect } from "react";
import { ArrowRight, CornerDownRight, Loader2, AlertCircle, Link2, Check, Sparkles, X } from "lucide-react";
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

  // "Describe your event" : a multi-turn interview (api.intakeTurn →
  // /events/intake/turn). The host chats; the model either asks one clarifying
  // question or finalizes, at which point we snap the result onto the form's
  // chips. Mode-less : nothing is persisted server-side and the transcript
  // lives only in this component's state (cleared on refresh). Only fields the
  // model returned are applied, so the form's defaults survive anything the
  // conversation didn't mention.
  //
  // Each chatLog entry carries `text` (shown in the bubble) and `content` (sent
  // back to the model as that turn's message : for the assistant, the raw JSON
  // it returned, so the next turn sees a coherent history).
  const [chatLog, setChatLog] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatError, setChatError] = useState(null);
  // The interview is a floating widget : a launcher bubble (bottom-right) opens
  // a panel over the form. Closed by default so the form is the first thing the
  // host sees; the panel writes its result onto the form behind it. The chat is
  // CONTINUOUS : it never locks after a finalize — the host keeps refining and
  // each finalize re-syncs the form, so this is a live editing conversation.
  const [chatOpen, setChatOpen] = useState(false);

  const set = (k, v) => setProfile((p) => ({ ...p, [k]: v }));
  const toggle = (k, v) => setProfile((p) => ({ ...p, [k]: toggleIn(p[k], v) }));

  const applyExtractedProfile = (p, triageConfig, rawText) => {
    setProfile((prev) => {
      const next = { ...prev };
      // Keep the host's verbatim description so it persists onto the Event as
      // `brief` at createEvent and feeds outreach compose with real context.
      if (rawText && rawText.trim()) next.brief = rawText.trim();
      // Stash the rich ICP (anti-fit / nice-to-have / archetype_priority /
      // thresholds) the model captured beyond the chips. The form doesn't
      // render it, but it rides along in profile state and is persisted later
      // at the inbound commit (Stage02.startInbound -> setTriageConfig), so the
      // host's full intent survives even though the screen stays mode-less.
      if (triageConfig && Object.keys(triageConfig).length) {
        next.triageConfig = triageConfig;
      }
      if (p.role) next.role = p.role;
      if (p.city) next.city = p.city;
      if (p.event_name) next.eventName = p.event_name;
      if (Array.isArray(p.seniority) && p.seniority.length) next.seniority = p.seniority;
      if (Array.isArray(p.co_stage) && p.co_stage.length) next.coStage = p.co_stage;
      if (Array.isArray(p.yoe) && p.yoe.length) next.yoe = p.yoe;
      if (Array.isArray(p.goal) && p.goal.length) next.goal = p.goal;
      if (Array.isArray(p.sources) && p.sources.length) {
        // linkedin is the locked, always-on source : keep it first.
        next.sources = Array.from(new Set(["linkedin", ...p.sources]));
      }
      if (p.format && FORMATS.includes(p.format)) next.format = p.format;
      if (typeof p.headcount === "number") {
        next.headcount = Math.max(0, Math.min(160, Math.round(p.headcount)));
      }
      if (typeof p.budget === "number") {
        next.budget = Math.max(0, Math.min(40000, Math.round(p.budget)));
      }
      return next;
    });
  };

  const handleChatSend = async () => {
    setChatError(null);
    const text = (chatInput || "").trim();
    if (!text || chatBusy) return;
    // Append the host's turn first so the bubble shows immediately, then replay
    // the whole transcript to the model. `content` is what the model sees;
    // `text` is what we render (identical for the host).
    const userTurn = { role: "user", text, content: text };
    const nextLog = [...chatLog, userTurn];
    setChatLog(nextLog);
    setChatInput("");
    setChatBusy(true);
    try {
      const res = await api.intakeTurn(
        nextLog.map((m) => ({ role: m.role, content: m.content })),
      );
      if (res?.error && !res?.complete && !res?.question) {
        setChatError(`Couldn't read that (${res.error}). Try rephrasing.`);
        return;
      }
      // The host's combined words become the Event `brief` (persisted at
      // createEvent, feeds outreach compose with real context).
      const brief = nextLog
        .filter((m) => m.role === "user")
        .map((m) => m.text)
        .join("\n");
      // Apply whatever the model extracted THIS turn — even on an 'ask' turn it
      // returns the fields it already learned, so the form fills incrementally
      // (e.g. the host names the event, we set it now instead of waiting for a
      // full finalize). applyExtractedProfile only writes truthy fields, so a
      // mostly-empty partial profile is a safe no-op.
      if (res?.profile) {
        applyExtractedProfile(res.profile, res.triage_config || null, brief);
      }
      if (res?.complete) {
        // Each finalize re-applies the COMPLETE picture, so refinements
        // ('make it 60 seats') re-sync the form on top of what's already there.
        const captured = Array.isArray(res.captured) ? res.captured : [];
        const base =
          res.summary || "Updated the form - tell me anything you'd like to change.";
        const summary = captured.length
          ? `${base} (Also captured: ${captured.join("; ")}.)`
          : base;
        // `finalized` marks this as a turn that synced the form, so the bubble
        // can show a "✓ form updated" caption. The conversation stays open.
        setChatLog([
          ...nextLog,
          { role: "assistant", text: summary, content: res.assistant_json || summary, finalized: true },
        ]);
      } else {
        const q = res?.question || "Tell me a bit more about who this event is for.";
        setChatLog([
          ...nextLog,
          { role: "assistant", text: q, content: res?.assistant_json || q },
        ]);
      }
    } catch (err) {
      setChatError(err?.message || "Could not reach the interviewer. Try again.");
    } finally {
      setChatBusy(false);
    }
  };

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
        brief: profile.brief || "",
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

      {/* The "Describe it" interview is a floating widget rendered at the end
          of this component (see <IntakeChatWidget> below) so it overlays the
          form rather than pushing it down. */}

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

      {/* ── Floating "Describe it" interview ──────────────────────────────
          Fixed bottom-right: a launcher bubble toggles a chat panel that
          overlays the form. The conversation lives in component state and,
          on finalize, writes the chips behind the panel via applyExtracted-
          Profile. Closed by default so the form stays the primary surface. */}
      <div
        className="intake-chat-fab"
        style={{
          position: "fixed", right: 24, bottom: 24, zIndex: 60,
          display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 12,
        }}
      >
        {chatOpen && (
          <div
            className="intake-chat-panel card"
            role="dialog"
            aria-label="Describe your event"
            style={{
              width: 360, maxWidth: "calc(100vw - 32px)",
              maxHeight: "min(70vh, 560px)",
              display: "flex", flexDirection: "column",
              boxShadow: "0 16px 48px rgba(0,0,0,0.28)",
              borderRadius: 14, overflow: "hidden", padding: 0,
            }}
          >
            <div
              style={{
                display: "flex", alignItems: "center", gap: 8,
                padding: "12px 14px",
                borderBottom: "1px solid rgba(127,127,127,0.18)",
              }}
            >
              <Sparkles size={15} className="luma-quick-icon" aria-hidden />
              <strong style={{ flex: 1, fontSize: 14 }}>Describe your event</strong>
              <button
                type="button" aria-label="Close"
                onClick={() => setChatOpen(false)}
                style={{ background: "none", border: "none", cursor: "pointer", padding: 2, lineHeight: 0, color: "inherit" }}
              >
                <X size={16} />
              </button>
            </div>

            <div
              className="intake-chat-log"
              style={{
                flex: 1, minHeight: 120, overflowY: "auto",
                padding: 12, display: "flex", flexDirection: "column", gap: 6,
              }}
            >
              {chatLog.length === 0 && !chatBusy && (
                <p className="hint" style={{ margin: 0, fontSize: 13, lineHeight: 1.5 }}>
                  Tell me about your event in a sentence - who it&apos;s for, the vibe,
                  roughly how many seats. I&apos;ll ask if I need more, then fill the
                  form for you.
                </p>
              )}
              {chatLog.map((m, i) => (
                <React.Fragment key={i}>
                  <div
                    className={`intake-chat-bubble intake-chat-${m.role}`}
                    style={{
                      alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                      maxWidth: "85%", padding: "6px 10px", borderRadius: 10,
                      fontSize: 13, lineHeight: 1.4, whiteSpace: "pre-wrap",
                      background: m.role === "user" ? "var(--accent, #2563eb)" : "rgba(127,127,127,0.12)",
                      color: m.role === "user" ? "#fff" : "inherit",
                    }}
                  >
                    {m.text}
                  </div>
                  {m.finalized && (
                    <span
                      style={{
                        alignSelf: "flex-start", display: "inline-flex", alignItems: "center",
                        gap: 4, fontSize: 11, color: "var(--accent, #2563eb)", marginTop: -2,
                      }}
                    >
                      <Check size={12} /> form updated
                    </span>
                  )}
                </React.Fragment>
              ))}
              {chatBusy && (
                <div
                  className="intake-chat-bubble intake-chat-assistant"
                  style={{ alignSelf: "flex-start", padding: "6px 10px", fontSize: 13, opacity: 0.6 }}
                >
                  <Loader2 className="spin" size={13} /> thinking…
                </div>
              )}
            </div>

            {chatError && (
              <div className="api-error" role="alert" style={{ margin: "0 12px 8px" }}>
                <AlertCircle size={14} /> {chatError}
              </div>
            )}

            {/* Input never locks : the host keeps refining and each finalize
                re-syncs the form behind the panel. */}
            <div
              style={{
                display: "flex", gap: 8, padding: 12,
                borderTop: "1px solid rgba(127,127,127,0.18)",
              }}
            >
              <textarea
                id="describe-event"
                className="text-in"
                style={{ minHeight: 40, resize: "none", flex: 1 }}
                rows={2}
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleChatSend();
                  }
                }}
                placeholder={chatLog.length === 0 ? "Describe your event…" : "Refine or ask…"}
                /* eslint-disable-next-line jsx-a11y/no-autofocus */
                autoFocus
              />
              <button
                type="button" className="btn-primary"
                onClick={handleChatSend}
                disabled={chatBusy || !chatInput.trim()}
                style={{ alignSelf: "stretch" }}
              >
                {chatBusy ? <Loader2 className="spin" size={14} /> : <ArrowRight size={14} />}
              </button>
            </div>
          </div>
        )}

        <button
          type="button"
          className="btn-primary intake-chat-launch"
          onClick={() => setChatOpen((o) => !o)}
          aria-expanded={chatOpen}
          style={{
            borderRadius: 999, padding: "12px 18px",
            display: "flex", alignItems: "center", gap: 8,
            boxShadow: "0 8px 24px rgba(0,0,0,0.28)",
          }}
        >
          {chatOpen ? <X size={16} /> : <Sparkles size={16} />}
          {chatOpen ? "Close" : chatLog.length > 0 ? "Refine event" : "Describe it"}
        </button>
      </div>
    </div>
  );
}
