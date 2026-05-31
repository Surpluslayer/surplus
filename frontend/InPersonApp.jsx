// InPersonApp.jsx : the phone-first "scan-to-connect" surface.
//
// A one-handed companion to the desktop pipeline. The operator is standing in
// front of someone at an event: pick the event, capture the person (QR / paste
// link / type a name), review a warm draft, and send or save-to-review. A
// captures tab is the lightweight relationship manager for everyone scanned.
//
// Reuse:
//   - lib/api.js          : same request() wrapper + cookie session
//   - lib/notify.js       : ensureNotifyPermission / notifyDevice
//   - lib/labels.js       : actionLabel / statusMeta / outreachStateLabel
//   - current_user session: api.me(); bounce to LinkedIn sign-in if unauthed
//
// Mounted by main.jsx when the path is /inperson (or ?surface=inperson).
import React, { useState, useEffect, useRef, useCallback } from "react";
import jsQR from "jsqr";
import {
  Camera, Link2, Search, Send, Bookmark, ArrowLeft, Check, Loader2,
  QrCode, User, Users, RefreshCw, AlertCircle, ChevronRight,
} from "lucide-react";
import { api } from "./lib/api.js";
import { ensureNotifyPermission, notifyDevice } from "./lib/notify.js";
import { actionLabel, statusMeta, outreachStateLabel } from "./lib/labels.js";

const ACTIVE_EVENT_KEY = "surplus_inperson_event";   // sessionStorage
const RECENT_LABELS_KEY = "surplus_inperson_recent";  // localStorage

function loadActiveEvent() {
  try { return JSON.parse(sessionStorage.getItem(ACTIVE_EVENT_KEY) || "null"); }
  catch { return null; }
}
function saveActiveEvent(ev) {
  try { sessionStorage.setItem(ACTIVE_EVENT_KEY, JSON.stringify(ev)); } catch {}
}
function loadRecentLabels() {
  try { return JSON.parse(localStorage.getItem(RECENT_LABELS_KEY) || "[]"); }
  catch { return []; }
}
function pushRecentLabel(label) {
  try {
    const cur = loadRecentLabels().filter((l) => l !== label);
    localStorage.setItem(RECENT_LABELS_KEY,
      JSON.stringify([label, ...cur].slice(0, 6)));
  } catch {}
}

// ── root ───────────────────────────────────────────────────────────────────

export default function InPersonApp() {
  const [user, setUser] = useState(null);          // null=loading, undefined=out
  const [event, setEvent] = useState(loadActiveEvent);
  const [tab, setTab] = useState("capture");       // "capture" | "people"
  const [result, setResult] = useState(null);      // scan result -> result screen
  const [openCapture, setOpenCapture] = useState(null);

  useEffect(() => { ensureNotifyPermission(); }, []);

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then((u) => { if (!cancelled) setUser(u || undefined); })
      .catch(() => { if (!cancelled) setUser(undefined); });
    return () => { cancelled = true; };
  }, []);

  const pickEvent = (ev) => { setEvent(ev); saveActiveEvent(ev); };

  if (user === null) {
    return <Centered><Loader2 className="spin" size={28} /></Centered>;
  }
  if (user === undefined) return <SignInBounce />;

  const notConnected = !user.unipile_account_id;

  return (
    <div className="ip-root">
      <style>{IP_CSS}</style>

      <EventBar event={event} onPick={pickEvent} />

      {notConnected && (
        <div className="ip-banner">
          <AlertCircle size={14} /> Connect LinkedIn to send. You can still
          scan and save captures.
        </div>
      )}

      {!event ? (
        <Centered>
          <div className="ip-empty">
            <QrCode size={34} />
            <p>Pick the event you’re at to start capturing.</p>
          </div>
        </Centered>
      ) : result ? (
        <ScanResult
          event={event}
          result={result}
          canSend={!notConnected}
          onDone={() => { setResult(null); setTab("people"); }}
          onCancel={() => setResult(null)}
        />
      ) : openCapture ? (
        <CaptureDetail
          event={event}
          capture={openCapture}
          canSend={!notConnected}
          onBack={() => setOpenCapture(null)}
        />
      ) : tab === "capture" ? (
        <CaptureScreen event={event} onResult={(r) => setResult(r)} />
      ) : (
        <CapturesScreen event={event} onOpen={(c) => setOpenCapture(c)} />
      )}

      {!result && !openCapture && (
        <nav className="ip-tabs">
          <button className={tab === "capture" ? "on" : ""}
                  onClick={() => setTab("capture")}>
            <Camera size={20} /><span>Capture</span>
          </button>
          <button className={tab === "people" ? "on" : ""}
                  onClick={() => setTab("people")}>
            <Users size={20} /><span>People</span>
          </button>
        </nav>
      )}
    </div>
  );
}

// ── sign-in bounce ───────────────────────────────────────────────────────────

function SignInBounce() {
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    try {
      const r = await api.startLinkedinAuth();
      if (r?.url) window.location.href = r.url;
    } catch (e) {
      alert("Could not start sign-in: " + (e.message || "unknown"));
      setBusy(false);
    }
  };
  return (
    <div className="ip-root">
      <style>{IP_CSS}</style>
      <Centered>
        <div className="ip-empty">
          <User size={34} />
          <p>Sign in to capture connections at your event.</p>
          <button className="ip-btn primary" onClick={go} disabled={busy}>
            {busy ? <Loader2 className="spin" size={16} /> : "Sign in with LinkedIn"}
          </button>
        </div>
      </Centered>
    </div>
  );
}

// ── event bar ────────────────────────────────────────────────────────────────

function EventBar({ event, onPick }) {
  const [open, setOpen] = useState(!event);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const recent = loadRecentLabels();

  const choose = async (lbl) => {
    const trimmed = (lbl || "").trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      const r = await api.inpersonCreateEvent(trimmed);
      pushRecentLabel(trimmed);
      onPick({ event_id: r.event_id, label: r.label || trimmed });
      setOpen(false);
      setLabel("");
    } catch (e) {
      alert("Could not set event: " + (e.message || "unknown"));
    } finally { setBusy(false); }
  };

  return (
    <div className="ip-eventbar">
      <button className="ip-eventpick" onClick={() => setOpen((o) => !o)}>
        <span className="ip-eventlabel">
          {event ? <><b>I’m at:</b> {event.label}</> : "Pick event"}
        </span>
        <ChevronRight size={16} className={open ? "rot" : ""} />
      </button>
      {open && (
        <div className="ip-eventmenu">
          <div className="ip-eventrow">
            <input
              className="ip-input" placeholder="e.g. NYC Tech Week — Founders Inc"
              value={label} onChange={(e) => setLabel(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") choose(label); }}
            />
            <button className="ip-btn primary sm" disabled={busy || !label.trim()}
                    onClick={() => choose(label)}>
              {busy ? <Loader2 className="spin" size={14} /> : "Set"}
            </button>
          </div>
          {recent.length > 0 && (
            <div className="ip-recents">
              {recent.map((l) => (
                <button key={l} className="ip-chip" onClick={() => choose(l)}>{l}</button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── capture screen (3 modes) ────────────────────────────────────────────────

function CaptureScreen({ event, onResult }) {
  const [mode, setMode] = useState("scan");   // "scan" | "paste" | "type"
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const doScan = useCallback(async (linkedin_url, source, enrich = {}) => {
    setErr(""); setBusy(true);
    try {
      const r = await api.inpersonScan({
        event_id: event.event_id, linkedin_url, source, ...enrich,
      });
      onResult(r);
    } catch (e) {
      setErr(e.message || "Capture failed");
    } finally { setBusy(false); }
  }, [event, onResult]);

  return (
    <div className="ip-screen">
      <div className="ip-seg">
        {[["scan", "Scan", QrCode], ["paste", "Paste", Link2], ["type", "Type", Search]]
          .map(([k, lbl, Icon]) => (
            <button key={k} className={mode === k ? "on" : ""} onClick={() => setMode(k)}>
              <Icon size={15} /> {lbl}
            </button>
          ))}
      </div>

      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      {mode === "scan" && <QrScanner busy={busy} onUrl={(u) => doScan(u, "scan")} />}
      {mode === "paste" && <PasteLink busy={busy} onSubmit={(u) => doScan(u, "link")} />}
      {mode === "type" && (
        <TypeSearch busy={busy}
          onConfirm={(cand) => doScan(cand.linkedin_url, "text", {
            name: cand.name, role: cand.headline || "",
          })} />
      )}
    </div>
  );
}

// QR mode : getUserMedia + jsQR. Decodes any QR; we only accept LinkedIn
// profile URLs (the backend's normalize_linkedin_url is the source of truth,
// so we hand it the raw decoded string and let it strip tracking params).
function QrScanner({ onUrl, busy }) {
  const videoRef = useRef(null);
  const canvasRef = useRef(null);
  const rafRef = useRef(0);
  const streamRef = useRef(null);
  const firedRef = useRef(false);
  const [status, setStatus] = useState("starting");  // starting|scanning|denied|nocam|unsupported
  const [hint, setHint] = useState("");

  useEffect(() => {
    let cancelled = false;
    if (!navigator.mediaDevices?.getUserMedia) { setStatus("unsupported"); return; }

    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" } }, audio: false,
        });
        if (cancelled) { stream.getTracks().forEach((t) => t.stop()); return; }
        streamRef.current = stream;
        const v = videoRef.current;
        if (v) {
          v.srcObject = stream;
          v.setAttribute("playsinline", "true");
          await v.play().catch(() => {});
        }
        setStatus("scanning");
        tick();
      } catch (e) {
        setStatus(e?.name === "NotAllowedError" ? "denied"
          : e?.name === "NotFoundError" ? "nocam" : "unsupported");
      }
    })();

    function tick() {
      rafRef.current = requestAnimationFrame(tick);
      const v = videoRef.current, c = canvasRef.current;
      if (!v || !c || v.readyState !== v.HAVE_ENOUGH_DATA || firedRef.current) return;
      const w = v.videoWidth, h = v.videoHeight;
      if (!w || !h) return;
      c.width = w; c.height = h;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(v, 0, 0, w, h);
      let img;
      try { img = ctx.getImageData(0, 0, w, h); } catch { return; }
      const code = jsQR(img.data, w, h, { inversionAttempts: "dontInvert" });
      if (!code || !code.data) return;
      const text = code.data.trim();
      if (/linkedin\.com\/in\//i.test(text)) {
        firedRef.current = true;
        onUrl(text);
      } else {
        setHint("That QR isn’t a LinkedIn profile. Try again or paste the link.");
      }
    }

    return () => {
      cancelled = true;
      cancelAnimationFrame(rafRef.current);
      if (streamRef.current) streamRef.current.getTracks().forEach((t) => t.stop());
    };
  }, [onUrl]);

  if (status === "denied" || status === "nocam" || status === "unsupported") {
    return (
      <div className="ip-camfallback">
        <Camera size={28} />
        <p>{status === "denied" ? "Camera permission denied."
          : status === "nocam" ? "No camera found."
          : "Camera isn’t available here."}</p>
        <p className="ip-dim">Switch to <b>Paste</b> and drop the profile link instead.</p>
      </div>
    );
  }

  return (
    <div className="ip-cam">
      <video ref={videoRef} className="ip-video" muted />
      <div className="ip-reticle" />
      <canvas ref={canvasRef} style={{ display: "none" }} />
      <div className="ip-camstatus">
        {busy ? <><Loader2 className="spin" size={14} /> Capturing…</>
          : status === "starting" ? "Starting camera…"
          : (hint || "Point at a LinkedIn ‘My Code’ QR")}
      </div>
    </div>
  );
}

function PasteLink({ onSubmit, busy }) {
  const [url, setUrl] = useState("");
  return (
    <div className="ip-pad">
      <label className="ip-lbl">LinkedIn profile link</label>
      <input className="ip-input" inputMode="url" autoCapitalize="off"
        placeholder="https://www.linkedin.com/in/…"
        value={url} onChange={(e) => setUrl(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && url.trim()) onSubmit(url.trim()); }} />
      <button className="ip-btn primary block" disabled={busy || !url.trim()}
              onClick={() => onSubmit(url.trim())}>
        {busy ? <Loader2 className="spin" size={16} /> : "Capture"}
      </button>
    </div>
  );
}

// Type mode : resolve -> ranked candidates -> tap to CONFIRM (no auto-pick).
function TypeSearch({ onConfirm, busy }) {
  const [f, setF] = useState({ name: "", title: "", company: "" });
  const [cands, setCands] = useState(null);   // null=not searched, []=none
  const [searching, setSearching] = useState(false);
  const [err, setErr] = useState("");

  const search = async () => {
    if (!f.name.trim()) return;
    setErr(""); setSearching(true); setCands(null);
    try {
      const r = await api.inpersonResolve({
        method: "text", name: f.name.trim(),
        title: f.title.trim(), company: f.company.trim(),
      });
      setCands(r.candidates || []);
    } catch (e) {
      setErr(e.message || "Search failed");
    } finally { setSearching(false); }
  };

  return (
    <div className="ip-pad">
      {["name", "title", "company"].map((k) => (
        <input key={k} className="ip-input" placeholder={k[0].toUpperCase() + k.slice(1)}
          value={f[k]} onChange={(e) => setF((s) => ({ ...s, [k]: e.target.value }))}
          onKeyDown={(e) => { if (e.key === "Enter") search(); }} />
      ))}
      <button className="ip-btn block" disabled={searching || !f.name.trim()} onClick={search}>
        {searching ? <Loader2 className="spin" size={16} /> : "Search LinkedIn"}
      </button>
      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      {cands?.length === 0 && (
        <div className="ip-dim ip-center">
          No matches. Ask for their QR or paste the link instead.
        </div>
      )}
      {cands?.length > 0 && (
        <div className="ip-cands">
          <div className="ip-dim ip-confirm-hint">Tap the right person to confirm:</div>
          {cands.map((c) => (
            <button key={c.linkedin_url} className="ip-cand" disabled={busy}
                    onClick={() => onConfirm(c)}>
              <div className="ip-cand-name">{c.name}</div>
              {c.headline && <div className="ip-cand-sub">{c.headline}</div>}
              <div className="ip-cand-url">{c.linkedin_url.replace(/^https?:\/\/(www\.)?/, "")}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── scan result : draft + send / save ────────────────────────────────────────

function ScanResult({ event, result, onDone, onCancel, canSend }) {
  const p = result.prospect || {};
  const [draftNote, setDraftNote] = useState(result.draft_note || "");
  const [draftMsg, setDraftMsg] = useState(result.draft_message || "");
  const [note, setNote] = useState(p.note || "");
  const [busy, setBusy] = useState("");          // "" | "send" | "save"
  const [err, setErr] = useState("");

  // Persist the (possibly edited) personal note back onto the capture via the
  // /scan upsert. The edited DRAFT text isn't server-persistable yet (no PATCH
  // endpoint) so we stash it locally for the captures detail to re-read.
  const persistNote = async () => {
    if ((note || "") === (p.note || "")) return;
    await api.inpersonScan({
      event_id: event.event_id, linkedin_url: p.linkedin_url,
      source: p.source || "scan", note,
    });
  };
  const stashDraft = () => {
    try {
      sessionStorage.setItem(`ip_draft_${p.prospect_id}`,
        JSON.stringify({ draftNote, draftMsg }));
    } catch {}
  };

  const save = async () => {
    setErr(""); setBusy("save");
    try { await persistNote(); stashDraft(); onDone(); }
    catch (e) { setErr(e.message || "Save failed"); setBusy(""); }
  };
  const send = async () => {
    setErr(""); setBusy("send");
    try {
      await persistNote();
      const res = await api.inpersonSend(p.prospect_id, {
        note: draftNote, message: draftMsg,
      });
      if (!res.dry_run && res.state) {
        notifyDevice(`${p.name}: ${outreachStateLabel(res.state)}`);
      }
      onDone();
    } catch (e) { setErr(e.message || "Send failed"); setBusy(""); }
  };

  return (
    <div className="ip-screen ip-result">
      <button className="ip-back" onClick={onCancel}><ArrowLeft size={18} /> Back</button>

      <div className="ip-person">
        <div className="ip-person-name">{p.name || "Unknown"}</div>
        <div className="ip-person-sub">
          {[p.role, p.company].filter(Boolean).join(" · ") || p.linkedin_url}
        </div>
        {result.resolve_failed && (
          <div className="ip-warn"><AlertCircle size={13} /> Couldn’t resolve on
            LinkedIn — saved anyway, retry from People.</div>
        )}
      </div>

      <label className="ip-lbl">Connection note <span className="ip-dim">≤300</span></label>
      <textarea className="ip-area" rows={3} maxLength={300}
        value={draftNote} onChange={(e) => setDraftNote(e.target.value)} />

      <label className="ip-lbl">First message</label>
      <textarea className="ip-area" rows={5}
        value={draftMsg} onChange={(e) => setDraftMsg(e.target.value)} />

      <label className="ip-lbl">Private note <span className="ip-dim">(just for you)</span></label>
      <input className="ip-input" placeholder="what you talked about…"
        value={note} onChange={(e) => setNote(e.target.value)} />

      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      <div className="ip-actions">
        <button className="ip-btn primary" onClick={save} disabled={!!busy}>
          {busy === "save" ? <Loader2 className="spin" size={16} />
            : <><Bookmark size={16} /> Save to review</>}
        </button>
        <button className="ip-btn ghost" onClick={send}
                disabled={!!busy || !canSend}
                title={canSend ? "" : "Connect LinkedIn to send"}>
          {busy === "send" ? <Loader2 className="spin" size={16} />
            : <><Send size={16} /> Send now</>}
        </button>
      </div>
      {!canSend && <div className="ip-dim ip-center">Connect LinkedIn to send now.</div>}
    </div>
  );
}

// ── captures : the relationship manager ──────────────────────────────────────

function connChip(c) {
  if (c.connection_status === "connected") return { label: "Connected", cls: "c-on" };
  if (c.connection_status === "not_connected") return { label: "Not connected", cls: "c-off" };
  return { label: "Unknown", cls: "c-unk" };
}

function CapturesScreen({ event, onOpen }) {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState("");
  const prevRef = useRef({});

  const load = useCallback(async () => {
    try {
      const r = await api.inpersonCaptures(event.event_id);
      const next = r.captures || [];
      // Diff against the previous poll : when a capture flips to connected and
      // the auto-DM has fired (message_sent), notify the device.
      const prev = prevRef.current;
      for (const c of next) {
        const was = prev[c.prospect_id];
        const dmJustSent = c.last_outreach?.state === "message_sent"
          && was?.last_outreach?.state !== "message_sent";
        if (was && c.connection_status === "connected" && dmJustSent) {
          notifyDevice(`${c.name} accepted — auto-DM sent`);
        }
      }
      prevRef.current = Object.fromEntries(next.map((c) => [c.prospect_id, c]));
      setRows(next);
    } catch (e) { setErr(e.message || "Could not load"); }
  }, [event]);

  useEffect(() => {
    load();
    const t = setInterval(load, 12000);   // poll : no push channel yet
    return () => clearInterval(t);
  }, [load]);

  if (rows === null) return <Centered><Loader2 className="spin" size={24} /></Centered>;

  return (
    <div className="ip-screen">
      <div className="ip-listhead">
        <span>{rows.length} captured</span>
        <button className="ip-iconbtn" onClick={load}><RefreshCw size={15} /></button>
      </div>
      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}
      {rows.length === 0 && (
        <div className="ip-dim ip-center" style={{ marginTop: 40 }}>
          No captures yet. Head to <b>Capture</b>.
        </div>
      )}
      <div className="ip-list">
        {rows.map((c) => {
          const st = statusMeta(c.status);
          const cc = connChip(c);
          return (
            <button key={c.prospect_id} className="ip-rowitem" onClick={() => onOpen(c)}>
              <div className="ip-row-main">
                <div className="ip-row-name">{c.name}</div>
                <div className="ip-row-sub">
                  {[c.role, c.company].filter(Boolean).join(" · ") || "—"}
                </div>
                <div className="ip-row-chips">
                  <span className={`ip-st ${st.cls}`}>{st.label}</span>
                  <span className={`ip-cc ${cc.cls}`}>{cc.label}</span>
                  {c.conversion && <span className="ip-st st-rsvp">{c.conversion}</span>}
                  {c.resolve_failed && <span className="ip-cc c-off">Unresolved</span>}
                </div>
              </div>
              <div className="ip-row-meta">
                {c.last_outreach
                  ? <span>{outreachStateLabel(c.last_outreach.state)}</span>
                  : <ChevronRight size={16} />}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CaptureDetail({ event, capture, onBack, canSend }) {
  const [draftNote, setDraftNote] = useState("");
  const [draftMsg, setDraftMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [sent, setSent] = useState(null);
  const isPending = capture.status === "pending";

  // Re-read any draft the operator stashed on the scan-result screen.
  useEffect(() => {
    try {
      const d = JSON.parse(sessionStorage.getItem(`ip_draft_${capture.prospect_id}`) || "null");
      if (d) { setDraftNote(d.draftNote || ""); setDraftMsg(d.draftMsg || ""); }
    } catch {}
  }, [capture.prospect_id]);

  const send = async () => {
    setErr(""); setBusy("send");
    try {
      const override = {};
      if (draftNote.trim()) override.note = draftNote;
      if (draftMsg.trim()) override.message = draftMsg;
      const res = await api.inpersonSend(capture.prospect_id, override);
      setSent(res);
      if (!res.dry_run && res.state) notifyDevice(`${capture.name}: ${outreachStateLabel(res.state)}`);
    } catch (e) { setErr(e.message || "Send failed"); }
    finally { setBusy(""); }
  };

  const cc = connChip(capture);
  const st = statusMeta(capture.status);

  return (
    <div className="ip-screen ip-result">
      <button className="ip-back" onClick={onBack}><ArrowLeft size={18} /> People</button>

      <div className="ip-person">
        <div className="ip-person-name">{capture.name}</div>
        <div className="ip-person-sub">
          {[capture.role, capture.company].filter(Boolean).join(" · ") || capture.linkedin_url}
        </div>
        <div className="ip-row-chips" style={{ marginTop: 8 }}>
          <span className={`ip-st ${st.cls}`}>{st.label}</span>
          <span className={`ip-cc ${cc.cls}`}>{cc.label}</span>
        </div>
      </div>

      {/* thread : only the last logged state is exposed today (see API gaps) */}
      <div className="ip-thread">
        <div className="ip-lbl">Activity</div>
        {capture.last_outreach
          ? <div className="ip-threadrow">
              <Check size={13} /> {outreachStateLabel(capture.last_outreach.state)}
              <span className="ip-dim"> · {fmtTs(capture.last_outreach.ts)}</span>
            </div>
          : <div className="ip-dim">Nothing sent yet.</div>}
        {capture.note && <div className="ip-privnote">“{capture.note}”</div>}
      </div>

      {isPending && (
        <>
          <label className="ip-lbl">Connection note</label>
          <textarea className="ip-area" rows={3} maxLength={300}
            placeholder="Leave blank to use the agent draft"
            value={draftNote} onChange={(e) => setDraftNote(e.target.value)} />
          <label className="ip-lbl">First message</label>
          <textarea className="ip-area" rows={4}
            placeholder="Leave blank to use the agent draft"
            value={draftMsg} onChange={(e) => setDraftMsg(e.target.value)} />
          {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}
          {sent
            ? <div className="ip-ok"><Check size={15} /> {outreachStateLabel(sent.state)}
                {sent.dry_run ? " (dry-run)" : ""} · {sent.path_taken}</div>
            : <button className="ip-btn primary block" onClick={send}
                      disabled={!!busy || !canSend}>
                {busy === "send" ? <Loader2 className="spin" size={16} />
                  : <><Send size={16} /> {actionLabel(capture.connection_status, false)}</>}
              </button>}
          {!canSend && <div className="ip-dim ip-center">Connect LinkedIn to send.</div>}
        </>
      )}

      {/* Mark-conversion is intentionally not wired : no in-person conversion
          endpoint exists yet (see API gaps in the PR description). */}
      <button className="ip-btn ghost block" disabled title="Coming soon">
        Mark conversion
      </button>
    </div>
  );
}

// ── tiny shared bits ─────────────────────────────────────────────────────────

function Centered({ children }) {
  return <div className="ip-centered">{children}</div>;
}
function fmtTs(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  } catch { return ""; }
}

const IP_CSS = `
:root { --ip-bg:#f6f7f9; --ip-card:#fff; --ip-line:#e6e8ec; --ip-ink:#1c2330;
  --ip-dim:#6b7585; --ip-accent:#0a66c2; --ip-accent-ink:#fff; }
.ip-root { max-width:520px; margin:0 auto; min-height:100dvh; background:var(--ip-bg);
  color:var(--ip-ink); display:flex; flex-direction:column;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.ip-root * { box-sizing:border-box; }
.spin { animation:ipspin 1s linear infinite; }
@keyframes ipspin { to { transform:rotate(360deg); } }

.ip-centered { flex:1; display:flex; align-items:center; justify-content:center; padding:24px; }
.ip-empty { text-align:center; color:var(--ip-dim); display:flex; flex-direction:column;
  align-items:center; gap:12px; }
.ip-empty p { margin:0; }

.ip-banner { background:#fff6e5; color:#7a5200; font-size:13px; padding:8px 14px;
  display:flex; gap:6px; align-items:center; }

/* event bar */
.ip-eventbar { position:sticky; top:0; z-index:5; background:var(--ip-card);
  border-bottom:1px solid var(--ip-line); }
.ip-eventpick { width:100%; display:flex; align-items:center; justify-content:space-between;
  padding:13px 16px; background:none; border:0; font-size:15px; color:var(--ip-ink); }
.ip-eventlabel b { color:var(--ip-dim); font-weight:600; margin-right:4px; }
.ip-eventpick .rot { transform:rotate(90deg); }
.ip-eventmenu { padding:0 12px 12px; }
.ip-eventrow { display:flex; gap:8px; }
.ip-recents { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.ip-chip { border:1px solid var(--ip-line); background:#fff; border-radius:999px;
  padding:5px 11px; font-size:12px; color:var(--ip-ink); }

/* screens */
.ip-screen { flex:1; padding:14px 14px 88px; overflow-y:auto; }
.ip-pad { display:flex; flex-direction:column; gap:10px; margin-top:12px; }
.ip-seg { display:flex; gap:4px; background:#eceef1; border-radius:12px; padding:4px; }
.ip-seg button { flex:1; border:0; background:none; padding:9px; border-radius:9px;
  font-size:13px; font-weight:600; color:var(--ip-dim); display:flex; gap:5px;
  align-items:center; justify-content:center; }
.ip-seg button.on { background:#fff; color:var(--ip-ink); box-shadow:0 1px 3px rgba(0,0,0,.08); }

.ip-input { width:100%; padding:12px 13px; border:1px solid var(--ip-line);
  border-radius:11px; font-size:16px; background:#fff; color:var(--ip-ink); }
.ip-area { width:100%; padding:11px 13px; border:1px solid var(--ip-line);
  border-radius:11px; font-size:15px; line-height:1.4; resize:vertical; background:#fff;
  color:var(--ip-ink); font-family:inherit; }
.ip-lbl { font-size:12px; font-weight:700; color:var(--ip-dim); text-transform:uppercase;
  letter-spacing:.03em; margin:12px 0 5px; display:block; }
.ip-lbl .ip-dim { font-weight:500; text-transform:none; letter-spacing:0; }

/* buttons */
.ip-btn { display:inline-flex; gap:7px; align-items:center; justify-content:center;
  border:1px solid var(--ip-line); background:#fff; color:var(--ip-ink); border-radius:12px;
  padding:13px 16px; font-size:15px; font-weight:600; cursor:pointer; }
.ip-btn:disabled { opacity:.5; }
.ip-btn.primary { background:var(--ip-accent); color:var(--ip-accent-ink); border-color:var(--ip-accent); }
.ip-btn.ghost { background:#fff; }
.ip-btn.sm { padding:10px 13px; font-size:13px; }
.ip-btn.block { width:100%; }
.ip-actions { display:flex; flex-direction:column; gap:9px; margin-top:16px; }
.ip-actions .ip-btn { width:100%; }

/* camera */
.ip-cam { position:relative; margin-top:14px; border-radius:16px; overflow:hidden;
  background:#000; aspect-ratio:1/1; }
.ip-video { width:100%; height:100%; object-fit:cover; }
.ip-reticle { position:absolute; inset:18%; border:3px solid rgba(255,255,255,.85);
  border-radius:18px; box-shadow:0 0 0 9999px rgba(0,0,0,.25); }
.ip-camstatus { position:absolute; left:0; right:0; bottom:0; padding:10px;
  text-align:center; color:#fff; font-size:13px; background:linear-gradient(transparent,rgba(0,0,0,.6));
  display:flex; gap:6px; align-items:center; justify-content:center; }
.ip-camfallback { text-align:center; color:var(--ip-dim); padding:36px 18px;
  display:flex; flex-direction:column; gap:8px; align-items:center; margin-top:14px;
  border:1px dashed var(--ip-line); border-radius:16px; }
.ip-camfallback p { margin:0; }

/* candidates */
.ip-cands { display:flex; flex-direction:column; gap:8px; margin-top:6px; }
.ip-confirm-hint { margin:6px 0 2px; }
.ip-cand { text-align:left; border:1px solid var(--ip-line); background:#fff;
  border-radius:12px; padding:11px 13px; }
.ip-cand-name { font-weight:700; font-size:15px; }
.ip-cand-sub { font-size:13px; color:var(--ip-dim); margin-top:2px; }
.ip-cand-url { font-size:11px; color:var(--ip-accent); margin-top:3px; }

/* result / detail */
.ip-back { background:none; border:0; color:var(--ip-accent); font-size:14px;
  display:inline-flex; gap:5px; align-items:center; padding:2px 0 10px; }
.ip-person { background:#fff; border:1px solid var(--ip-line); border-radius:14px; padding:14px; }
.ip-person-name { font-size:19px; font-weight:800; }
.ip-person-sub { font-size:13px; color:var(--ip-dim); margin-top:3px; }
.ip-warn,.ip-err,.ip-ok { font-size:13px; display:flex; gap:6px; align-items:center;
  margin-top:8px; padding:8px 11px; border-radius:10px; }
.ip-warn { background:#fff6e5; color:#7a5200; }
.ip-err { background:#fdecec; color:#a01818; }
.ip-ok { background:#e8f6ec; color:#1d7a37; }
.ip-thread { background:#fff; border:1px solid var(--ip-line); border-radius:14px;
  padding:12px 14px; margin-top:12px; }
.ip-threadrow { font-size:14px; display:flex; gap:6px; align-items:center; }
.ip-privnote { font-style:italic; color:var(--ip-dim); font-size:13px; margin-top:8px; }

/* list */
.ip-listhead { display:flex; justify-content:space-between; align-items:center;
  font-size:13px; color:var(--ip-dim); padding:2px 2px 10px; }
.ip-iconbtn,.ip-rowitem { background:#fff; border:1px solid var(--ip-line); }
.ip-iconbtn { border-radius:10px; padding:7px; color:var(--ip-dim); }
.ip-list { display:flex; flex-direction:column; gap:8px; }
.ip-rowitem { display:flex; align-items:center; gap:10px; border-radius:13px;
  padding:12px 13px; text-align:left; }
.ip-row-main { flex:1; min-width:0; }
.ip-row-name { font-weight:700; font-size:15px; }
.ip-row-sub { font-size:12px; color:var(--ip-dim); margin-top:1px; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.ip-row-chips { display:flex; flex-wrap:wrap; gap:5px; margin-top:6px; }
.ip-row-meta { font-size:11px; color:var(--ip-dim); flex-shrink:0; }
.ip-st,.ip-cc { font-size:10.5px; font-weight:700; padding:3px 7px; border-radius:999px;
  text-transform:uppercase; letter-spacing:.02em; }
.ip-st { background:#eef1f4; color:#566; }
.st-rsvp { background:#e8f6ec; color:#1d7a37; }
.st-contacted { background:#eaf1fb; color:#1c4f8a; }
.st-pending { background:#fff1d9; color:#8a5a00; }
.st-below { background:#f1f1f1; color:#888; }
.ip-cc.c-on { background:#e8f6ec; color:#1d7a37; }
.ip-cc.c-off { background:#fdecec; color:#a33; }
.ip-cc.c-unk { background:#eef1f4; color:#667; }

.ip-dim { color:var(--ip-dim); font-size:13px; }
.ip-center { text-align:center; }

/* bottom tabs */
.ip-tabs { position:sticky; bottom:0; display:flex; background:var(--ip-card);
  border-top:1px solid var(--ip-line); padding-bottom:env(safe-area-inset-bottom); }
.ip-tabs button { flex:1; border:0; background:none; padding:11px; display:flex;
  flex-direction:column; align-items:center; gap:3px; font-size:11px; color:var(--ip-dim); }
.ip-tabs button.on { color:var(--ip-accent); }
`;
