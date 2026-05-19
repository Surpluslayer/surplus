import React, { useState, useEffect, useRef, useMemo } from "react";
import {
  ArrowRight, Check, Upload, Search, Filter, ChevronRight,
  Loader2, FileText, Sparkles, AlertCircle, ExternalLink, Link2,
} from "lucide-react";
import { api } from "./lib/api.js";

// =============================================================
// Applicant Triage : the inbound flow.
// 3 stages : Configure -> Upload -> Review
//
// Different from the outbound 5-stage rail (Intake -> Prospecting
// -> Outreach -> Matching -> ROI). Same Event entity in the DB,
// different mode. Operator picks at app-level mode switch in
// the topbar.
//
// Demo path : sign in (skip-LinkedIn), enter Triage mode, fill
// sponsor criteria, drop a Luma CSV, watch scores stream in,
// open the review drawer for any applicant.
// =============================================================

const EVENT_TYPES = [
  { key: "sponsor_cafe",    label: "Sponsor cafe" },
  { key: "founder_dinner",  label: "Founder dinner" },
  { key: "partner_dinner",  label: "Partner dinner" },
  { key: "member_social",   label: "Member social" },
  { key: "community_event", label: "Community event" },
  { key: "research_event",  label: "Research event" },
  { key: "other",           label: "Other" },
];

const STAGES = [
  { key: "config", label: "Configure" },
  { key: "upload", label: "Upload" },
  { key: "review", label: "Review" },
];

const REC_META = {
  accept:        { color: "rec-accept",   label: "Accept" },
  maybe:         { color: "rec-maybe",    label: "Maybe" },
  reject:        { color: "rec-reject",   label: "Reject" },
  needs_review:  { color: "rec-needs",    label: "Needs Review" },
};

export default function TriageApp({ user, onLogout, onSwitchMode, onSignedIn }) {
  const [eventId, setEventId] = useState(null);
  const [stage, setStage] = useState("config");
  const [maxReached, setMaxReached] = useState(0);

  const goTo = (s) => {
    const idx = STAGES.findIndex((x) => x.key === s);
    setStage(s);
    setMaxReached((m) => Math.max(m, idx));
  };

  // Signed-out triage users are sent back to the SurplusApp entry where the
  // signin modal lives. App.jsx's mode-switch is gated on `user` so we
  // shouldn't normally see this branch; defensive fallback.
  if (!user) return null;

  return (
    <div className="triage-root">
      <style>{TRIAGE_CSS}</style>
      <div className="triage-frame">
        <header className="triage-topbar">
          <div className="triage-brand">
            <img className="triage-logo" src="/surplus-logo.png" alt="" />
            <span className="triage-name">surplus</span>
            <span className="triage-mode-tag">Applicant Triage</span>
          </div>
          <nav className="triage-rail">
            {STAGES.map((s, i) => {
              const active = s.key === stage;
              const done = i < STAGES.findIndex((x) => x.key === stage);
              const reachable = i <= maxReached;
              return (
                <button key={s.key}
                  className={`triage-rail-item ${active ? "active" : ""} ${done ? "done" : ""}`}
                  disabled={!reachable}
                  onClick={() => reachable && setStage(s.key)}>
                  <span className="triage-rail-dot">
                    {done ? <Check size={12} strokeWidth={3} /> : <span>{i + 1}</span>}
                  </span>
                  {s.label}
                </button>
              );
            })}
          </nav>
          <div className="triage-user">
            <button className="triage-mode-switch" onClick={onSwitchMode} title="Switch to outbound prospecting">
              Outbound mode
            </button>
            {user && (
              <span className="triage-user-name" title={user.email || ""}>
                {user.name || user.email || "Operator"}
              </span>
            )}
            <button className="triage-logout" onClick={onLogout}>Log out</button>
          </div>
        </header>

        <main className="triage-stage">
          {stage === "config" && (
            <ConfigStep
              user={user}
              eventId={eventId}
              setEventId={setEventId}
              onNext={() => goTo("upload")}
            />
          )}
          {stage === "upload" && (
            <UploadStep
              eventId={eventId}
              onNext={() => goTo("review")}
            />
          )}
          {stage === "review" && (
            <ReviewStep eventId={eventId} />
          )}
        </main>
      </div>
    </div>
  );
}


// ─── Triage landing : signup for not-signed-in users ─────────
//
// Self-contained signup form. No LinkedIn pitch, no outbound copy.
// A user lands here from clicking "Triage mode" while signed-out.
// On success, calls onSignedIn() so the parent App can re-fetch /me.

// LinkedIn brand glyph for the primary signin button. Inline SVG so we don't
// need to import another icon library.
function LinkedInMark({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M20.45 20.45h-3.55v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.13 1.45-2.13 2.94v5.67H9.37V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.38-1.85 3.61 0 4.28 2.38 4.28 5.47v6.27ZM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13ZM7.12 20.45H3.56V9h3.56v11.45ZM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.22 0Z"/>
    </svg>
  );
}


function TriageLanding({ onSignedIn }) {
  const [showSkipForm, setShowSkipForm] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [liBusy, setLiBusy] = useState(false);
  const [error, setError] = useState(null);

  const handleLinkedInSignin = async () => {
    setError(null);
    setLiBusy(true);
    try {
      const r = await api.startLinkedinAuth();
      if (!r?.url) throw new Error("Backend didn't return a hosted-auth URL");
      // Top-level navigation : surplus_last_account cookie + session cookie
      // are set during the callback redirect, not a fetch.
      window.location.href = r.url;
    } catch (err) {
      setLiBusy(false);
      setError(err.message || "Could not start LinkedIn sign-in.");
    }
  };

  const handleSkipSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.triageSignup({ name: name.trim(), email: email.trim() });
      if (onSignedIn) onSignedIn();
      else window.location.reload();
    } catch (err) {
      setBusy(false);
      setError(err.message || "Could not create your account.");
    }
  };

  return (
    <div className="triage-landing">
      <style>{TRIAGE_CSS}</style>
      <div className="triage-landing-card">
        <div className="triage-landing-brand">
          <img className="triage-logo" src="/surplus-logo.png" alt="" />
          <span className="triage-name">surplus</span>
          <span className="triage-mode-tag">Applicant Triage</span>
        </div>

        <h1 className="triage-landing-h1">
          Review Luma applicants in <em>minutes</em>, not hours.
        </h1>
        <p className="triage-landing-sub">
          Upload your Luma CSV, tell us about the event and sponsor, and get
          accept / maybe / reject recommendations with fit + confidence scores
          for every applicant.
        </p>

        {/* Primary path : Sign in with LinkedIn. Most operators have a
            LinkedIn already + want their existing connection to be the
            identity for follow-up communications later. */}
        <button type="button"
                className="triage-li-cta"
                onClick={handleLinkedInSignin}
                disabled={liBusy}>
          {liBusy ? (
            <><Loader2 className="spin" size={16} /> Redirecting to LinkedIn…</>
          ) : (
            <><LinkedInMark size={16} /> <span>Sign in with LinkedIn</span></>
          )}
        </button>

        {error && (
          <div className="triage-error" style={{ marginTop: 12 }} role="alert">
            <AlertCircle size={14} /> {error}
          </div>
        )}

        <div className="triage-landing-divider"><span>or</span></div>

        {!showSkipForm ? (
          <button type="button" className="triage-landing-secondary"
                  onClick={() => setShowSkipForm(true)}>
            Don't have / want to connect LinkedIn? Sign up with email →
          </button>
        ) : (
          <form onSubmit={handleSkipSubmit} className="triage-landing-form">
            <label>Your name</label>
            <input className="triage-in" value={name} required autoFocus
                   onChange={(e) => setName(e.target.value)}
                   placeholder="Verci Ops" />
            <label>Email</label>
            <input className="triage-in" type="email" value={email} required
                   onChange={(e) => setEmail(e.target.value)}
                   placeholder="ops@verci.com" />
            <button type="submit" className="triage-cta triage-landing-cta"
                    disabled={busy || !name.trim() || !email.trim()}>
              {busy ? (
                <><Loader2 className="spin" size={16} /> Creating your account…</>
              ) : (
                <>Get started <ArrowRight size={16} /></>
              )}
            </button>
            <button type="button" className="triage-landing-cancel"
                    onClick={() => setShowSkipForm(false)}>
              Cancel
            </button>
          </form>
        )}

        <ul className="triage-landing-bullets">
          <li>Sponsor-aware scoring : photography founders ranked below B2B AI even if both "use Stripe"</li>
          <li>Fit + confidence as separate scores : you know when to trust the recommendation</li>
          <li>Every score cites evidence from the actual application + LinkedIn</li>
          <li>Export the reviewed CSV back to Luma when you're done</li>
        </ul>
      </div>
    </div>
  );
}


// ─── Stage 01 : Configure ─────────────────────────────────────

function ConfigStep({ user, eventId, setEventId, onNext }) {
  const [eventName, setEventName] = useState("");
  const [eventType, setEventType] = useState("sponsor_cafe");
  const [sponsorName, setSponsorName] = useState("");
  const [eventGoal, setEventGoal] = useState("");
  const [idealProfile, setIdealProfile] = useState("");
  const [hardFilters, setHardFilters] = useState("");          // newline-separated
  const [niceToHave, setNiceToHave] = useState("");
  const [antiFit, setAntiFit] = useState("");
  const [capacity, setCapacity] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Luma import : paste a lu.ma URL, server scrapes the public page,
  // we pre-fill the form. Saves ~30s of retyping and gives the rubric
  // synthesizer real event context.
  const [lumaUrl, setLumaUrl] = useState("");
  const [lumaLoading, setLumaLoading] = useState(false);
  const [lumaError, setLumaError] = useState(null);
  const [lumaImported, setLumaImported] = useState(null);

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
      // ── Direct fields from the Luma page ────────────────────────────
      if (ev.name) setEventName(ev.name);
      if (ev.description) {
        setEventGoal((prev) => prev || ev.description);
      }
      if (ev.capacity && !capacity) setCapacity(String(ev.capacity));
      if (ev.location) {
        setNotes((prev) => prev ? prev : `Location: ${ev.location}`);
      }
      // ── Claude-inferred fields (don't overwrite operator typing) ────
      if (sug.sponsor_name) {
        setSponsorName((prev) => prev || sug.sponsor_name);
      }
      if (sug.ideal_attendee_profile) {
        setIdealProfile((prev) => prev || sug.ideal_attendee_profile);
      }
      if (Array.isArray(sug.hard_filters) && sug.hard_filters.length) {
        setHardFilters((prev) => prev || sug.hard_filters.join("\n"));
      }
      if (Array.isArray(sug.anti_fit_examples) && sug.anti_fit_examples.length) {
        setAntiFit((prev) => prev || sug.anti_fit_examples.join("\n"));
      }
      if (Array.isArray(sug.nice_to_have_signals) && sug.nice_to_have_signals.length) {
        setNiceToHave((prev) => prev || sug.nice_to_have_signals.join("\n"));
      }
      setLumaImported(ev);
    } catch (err) {
      setLumaError(err.message || "Could not import from Luma.");
    } finally {
      setLumaLoading(false);
    }
  };

  // If an eventId was already created (operator backed out and came back),
  // hydrate the form from the saved config.
  useEffect(() => {
    if (!eventId) return;
    let cancelled = false;
    (async () => {
      try {
        const cfg = await api.getTriageConfig(eventId);
        if (cancelled) return;
        setEventType(cfg.event_type || "sponsor_cafe");
        setSponsorName(cfg.sponsor_name || "");
        setEventGoal(cfg.event_goal || "");
        setIdealProfile(cfg.ideal_attendee_profile || "");
        setHardFilters((cfg.hard_filters || []).join("\n"));
        setNiceToHave((cfg.nice_to_have_signals || []).join("\n"));
        setAntiFit((cfg.anti_fit_examples || []).join("\n"));
        setCapacity(cfg.capacity ? String(cfg.capacity) : "");
        setNotes(cfg.notes || "");
      } catch {}
    })();
    return () => { cancelled = true; };
  }, [eventId]);

  const splitLines = (s) =>
    (s || "").split("\n").map((x) => x.trim()).filter(Boolean);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      let id = eventId;
      if (!id) {
        // Lightweight outbound-shaped Event with the bare minimum fields :
        // backend POST /events expects a few required outbound fields, but
        // triage doesn't really use them. Defaults are fine.
        const ev = await api.createEvent({
          role: eventName || "(triage event)",
          seniority: ["Staff+"],
          co_stage: ["Seed"],
          headcount: parseInt(capacity, 10) || 40,
          format: "Sit-down dinner",
          city: "",
          goal: ["Hiring pipeline"],
          budget: 0,
          sources: ["linkedin"],
        });
        id = ev.id;
        setEventId(id);
      }
      await api.setTriageConfig(id, {
        event_type: eventType,
        sponsor_name: sponsorName.trim() || null,
        event_goal: eventGoal.trim() || null,
        ideal_attendee_profile: idealProfile.trim() || null,
        hard_filters: splitLines(hardFilters),
        nice_to_have_signals: splitLines(niceToHave),
        anti_fit_examples: splitLines(antiFit),
        capacity: capacity ? parseInt(capacity, 10) : null,
        notes: notes.trim() || null,
      });
      onNext();
    } catch (err) {
      setError(err.message || "Could not save config.");
      setSaving(false);
    }
  };

  return (
    <form className="triage-form" onSubmit={handleSubmit}>
      <header className="triage-head">
        <h1>Configure the event</h1>
        <p>Who's the sponsor, what do they want from the room, and who's the wrong fit.</p>
      </header>

      <section className="triage-card triage-luma">
        <h3><span className="triage-card-num"><Link2 size={14} /></span> Import from Luma <span className="triage-hint">: optional — we'll pre-fill name + description</span></h3>
        <div className="triage-luma-row">
          <input className="triage-in" value={lumaUrl}
                 onChange={(e) => setLumaUrl(e.target.value)}
                 placeholder="https://lu.ma/your-event"
                 onKeyDown={(e) => {
                   if (e.key === "Enter") { e.preventDefault(); handleLumaImport(); }
                 }} />
          <button type="button" className="triage-cta-secondary"
                  disabled={lumaLoading || !lumaUrl.trim()}
                  onClick={handleLumaImport}>
            {lumaLoading ? (
              <><Loader2 className="spin" size={14} /> Importing…</>
            ) : (
              <>Import <ArrowRight size={14} /></>
            )}
          </button>
        </div>
        {lumaError && (
          <div className="triage-error" role="alert" style={{ marginTop: 8 }}>
            <AlertCircle size={14} /> {lumaError}
          </div>
        )}
        {lumaImported && !lumaError && (
          <div className="triage-luma-ok">
            <Check size={14} /> Imported "{lumaImported.name || "event"}"
            {lumaImported.location ? ` · ${lumaImported.location}` : ""}
            {lumaImported.capacity ? ` · cap ${lumaImported.capacity}` : ""}
            . We also proposed sponsor / ideal-profile / anti-fit from the
            description — review and tighten the fields below before continuing.
          </div>
        )}
      </section>

      <div className="triage-grid">
        <section className="triage-card">
          <h3><span className="triage-card-num">A</span> Sponsor + event</h3>

          <label>Event name <span className="triage-hint">: just for your reference</span></label>
          <input className="triage-in" value={eventName}
                 onChange={(e) => setEventName(e.target.value)}
                 placeholder="Event name" />

          <label>Event type</label>
          <div className="triage-chips">
            {EVENT_TYPES.map((t) => (
              <button type="button" key={t.key}
                className={`triage-chip ${eventType === t.key ? "on" : ""}`}
                onClick={() => setEventType(t.key)}>{t.label}</button>
            ))}
          </div>

          <label>Sponsor / partner name</label>
          <input className="triage-in" value={sponsorName}
                 onChange={(e) => setSponsorName(e.target.value)}
                 placeholder="Sponsor or partner" />

          <label>Capacity <span className="triage-hint">: optional</span></label>
          <input className="triage-in" value={capacity} type="number" min="1"
                 onChange={(e) => setCapacity(e.target.value)}
                 placeholder="" />
        </section>

        <section className="triage-card">
          <h3><span className="triage-card-num">B</span> Who's the right room</h3>

          <label>Event goal <span className="triage-hint">: what should this room produce for the sponsor?</span></label>
          <textarea className="triage-ta" value={eventGoal} rows={3}
                    onChange={(e) => setEventGoal(e.target.value)}
                    placeholder="What should this room produce for the sponsor?" />

          <label>Ideal attendee profile</label>
          <textarea className="triage-ta" value={idealProfile} rows={3}
                    onChange={(e) => setIdealProfile(e.target.value)}
                    placeholder="Who is the right attendee?" />

          <label>Hard filters <span className="triage-hint">: one per line. Violations cap the score.</span></label>
          <textarea className="triage-ta" value={hardFilters} rows={3}
                    onChange={(e) => setHardFilters(e.target.value)}
                    placeholder="One filter per line" />
        </section>

        <section className="triage-card">
          <h3><span className="triage-card-num">C</span> Anti-fit + nice-to-have</h3>

          <label>Anti-fit examples <span className="triage-hint">: categories the sponsor does NOT want</span></label>
          <textarea className="triage-ta" value={antiFit} rows={4}
                    onChange={(e) => setAntiFit(e.target.value)}
                    placeholder="One per line" />

          <label>Nice-to-have signals <span className="triage-hint">: bonus points if applicants show these</span></label>
          <textarea className="triage-ta" value={niceToHave} rows={3}
                    onChange={(e) => setNiceToHave(e.target.value)}
                    placeholder="One per line" />

          <label>Notes for reviewers <span className="triage-hint">: optional</span></label>
          <textarea className="triage-ta" value={notes} rows={2}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="" />
        </section>
      </div>

      {error && (
        <div className="triage-error" role="alert">
          <AlertCircle size={14} /> {error}
        </div>
      )}

      <div className="triage-foot">
        <p className="triage-foot-hint">
          We'll synthesize a scoring rubric from this when you upload your applicant CSV.
        </p>
        <button type="submit" className="triage-cta" disabled={saving}>
          {saving ? (
            <><Loader2 className="spin" size={16} /> Saving…</>
          ) : (
            <>Continue <ArrowRight size={16} /></>
          )}
        </button>
      </div>
    </form>
  );
}


// ─── Stage 02 : Upload ─────────────────────────────────────────

function UploadStep({ eventId, onNext }) {
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploaded, setUploaded] = useState(null);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState(null);
  const fileRef = useRef(null);
  const pollRef = useRef(null);

  // Poll the evaluation-progress endpoint after upload so the operator
  // sees scores fill in. Stops once everything's scored.
  useEffect(() => {
    if (!uploaded || !eventId) return;
    let alive = true;
    pollRef.current = setInterval(async () => {
      if (!alive) return;
      try {
        const p = await api.getTriageProgress(eventId);
        if (!alive) return;
        setProgress(p);
        if (p.pending === 0 && p.total_applicants > 0) {
          clearInterval(pollRef.current);
        }
      } catch {}
    }, 1500);
    return () => { alive = false; clearInterval(pollRef.current); };
  }, [uploaded, eventId]);

  const handleFile = async (file) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".csv")) {
      setError("That doesn't look like a CSV. Drop a Luma .csv export.");
      return;
    }
    setError(null);
    setUploading(true);
    try {
      const r = await api.uploadTriageCsv(eventId, file);
      setUploaded(r);
    } catch (e) {
      setError(e.message || "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    handleFile(e.dataTransfer.files?.[0]);
  };

  return (
    <div className="triage-upload">
      <header className="triage-head">
        <h1>Upload the applicant CSV</h1>
        <p>Drop your Luma export. We'll score every applicant against the rubric from step 1.</p>
      </header>

      {!uploaded ? (
        <div
          className={`triage-drop ${dragging ? "drag" : ""} ${uploading ? "busy" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => fileRef.current?.click()}
        >
          <input ref={fileRef} type="file" accept=".csv,text/csv" hidden
                 onChange={(e) => handleFile(e.target.files?.[0])} />
          {uploading ? (
            <>
              <Loader2 className="spin" size={32} />
              <p>Parsing applicants and kicking off scoring…</p>
            </>
          ) : (
            <>
              <Upload size={32} />
              <p className="triage-drop-h">Drop your Luma CSV here</p>
              <p className="triage-drop-sub">or click to choose a file</p>
            </>
          )}
        </div>
      ) : (
        <div className="triage-uploaded">
          <div className="triage-uploaded-head">
            <FileText size={20} />
            <div>
              <p className="triage-uploaded-title">
                {uploaded.inserted} applicant{uploaded.inserted === 1 ? "" : "s"} loaded
              </p>
              <p className="triage-uploaded-sub">
                {uploaded.parsed === uploaded.inserted
                  ? "All rows parsed cleanly."
                  : `${uploaded.parsed - uploaded.inserted} rows skipped (no name or email).`}
              </p>
            </div>
          </div>

          <div className="triage-progress">
            <div className="triage-progress-head">
              <Sparkles size={14} /> Scoring in progress
              {progress && (
                <span className="triage-progress-counts">
                  {progress.scored} / {progress.total_applicants} scored
                </span>
              )}
            </div>
            <div className="triage-progress-bar">
              <div className="triage-progress-fill" style={{
                width: progress && progress.total_applicants
                  ? `${(progress.scored / progress.total_applicants) * 100}%`
                  : "5%",
              }} />
            </div>
            <p className="triage-progress-hint">
              Sonnet is generating a per-event rubric, then Haiku scores each applicant in parallel.
              First scores appear in ~5 seconds. You can move to Review now if you want to watch them stream in.
            </p>
          </div>

          <div className="triage-foot triage-foot-right">
            <button className="triage-cta" onClick={onNext}>
              See review queue <ArrowRight size={16} />
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="triage-error" role="alert">
          <AlertCircle size={14} /> {error}
        </div>
      )}
    </div>
  );
}


// ─── Stage 03 : Review queue ───────────────────────────────────

const FILTER_OPTIONS = [
  { key: "all",          label: "All" },
  { key: "accept",       label: "Accept" },
  { key: "maybe",        label: "Maybe" },
  { key: "needs_review", label: "Needs Review" },
  { key: "reject",       label: "Reject" },
];

function ReviewStep({ eventId }) {
  const [applicants, setApplicants] = useState([]);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState(null);

  // Poll continuously while there are unscored applicants so the table
  // fills in live. Stop once everyone's scored.
  useEffect(() => {
    if (!eventId) return;
    let alive = true;
    const tick = async () => {
      try {
        const [list, prog] = await Promise.all([
          api.listTriageApplicants(eventId),
          api.getTriageProgress(eventId),
        ]);
        if (!alive) return;
        setApplicants(list);
        setProgress(prog);
        setLoading(false);
      } catch {}
    };
    tick();
    const t = setInterval(() => {
      if (!alive) return;
      tick();
    }, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [eventId]);

  const filtered = useMemo(() => {
    let rows = applicants;
    if (filter !== "all") {
      rows = rows.filter((a) => a.evaluation && a.evaluation.recommendation === filter);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      rows = rows.filter((a) =>
        (a.name || "").toLowerCase().includes(q) ||
        (a.company || "").toLowerCase().includes(q) ||
        (a.role || "").toLowerCase().includes(q),
      );
    }
    return rows;
  }, [applicants, filter, search]);

  const counts = useMemo(() => {
    const c = { all: applicants.length, accept: 0, maybe: 0, reject: 0, needs_review: 0 };
    for (const a of applicants) {
      const r = a.evaluation?.recommendation;
      if (r && c[r] !== undefined) c[r]++;
    }
    return c;
  }, [applicants]);

  const selected = applicants.find((a) => a.id === selectedId) || null;

  return (
    <div className="triage-review">
      <header className="triage-head">
        <h1>Review queue</h1>
        <p>
          {applicants.length} applicants
          {progress && progress.pending > 0 && (
            <span className="triage-progress-inline">
              · <Loader2 className="spin" size={12} /> scoring {progress.pending} more
            </span>
          )}
        </p>
      </header>

      <div className="triage-filterbar">
        <div className="triage-filter-pills">
          {FILTER_OPTIONS.map((f) => (
            <button key={f.key}
              className={`triage-pill ${filter === f.key ? "on" : ""}`}
              onClick={() => setFilter(f.key)}>
              {f.label} <span className="triage-pill-count">{counts[f.key]}</span>
            </button>
          ))}
        </div>
        <div className="triage-search">
          <Search size={14} />
          <input value={search} onChange={(e) => setSearch(e.target.value)}
                 placeholder="Search name, company, or role…" />
        </div>
      </div>

      <div className="triage-table-wrap">
        <table className="triage-table">
          <thead>
            <tr>
              <th>Applicant</th>
              <th>Role · Company</th>
              <th>Archetype</th>
              <th className="num">Fit</th>
              <th className="num">Conf</th>
              <th>Recommendation</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={7} className="triage-table-empty">
                <Loader2 className="spin" size={16} /> Loading applicants…
              </td></tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr><td colSpan={7} className="triage-table-empty">
                No applicants match this filter.
              </td></tr>
            )}
            {filtered.map((a) => (
              <ApplicantRow key={a.id} a={a}
                            selected={a.id === selectedId}
                            onClick={() => setSelectedId(a.id)} />
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <ApplicantDrawer applicant={selected} onClose={() => setSelectedId(null)} />
      )}
    </div>
  );
}

function ApplicantRow({ a, selected, onClick }) {
  const ev = a.evaluation;
  const rec = ev?.recommendation || "needs_review";
  const meta = REC_META[rec] || REC_META.needs_review;
  return (
    <tr className={`triage-row ${selected ? "sel" : ""}`} onClick={onClick}>
      <td>
        <div className="triage-name">{a.name || "(unnamed)"}</div>
        <div className="triage-sub">{a.email || ""}</div>
      </td>
      <td>
        <div>{a.role || "—"}</div>
        <div className="triage-sub">{a.company || ""}</div>
      </td>
      <td className="triage-sub-cell">{ev?.archetype || "—"}</td>
      <td className="num">{ev ? <ScorePill v={ev.fit_score} /> : "—"}</td>
      <td className="num">{ev ? <ScorePill v={ev.confidence_score} muted /> : "—"}</td>
      <td>
        {ev ? (
          <span className={`triage-rec ${meta.color}`}>{meta.label}</span>
        ) : (
          <span className="triage-rec triage-rec-pending">
            <Loader2 className="spin" size={10} /> scoring…
          </span>
        )}
      </td>
      <td className="triage-reason">{ev?.one_sentence_summary || ""}</td>
    </tr>
  );
}

function ScorePill({ v, muted }) {
  const tone = v >= 75 ? "hi" : v >= 50 ? "mid" : "lo";
  return (
    <span className={`triage-score ${tone} ${muted ? "muted" : ""}`}>{v}</span>
  );
}

function ApplicantDrawer({ applicant, onClose }) {
  const ev = applicant.evaluation;
  return (
    <div className="triage-drawer-backdrop" onClick={onClose}>
      <aside className="triage-drawer" onClick={(e) => e.stopPropagation()}>
        <header className="triage-drawer-head">
          <button className="triage-drawer-close" onClick={onClose}>×</button>
          <h2>{applicant.name}</h2>
          <div className="triage-sub">
            {applicant.role}{applicant.role && applicant.company ? " · " : ""}{applicant.company}
          </div>
          <div className="triage-drawer-links">
            {applicant.linkedin_url && (
              <a href={applicant.linkedin_url} target="_blank" rel="noopener noreferrer">
                LinkedIn <ExternalLink size={11} />
              </a>
            )}
            {applicant.website && (
              <a href={applicant.website} target="_blank" rel="noopener noreferrer">
                Website <ExternalLink size={11} />
              </a>
            )}
            {applicant.email && (
              <a href={`mailto:${applicant.email}`}>{applicant.email}</a>
            )}
          </div>
        </header>

        {ev ? (
          <>
            <div className="triage-drawer-rec">
              <span className={`triage-rec ${REC_META[ev.recommendation]?.color || ""}`}>
                {REC_META[ev.recommendation]?.label || ev.recommendation}
              </span>
              <div className="triage-drawer-scores">
                <div><span className="triage-k">Fit</span><ScorePill v={ev.fit_score} /></div>
                <div><span className="triage-k">Confidence</span><ScorePill v={ev.confidence_score} muted /></div>
                <div><span className="triage-k">Archetype</span><span className="triage-arch">{ev.archetype}</span></div>
              </div>
            </div>

            <DrawerSection title="Why fit">{ev.why_fit || "—"}</DrawerSection>
            <DrawerSection title="Why not">{ev.why_not_fit || "—"}</DrawerSection>

            <DrawerSection title="Dimension breakdown">
              <div className="triage-dim-grid">
                <DimBar label="Sponsor fit"        v={ev.sponsor_fit} />
                <DimBar label="Event fit"          v={ev.event_fit} />
                <DimBar label="Role relevance"     v={ev.role_relevance} />
                <DimBar label="Company relevance"  v={ev.company_relevance} />
                <DimBar label="Stage relevance"    v={ev.stage_relevance} />
                <DimBar label="Seriousness"        v={ev.seriousness_legitimacy} />
                <DimBar label="Room value"         v={ev.room_value} />
                <DimBar label="App quality"        v={ev.application_quality} />
              </div>
            </DrawerSection>

            <DrawerSection title="Evidence used">
              <ul className="triage-evidence">
                {(ev.evidence_used || []).map((e, i) => <li key={i}>{e}</li>)}
                {(!ev.evidence_used || ev.evidence_used.length === 0) && <li className="triage-sub">— no evidence cited —</li>}
              </ul>
            </DrawerSection>

            {ev.missing_info && ev.missing_info.length > 0 && (
              <DrawerSection title="Missing info">
                <ul className="triage-evidence">
                  {ev.missing_info.map((e, i) => <li key={i}>{e}</li>)}
                </ul>
              </DrawerSection>
            )}

            <DrawerSection title="Application answers">
              <pre className="triage-raw">{JSON.stringify(applicant.raw_application_data || {}, null, 2)}</pre>
            </DrawerSection>
          </>
        ) : (
          <div className="triage-drawer-pending">
            <Loader2 className="spin" size={20} /> Scoring in progress…
          </div>
        )}
      </aside>
    </div>
  );
}

function DrawerSection({ title, children }) {
  return (
    <section className="triage-drawer-sec">
      <h3>{title}</h3>
      <div>{children}</div>
    </section>
  );
}

function DimBar({ label, v }) {
  const tone = v >= 75 ? "hi" : v >= 50 ? "mid" : "lo";
  return (
    <div className="triage-dim">
      <div className="triage-dim-head">
        <span>{label}</span>
        <span className={`triage-dim-v ${tone}`}>{v}</span>
      </div>
      <div className="triage-dim-bar">
        <div className={`triage-dim-fill ${tone}`} style={{ width: `${v}%` }} />
      </div>
    </div>
  );
}


const TRIAGE_CSS = `
.triage-root, .triage-landing {
  --bg:#f5f6fa; --panel:#fff; --line:#e5e8ef;
  --ink:#1a1825; --ink-dim:#5b596b; --ink-faint:#9a96aa;
  --acc:#6b46e0; --acc-deep:#5836c6; --acc-soft:#ede9fb;
  --ok:#1a8f3b; --ok-soft:#e7f6ec;
  --warn:#a87100; --warn-soft:#fef5e0;
  --bad:#c43146; --bad-soft:#fce6ea;
  --gray:#5b596b; --gray-soft:#f0f0f5;
  --shadow:0 4px 16px rgba(15,15,30,0.05);
  --shadow-md:0 8px 24px rgba(15,15,30,0.08);
  font-family:'Plus Jakarta Sans',system-ui,-apple-system,sans-serif;
  color:var(--ink);
}
.triage-root { background:var(--bg); min-height:100vh; }
.triage-frame { max-width:1320px; margin:0 auto; padding:16px 24px 64px; }
.triage-topbar {
  display:flex; align-items:center; justify-content:space-between;
  padding:14px 18px; background:var(--panel); border:1px solid var(--line);
  border-radius:14px; box-shadow:var(--shadow); margin-bottom:18px;
}
.triage-brand { display:flex; align-items:center; gap:10px; }
.triage-logo { width:28px; height:28px; }
.triage-name { font-weight:800; letter-spacing:-0.04em; font-size:1.35rem; }
.triage-mode-tag {
  margin-left:6px; padding:3px 9px; border-radius:999px;
  font-size:10.5px; font-weight:600; letter-spacing:0.02em; text-transform:uppercase;
  background:var(--acc-soft); color:var(--acc); border:1px solid rgba(108,67,217,0.18);
}
.triage-rail { display:flex; gap:4px; }
.triage-rail-item {
  display:flex; align-items:center; gap:7px; background:transparent;
  border:1px solid transparent; color:var(--ink-faint);
  padding:7px 12px; border-radius:999px;
  font-family:inherit; font-size:11.5px; font-weight:500;
  cursor:pointer; transition:all 0.15s;
}
.triage-rail-item:disabled { cursor:not-allowed; opacity:0.4; }
.triage-rail-item:not(:disabled):hover { color:var(--acc); background:var(--acc-soft); }
.triage-rail-item.active { background:var(--acc); color:#fff; box-shadow:0 4px 12px rgba(108,67,217,0.3); }
.triage-rail-item.done { color:var(--ink-dim); }
.triage-rail-dot {
  display:flex; align-items:center; justify-content:center;
  width:16px; height:16px; border-radius:50%; font-size:10px; font-weight:700;
  background:rgba(255,255,255,0.2);
}
.triage-rail-item.active .triage-rail-dot { background:rgba(255,255,255,0.3); }
.triage-rail-item:not(.active):not(.done) .triage-rail-dot {
  background:var(--gray-soft); color:var(--ink-faint);
}
.triage-user { display:flex; align-items:center; gap:10px; font-size:12px; color:var(--ink-dim); }
.triage-mode-switch {
  padding:6px 10px; background:transparent; border:1px solid var(--line);
  border-radius:999px; font-family:inherit; font-size:11.5px;
  color:var(--ink-dim); cursor:pointer; transition:all 0.15s;
}
.triage-mode-switch:hover { color:var(--acc); border-color:var(--acc); }
.triage-user-name { font-weight:500; }
.triage-logout {
  padding:5px 9px; background:transparent; border:0; color:var(--ink-faint);
  font-family:inherit; font-size:11.5px; cursor:pointer;
}
.triage-logout:hover { color:var(--ink-dim); }

.triage-stage { animation:tr-fade 0.4s ease; }
@keyframes tr-fade { from{opacity:0;transform:translateY(6px);} to{opacity:1;transform:none;} }

.triage-head { margin-bottom:18px; max-width:680px; }
.triage-head h1 { font-family:'Playfair Display',Georgia,serif;
  font-size:36px; font-weight:600; letter-spacing:-0.01em; line-height:1.15; margin:0 0 6px; }
.triage-head p { font-size:14px; line-height:1.55; color:var(--ink-dim); margin:0; }

/* Form */
.triage-grid {
  display:grid; grid-template-columns:repeat(3, minmax(0, 1fr));
  gap:18px; margin-bottom:18px;
}
@media (max-width:1100px) { .triage-grid { grid-template-columns:1fr; } }
.triage-card {
  background:var(--panel); border:1px solid var(--line);
  border-radius:14px; padding:20px 18px; box-shadow:var(--shadow);
}
.triage-card h3 {
  display:flex; align-items:center; gap:10px; margin:0 0 14px;
  font-size:14px; font-weight:600; letter-spacing:-0.01em;
}
.triage-card-num {
  width:22px; height:22px; border-radius:6px; display:inline-flex;
  align-items:center; justify-content:center; background:var(--acc-soft);
  color:var(--acc); font-size:11px; font-weight:700;
}
.triage-card label {
  display:block; font-size:11px; text-transform:uppercase; letter-spacing:0.06em;
  color:var(--ink-faint); font-weight:600; margin-top:14px; margin-bottom:6px;
}
.triage-card label:first-of-type { margin-top:0; }
.triage-hint { text-transform:none; letter-spacing:0; font-weight:400;
  color:var(--ink-faint); }
.triage-in, .triage-ta {
  width:100%; padding:9px 12px; border-radius:9px;
  border:1px solid var(--line); background:var(--panel);
  font-family:inherit; font-size:13.5px; color:var(--ink);
  box-sizing:border-box; transition:border-color 0.12s;
}
.triage-ta { line-height:1.5; resize:vertical; }
.triage-in:focus, .triage-ta:focus { outline:none; border-color:var(--acc); }

.triage-chips { display:flex; flex-wrap:wrap; gap:6px; }
.triage-chip {
  padding:6px 11px; border-radius:999px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink-dim);
  font-family:inherit; font-size:12px; cursor:pointer; transition:all 0.12s;
}
.triage-chip:hover { color:var(--acc); border-color:var(--acc); }
.triage-chip.on { background:var(--acc); color:#fff; border-color:var(--acc); }

.triage-foot {
  display:flex; align-items:center; justify-content:space-between;
  margin-top:6px; padding-top:14px; border-top:1px solid var(--line);
}
.triage-foot-right { justify-content:flex-end; }
.triage-foot-hint { font-size:12px; color:var(--ink-faint); margin:0; }
.triage-cta {
  display:inline-flex; align-items:center; gap:8px; padding:10px 18px;
  border-radius:999px; border:0; background:var(--acc); color:#fff;
  font-family:inherit; font-size:13.5px; font-weight:600; cursor:pointer;
  transition:all 0.15s;
}
.triage-cta:hover:not(:disabled) { background:var(--acc-deep); box-shadow:0 6px 16px rgba(108,67,217,0.3); }
.triage-cta:disabled { opacity:0.7; cursor:wait; }

.triage-error {
  display:flex; align-items:center; gap:7px; padding:10px 13px;
  margin:14px 0 0; border-radius:9px;
  background:var(--bad-soft); color:var(--bad); border:1px solid #f3d6dc;
  font-size:13px;
}

.triage-luma { margin-bottom:18px; }
.triage-luma-row { display:flex; gap:8px; align-items:stretch; }
.triage-luma-row .triage-in { flex:1; }
.triage-cta-secondary {
  display:inline-flex; align-items:center; gap:6px; padding:9px 14px;
  border-radius:9px; border:1px solid var(--acc); background:var(--panel);
  color:var(--acc); font-family:inherit; font-size:13px; font-weight:600;
  cursor:pointer; transition:all 0.15s; white-space:nowrap;
}
.triage-cta-secondary:hover:not(:disabled) { background:var(--acc-soft); }
.triage-cta-secondary:disabled { opacity:0.5; cursor:not-allowed; }
.triage-luma-ok {
  display:flex; align-items:center; gap:7px; padding:9px 12px; margin-top:10px;
  border-radius:9px; background:var(--good-soft, #e9f7ef); color:var(--good, #137a3d);
  border:1px solid #c9eedb; font-size:12.5px;
}

/* Upload */
.triage-drop {
  background:var(--panel); border:2px dashed var(--line); border-radius:14px;
  padding:60px 24px; display:flex; flex-direction:column; align-items:center;
  gap:10px; cursor:pointer; transition:all 0.15s; color:var(--ink-dim);
}
.triage-drop:hover { border-color:var(--acc); background:var(--acc-soft); color:var(--acc); }
.triage-drop.drag { border-color:var(--acc); background:var(--acc-soft); color:var(--acc); }
.triage-drop.busy { cursor:wait; }
.triage-drop-h { font-size:16px; font-weight:600; margin:6px 0 0; }
.triage-drop-sub { font-size:13px; margin:0; color:var(--ink-faint); }

.triage-uploaded {
  background:var(--panel); border:1px solid var(--line); border-radius:14px;
  padding:22px 22px; box-shadow:var(--shadow);
}
.triage-uploaded-head {
  display:flex; align-items:center; gap:14px; padding-bottom:14px;
  border-bottom:1px solid var(--line); color:var(--ink-dim);
}
.triage-uploaded-title { font-size:15px; font-weight:600; color:var(--ink); margin:0; }
.triage-uploaded-sub { font-size:12.5px; color:var(--ink-faint); margin:2px 0 0; }
.triage-progress { margin-top:14px; }
.triage-progress-head {
  display:flex; align-items:center; gap:7px; font-size:13px; color:var(--ink-dim);
  margin-bottom:8px;
}
.triage-progress-counts { margin-left:auto; font-size:12px; color:var(--ink-faint); font-variant-numeric:tabular-nums; }
.triage-progress-bar {
  height:6px; background:var(--gray-soft); border-radius:999px; overflow:hidden;
}
.triage-progress-fill {
  height:100%; background:linear-gradient(90deg,var(--acc),var(--acc-deep));
  transition:width 0.4s ease;
}
.triage-progress-hint { font-size:12px; color:var(--ink-faint); margin:10px 0 0; line-height:1.55; }
.triage-progress-inline { display:inline-flex; align-items:center; gap:4px; color:var(--ink-faint); }

/* Review */
.triage-filterbar {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  margin-bottom:14px; flex-wrap:wrap;
}
.triage-filter-pills { display:flex; gap:6px; flex-wrap:wrap; }
.triage-pill {
  display:inline-flex; align-items:center; gap:7px;
  padding:6px 12px; border-radius:999px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink-dim);
  font-family:inherit; font-size:12px; cursor:pointer; transition:all 0.12s;
}
.triage-pill:hover { color:var(--acc); border-color:var(--acc); }
.triage-pill.on { background:var(--ink); color:#fff; border-color:var(--ink); }
.triage-pill-count {
  background:rgba(0,0,0,0.08); color:inherit; padding:1px 7px;
  border-radius:999px; font-size:10.5px; font-weight:600;
}
.triage-pill.on .triage-pill-count { background:rgba(255,255,255,0.2); }
.triage-search {
  display:flex; align-items:center; gap:7px; padding:6px 12px;
  background:var(--panel); border:1px solid var(--line); border-radius:9px;
  color:var(--ink-faint); min-width:280px;
}
.triage-search input {
  flex:1; border:0; background:transparent; outline:none;
  font-family:inherit; font-size:13px; color:var(--ink);
}

.triage-table-wrap {
  background:var(--panel); border:1px solid var(--line); border-radius:12px;
  overflow:hidden; box-shadow:var(--shadow);
}
.triage-table { width:100%; border-collapse:collapse; font-size:13.5px; }
.triage-table thead th {
  background:#fbfbfd; border-bottom:1px solid var(--line);
  padding:12px 14px; text-align:left;
  font-size:11px; font-weight:600; color:var(--ink-faint);
  text-transform:uppercase; letter-spacing:0.06em;
}
.triage-table th.num { text-align:right; }
.triage-table td { padding:12px 14px; border-top:1px solid var(--line); vertical-align:top; }
.triage-table td.num { text-align:right; font-variant-numeric:tabular-nums; }
.triage-table .triage-table-empty {
  text-align:center; color:var(--ink-faint); padding:36px 14px;
}
.triage-row { cursor:pointer; transition:background 0.1s; }
.triage-row:hover { background:#fafbfd; }
.triage-row.sel { background:var(--acc-soft); }
.triage-name { font-weight:600; color:var(--ink); }
.triage-sub { font-size:12px; color:var(--ink-faint); margin-top:2px; }
.triage-sub-cell { font-size:12.5px; color:var(--ink-dim); }
.triage-reason { color:var(--ink-dim); font-size:12.5px; max-width:300px; line-height:1.45; }

.triage-score {
  display:inline-flex; align-items:center; justify-content:center;
  min-width:32px; padding:3px 9px; border-radius:7px;
  font-weight:700; font-size:13px; font-variant-numeric:tabular-nums;
}
.triage-score.hi  { background:var(--ok-soft);   color:var(--ok); }
.triage-score.mid { background:var(--warn-soft); color:var(--warn); }
.triage-score.lo  { background:var(--bad-soft);  color:var(--bad); }
.triage-score.muted { opacity:0.8; }

.triage-rec {
  display:inline-flex; align-items:center; gap:5px;
  padding:3px 10px; border-radius:999px;
  font-size:12px; font-weight:600;
}
.rec-accept  { background:var(--ok-soft);   color:var(--ok);   border:1px solid #d0eadb; }
.rec-maybe   { background:var(--warn-soft); color:var(--warn); border:1px solid #f1e1ba; }
.rec-reject  { background:var(--bad-soft);  color:var(--bad);  border:1px solid #f3d6dc; }
.rec-needs   { background:var(--gray-soft); color:var(--gray); border:1px solid var(--line); }
.triage-rec-pending { background:var(--gray-soft); color:var(--ink-faint); border:1px dashed var(--line); }

/* Drawer */
.triage-drawer-backdrop {
  position:fixed; inset:0; z-index:1000; background:rgba(15,15,30,0.32);
  display:flex; justify-content:flex-end; animation:tr-fade 0.18s ease;
}
.triage-drawer {
  width:540px; max-width:95vw; background:var(--panel);
  height:100vh; overflow-y:auto; padding:0 28px 32px;
  box-shadow:-12px 0 32px rgba(15,15,30,0.18);
}
.triage-drawer-head { padding:24px 0 18px; position:relative; }
.triage-drawer-close {
  position:absolute; right:0; top:24px; background:transparent; border:0;
  font-size:28px; line-height:1; color:var(--ink-faint); cursor:pointer;
  padding:0 6px;
}
.triage-drawer-close:hover { color:var(--ink); }
.triage-drawer h2 {
  margin:0 0 6px; font-size:22px; font-weight:700; letter-spacing:-0.015em;
}
.triage-drawer-links { display:flex; gap:14px; margin-top:10px; flex-wrap:wrap; }
.triage-drawer-links a {
  display:inline-flex; align-items:center; gap:4px;
  font-size:12.5px; color:var(--acc); text-decoration:none;
}
.triage-drawer-links a:hover { text-decoration:underline; }
.triage-drawer-rec {
  display:flex; align-items:center; justify-content:space-between;
  padding:14px 16px; background:#fafbfd; border:1px solid var(--line);
  border-radius:10px; margin-bottom:18px;
}
.triage-drawer-scores { display:flex; gap:18px; align-items:center; }
.triage-drawer-scores > div {
  display:flex; flex-direction:column; align-items:flex-end; gap:3px;
}
.triage-k {
  font-size:10px; text-transform:uppercase; letter-spacing:0.06em;
  color:var(--ink-faint); font-weight:600;
}
.triage-arch { font-size:12.5px; color:var(--ink-dim); text-transform:capitalize; }

.triage-drawer-sec { margin-bottom:16px; }
.triage-drawer-sec h3 {
  margin:0 0 7px; font-size:12px; text-transform:uppercase; letter-spacing:0.06em;
  color:var(--ink-faint); font-weight:600;
}
.triage-drawer-sec > div { font-size:13.5px; color:var(--ink-dim); line-height:1.6; }

.triage-dim-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px 18px; }
.triage-dim { font-size:12px; }
.triage-dim-head {
  display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;
  color:var(--ink-dim);
}
.triage-dim-v {
  font-weight:700; font-variant-numeric:tabular-nums;
  padding:1px 7px; border-radius:6px; font-size:11.5px;
}
.triage-dim-v.hi  { background:var(--ok-soft); color:var(--ok); }
.triage-dim-v.mid { background:var(--warn-soft); color:var(--warn); }
.triage-dim-v.lo  { background:var(--bad-soft); color:var(--bad); }
.triage-dim-bar { height:4px; background:var(--gray-soft); border-radius:999px; overflow:hidden; }
.triage-dim-fill { height:100%; transition:width 0.3s; }
.triage-dim-fill.hi  { background:var(--ok); }
.triage-dim-fill.mid { background:var(--warn); }
.triage-dim-fill.lo  { background:var(--bad); }

.triage-evidence { padding-left:18px; margin:0; }
.triage-evidence li { margin-bottom:4px; }
.triage-raw {
  background:#fafbfd; border:1px solid var(--line); border-radius:8px;
  padding:11px 13px; font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;
  font-size:11.5px; line-height:1.5; max-height:200px; overflow:auto;
  white-space:pre-wrap; word-break:break-word;
}
.triage-drawer-pending {
  display:flex; align-items:center; gap:10px; padding:32px;
  color:var(--ink-faint); justify-content:center;
}
.spin { animation:tr-spin 0.8s linear infinite; }
@keyframes tr-spin { to { transform:rotate(360deg); } }

/* Landing (signed-out triage signup) */
.triage-landing {
  min-height:100vh; background:var(--bg);
  display:flex; align-items:center; justify-content:center; padding:32px;
}
.triage-landing-card {
  width:100%; max-width:520px; background:var(--panel);
  border:1px solid var(--line); border-radius:16px;
  padding:36px 36px 32px; box-shadow:var(--shadow-md);
}
.triage-landing-brand { display:flex; align-items:center; gap:10px; margin-bottom:24px; }
.triage-landing-h1 {
  font-family:'Playfair Display',Georgia,serif; font-weight:600;
  font-size:32px; line-height:1.18; letter-spacing:-0.015em;
  margin:0 0 12px;
}
.triage-landing-h1 em { color:var(--acc); font-style:italic; }
.triage-landing-sub {
  font-size:14px; line-height:1.6; color:var(--ink-dim);
  margin:0 0 24px;
}
.triage-landing-form { display:flex; flex-direction:column; gap:6px; }
.triage-landing-form label {
  font-size:11px; text-transform:uppercase; letter-spacing:0.06em;
  color:var(--ink-faint); font-weight:600; margin-top:8px;
}
.triage-landing-form label:first-of-type { margin-top:0; }
.triage-landing-cta {
  width:100%; justify-content:center; margin-top:14px;
}
.triage-landing-bullets {
  list-style:none; padding:0; margin:24px 0 0;
  display:flex; flex-direction:column; gap:7px;
}
.triage-landing-bullets li {
  position:relative; padding-left:18px;
  font-size:12.5px; color:var(--ink-faint); line-height:1.5;
}
.triage-landing-bullets li::before {
  content:""; position:absolute; left:0; top:8px;
  width:5px; height:5px; border-radius:50%; background:var(--acc);
}
.triage-landing-secondary {
  width:100%; margin-top:18px; padding:9px 14px;
  background:transparent; border:1px dashed var(--line);
  border-radius:10px; color:var(--ink-faint);
  font-family:inherit; font-size:12.5px; cursor:pointer;
  transition:all 0.15s;
}
.triage-landing-secondary:hover {
  color:var(--ink-dim); border-color:var(--ink-faint);
}
.triage-li-cta {
  display:inline-flex; align-items:center; justify-content:center; gap:10px;
  width:100%; padding:13px 22px; border-radius:999px; border:0;
  background:var(--li); color:#fff; font-family:inherit;
  font-weight:600; font-size:14.5px; cursor:pointer;
  transition:all 0.15s;
  box-shadow:0 2px 6px rgba(10,102,194,0.25);
}
.triage-li-cta:hover:not(:disabled) {
  background:var(--li-deep);
  box-shadow:0 6px 14px rgba(10,102,194,0.3);
  transform:translateY(-1px);
}
.triage-li-cta:disabled { opacity:0.7; cursor:wait; }
.triage-landing-divider {
  display:flex; align-items:center; gap:12px; margin:18px 0 14px;
  color:var(--ink-faint); font-size:11px; text-transform:uppercase;
  letter-spacing:0.08em;
}
.triage-landing-divider::before, .triage-landing-divider::after {
  content:""; flex:1; height:1px; background:var(--line);
}
.triage-landing-cancel {
  margin-top:6px; background:none; border:0; padding:6px;
  color:var(--ink-faint); font-family:inherit; font-size:12px;
  cursor:pointer; text-decoration:underline;
}
.triage-landing-cancel:hover { color:var(--ink-dim); }
`;
