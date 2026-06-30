import React, { useState, useEffect, useRef, useMemo } from "react";
import {
  ArrowRight, Check, Upload, Search,
  Loader2, FileText, Sparkles, AlertCircle, ExternalLink,
} from "lucide-react";
import { api } from "./lib/api.js";
import { SURPLUS_APP_CSS } from "./surplusTheme.js";


// ─── Stage 02 : Upload ─────────────────────────────────────────

export function UploadStep({ eventId, onNext }) {
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
      <header className="stage-head">
        <h1>Upload the applicant CSV</h1>
      </header>
      <p className="lede">Drop your Luma export. We&apos;ll score every applicant against the rubric from step 1.</p>

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
          </div>

          <div className="stage-foot" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="btn-primary" onClick={onNext}>
              See review queue <ArrowRight size={16} />
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="api-error" role="alert">
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

export function ReviewStep({ eventId }) {
  const [applicants, setApplicants] = useState([]);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState(null);
  // Event capacity (headcount) : caps how many we auto-accept so we only
  // take the top-N best-fit recommended applicants.
  const [capacity, setCapacity] = useState(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkMsg, setBulkMsg] = useState(null);

  useEffect(() => {
    if (!eventId) return;
    let alive = true;
    api.getEvent(eventId)
      .then((ev) => { if (alive) setCapacity(ev?.capacity != null ? Number(ev.capacity) : null); })
      .catch(() => {});
    return () => { alive = false; };
  }, [eventId]);

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

  // System-recommended "accept"s, best fit first. If the event has a
  // capacity, only the top-N fit into the room, so that's the slice we
  // auto-accept.
  const recommendedAccepts = useMemo(() => (
    applicants
      .filter((a) => a.evaluation?.recommendation === "accept")
      .sort((x, y) => (y.evaluation?.fit_score ?? 0) - (x.evaluation?.fit_score ?? 0))
  ), [applicants]);
  const capped = capacity != null && capacity > 0;
  const acceptTarget = capped ? recommendedAccepts.slice(0, capacity) : recommendedAccepts;
  // Skip anyone already accepted so re-clicks are no-ops.
  const toAccept = acceptTarget.filter((a) => a.decision?.human_decision !== "accept");

  const autoAcceptRecommended = async () => {
    if (bulkBusy || toAccept.length === 0) return;
    setBulkBusy(true);
    setBulkMsg(null);
    try {
      const updated = await Promise.all(
        toAccept.map((a) => api.setTriageDecision(eventId, a.id, {
          decision: "accept",
          notes: a.decision?.reviewer_notes || "",
        })),
      );
      const byId = new Map(updated.map((u) => [u.id, u]));
      setApplicants((prev) => prev.map((a) => byId.get(a.id) || a));
      setBulkMsg(`Accepted ${updated.length} applicant${updated.length === 1 ? "" : "s"}.`);
    } catch (e) {
      setBulkMsg(e.message || "Bulk accept failed.");
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div className="triage-review">
      <header className="stage-head triage-head-row">
        <div>
          <h1>Review queue</h1>
          <p className="lede" style={{ marginTop: 6 }}>
            {applicants.length} applicants
            {progress && progress.pending > 0 && (
              <span className="triage-progress-inline">
                · <Loader2 className="spin" size={12} /> scoring {progress.pending} more
              </span>
            )}
          </p>
        </div>
        <div className="triage-head-actions">
          {recommendedAccepts.length > 0 && (
            <button
              type="button"
              className="triage-cta-primary"
              disabled={bulkBusy || toAccept.length === 0}
              onClick={autoAcceptRecommended}
              title={capped && recommendedAccepts.length > capacity
                ? `${recommendedAccepts.length} recommended · capped to top ${capacity} (event capacity)`
                : undefined}
            >
              {bulkBusy
                ? <><Loader2 className="spin" size={14} /> Accepting…</>
                : <><Check size={14} /> Accept top {acceptTarget.length} recommended
                    {capped && recommendedAccepts.length > capacity ? ` (capacity ${capacity})` : ""}</>}
            </button>
          )}
          {eventId && applicants.length > 0 && (
            <a
              className="triage-cta-secondary"
              href={api.triageExportUrl(eventId)}
              target="_blank"
              rel="noopener noreferrer"
              download
            >
              <FileText size={14} /> Export CSV
            </a>
          )}
        </div>
      </header>
      {bulkMsg && (
        <p className="triage-bulk-msg">{bulkMsg}</p>
      )}

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
        <ApplicantDrawer
          applicant={selected}
          eventId={eventId}
          onApplicantUpdated={(updated) => {
            setApplicants((prev) =>
              prev.map((a) => (a.id === updated.id ? updated : a))
            );
          }}
          onClose={() => setSelectedId(null)}
        />
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
        <div>{a.role || "-"}</div>
        <div className="triage-sub">{a.company || ""}</div>
      </td>
      <td className="triage-sub-cell">{ev?.archetype || "-"}</td>
      <td className="num">{ev ? <ScorePill v={ev.fit_score} /> : "-"}</td>
      <td className="num">{ev ? <ScorePill v={ev.confidence_score} muted /> : "-"}</td>
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

function ApplicantDrawer({ applicant, eventId, onApplicantUpdated, onClose }) {
  const ev = applicant.evaluation;
  const decision = applicant.decision;
  const [notes, setNotes] = useState(decision?.reviewer_notes || "");
  const [savingDecision, setSavingDecision] = useState(null); // which button is in-flight
  const [decisionError, setDecisionError] = useState(null);

  useEffect(() => {
    setNotes(applicant.decision?.reviewer_notes || "");
  }, [applicant.id, applicant.decision?.reviewer_notes]);

  const submitDecision = async (choice) => {
    if (!eventId) return;
    setDecisionError(null);
    setSavingDecision(choice);
    try {
      const updated = await api.setTriageDecision(eventId, applicant.id, {
        decision: choice,
        notes: notes.trim(),
      });
      onApplicantUpdated && onApplicantUpdated(updated);
    } catch (err) {
      setDecisionError(err.message || "Could not save decision.");
    } finally {
      setSavingDecision(null);
    }
  };

  const DECISION_BUTTONS = [
    { key: "accept", label: "Accept" },
    { key: "maybe",  label: "Maybe"  },
    { key: "reject", label: "Reject" },
  ];

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

        <section className="triage-decision">
          <div className="triage-decision-row">
            {DECISION_BUTTONS.map((b) => {
              const active = decision?.human_decision === b.key;
              return (
                <button
                  key={b.key}
                  type="button"
                  className={`triage-decision-btn dec-${b.key} ${active ? "on" : ""}`}
                  disabled={savingDecision !== null}
                  onClick={() => submitDecision(b.key)}
                >
                  {savingDecision === b.key ? (
                    <Loader2 className="spin" size={14} />
                  ) : (active ? <Check size={14} /> : null)}
                  {b.label}
                </button>
              );
            })}
          </div>
          <textarea
            className="triage-decision-notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Notes for the cut list (optional)"
            rows={2}
          />
          {decisionError && (
            <div className="triage-error" role="alert" style={{ marginTop: 6 }}>
              <AlertCircle size={14} /> {decisionError}
            </div>
          )}
          {decision && !decisionError && (
            <div className="triage-decision-meta">
              Saved · system rec was{" "}
              <strong>{decision.system_recommendation || "-"}</strong>
            </div>
          )}
        </section>

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

            <DrawerSection title="Why fit">{ev.why_fit || "-"}</DrawerSection>
            <DrawerSection title="Why not">{ev.why_not_fit || "-"}</DrawerSection>

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
                {(!ev.evidence_used || ev.evidence_used.length === 0) && <li className="triage-sub">no evidence cited</li>}
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


export const TRIAGE_CSS = `
/* Triage-only: upload / review / drawer / landing (shell + configure use surplusTheme) */
.triage-landing {
  --bg:#f4f5f7; --panel:#ffffff; --panel-2:#fbfcfd; --line:#e6e8eb;
  --ink:#1b1e22; --ink-dim:#5b616a; --ink-faint:#99a0a8;
  --acc:#2f6df6; --acc-deep:#2257d6; --acc-soft:#eaf1fe;
  --r-card:16px; --r-pill:999px;
  --warn:#a87100; --warn-soft:#fef5e0;
  --bad:#c43146; --bad-soft:#fce6ea;
  --gray:#5b596b; --gray-soft:#f0f0f5;
  --shadow:0 8px 30px rgba(20,23,28,0.08); --shadow-sm:0 3px 14px rgba(20,23,28,0.06);
  --shadow-md:0 8px 24px rgba(15,15,30,0.08);
  --li:#0a66c2; --li-deep:#084e96;
  font-family:'Inter',system-ui,sans-serif;
  color:var(--ink);
  background:var(--bg);
  min-height:100vh;
}

.triage-cta-secondary {
  display:inline-flex; align-items:center; gap:6px; padding:9px 14px;
  border-radius:var(--r-el); border:1px solid var(--acc); background:var(--panel-2);
  color:var(--acc); font-family:inherit; font-size:12.5px; font-weight:600;
  cursor:pointer; transition:all 0.15s; white-space:nowrap; text-decoration:none;
  box-sizing:border-box;
}
.triage-cta-secondary:hover { background:var(--acc-soft); }
.triage-head-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.triage-cta-primary {
  display:inline-flex; align-items:center; gap:6px; padding:9px 14px;
  border-radius:var(--r-el); border:1px solid var(--acc); background:var(--acc);
  color:#fff; font-family:inherit; font-size:12.5px; font-weight:600;
  cursor:pointer; transition:all 0.15s; white-space:nowrap; box-sizing:border-box;
}
.triage-cta-primary:hover:not(:disabled) { background:var(--acc-deep); }
.triage-cta-primary:disabled { opacity:0.5; cursor:not-allowed; }
.triage-bulk-msg { font-size:12px; color:var(--ink-dim); margin:8px 0 0; }

.triage-error {
  display:flex; align-items:center; gap:7px; padding:10px 13px;
  margin:14px 0 0; border-radius:9px;
  background:var(--no-soft); color:var(--no); border:1px solid #f3d6dc;
  font-size:13px;
}
.triage-upload .lede { margin-bottom:18px; max-width:560px; }

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

/* Decision bar : accept / maybe / reject + notes */
.stage-head.triage-head-row {
  display:flex; align-items:flex-start; justify-content:space-between; gap:16px;
  max-width:none; width:100%; margin-bottom:12px;
}
.triage-head-row .triage-cta-secondary { margin-top:4px; text-decoration:none; }
.triage-decision {
  margin-bottom:18px; padding:14px; border-radius:10px;
  background:#fafbfd; border:1px solid var(--line);
}
.triage-decision-row { display:flex; gap:8px; margin-bottom:10px; }
.triage-decision-btn {
  flex:1; display:inline-flex; align-items:center; justify-content:center; gap:6px;
  padding:9px 14px; border-radius:9px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink-dim);
  font-family:inherit; font-size:13px; font-weight:600;
  cursor:pointer; transition:all 0.12s;
}
.triage-decision-btn:hover:not(:disabled) { transform:translateY(-1px); }
.triage-decision-btn:disabled { opacity:0.5; cursor:not-allowed; }
.triage-decision-btn.dec-accept.on { background:var(--ok-soft); color:var(--ok); border-color:#a8d9be; }
.triage-decision-btn.dec-maybe.on  { background:var(--warn-soft); color:var(--warn); border-color:#e8d39a; }
.triage-decision-btn.dec-reject.on { background:var(--bad-soft); color:var(--bad); border-color:#e7b8c0; }
.triage-decision-notes {
  width:100%; padding:8px 11px; border-radius:8px; border:1px solid var(--line);
  font-family:inherit; font-size:12.5px; color:var(--ink); resize:vertical;
  box-sizing:border-box; background:var(--panel);
}
.triage-decision-notes:focus { outline:none; border-color:var(--acc); }
.triage-decision-meta {
  margin-top:8px; font-size:11.5px; color:var(--ink-faint);
}
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
