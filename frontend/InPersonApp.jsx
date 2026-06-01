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
import React, { useState, useEffect, useRef, useCallback, Component } from "react";
import jsQR from "jsqr";
import {
  Camera, Link2, Search, Send, Bookmark, ArrowLeft, Check, Loader2,
  QrCode, User, Users, RefreshCw, AlertCircle, ChevronRight, Activity,
  LogOut, Mic, MicOff, MapPin,
} from "lucide-react";
import { api } from "./lib/api.js";
import { ensureNotifyPermission, notifyDevice } from "./lib/notify.js";
import { actionLabel, statusMeta, outreachStateLabel } from "./lib/labels.js";

const ACTIVE_EVENT_KEY = "surplus_inperson_event";   // sessionStorage
const RECENT_LABELS_KEY = "surplus_inperson_recent";  // localStorage
const SETUP_SEEN_KEY = "surplus_inperson_setup_seen"; // localStorage : skip the one-time setup card

function loadActiveEvent() {
  try { return JSON.parse(sessionStorage.getItem(ACTIVE_EVENT_KEY) || "null"); }
  catch { return null; }
}
function saveActiveEvent(ev) {
  try { sessionStorage.setItem(ACTIVE_EVENT_KEY, JSON.stringify(ev)); } catch {}
}
function clearActiveEvent() {
  try { sessionStorage.removeItem(ACTIVE_EVENT_KEY); } catch {}
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

// Guest mode lives at /guest (event.surpluslayer.com/guest). Anything under
// that path opts into the auto-guest session; the bare host keeps the normal
// LinkedIn sign-in gate.
function isGuestPath() {
  try {
    const p = window.location.pathname || "";
    return p === "/guest" || p.startsWith("/guest/");
  } catch { return false; }
}

// ── error boundary ──────────────────────────────────────────────────────────
// Without this, ANY render throw blanks the whole app to a white screen with no
// message (a classic "loads, then goes blank after picking an event"). This
// catches it and shows the actual error + a reload, so failures are visible and
// reportable instead of silent.
class IpErrorBoundary extends Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) {
    console.error("[InPersonApp] render error", error, info?.componentStack);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="ip-root">
        <style>{IP_CSS}</style>
        <Centered>
          <div className="ip-empty">
            <AlertCircle size={34} />
            <p className="ip-empty-title">Something broke on this screen</p>
            <p style={{ fontSize: 13, wordBreak: "break-word" }}>
              {String(this.state.error?.message || this.state.error)}
            </p>
            <button className="ip-btn primary lg block"
                    onClick={() => window.location.reload()}>Reload</button>
          </div>
        </Centered>
      </div>
    );
  }
}

export default function InPersonApp() {
  return (
    <IpErrorBoundary>
      <InPersonAppInner />
    </IpErrorBoundary>
  );
}

function InPersonAppInner() {
  const [user, setUser] = useState(null);          // null=loading, undefined=out
  const [authError, setAuthError] = useState(null); // {status, message} for non-401 failures
  const [event, setEvent] = useState(loadActiveEvent);
  const [tab, setTab] = useState("capture");       // "capture" | "people" | "activity"
  const [result, setResult] = useState(null);      // scan result -> result screen
  const [openCapture, setOpenCapture] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [isOperator, setIsOperator] = useState(false);  // can see the Activity page
  const [setupDone, setSetupDone] = useState(
    () => { try { return localStorage.getItem(SETUP_SEEN_KEY) === "1"; } catch { return false; } });

  useEffect(() => { ensureNotifyPermission(); }, []);

  useEffect(() => {
    let cancelled = false;
    setUser(null); setAuthError(null);
    // Guest mode is gated to the /guest path : event.surpluslayer.com/guest
    // auto-mints a LinkedIn-less guest so a tester lands straight in the
    // capture flow (real sends still blocked -> "Connect LinkedIn to send").
    // Plain event.surpluslayer.com keeps the normal LinkedIn sign-in gate.
    const guestMode = isGuestPath();
    const resolveUser = async () => {
      try {
        const u = await api.me();
        if (u && typeof u === "object" && u.id) return u;
        // 200-but-not-a-user : request didn't reach the API cleanly. Surface it.
        throw Object.assign(new Error("non-account 200"), { _nonAccount: true });
      } catch (e) {
        if (e?.status === 401) {
          if (!guestMode) throw e;            // root path : show the sign-in screen
          // /guest : become a guest, then re-read me().
          await api.inpersonGuest();
          const u2 = await api.me();
          if (u2 && typeof u2 === "object" && u2.id) return u2;
          throw Object.assign(new Error("guest session not recognized"),
                              { status: 200 });
        }
        throw e;
      }
    };
    resolveUser()
      .then((u) => { if (!cancelled) setUser(u); })
      .catch((e) => {
        if (cancelled) return;
        if (e?.status === 401) { setUser(undefined); return; }  // genuinely signed out
        setAuthError({
          status: e?.status,
          message: e?._nonAccount
            ? "Reached the server but couldn't read your account."
            : (e?.message || "Could not reach the server."),
        });
        setUser(undefined);
      });
    return () => { cancelled = true; };
  }, [reloadKey]);

  // The operator (env-var account) can see the Activity roll-up. We can't read
  // the env var client-side, so probe the operator-only endpoint : 200 -> show
  // the tab, 403/anything -> hide it. Connected users only (guests skip it).
  useEffect(() => {
    let cancelled = false;
    if (!user || typeof user !== "object" || !user.unipile_account_id) {
      setIsOperator(false);
      return;
    }
    api.inpersonActivity()
      .then(() => { if (!cancelled) setIsOperator(true); })
      .catch(() => { if (!cancelled) setIsOperator(false); });
    return () => { cancelled = true; };
  }, [user]);

  const pickEvent = (ev) => { setEvent(ev); saveActiveEvent(ev); };

  // Sign out : revoke the session, drop the picked event, and reset to the
  // sign-in screen. For a guest this is the path to "upgrade" to a real
  // LinkedIn account. We deliberately do NOT bump reloadKey (that effect would
  // auto-mint a fresh guest on the /guest path) : staying on SignInBounce lets
  // the user choose how to come back.
  const signOut = async () => {
    try { await api.logout(); } catch { /* best-effort : clear locally anyway */ }
    clearActiveEvent();
    setEvent(null);
    setResult(null);
    setOpenCapture(null);
    setTab("capture");
    setIsOperator(false);
    setUser(undefined);
  };

  if (user === null) {
    return <Centered><Loader2 className="spin" size={28} /></Centered>;
  }
  if (user === undefined) {
    return <SignInBounce authError={authError}
                         onRetry={() => setReloadKey((k) => k + 1)} />;
  }

  const notConnected = !user.unipile_account_id;

  return (
    <div className="ip-root">
      <style>{IP_CSS}</style>

      <EventBar event={event} onPick={pickEvent} user={user} onSignOut={signOut} />

      {notConnected && (
        <div className="ip-banner">
          <AlertCircle size={14} /> You can capture people now. Connect LinkedIn
          when you’re ready to send.
        </div>
      )}

      {/* One-time setup : ask for the Calendly link + reply-to email up front so
          the next step auto-fills later. Connected users only; dismissable. */}
      {!notConnected && !setupDone && !user.calendly_url ? (
        <SchedulingSetup
          user={user}
          onDone={(patch) => {
            if (patch) setUser((u) => ({ ...u, ...patch }));
            try { localStorage.setItem(SETUP_SEEN_KEY, "1"); } catch {}
            setSetupDone(true);
          }}
        />
      ) : tab === "activity" && isOperator ? (
        <ActivityScreen />
      ) : !event ? (
        <Centered>
          <div className="ip-empty">
            <QrCode size={40} />
            <p className="ip-empty-title">Which event are you at?</p>
            <p>Tap <b>Pick event</b> up top to start capturing people.</p>
          </div>
        </Centered>
      ) : result ? (
        <ScanResult
          event={event}
          result={result}
          user={user}
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
          {isOperator && (
            <button className={tab === "activity" ? "on" : ""}
                    onClick={() => setTab("activity")}>
              <Activity size={20} /><span>Activity</span>
            </button>
          )}
        </nav>
      )}
    </div>
  );
}

// ── sign-in bounce ───────────────────────────────────────────────────────────

function SignInBounce({ authError = null, onRetry = null }) {
  const [busy, setBusy] = useState(false);
  // Mirror the desktop App's onSignIn: LinkedIn-connect is gated behind Stripe
  // payment (pay-first product flow), so /linkedin/start returns 402
  // payment_required for an anonymous / unpaid browser. Instead of dumping that
  // JSON in an alert, fall back to Stripe Checkout : the checkout mints the
  // account, and post-payment they come back signed-in + paid and can connect.
  const go = async () => {
    setBusy(true);
    try {
      const r = await api.startLinkedinAuth();
      if (r?.url) { window.location.href = r.url; return; }
      setBusy(false);
    } catch (e) {
      const code = e?.body?.detail?.code || e?.body?.code;
      if (e?.status === 402 || code === "payment_required") {
        try {
          const r = await api.startCheckout();
          if (r?.url) { window.location.href = r.url; return; }
        } catch (e2) {
          alert("Could not start checkout: " + (e2.message || "unknown"));
        }
      } else {
        alert("Could not start sign-in: " + (e.message || "unknown"));
      }
      setBusy(false);
    }
  };
  return (
    <div className="ip-root">
      <style>{IP_CSS}</style>
      <Centered>
        <div className="ip-empty">
          <QrCode size={40} />
          <p className="ip-empty-title">Capture people you meet</p>
          <p>Scan a LinkedIn QR, send a connection, and we’ll draft the message for you.</p>
          <button className="ip-btn primary lg block" onClick={go} disabled={busy}>
            {busy ? <Loader2 className="spin" size={18} /> : "Sign in with LinkedIn"}
          </button>
          {authError && (
            <div className="ip-warn" style={{ marginTop: 14, maxWidth: 340 }}>
              <AlertCircle size={13} />
              <span>
                {authError.status === 200
                  ? "We reached the server but couldn't read your account."
                  : `Couldn't reach the account service${authError.status ? ` (${authError.status})` : ""}.`}
                {onRetry && (
                  <> {" "}
                    <button className="ip-linkbtn" onClick={onRetry}>Retry</button>
                  </>
                )}
              </span>
            </div>
          )}
        </div>
      </Centered>
    </div>
  );
}

// ── event bar ────────────────────────────────────────────────────────────────

function EventBar({ event, onPick, user, onSignOut }) {
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
      <div className="ip-eventhead">
        <button className={`ip-eventpick${event ? "" : " empty"}`} onClick={() => setOpen((o) => !o)}>
          <span className="ip-eventlabel">
            {event ? <><MapPin size={15} /> {event.label}</> : "Pick event to start →"}
          </span>
          <ChevronRight size={16} className={open ? "rot" : ""} />
        </button>
        {onSignOut && (
          <button className="ip-signout"
                  title={user?.unipile_account_id
                    ? `Sign out${user?.name ? ` (${user.name})` : ""}`
                    : "Sign out of guest"}
                  onClick={onSignOut}>
            <LogOut size={15} />
            <span>{user?.unipile_account_id ? "Sign out" : "Guest"}</span>
          </button>
        )}
      </div>
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
      <div className="ip-howto">
        <b>1.</b> Add the person · <b>2.</b> Connect. That’s it.
      </div>
      <div className="ip-seg">
        {[["scan", "Scan QR", QrCode], ["paste", "Paste link", Link2], ["type", "By name", Search]]
          .map(([k, lbl, Icon]) => (
            <button key={k} className={mode === k ? "on" : ""} onClick={() => setMode(k)}>
              <Icon size={17} /> {lbl}
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
        {busy ? <><Loader2 className="spin" size={14} /> Got it — saving…</>
          : status === "starting" ? "Starting camera…"
          : (hint || "Ask them to open their LinkedIn QR, then point here")}
      </div>
    </div>
  );
}

function PasteLink({ onSubmit, busy }) {
  const [url, setUrl] = useState("");
  return (
    <div className="ip-pad">
      <label className="ip-lbl">Paste their LinkedIn link</label>
      <input className="ip-input" inputMode="url" autoCapitalize="off"
        placeholder="linkedin.com/in/…"
        value={url} onChange={(e) => setUrl(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && url.trim()) onSubmit(url.trim()); }} />
      <button className="ip-btn primary block lg" disabled={busy || !url.trim()}
              onClick={() => onSubmit(url.trim())}>
        {busy ? <Loader2 className="spin" size={18} /> : <><Check size={18} /> Add person</>}
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
      {[["name", "Full name (required)"], ["title", "Job title (optional)"],
        ["company", "Company (optional)"]].map(([k, ph]) => (
        <input key={k} className="ip-input" placeholder={ph}
          value={f[k]} onChange={(e) => setF((s) => ({ ...s, [k]: e.target.value }))}
          onKeyDown={(e) => { if (e.key === "Enter") search(); }} />
      ))}
      <button className="ip-btn block lg" disabled={searching || !f.name.trim()} onClick={search}>
        {searching ? <Loader2 className="spin" size={18} /> : <><Search size={18} /> Find on LinkedIn</>}
      </button>
      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      {cands?.length === 0 && (
        <div className="ip-dim ip-center">
          No matches. Ask for their QR or paste the link instead.
        </div>
      )}
      {cands?.length > 0 && (
        <div className="ip-cands">
          <div className="ip-dim ip-confirm-hint">Which one is them? Tap to add:</div>
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

// One-time setup : the user's reusable booking link + reply-to email, set up
// front so "book a time" auto-fills as the next step on every capture.
function SchedulingSetup({ user, onDone }) {
  const [calendly, setCalendly] = useState(user?.calendly_url || "");
  const [email, setEmail] = useState(user?.email || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const save = async () => {
    setErr(""); setBusy(true);
    try {
      const r = await api.updateScheduling({ calendly_url: calendly, email });
      onDone({ calendly_url: r.calendly_url, email: r.email });
    } catch (e) { setErr(e.message || "Couldn’t save"); setBusy(false); }
  };

  return (
    <div className="ip-screen">
      <div className="ip-person">
        <div className="ip-person-name">Quick setup</div>
        <div className="ip-person-sub">
          Set these once — we’ll offer your booking link as the next step on every
          person you capture.
        </div>
      </div>

      <label className="ip-lbl">Your booking link
        <span className="ip-dim"> · Calendly, Cal.com, etc.</span></label>
      <input className="ip-input" inputMode="url" autoCapitalize="off"
        placeholder="calendly.com/you/15min"
        value={calendly} onChange={(e) => setCalendly(e.target.value)} />

      <label className="ip-lbl">Reply-to email <span className="ip-dim">· optional</span></label>
      <input className="ip-input" inputMode="email" autoCapitalize="off"
        placeholder="you@example.com"
        value={email} onChange={(e) => setEmail(e.target.value)} />

      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      <div className="ip-actions">
        <button className="ip-btn primary lg" onClick={save} disabled={busy}>
          {busy ? <Loader2 className="spin" size={18} /> : <><Check size={18} /> Save & continue</>}
        </button>
        <button className="ip-btn ghost sm" onClick={() => onDone(null)} disabled={busy}>
          Skip for now
        </button>
      </div>
      <div className="ip-dim ip-center ip-laterhint">
        You can change this anytime — it just pre-fills the next step.
      </div>
    </div>
  );
}

// Intent presets. The chip VALUE is stored on the capture (contact_type) and the
// LABEL is the intent string the draft engine steers on (Sales / Networking /
// Hiring / Vibes). "Other" reveals a freeform box whose text IS the intent.
const INTENTS = [["sales", "Sales"], ["networking", "Networking"],
                 ["hiring", "Hiring"], ["vibes", "Vibes"], ["other", "Other"]];

function initialsOf(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return ((parts[0][0] || "") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

function ScanResult({ event, result, user, onDone, onCancel, canSend }) {
  const p = result.prospect || {};
  const headline = [p.role, p.company].filter(Boolean).join(" · ");
  const knownIntent = INTENTS.some(([v]) => v === p.contact_type) ? p.contact_type : "";

  const [intent, setIntent] = useState(knownIntent);   // chip value, "" = none
  const [otherText, setOtherText] = useState("");        // freeform when intent === "other"
  const [context, setContext] = useState(p.note || ""); // "what stuck"
  const [typing, setTyping] = useState(!!(p.note || "")); // reveal the type-it field
  const [draftNote, setDraftNote] = useState("");
  const [draftMsg, setDraftMsg] = useState("");
  const [showPreview, setShowPreview] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [busy, setBusy] = useState("");   // "" | "send" | "save" | "nonote"
  const [err, setErr] = useState("");

  // Dictation feeds the same "what stuck" field. Appends finalized phrases.
  const stt = useSpeechToText((t) => { setTyping(true); setContext((c) => (c ? `${c} ${t}` : t)); });

  // The intent string the engine steers on : the freeform text for "Other",
  // else the preset label. Defaults to Networking when nothing's picked.
  const effectiveIntent = intent === "other"
    ? (otherText.trim() || "Other")
    : (INTENTS.find(([v]) => v === intent)?.[1] || "Networking");

  // Key identifying what produced the current draft, so we know when it's stale.
  const draftKeyRef = useRef(null);
  const curKey = () => JSON.stringify([effectiveIntent, context.trim()]);
  const isStale = () => draftKeyRef.current !== curKey();

  const genDraft = async () => {
    setDrafting(true); setErr("");
    try {
      const d = await api.draftConnect({
        contact: { name: p.name || "", headline },
        intent: effectiveIntent, context,
      });
      setDraftNote(d.connection_note || "");
      setDraftMsg(d.first_message || "");
      draftKeyRef.current = curKey();
      return d;
    } catch (e) { setErr(e.message || "Couldn’t draft"); throw e; }
    finally { setDrafting(false); }
  };

  const togglePreview = () => {
    const next = !showPreview;
    setShowPreview(next);
    if (next && (isStale() || !draftMsg)) genDraft().catch(() => {});
  };

  // Persist "what stuck" (note) + the chosen intent (contact_type) onto the
  // capture. Idempotent upsert; skip when nothing changed.
  const persist = async () => {
    if ((context || "") === (p.note || "") && (intent || "") === (p.contact_type || "")) return;
    await api.inpersonScan({
      event_id: event.event_id, linkedin_url: p.linkedin_url,
      source: p.source || "scan", note: context, contact_type: intent || undefined,
    });
  };
  const stashDraft = (note, msg) => {
    try {
      sessionStorage.setItem(`ip_draft_${p.prospect_id}`,
        JSON.stringify({ draftNote: note, draftMsg: msg }));
    } catch {}
  };

  const save = async () => {
    setErr(""); setBusy("save");
    try { await persist(); stashDraft(draftNote, draftMsg); onDone(); }
    catch (e) { setErr(e.message || "Save failed"); setBusy(""); }
  };

  // Connect. We need a draft : if the operator never opened the preview (or it's
  // stale), generate it on the fly so the note + first message reflect their
  // intent. noNote sends a bare invite; the first message still fires on accept.
  const connect = async (noNote = false) => {
    setErr(""); setBusy(noNote ? "nonote" : "send");
    try {
      await persist();
      let note = draftNote, msg = draftMsg;
      if (isStale() || !msg) {
        const d = await genDraft();
        note = d.connection_note || ""; msg = d.first_message || "";
      }
      const res = await api.inpersonSend(p.prospect_id,
        noNote ? { no_note: true, message: msg } : { note, message: msg });
      if (!res.dry_run && res.state) notifyDevice(`${p.name}: ${outreachStateLabel(res.state)}`);
      onDone();
    } catch (e) { setErr(e.message || "Send failed"); setBusy(""); }
  };

  return (
    <div className="ip-screen ip-result">
      <button className="ip-back" onClick={onCancel}><ArrowLeft size={18} /> Back</button>

      <div className="ip-person ip-person-row">
        <div className="ip-avatar">{initialsOf(p.name)}</div>
        <div className="ip-person-id">
          <div className="ip-person-name">{p.name || "Unknown"}</div>
          <div className="ip-person-sub">{headline || p.linkedin_url}</div>
        </div>
      </div>
      {result.resolve_failed && (
        <div className="ip-warn"><AlertCircle size={13} /> Couldn’t resolve on
          LinkedIn — saved anyway, retry from People.</div>
      )}

      {/* Why connect? : intent is the strongest lever on the draft. */}
      <label className="ip-lbl">Why connect?</label>
      <div className="ip-chiprow">
        {INTENTS.map(([v, lbl]) => (
          <button key={v} type="button"
                  className={`ip-chip${intent === v ? " on" : ""}`}
                  onClick={() => setIntent(intent === v ? "" : v)}>
            {lbl}
          </button>
        ))}
      </div>
      {intent === "other" && (
        <input className="ip-input" autoFocus
          placeholder="e.g. mentor, press, advice on fundraising…"
          value={otherText} onChange={(e) => setOtherText(e.target.value)} />
      )}

      {/* What stuck? : the talked-about detail that personalizes the draft. */}
      <label className="ip-lbl">What stuck?</label>
      {stt.supported && (
        <button type="button" className={`ip-talkbtn${stt.listening ? " on" : ""}`}
                onClick={stt.toggle}>
          <Mic size={18} /> {stt.listening ? "Listening… tap to stop" : "Tap to talk"}
        </button>
      )}
      <div className="ip-talkhint">
        {stt.supported && !typing && (
          <>or <button type="button" className="ip-linkbtn"
                       onClick={() => setTyping(true)}>type it</button> · </>)}
        <span className="ip-dim">“from Ottawa · just raised · loves climbing”</span>
      </div>
      {(typing || !stt.supported) && (
        <input className="ip-input ip-stuck-input"
          placeholder="from Ottawa · just raised · loves climbing"
          value={context} onChange={(e) => setContext(e.target.value)} />
      )}

      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      <button className="ip-btn primary lg block ip-connectbtn" onClick={() => connect(false)}
              disabled={!!busy || !canSend}
              title={canSend ? "" : "Connect LinkedIn to send"}>
        {busy === "send" ? <Loader2 className="spin" size={18} />
          : <><Send size={18} /> Connect on LinkedIn</>}
      </button>

      {/* Preview note + follow-up : drafts (or re-drafts) on open, editable. */}
      <button className="ip-btn ghost block ip-previewtoggle" onClick={togglePreview}>
        <ChevronRight size={15} className={showPreview ? "rot" : ""} /> Preview note + follow-up
      </button>
      {showPreview && (
        <div className="ip-preview">
          {drafting ? (
            <div className="ip-dim ip-microw-hint" style={{ padding: "10px 2px" }}>
              <Loader2 className="spin" size={14} /> Drafting…
            </div>
          ) : (
            <>
              <label className="ip-lbl">Connection note</label>
              <textarea className="ip-area" rows={3} maxLength={300}
                value={draftNote} onChange={(e) => setDraftNote(e.target.value)} />
              <label className="ip-lbl">First message
                <span className="ip-dim"> · sent after they accept</span></label>
              <textarea className="ip-area" rows={5}
                value={draftMsg} onChange={(e) => setDraftMsg(e.target.value)} />
              <button className="ip-linkbtn ip-microw-hint" onClick={() => genDraft().catch(() => {})}>
                <RefreshCw size={12} /> Regenerate
              </button>
            </>
          )}
        </div>
      )}

      <div className="ip-actions-row" style={{ marginTop: 12 }}>
        <button className="ip-btn ghost sm" onClick={save} disabled={!!busy}>
          {busy === "save" ? <Loader2 className="spin" size={15} />
            : <><Bookmark size={15} /> Save for later</>}
        </button>
        <button className="ip-btn ghost sm" onClick={() => connect(true)}
                disabled={!!busy || !canSend}
                title={canSend ? "Send a bare invite; the message goes out once accepted"
                              : "Connect LinkedIn to send"}>
          {busy === "nonote" ? <Loader2 className="spin" size={15} /> : "Connect, no note"}
        </button>
      </div>
      {!canSend && <div className="ip-dim ip-center">Connect LinkedIn to send now.</div>}
      <div className="ip-dim ip-center ip-laterhint">
        You can also edit any of this later from <b>People</b>.
      </div>
    </div>
  );
}

// ── activity : operator-only roll-up of ALL in-person captures ───────────────

function ActivityScreen() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    setErr("");
    api.inpersonActivity()
      .then(setData)
      .catch((e) => setErr(e?.message || "Could not load activity"));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (err) return <div className="ip-screen"><div className="ip-err"><AlertCircle size={14} /> {err}</div></div>;
  if (!data) return <Centered><Loader2 className="spin" size={24} /></Centered>;

  return (
    <div className="ip-screen">
      <div className="ip-listhead">
        <span>{data.capture_count} captures · {data.event_count} events (all)</span>
        <button className="ip-iconbtn" onClick={load}><RefreshCw size={15} /></button>
      </div>
      {data.events.length === 0 && (
        <div className="ip-dim ip-center" style={{ marginTop: 40 }}>
          No in-person activity yet.
        </div>
      )}
      {data.events.map((ev) => (
        <div key={ev.event_id} className="ip-actgroup">
          <div className="ip-actgroup-head">
            <span className="ip-actgroup-label">{ev.label || `event #${ev.event_id}`}</span>
            <span className="ip-actgroup-meta">
              {ev.owner?.is_guest ? "guest" : (ev.owner?.name || "—")} · {ev.count}
            </span>
          </div>
          {ev.captures.length === 0 && <div className="ip-dim" style={{ padding: "4px 2px 10px" }}>No captures.</div>}
          <div className="ip-list">
            {ev.captures.map((c) => {
              const st = statusMeta(c.status);
              const cc = connChip(c);
              return (
                <div key={c.prospect_id} className="ip-rowitem" style={{ cursor: "default" }}>
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
                    {c.last_outreach ? outreachStateLabel(c.last_outreach.state) : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
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

  const send = async (noNote = false) => {
    setErr(""); setBusy(noNote ? "nonote" : "send");
    try {
      const override = {};
      if (noNote) override.no_note = true;
      else if (draftNote.trim()) override.note = draftNote;
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
        {capture.note && <div className="ip-privnote">Talked about: “{capture.note}”</div>}
        {capture.next_step && (
          <div className="ip-privnote">Next step: “{capture.next_step}”</div>
        )}
        {capture.contact_type && (
          <div className="ip-row-chips" style={{ marginTop: 6 }}>
            <span className="ip-st st-contacted">{capture.contact_type.replace("_", " ")}</span>
          </div>
        )}
        {capture.private_note && (
          <div className="ip-privnote">Private: “{capture.private_note}”</div>
        )}
      </div>

      {isPending && (
        <>
          <label className="ip-lbl">Connection note <span className="ip-dim">· optional</span></label>
          <textarea className="ip-area" rows={3} maxLength={300}
            placeholder="Leave blank to use the agent draft"
            value={draftNote} onChange={(e) => setDraftNote(e.target.value)} />
          <label className="ip-lbl">First message <span className="ip-dim">· sent after they accept</span></label>
          <textarea className="ip-area" rows={4}
            placeholder="Leave blank to use the agent draft"
            value={draftMsg} onChange={(e) => setDraftMsg(e.target.value)} />
          {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}
          {sent
            ? <div className="ip-ok"><Check size={15} /> {outreachStateLabel(sent.state)}
                {sent.dry_run ? " (dry-run)" : ""} · {sent.path_taken}</div>
            : <>
                <button className="ip-btn primary block" onClick={() => send(false)}
                        disabled={!!busy || !canSend}>
                  {busy === "send" ? <Loader2 className="spin" size={16} />
                    : <><Send size={16} /> {actionLabel(capture.connection_status, false)}</>}
                </button>
                {capture.connection_status !== "connected" && (
                  <button className="ip-btn ghost block" onClick={() => send(true)}
                          disabled={!!busy || !canSend}
                          title="Send a bare invite; the message goes out once accepted">
                    {busy === "nonote" ? <Loader2 className="spin" size={16} />
                      : <><Send size={16} /> Connect without note</>}
                  </button>
                )}
              </>}
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

// On-device dictation via the Web Speech API (SpeechRecognition). No API key,
// no audio upload : the browser transcribes locally and we just append the
// final text. Unsupported browsers (notably desktop Firefox) get `supported:
// false` so the caller can hide the mic entirely. iOS Safari + Chrome (the
// phone-first targets) support it.
function useSpeechToText(onText) {
  const SR = typeof window !== "undefined"
    && (window.SpeechRecognition || window.webkitSpeechRecognition);
  const recRef = useRef(null);
  const [listening, setListening] = useState(false);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  useEffect(() => () => { try { recRef.current?.stop(); } catch {} }, []);

  const toggle = useCallback(() => {
    if (!SR) return;
    if (listening) { try { recRef.current?.stop(); } catch {} return; }
    const rec = new SR();
    rec.lang = navigator.language || "en-US";
    rec.interimResults = false;     // only commit finalized phrases
    rec.continuous = false;          // one utterance per tap : phone-friendly
    rec.onresult = (e) => {
      const text = Array.from(e.results)
        .map((r) => r[0]?.transcript || "").join(" ").trim();
      if (text) onTextRef.current(text);
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    recRef.current = rec;
    try { rec.start(); setListening(true); } catch { setListening(false); }
  }, [SR, listening]);

  return { supported: !!SR, listening, toggle };
}

// A mic toggle that appends dictated text to a string field. `value`/`onChange`
// mirror an input so the caller stays the source of truth. Renders nothing when
// the browser can't transcribe, so existing typing UX is untouched.
function MicButton({ value, onChange, title = "Dictate" }) {
  const append = (text) => {
    const cur = (value || "").trim();
    onChange(cur ? `${cur} ${text}` : text);
  };
  const { supported, listening, toggle } = useSpeechToText(append);
  if (!supported) return null;
  return (
    <button type="button"
            className={`ip-mic${listening ? " on" : ""}`}
            onClick={toggle}
            title={listening ? "Stop dictation" : title}
            aria-label={listening ? "Stop dictation" : title}>
      {listening ? <MicOff size={16} /> : <Mic size={16} />}
    </button>
  );
}

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
  align-items:center; gap:10px; max-width:300px; }
.ip-empty p { margin:0; font-size:15px; line-height:1.45; }
.ip-empty-title { font-size:19px; font-weight:800; color:var(--ip-ink); }

.ip-banner { background:#fff6e5; color:#7a5200; font-size:13px; padding:10px 14px;
  display:flex; gap:7px; align-items:center; line-height:1.35; }

.ip-howto { background:#eef4fb; color:#1c4f8a; font-size:13.5px; line-height:1.4;
  padding:11px 14px; border-radius:12px; margin-bottom:12px; text-align:center; }

/* event bar */
.ip-eventbar { position:sticky; top:0; z-index:5; background:var(--ip-card);
  border-bottom:1px solid var(--ip-line); }
.ip-eventhead { display:flex; align-items:center; gap:8px; padding-right:12px; }
.ip-eventpick { flex:1; min-width:0; display:flex; align-items:center; justify-content:space-between;
  padding:14px 16px; background:none; border:0; font-size:15.5px; font-weight:600;
  color:var(--ip-ink); }
.ip-eventpick.empty { color:var(--ip-accent); }
.ip-eventlabel { display:inline-flex; align-items:center; gap:6px; min-width:0;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ip-eventpick .rot { transform:rotate(90deg); }
.ip-signout { flex-shrink:0; display:flex; align-items:center; gap:6px;
  background:none; border:1px solid var(--ip-line); border-radius:8px;
  padding:6px 10px; color:var(--ip-dim); font-size:13px; cursor:pointer; }
.ip-signout:active { background:var(--ip-line); }
.ip-eventmenu { padding:0 12px 12px; }
.ip-eventrow { display:flex; gap:8px; }
.ip-recents { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.ip-chip { border:1px solid var(--ip-line); background:#fff; border-radius:999px;
  padding:5px 11px; font-size:12px; color:var(--ip-ink); cursor:pointer; }
.ip-chip.on { background:var(--ip-accent); color:var(--ip-accent-ink);
  border-color:var(--ip-accent); }
.ip-chip { padding:7px 14px; font-size:13.5px; }
.ip-chiprow { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:4px; }

/* connect screen : avatar header, talk button, intent + preview */
.ip-person-row { display:flex; gap:12px; align-items:center; }
.ip-person-id { min-width:0; }
.ip-avatar { flex-shrink:0; width:46px; height:46px; border-radius:50%;
  background:#e7f0fb; color:var(--ip-accent); font-weight:800; font-size:15px;
  display:flex; align-items:center; justify-content:center; letter-spacing:.02em; }
.ip-talkbtn { width:100%; display:flex; gap:9px; align-items:center; justify-content:center;
  padding:15px; border:1px solid var(--ip-line); border-radius:12px; background:#f1f5fb;
  color:var(--ip-ink); font-size:15px; font-weight:600; cursor:pointer; min-height:54px; }
.ip-talkbtn:active { transform:scale(.99); }
.ip-talkbtn.on { background:#fdecec; border-color:#f1b4b4; color:#a01818;
  animation:ipmicpulse 1.1s ease-in-out infinite; }
.ip-talkhint { text-align:center; font-size:12.5px; color:var(--ip-dim); margin-top:7px; }
.ip-stuck-input { margin-top:8px; }
.ip-linkbtn { background:none; border:0; padding:0; color:var(--ip-accent);
  font:inherit; cursor:pointer; text-decoration:underline; display:inline-flex;
  gap:4px; align-items:center; }
.ip-connectbtn { margin-top:16px; }
.ip-previewtoggle { justify-content:center; margin-top:10px; font-weight:600; color:var(--ip-ink); }
.ip-preview { margin-top:2px; }

/* screens */
.ip-screen { flex:1; padding:14px 14px 88px; overflow-y:auto; }
.ip-pad { display:flex; flex-direction:column; gap:10px; margin-top:12px; }
.ip-seg { display:flex; gap:4px; background:#eceef1; border-radius:13px; padding:5px; }
.ip-seg button { flex:1; border:0; background:none; padding:12px 6px; border-radius:10px;
  font-size:13.5px; font-weight:600; color:var(--ip-dim); display:flex; gap:6px;
  align-items:center; justify-content:center; transition:background .12s, color .12s; }
.ip-seg button.on { background:#fff; color:var(--ip-accent); box-shadow:0 1px 4px rgba(0,0,0,.1); }

.ip-input { width:100%; padding:14px; border:1px solid var(--ip-line);
  border-radius:12px; font-size:16px; background:#fff; color:var(--ip-ink); min-height:52px; }
.ip-input:focus, .ip-area:focus { outline:none; border-color:var(--ip-accent);
  box-shadow:0 0 0 3px rgba(10,102,194,.12); }
.ip-microw { display:flex; gap:8px; align-items:stretch; }
.ip-microw-hint { display:inline-flex; align-items:center; gap:5px; margin-top:6px;
  font-size:12px; background:none; border:0; padding:0; cursor:inherit; }
button.ip-microw-hint { cursor:pointer; }
.ip-microw .ip-input { flex:1; min-width:0; }
.ip-mic { flex-shrink:0; width:46px; display:flex; align-items:center; justify-content:center;
  border:1px solid var(--ip-line); border-radius:11px; background:#fff; color:var(--ip-dim);
  cursor:pointer; }
.ip-mic.on { background:#fdecec; border-color:#f1b4b4; color:#a01818;
  animation:ipmicpulse 1.1s ease-in-out infinite; }
@keyframes ipmicpulse { 0%,100% { opacity:1; } 50% { opacity:.55; } }
.ip-area { width:100%; padding:11px 13px; border:1px solid var(--ip-line);
  border-radius:11px; font-size:15px; line-height:1.4; resize:vertical; background:#fff;
  color:var(--ip-ink); font-family:inherit; }
.ip-lbl { font-size:12px; font-weight:700; color:var(--ip-dim); text-transform:uppercase;
  letter-spacing:.03em; margin:12px 0 5px; display:block; }
.ip-lbl .ip-dim { font-weight:500; text-transform:none; letter-spacing:0; }

/* buttons */
.ip-btn { display:inline-flex; gap:8px; align-items:center; justify-content:center;
  border:1px solid var(--ip-line); background:#fff; color:var(--ip-ink); border-radius:13px;
  padding:14px 16px; font-size:15px; font-weight:650; cursor:pointer; min-height:50px;
  transition:transform .08s ease, filter .12s ease; }
.ip-btn:active { transform:scale(.98); }
.ip-btn:disabled { opacity:.45; }
.ip-btn.primary { background:var(--ip-accent); color:var(--ip-accent-ink); border-color:var(--ip-accent);
  box-shadow:0 2px 8px rgba(10,102,194,.25); }
.ip-btn.ghost { background:#fff; }
.ip-btn.sm { padding:11px 13px; font-size:13.5px; min-height:44px; }
.ip-btn.lg { padding:16px; font-size:16.5px; min-height:56px; border-radius:14px; }
.ip-btn.block { width:100%; }
.ip-actions { display:flex; flex-direction:column; gap:9px; margin-top:16px; }
.ip-actions > .ip-btn { width:100%; }
.ip-actions-row { display:flex; gap:9px; }
.ip-actions-row .ip-btn { flex:1; }
.ip-disclosure { display:flex; align-items:center; gap:6px; margin-top:14px;
  background:none; border:0; color:var(--ip-accent); font-size:13px; font-weight:600;
  padding:4px 0; cursor:pointer; }
.ip-disclosure .rot { transform:rotate(90deg); }
.ip-more { margin-top:4px; }
.ip-laterhint { margin-top:10px; font-size:12px; }

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
  border-radius:13px; padding:14px; min-height:60px; }
.ip-cand:active { background:#f4f8fd; }
.ip-cand-name { font-weight:700; font-size:16px; }
.ip-cand-sub { font-size:13px; color:var(--ip-dim); margin-top:2px; }
.ip-cand-url { font-size:11px; color:var(--ip-accent); margin-top:3px; }

/* result / detail */
.ip-back { background:none; border:0; color:var(--ip-accent); font-size:14px;
  display:inline-flex; gap:5px; align-items:center; padding:2px 0 10px; }
.ip-person { background:#fff; border:1px solid var(--ip-line); border-radius:16px;
  padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
.ip-person-name { font-size:21px; font-weight:800; }
.ip-person-sub { font-size:13.5px; color:var(--ip-dim); margin-top:3px; }
.ip-warn,.ip-err,.ip-ok { font-size:13px; display:flex; gap:6px; align-items:center;
  margin-top:8px; padding:8px 11px; border-radius:10px; }
.ip-warn { background:#fff6e5; color:#7a5200; }
.ip-warn { text-align:left; }
.ip-linkbtn { background:none; border:0; color:var(--ip-accent); font-weight:700;
  text-decoration:underline; cursor:pointer; padding:0; font-size:13px; }
.ip-err { background:#fdecec; color:#a01818; }
.ip-ok { background:#e8f6ec; color:#1d7a37; }
.ip-thread { background:#fff; border:1px solid var(--ip-line); border-radius:14px;
  padding:12px 14px; margin-top:12px; }
.ip-threadrow { font-size:14px; display:flex; gap:6px; align-items:center; }
.ip-privnote { font-style:italic; color:var(--ip-dim); font-size:13px; margin-top:8px; }

/* list */
.ip-listhead { display:flex; justify-content:space-between; align-items:center;
  font-size:13px; color:var(--ip-dim); padding:2px 2px 10px; }
.ip-actgroup { margin-bottom:16px; }
.ip-actgroup-head { display:flex; justify-content:space-between; align-items:baseline;
  padding:4px 2px 6px; border-bottom:1px solid var(--ip-line); margin-bottom:8px; }
.ip-actgroup-label { font-weight:700; font-size:14px; }
.ip-actgroup-meta { font-size:11px; color:var(--ip-dim); text-transform:uppercase;
  letter-spacing:.02em; }
.ip-iconbtn,.ip-rowitem { background:#fff; border:1px solid var(--ip-line); }
.ip-iconbtn { border-radius:10px; padding:7px; color:var(--ip-dim); }
.ip-list { display:flex; flex-direction:column; gap:8px; }
.ip-rowitem { display:flex; align-items:center; gap:10px; border-radius:14px;
  padding:14px; text-align:left; min-height:60px; }
.ip-rowitem:active { background:#f4f8fd; }
.ip-row-main { flex:1; min-width:0; }
.ip-row-name { font-weight:700; font-size:16px; }
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
  border-top:1px solid var(--ip-line); padding-bottom:env(safe-area-inset-bottom);
  box-shadow:0 -2px 10px rgba(0,0,0,.04); }
.ip-tabs button { flex:1; border:0; background:none; padding:13px 8px; display:flex;
  flex-direction:column; align-items:center; gap:4px; font-size:11.5px; font-weight:600;
  color:var(--ip-dim); }
.ip-tabs button.on { color:var(--ip-accent); }
`;
