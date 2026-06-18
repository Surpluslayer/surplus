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
  LogOut, Mic, MicOff, MapPin, Star, HelpCircle, Sparkles, ArrowRight, X,
} from "lucide-react";
import { api } from "./lib/api.js";
import ContactsButton from "./components/ContactsButton.jsx";
import ContactsPage from "./components/ContactsPage.jsx";
import { ensureNotifyPermission, notifyDevice } from "./lib/notify.js";
import { actionLabel, statusMeta, outreachStateLabel } from "./lib/labels.js";

const ACTIVE_EVENT_KEY = "surplus_inperson_event";   // sessionStorage
const RECENT_LABELS_KEY = "surplus_inperson_recent";  // localStorage

export function loadActiveEvent() {
  try { return JSON.parse(sessionStorage.getItem(ACTIVE_EVENT_KEY) || "null"); }
  catch { return null; }
}
export function saveActiveEvent(ev) {
  try { sessionStorage.setItem(ACTIVE_EVENT_KEY, JSON.stringify(ev)); } catch {}
}
function clearActiveEvent() {
  try { sessionStorage.removeItem(ACTIVE_EVENT_KEY); } catch {}
}
export function loadRecentLabels() {
  try { return JSON.parse(localStorage.getItem(RECENT_LABELS_KEY) || "[]"); }
  catch { return []; }
}
export function pushRecentLabel(label) {
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
  // Top-level surface toggle, same idea as the desktop shell : "flow" = the
  // capture surface, "crm" = the durable cross-event relationship spine.
  const [view, setView] = useState("flow");        // "flow" | "crm"
  const [result, setResult] = useState(null);      // scan result -> result screen
  const [openCapture, setOpenCapture] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [isOperator, setIsOperator] = useState(false);  // can see the Activity page

  // ─── First-time-user onboarding tour ──────────────────────────────────
  // The server is the source of truth (user.onboarding_status/step, armed the
  // instant LinkedIn is first connected). We mirror it into local state so the
  // coachmarks react instantly, and persist every advance back to the API so
  // the tour resumes in place after a refresh or device switch.
  const [onb, setOnb] = useState({ status: "", step: 0 });
  useEffect(() => {
    if (user && typeof user === "object") {
      setOnb({ status: user.onboarding_status || "",
               step: user.onboarding_step || 0 });
    }
  }, [user]);
  const persistOnb = (patch) => { api.setOnboarding(patch).catch(() => {}); };
  const onbAdvanceTo = (step) => {
    setOnb((o) => ({ ...o, step }));
    persistOnb({ step });
  };
  const onbFinish = () => {
    setOnb({ status: "done", step: ONB_STEPS.length - 1 });
    persistOnb({ status: "done" });
  };
  const onbSkip = () => {
    setOnb((o) => ({ ...o, status: "skipped" }));
    persistOnb({ status: "skipped" });
  };
  const onbRestart = () => {
    setOnb({ status: "active", step: 0 });
    persistOnb({ status: "active", step: 0 });
    setView("flow"); setTab("capture"); setResult(null); setOpenCapture(null);
  };

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

      <EventBar event={event} onPick={pickEvent} user={user} onSignOut={signOut}
                crmActive={view === "crm"}
                onToggleCrm={() => setView((v) => (v === "crm" ? "flow" : "crm"))}
                onReplayTour={onbRestart} />

      {notConnected && view !== "crm" && (
        <div className="ip-banner">
          <AlertCircle size={14} /> You can capture people now. Connect LinkedIn
          when you’re ready to send.
        </div>
      )}

      {view === "crm" ? (
        <div style={{ padding: "16px 14px 90px", overflowY: "auto", flex: 1 }}>
          <ContactsPage />
        </div>
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
          canSend={!notConnected}
          savedLink={(user && user.saved_send_link) || ""}
          onbStepKey={onb.status === "active" ? (ONB_STEPS[onb.step]?.key || null) : null}
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
          <button className={view === "flow" && tab === "capture" ? "on" : ""}
                  onClick={() => { setView("flow"); setTab("capture"); }}>
            <Camera size={20} /><span>Capture</span>
          </button>
          <button className={view === "crm" ? "on" : ""}
                  onClick={() => setView("crm")}>
            <Users size={20} /><span>Relationship</span>
          </button>
          {isOperator && (
            <button className={view === "flow" && tab === "activity" ? "on" : ""}
                    onClick={() => { setView("flow"); setTab("activity"); }}>
              <Activity size={20} /><span>Activity</span>
            </button>
          )}
        </nav>
      )}

      {onb.status === "active" && (
        <OnboardingCoach
          step={onb.step}
          context={{
            hasEvent: !!event,
            screen:
              view === "crm" ? "crm"
              : (tab === "activity" && isOperator) ? "activity"
              : !event ? "empty"
              : result ? "result"
              : openCapture ? "detail"
              : tab === "capture" ? "capture"
              : "people",
            openHub: () => { setView("crm"); },
          }}
          onAdvance={onbAdvanceTo}
          onSkip={onbSkip}
          onComplete={onbFinish}
        />
      )}
    </div>
  );
}

// ── sign-in bounce ───────────────────────────────────────────────────────────

export function SignInBounce({ authError = null, onRetry = null }) {
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

function EventBar({ event, onPick, user, onSignOut, crmActive, onToggleCrm, onReplayTour }) {
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
        <button data-onb="add-event"
                className={`ip-eventpick${event ? "" : " empty"}`} onClick={() => setOpen((o) => !o)}>
          <span className="ip-eventlabel">
            {event ? <><MapPin size={15} /> {event.label}</> : "Pick event to start →"}
          </span>
          <ChevronRight size={16} className={open ? "rot" : ""} />
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {onReplayTour && (
            <button className="ip-signout" title="Replay the getting-started tour"
                    aria-label="Replay the getting-started tour"
                    onClick={onReplayTour}>
              <HelpCircle size={15} />
            </button>
          )}
          {user && <span data-onb="hub" style={{ display: "inline-flex" }}>
                     <ContactsButton variant="inperson"
                                     active={crmActive}
                                     onClick={onToggleCrm} />
                   </span>}
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

export function CaptureScreen({ event, onResult }) {
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
      <div className="ip-seg" data-onb="add-contact">
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

export function ScanResult({ event, result, onDone, onCancel, canSend, savedLink = "", onbStepKey = null, isDemo = false }) {
  const p = result.prospect || {};
  const [draftNote, setDraftNote] = useState(result.draft_note || "");
  const [draftMsg, setDraftMsg] = useState(result.draft_message || "");
  const [note, setNote] = useState(p.note || "");               // fun fact
  const [privateNote, setPrivateNote] = useState(p.private_note || "");
  const [contactType, setContactType] = useState(p.contact_type || "");
  // "Captured once, reused forever" : a fresh capture pre-fills the next step
  // with the user's saved demo / Calendly link so it rides along on every send.
  const [nextStep, setNextStep] = useState(p.next_step || savedLink || "");
  const [vip, setVip] = useState(!!p.vip);                       // icon-only star
  const [busy, setBusy] = useState("");      // "" | "send" | "save" | "personalize" | "nonote"
  const [err, setErr] = useState("");
  const [signinPrompt, setSigninPrompt] = useState(false);  // demo: gate Send -> sign in
  const [demoSent, setDemoSent] = useState(false);          // demo: simulated connect success
  // The draft on screen was composed BEFORE the fun fact was typed. Track
  // whether the saved fun fact still matches what produced the current draft.
  const [draftFromNote, setDraftFromNote] = useState(p.note || "");
  const [draftFromStep, setDraftFromStep] = useState(p.next_step || "");
  // Once the operator hand-edits the draft, stop auto-recomposing so we never
  // clobber their wording. They can still personalize manually.
  const [draftEdited, setDraftEdited] = useState(false);
  // Capture stays minimal for mobile : the optional extras (note, private memo,
  // first message, contact type, next step) all hide behind a disclosure.
  const [showMore, setShowMore] = useState(false);
  // The classify / attach-link onboarding coachmarks anchor to controls that
  // live behind the "Add more" disclosure, so auto-expand it while the tour is
  // pointing at them (otherwise the popover would have nothing to anchor to).
  useEffect(() => {
    if (onbStepKey === "classify" || onbStepKey === "link") setShowMore(true);
  }, [onbStepKey]);
  // Re-compose when EITHER the fun fact or the next step moved the draft out of
  // sync : both feed the composed copy.
  const stale = (note || "").trim() !== (draftFromNote || "").trim()
             || (nextStep || "").trim() !== (draftFromStep || "").trim();

  // Persist the fun fact + private note onto the capture and RE-COMPOSE the
  // draft from the just-saved fun fact. This is the fix for "the message won't
  // personalize" : the draft is only as personal as the note it was built from,
  // so saving the note has to refresh the draft.
  const repersonalize = useCallback(async () => {
    setErr(""); setBusy("personalize");
    try {
      const r = await api.inpersonScan({
        event_id: event.event_id, linkedin_url: p.linkedin_url,
        source: p.source || "scan", note, private_note: privateNote,
        contact_type: contactType || undefined, next_step: nextStep || undefined,
        vip,
      });
      setDraftNote(r.draft_note || "");
      setDraftMsg(r.draft_message || "");
      setDraftFromNote(note || "");
      setDraftFromStep(nextStep || "");
      setDraftEdited(false);
    } catch (e) { setErr(e.message || "Couldn’t personalize"); }
    finally { setBusy(""); }
  }, [event.event_id, p.linkedin_url, p.source, note, privateNote,
      contactType, nextStep, vip]);

  // Quick + frictionless : auto-personalize ~0.7s after the fun fact stops
  // changing, so the operator never has to tap a button. We only do this while
  // the draft is untouched (draftEdited=false) so hand-edits are never lost, and
  // only when the note actually moved the draft out of sync (stale).
  useEffect(() => {
    if (draftEdited || busy || !stale) return;
    const t = setTimeout(() => { repersonalize(); }, 700);
    return () => clearTimeout(t);
  }, [note, stale, draftEdited, busy, repersonalize]);

  // Lightweight persist (fun fact + private note) without forcing a recompose,
  // for Save/Send when the operator already edited the draft by hand.
  const persistNotes = async () => {
    if ((note || "") === (p.note || "") &&
        (privateNote || "") === (p.private_note || "") &&
        (contactType || "") === (p.contact_type || "") &&
        (nextStep || "") === (p.next_step || "") &&
        !!vip === !!p.vip) return;
    await api.inpersonScan({
      event_id: event.event_id, linkedin_url: p.linkedin_url,
      source: p.source || "scan", note, private_note: privateNote,
      contact_type: contactType || undefined, next_step: nextStep || undefined,
      vip,
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
    try { await persistNotes(); stashDraft(); onDone(); }
    catch (e) { setErr(e.message || "Save failed"); setBusy(""); }
  };
  // noNote=true -> send a BARE invite (no connection note). The personalized
  // DM still fires automatically once they accept.
  const send = async (noNote = false) => {
    setErr(""); setBusy(noNote ? "nonote" : "send");
    try {
      await persistNotes();
      const res = await api.inpersonSend(p.prospect_id,
        noNote ? { no_note: true, message: draftMsg }
               : { note: draftNote, message: draftMsg });
      if (!res.dry_run && res.state) {
        notifyDevice(`${p.name}: ${outreachStateLabel(res.state)}`);
      }
      onDone();
    } catch (e) {
      const code = e?.body?.detail?.code || e?.body?.code;
      if (e?.status === 402 || code === "linkedin_send_locked" || code === "payment_required") {
        // Sending is gated for demo / not-signed-in visitors : take them to
        // sign in for real (LinkedIn hosted auth) instead of a red error.
        window.location.href = "/api/auth/linkedin/start-redirect";
        return;
      }
      setErr(e.message || "Send failed"); setBusy("");
    }
  };

  // Demo: the captured person isn't a real LinkedIn recipient, so simulate a
  // genuine connection-request send for filming (Connecting… -> Sent ✓) without
  // hitting the network or bouncing to sign-in.
  const sendDemo = () => {
    if (busy) return;
    setErr(""); setBusy("send");
    setTimeout(() => { setBusy(""); setDemoSent(true); }, 1000);
  };

  return (
    <div className="ip-screen ip-result">
      <button className="ip-back" onClick={onCancel}><ArrowLeft size={18} /> Back</button>

      <div className="ip-person">
        <div className="ip-person-head">
          <div className="ip-person-name">{p.name || "Unknown"}</div>
          <button type="button" data-onb="vip"
                  className={`ip-star${vip ? " on" : ""}`}
                  aria-pressed={vip}
                  aria-label={vip ? "Unmark VIP" : "Mark as VIP"}
                  title={vip ? "Unmark VIP" : "Mark as VIP"}
                  onClick={() => setVip((v) => !v)}>
            <Star size={22} fill={vip ? "currentColor" : "none"} />
          </button>
        </div>
        <div className="ip-person-sub">
          {[p.role, p.company].filter(Boolean).join(" · ") || p.linkedin_url}
        </div>
        {result.resolve_failed && (
          <div className="ip-warn"><AlertCircle size={13} /> Couldn’t resolve on
            LinkedIn — saved anyway, retry from Relationship.</div>
        )}
      </div>

      {/* Fun fact FIRST : it drives the draft, so it's the thing to fill in.
          The draft auto-updates as you type/dictate : no button to tap. */}
      <label className="ip-lbl">What you talked about
        <span className="ip-dim"> · personalizes the message</span></label>
      <div className="ip-microw" data-onb="notes">
        <input className="ip-input" placeholder="e.g. from Ottawa · loves bagels · rock climbing"
          value={note} onChange={(e) => setNote(e.target.value)} />
        <MicButton value={note} onChange={setNote} title="Dictate the fun fact" />
      </div>
      {busy === "personalize"
        ? <div className="ip-dim ip-microw-hint"><Loader2 className="spin" size={13} /> Personalizing…</div>
        : draftEdited
          ? <button className="ip-linkbtn ip-microw-hint" onClick={repersonalize}
                    disabled={!(note || "").trim()}>
              <RefreshCw size={12} /> Re-personalize from the fun fact
            </button>
          : (draftFromNote || "").trim()
            ? <div className="ip-dim ip-microw-hint"><Check size={13} /> Draft personalized</div>
            : null}

      {/* Everything below is OPTIONAL at capture : the note auto-personalizes
          and the first message can be drafted later from People. Keep the
          capture moment to person + fun fact + Connect. */}
      <button className="ip-disclosure" onClick={() => setShowMore((s) => !s)}>
        <ChevronRight size={15} className={showMore ? "rot" : ""} />
        {showMore ? "Hide extras" : "Add more · message, type, next step"}
      </button>
      {showMore && (
        <div className="ip-more">
          {/* Who is this to you : tags the capture for later triage. */}
          <label className="ip-lbl">This person is…</label>
          <div className="ip-chiprow" data-onb="classify">
            {[["sales", "Sales"], ["hiring", "Hiring"], ["investor", "Investor"],
              ["partner", "Partner"], ["follow_up", "Follow-up"], ["other", "Other"]]
              .map(([v, lbl]) => (
              <button key={v} type="button"
                      className={`ip-chip${contactType === v ? " on" : ""}`}
                      onClick={() => setContactType(contactType === v ? "" : v)}>
                {lbl}
              </button>
            ))}
          </div>

          {/* Next step : woven into the first message. Quick presets for the
              common call/coffee ask; the input takes a Calendly link or text. */}
          <label className="ip-lbl">Next step
            <span className="ip-dim"> · added to the first message</span></label>
          <div className="ip-chiprow">
            {["grab a coffee", "hop on a quick call", "follow up next week"].map((preset) => (
              <button key={preset} type="button"
                      className={`ip-chip${nextStep === preset ? " on" : ""}`}
                      onClick={() => { setNextStep(preset); }}>
                {preset}
              </button>
            ))}
          </div>
          <div className="ip-microw" data-onb="link">
            <input className="ip-input" placeholder="…or paste a Calendly / demo link"
              value={nextStep} onChange={(e) => setNextStep(e.target.value)} />
            <MicButton value={nextStep} onChange={setNextStep} title="Dictate the next step" />
          </div>
          {savedLink && (nextStep || "").trim() === savedLink.trim() && (
            <div className="ip-dim ip-microw-hint">
              <Check size={13} /> Using your saved link · reused on every send
            </div>
          )}

          <label className="ip-lbl">Connection note
            <span className="ip-dim"> · optional, ≤300</span></label>
          <textarea className="ip-area" rows={3} maxLength={300}
            value={draftNote}
            onChange={(e) => { setDraftNote(e.target.value); setDraftEdited(true); }} />

          <label className="ip-lbl">First message
            <span className="ip-dim"> · sent after they accept</span></label>
          <textarea className="ip-area" rows={5}
            value={draftMsg}
            onChange={(e) => { setDraftMsg(e.target.value); setDraftEdited(true); }} />

          <label className="ip-lbl">Private note <span className="ip-dim">· just for you, never sent</span></label>
          <div className="ip-microw">
            <input className="ip-input" placeholder="reminder to self…"
              value={privateNote} onChange={(e) => setPrivateNote(e.target.value)} />
            <MicButton value={privateNote} onChange={setPrivateNote} title="Dictate a private note" />
          </div>
        </div>
      )}

      {err && <div className="ip-err"><AlertCircle size={14} /> {err}</div>}

      {/* Connect is the one obvious action. "Save for later" and the bare-invite
          variant are secondary. The first message is drafted later in People. */}
      {demoSent ? (
        <div className="ip-actions">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center",
                        gap: 8, padding: "12px 14px", marginBottom: 10, borderRadius: 12,
                        background: "rgba(34,197,94,.10)", color: "#15803d",
                        font: "600 14px Inter, system-ui, sans-serif" }}>
            <Check size={18} /> Connection request sent to {p.name || "them"}
          </div>
          <button className="ip-btn primary lg" onClick={onDone}>Done</button>
        </div>
      ) : (
        <div className="ip-actions">
        <button data-onb="send" className="ip-btn primary lg"
                onClick={() => (canSend ? send(false) : (isDemo ? sendDemo() : setSigninPrompt(true)))}
                disabled={!!busy}
                title={canSend || isDemo ? "" : "Sign in to send"}>
          {busy === "send" ? <Loader2 className="spin" size={18} />
            : <><Send size={18} /> Connect on LinkedIn</>}
        </button>
        <div className="ip-actions-row">
          <button className="ip-btn ghost sm" onClick={save} disabled={!!busy}>
            {busy === "save" ? <Loader2 className="spin" size={15} />
              : <><Bookmark size={15} /> Save for later</>}
          </button>
          <button className="ip-btn ghost sm"
                  onClick={() => (canSend ? send(true) : (isDemo ? sendDemo() : setSigninPrompt(true)))}
                  disabled={!!busy}
                  title={canSend ? "Send a bare invite; the message goes out once accepted"
                                : "Sign in to send"}>
            {busy === "nonote" ? <Loader2 className="spin" size={15} />
              : "Connect, no note"}
          </button>
        </div>
        </div>
      )}
      {!canSend && !isDemo && <div className="ip-dim ip-center">Connect LinkedIn to send now.</div>}
      <div className="ip-dim ip-center ip-laterhint">
        You can also edit any of this later from <b>Relationship</b>.
      </div>
      {signinPrompt && (
        <div onClick={() => setSigninPrompt(false)}
          style={{ position: "fixed", inset: 0, background: "rgba(10,12,16,.5)",
            display: "flex", alignItems: "center", justifyContent: "center",
            zIndex: 1000, padding: 22 }}>
          <div onClick={(e) => e.stopPropagation()}
            style={{ background: "#fff", borderRadius: 18, padding: "26px 22px",
              maxWidth: 360, width: "100%", textAlign: "center",
              boxShadow: "0 16px 48px rgba(0,0,0,.28)" }}>
            <p style={{ font: "700 18px Inter, system-ui, sans-serif", margin: "0 0 6px", color: "#0a0c10" }}>
              Connect LinkedIn to send
            </p>
            <p style={{ color: "#5b6472", font: "400 14px/1.45 Inter, system-ui, sans-serif", margin: "0 0 18px" }}>
              Your draft for {p.name || "this contact"} is saved. Connect your LinkedIn
              account to send it for real.
            </p>
            <button onClick={() => { window.location.href = "/api/auth/linkedin/start-redirect"; }}
              style={{ display: "inline-flex", alignItems: "center", justifyContent: "center",
                gap: 8, width: "100%", border: 0, borderRadius: 999, padding: "12px 18px",
                background: "#0a66c2", color: "#fff", font: "600 15px Inter, system-ui, sans-serif",
                cursor: "pointer" }}>
              <img src="/linkedin-icon.png" width={18} height={18} alt="" /> Sign in with LinkedIn
            </button>
            <button onClick={() => setSigninPrompt(false)}
              style={{ marginTop: 12, border: 0, background: "none", color: "#8a93a0",
                font: "500 13px Inter, system-ui, sans-serif", cursor: "pointer" }}>
              Not now
            </button>
          </div>
        </div>
      )}
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
      <button className="ip-back" onClick={onBack}><ArrowLeft size={18} /> Relationship</button>

      <div className="ip-person">
        <div className="ip-person-head">
          <div className="ip-person-name">{capture.name}</div>
          {capture.vip && (
            <span className="ip-star on" title="VIP" aria-label="VIP">
              <Star size={20} fill="currentColor" />
            </span>
          )}
        </div>
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

// ── first-time-user onboarding tour ──────────────────────────────────────────
//
// Seven lightweight coachmarks that ride alongside the real capture flow : each
// one anchors to a live control (by [data-onb] selector) and points the user at
// the next thing to do — add an event, add a contact, star a VIP, capture the
// conversation, classify, attach a link, send, then hand off to the agent. The
// popovers are ambient (pointer-events stay on the underlying UI) and dismiss
// naturally; progress + which-step persist server-side so they appear once, in
// order, and survive a refresh.

const ONB_STEPS = [
  {
    key: "event",
    title: "Add your first event",
    body: () => "Tap here to create an event — name it and set the date. Everyone you meet gets captured under it.",
    anchor: () => '[data-onb="add-event"]',
    place: "bottom",
    // Self-advance the moment an event is set : the user just did the thing.
    auto: (ctx) => ctx.hasEvent,
  },
  {
    key: "contact",
    title: (ctx) => (ctx.screen === "result" ? "Star a VIP" : "Add someone you met"),
    body: (ctx) =>
      ctx.screen === "result"
        ? "Tap the star to flag a VIP. Then keep going — there’s more to capture just below."
        : "Scan their LinkedIn QR, paste their profile link, or type their name to add them.",
    anchor: (ctx) => (ctx.screen === "result" ? '[data-onb="vip"]' : '[data-onb="add-contact"]'),
    place: "bottom",
  },
  {
    key: "notes",
    title: "Capture the conversation",
    body: () => "Jot what you talked about — we use it to personalize the follow-up automatically.",
    anchor: () => '[data-onb="notes"]',
    place: "bottom",
  },
  {
    key: "classify",
    title: "Classify the relationship",
    body: () => "Tag how they fit — sales, hiring, investor, partner. It sorts your follow-ups later.",
    anchor: () => '[data-onb="classify"]',
    place: "top",
  },
  {
    key: "link",
    title: "Attach a link",
    body: () => "Drop your demo or Calendly link. We save it to your profile and pre-fill it on every future send.",
    anchor: () => '[data-onb="link"]',
    place: "top",
  },
  {
    key: "send",
    title: "Send the follow-up",
    body: () => "Hit Connect — we generate the message from your notes, the relationship, and your saved link.",
    anchor: () => '[data-onb="send"]',
    place: "top",
    // Advance once the send navigates away from the result screen.
    auto: (ctx, prev) => prev && prev.screen === "result" && ctx.screen !== "result",
  },
  {
    key: "hub",
    title: "Hand off to your agent",
    body: () => "That’s it. Open your relationships hub and let the agent do your follow-ups and find updates.",
    anchor: () => '[data-onb="hub"]',
    place: "bottom",
    final: true,
    cta: "Open the hub",
  },
];

const ONB_CARD_W = 300;

function onbRingStyle(rect) {
  const pad = 6;
  return {
    position: "fixed",
    top: rect.top - pad,
    left: rect.left - pad,
    width: rect.width + pad * 2,
    height: rect.height + pad * 2,
  };
}

function onbCardStyle(rect, place) {
  const vw = typeof window !== "undefined" ? window.innerWidth : 380;
  const vh = typeof window !== "undefined" ? window.innerHeight : 720;
  const w = Math.min(ONB_CARD_W, vw - 24);
  const base = { position: "fixed", width: w, zIndex: 60 };
  if (!rect) {
    // No live anchor (the target screen isn't open yet) : fall back to an
    // ambient toast pinned above the bottom tab bar.
    return { ...base, left: "50%", bottom: 90, transform: "translateX(-50%)" };
  }
  let left = rect.left + rect.width / 2 - w / 2;
  left = Math.max(10, Math.min(left, vw - w - 10));
  const spaceBelow = vh - rect.bottom;
  const spaceAbove = rect.top;
  const NEED = 190;
  let above;
  if (place === "top") above = spaceAbove >= NEED || spaceAbove >= spaceBelow;
  else above = !(spaceBelow >= NEED || spaceBelow >= spaceAbove);
  const style = { ...base, left };
  if (above) style.bottom = vh - rect.top + 12;
  else style.top = rect.bottom + 12;
  return style;
}

function OnboardingCoach({ step, context, onAdvance, onSkip, onComplete }) {
  const total = ONB_STEPS.length;
  const idx = Math.min(Math.max(step | 0, 0), total - 1);
  const def = ONB_STEPS[idx];
  const [rect, setRect] = useState(null);
  const prevCtxRef = useRef(context);

  const title = typeof def.title === "function" ? def.title(context) : def.title;
  const body = typeof def.body === "function" ? def.body(context) : def.body;
  const selector = def.anchor(context);

  // Poll the anchor's rect (the underlying app re-renders as the user acts).
  // Cheap, and only while the tour is mounted.
  useEffect(() => {
    const measure = () => {
      const el = selector ? document.querySelector(selector) : null;
      if (el) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) { setRect(r); return; }
      }
      setRect(null);
    };
    measure();
    const id = setInterval(measure, 250);
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      clearInterval(id);
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [selector]);

  // Self-advance when a step's predicate fires (event picked, send done, …).
  // Manual Next still works for every step.
  useEffect(() => {
    const prev = prevCtxRef.current;
    if (def.auto && def.auto(context, prev)) {
      if (def.final) onComplete();
      else onAdvance(idx + 1);
    }
    prevCtxRef.current = context;
  }, [context, def, idx, onAdvance, onComplete]);

  const next = () => { if (def.final) onComplete(); else onAdvance(idx + 1); };
  const back = () => { if (idx > 0) onAdvance(idx - 1); };
  const cta = () => { if (context.openHub) context.openHub(); onComplete(); };

  return (
    <div className="ip-onb" role="dialog" aria-label="Getting started">
      {rect && <div className="ip-onb-ring" style={onbRingStyle(rect)} />}
      <div className={`ip-onb-card${rect ? "" : " floating"}`} style={onbCardStyle(rect, def.place)}>
        <div className="ip-onb-top">
          <span className="ip-onb-progress"><Sparkles size={13} /> Step {idx + 1} of {total}</span>
          <button className="ip-onb-x" onClick={onSkip} aria-label="Skip the tour"><X size={15} /></button>
        </div>
        <div className="ip-onb-title">{title}</div>
        <div className="ip-onb-body">{body}</div>
        <div className="ip-onb-actions">
          <button className="ip-onb-skip" onClick={onSkip}>Skip tour</button>
          <div className="ip-onb-nav">
            {idx > 0 && <button className="ip-onb-back" onClick={back}>Back</button>}
            <button className="ip-onb-next" onClick={def.final ? cta : next}>
              {def.final ? def.cta : "Next"} <ArrowRight size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Design tokens mirror BookApp's BOOK_CSS (the new Surplus design system):
// Inter UI / Newsreader display, #2f6df6 accent, hairline borders, soft
// surface panels. Class names are unchanged — this is a pure reskin.
export const IP_CSS = `
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap');
:root { --ip-bg:#ffffff; --ip-card:#ffffff; --ip-surface:#f4f5f7;
  --ip-line:rgba(20,23,28,.08); --ip-line-2:rgba(20,23,28,.16);
  --ip-ink:#1b1e22; --ip-dim:#5b616a; --ip-faint:#99a0a8;
  --ip-accent:#2f6df6; --ip-accent-bg:#eaf1fe; --ip-accent-ink:#fff;
  --ip-success:#1f9d62; --ip-success-bg:#e7f5ee;
  --ip-warning:#b07210; --ip-warning-bg:#fbf1e1;
  --ip-danger:#c0433d; --ip-danger-bg:#fbeceb;
  --ip-font-ui:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --ip-font-display:'Newsreader',Georgia,'Times New Roman',serif; }
body { background:#e9ebee; }
.ip-root { width:100%; max-width:430px; margin:0 auto; min-height:100dvh; background:var(--ip-bg);
  color:var(--ip-ink); display:flex; flex-direction:column;
  font-family:var(--ip-font-ui); font-size:14px; line-height:1.5;
  -webkit-font-smoothing:antialiased; }
.ip-root * { box-sizing:border-box; }
.spin { animation:ipspin 1s linear infinite; }
@keyframes ipspin { to { transform:rotate(360deg); } }

.ip-centered { flex:1; display:flex; align-items:center; justify-content:center; padding:24px; }
.ip-empty { text-align:center; color:var(--ip-dim); display:flex; flex-direction:column;
  align-items:center; gap:10px; max-width:300px; }
.ip-empty p { margin:0; font-size:14px; line-height:1.5; }
.ip-empty-title { font-family:var(--ip-font-display); font-size:21px; font-weight:400;
  color:var(--ip-ink); }

.ip-banner { background:var(--ip-warning-bg); color:var(--ip-warning); font-size:13px;
  padding:10px 14px; display:flex; gap:7px; align-items:center; line-height:1.35; }

.ip-howto { background:var(--ip-accent-bg); color:var(--ip-accent); font-size:13px;
  line-height:1.45; padding:11px 14px; border-radius:14px; margin-bottom:12px;
  text-align:center; }

/* event bar */
.ip-eventbar { position:sticky; top:0; z-index:5; background:var(--ip-card);
  border-bottom:.5px solid var(--ip-line); }
.ip-eventhead { display:flex; align-items:center; gap:8px; padding-right:12px; }
.ip-eventpick { flex:1; min-width:0; display:flex; align-items:center; justify-content:space-between;
  padding:14px 16px; background:none; border:0; font-family:var(--ip-font-display);
  font-size:17px; font-weight:400; color:var(--ip-ink); }
.ip-eventpick.empty { color:var(--ip-accent); font-family:var(--ip-font-ui);
  font-size:14px; font-weight:500; }
.ip-eventlabel { display:inline-flex; align-items:center; gap:6px; min-width:0;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ip-eventpick .rot { transform:rotate(90deg); }
.ip-signout { flex-shrink:0; display:flex; align-items:center; gap:6px;
  background:var(--ip-surface); border:.5px solid var(--ip-line); border-radius:999px;
  padding:6px 12px; color:var(--ip-dim); font-size:12px; cursor:pointer;
  font-family:var(--ip-font-ui); }
.ip-signout:active { background:var(--ip-accent-bg); }
.ip-eventmenu { padding:0 12px 12px; }
.ip-eventrow { display:flex; gap:8px; }
.ip-recents { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px;
  max-height:96px; overflow-y:auto; }
.ip-chip { border:.5px solid var(--ip-line-2); background:var(--ip-bg); border-radius:10px;
  padding:5px 10px; font-size:11px; color:var(--ip-ink); cursor:pointer;
  font-family:var(--ip-font-ui); }
.ip-chip.on { background:var(--ip-accent-bg); color:var(--ip-accent);
  border-color:var(--ip-accent); font-weight:500; }
.ip-chiprow { display:flex; flex-wrap:wrap; gap:7px; margin-bottom:4px; }

/* screens */
.ip-screen { flex:1; padding:14px 18px 88px; overflow-y:auto; }
.ip-pad { display:flex; flex-direction:column; gap:10px; margin-top:12px; }
.ip-seg { display:flex; gap:4px; background:var(--ip-surface); border-radius:999px; padding:4px; }
.ip-seg button { flex:1; border:0; background:none; padding:10px 6px; border-radius:999px;
  font-size:12.5px; font-weight:500; color:var(--ip-dim); display:flex; gap:6px;
  align-items:center; justify-content:center; transition:background .12s, color .12s;
  font-family:var(--ip-font-ui); }
.ip-seg button.on { background:var(--ip-accent-bg); color:var(--ip-accent); }

.ip-input { width:100%; padding:0 14px; border:.5px solid var(--ip-line-2);
  border-radius:10px; font-size:16px; background:var(--ip-bg); color:var(--ip-ink);
  min-height:48px; font-family:var(--ip-font-ui); }
.ip-input:focus, .ip-area:focus { outline:none; border-color:var(--ip-accent); }
.ip-microw { display:flex; gap:8px; align-items:stretch; }
.ip-microw-hint { display:inline-flex; align-items:center; gap:5px; margin-top:6px;
  font-size:12px; background:none; border:0; padding:0; cursor:inherit;
  font-family:var(--ip-font-ui); }
button.ip-microw-hint { cursor:pointer; }
.ip-microw .ip-input { flex:1; min-width:0; }
.ip-mic { flex-shrink:0; width:46px; display:flex; align-items:center; justify-content:center;
  border:.5px solid var(--ip-line-2); border-radius:10px; background:var(--ip-bg);
  color:var(--ip-dim); cursor:pointer; }
.ip-mic.on { background:var(--ip-danger-bg); border-color:var(--ip-danger); color:var(--ip-danger);
  animation:ipmicpulse 1.1s ease-in-out infinite; }
@keyframes ipmicpulse { 0%,100% { opacity:1; } 50% { opacity:.55; } }
.ip-area { width:100%; padding:11px 13px; border:.5px solid var(--ip-line-2);
  border-radius:10px; font-size:15px; line-height:1.45; resize:vertical; background:var(--ip-bg);
  color:var(--ip-ink); font-family:var(--ip-font-ui); }
.ip-lbl { font-size:11px; font-weight:600; color:var(--ip-faint); text-transform:uppercase;
  letter-spacing:.04em; margin:12px 0 5px; display:block; font-family:var(--ip-font-ui); }
.ip-lbl .ip-dim { font-weight:400; text-transform:none; letter-spacing:0; }

/* buttons */
.ip-btn { display:inline-flex; gap:8px; align-items:center; justify-content:center;
  border:.5px solid var(--ip-line-2); background:var(--ip-bg); color:var(--ip-ink);
  border-radius:10px; padding:13px 16px; font-size:14px; font-weight:500; cursor:pointer;
  min-height:48px; font-family:var(--ip-font-ui);
  transition:transform .08s ease, filter .12s ease; }
.ip-btn:active { transform:scale(.98); }
.ip-btn:disabled { opacity:.45; }
.ip-btn.primary { background:var(--ip-accent); color:var(--ip-accent-ink);
  border-color:var(--ip-accent); }
.ip-btn.ghost { background:var(--ip-surface); border-color:var(--ip-line); }
.ip-btn.sm { padding:10px 13px; font-size:13px; min-height:40px; }
.ip-btn.lg { padding:15px; font-size:15px; min-height:52px; border-radius:12px; }
.ip-btn.block { width:100%; }
.ip-actions { display:flex; flex-direction:column; gap:9px; margin-top:16px; }
.ip-actions > .ip-btn { width:100%; }
.ip-actions-row { display:flex; gap:9px; }
.ip-actions-row .ip-btn { flex:1; }
.ip-disclosure { display:flex; align-items:center; gap:6px; margin-top:14px;
  background:none; border:0; color:var(--ip-accent); font-size:13px; font-weight:500;
  padding:4px 0; cursor:pointer; font-family:var(--ip-font-ui); }
.ip-disclosure .rot { transform:rotate(90deg); }
.ip-more { margin-top:4px; }
.ip-laterhint { margin-top:10px; font-size:12px; }

/* camera */
.ip-cam { position:relative; margin-top:14px; border-radius:14px; overflow:hidden;
  background:#000; aspect-ratio:1/1; }
.ip-video { width:100%; height:100%; object-fit:cover; }
.ip-reticle { position:absolute; inset:18%; border:2px solid rgba(255,255,255,.85);
  border-radius:14px; box-shadow:0 0 0 9999px rgba(0,0,0,.25); }
.ip-camstatus { position:absolute; left:0; right:0; bottom:0; padding:10px;
  text-align:center; color:#fff; font-size:13px; background:linear-gradient(transparent,rgba(0,0,0,.6));
  display:flex; gap:6px; align-items:center; justify-content:center; }
.ip-camfallback { text-align:center; color:var(--ip-dim); padding:36px 18px;
  display:flex; flex-direction:column; gap:8px; align-items:center; margin-top:14px;
  border:1px dashed var(--ip-line-2); border-radius:14px; }
.ip-camfallback p { margin:0; }

/* candidates */
.ip-cands { display:flex; flex-direction:column; gap:8px; margin-top:6px; }
.ip-confirm-hint { margin:6px 0 2px; }
.ip-cand { text-align:left; border:.5px solid var(--ip-line); background:var(--ip-surface);
  border-radius:14px; padding:14px; min-height:60px; font-family:var(--ip-font-ui); }
.ip-cand:active { background:var(--ip-accent-bg); }
.ip-cand-name { font-weight:600; font-size:15px; }
.ip-cand-sub { font-size:13px; color:var(--ip-dim); margin-top:2px; }
.ip-cand-url { font-size:11px; color:var(--ip-accent); margin-top:3px; }

/* result / detail */
.ip-back { background:none; border:0; color:var(--ip-accent); font-size:13px;
  font-weight:500; display:inline-flex; gap:5px; align-items:center; padding:2px 0 10px;
  font-family:var(--ip-font-ui); }
.ip-person { background:var(--ip-surface); border:.5px solid var(--ip-line);
  border-radius:14px; padding:16px; }
.ip-person-name { font-family:var(--ip-font-display); font-size:22px; font-weight:400; }
.ip-person-sub { font-size:13px; color:var(--ip-dim); margin-top:3px; }
.ip-warn,.ip-err,.ip-ok { font-size:13px; display:flex; gap:6px; align-items:center;
  margin-top:8px; padding:8px 11px; border-radius:10px; }
.ip-warn { background:var(--ip-warning-bg); color:var(--ip-warning); }
.ip-warn { text-align:left; }
.ip-linkbtn { background:none; border:0; color:var(--ip-accent); font-weight:600;
  text-decoration:underline; cursor:pointer; padding:0; font-size:13px; }
.ip-err { background:var(--ip-danger-bg); color:var(--ip-danger); }
.ip-ok { background:var(--ip-success-bg); color:var(--ip-success); }
.ip-thread { background:var(--ip-surface); border:.5px solid var(--ip-line);
  border-radius:14px; padding:12px 14px; margin-top:12px; }
.ip-threadrow { font-size:13px; display:flex; gap:6px; align-items:center; }
.ip-privnote { font-style:italic; color:var(--ip-dim); font-size:13px; margin-top:8px; }

/* list */
.ip-listhead { display:flex; justify-content:space-between; align-items:center;
  font-size:12px; color:var(--ip-faint); padding:2px 2px 10px; }
.ip-actgroup { margin-bottom:16px; }
.ip-actgroup-head { display:flex; justify-content:space-between; align-items:baseline;
  padding:4px 2px 6px; margin-bottom:8px; }
.ip-actgroup-label { font-weight:500; font-size:13px; }
.ip-actgroup-meta { font-size:11px; color:var(--ip-faint); }
.ip-iconbtn,.ip-rowitem { background:var(--ip-surface); border:.5px solid var(--ip-line); }
.ip-iconbtn { border-radius:10px; padding:7px; color:var(--ip-dim); }
.ip-list { display:flex; flex-direction:column; gap:8px; }
.ip-rowitem { display:flex; align-items:center; gap:10px; border-radius:14px;
  padding:13px 14px; text-align:left; min-height:60px; font-family:var(--ip-font-ui); }
.ip-rowitem:active { background:var(--ip-accent-bg); }
.ip-row-main { flex:1; min-width:0; }
.ip-row-name { font-weight:600; font-size:15px; }
.ip-row-sub { font-size:12px; color:var(--ip-dim); margin-top:1px; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.ip-row-chips { display:flex; flex-wrap:wrap; gap:5px; margin-top:6px; }
.ip-row-meta { font-size:11px; color:var(--ip-faint); flex-shrink:0; }
.ip-st,.ip-cc { font-size:11px; font-weight:500; padding:3px 9px; border-radius:999px; }
.ip-st { background:var(--ip-surface); color:var(--ip-dim); border:.5px solid var(--ip-line); }
.st-rsvp { background:var(--ip-success-bg); color:var(--ip-success); border:0; }
.st-contacted { background:var(--ip-accent-bg); color:var(--ip-accent); border:0; }
.st-pending { background:var(--ip-warning-bg); color:var(--ip-warning); border:0; }
.st-below { background:var(--ip-surface); color:var(--ip-faint); }
.ip-cc.c-on { background:var(--ip-success-bg); color:var(--ip-success); }
.ip-cc.c-off { background:var(--ip-danger-bg); color:var(--ip-danger); }
.ip-cc.c-unk { background:var(--ip-surface); color:var(--ip-dim); }

.ip-dim { color:var(--ip-dim); font-size:13px; }
.ip-center { text-align:center; }

/* bottom tabs */
.ip-tabs { position:sticky; bottom:0; display:flex; background:var(--ip-card);
  border-top:.5px solid var(--ip-line); padding-bottom:env(safe-area-inset-bottom); }
.ip-tabs button { flex:1; border:0; background:none; padding:12px 8px; display:flex;
  flex-direction:column; align-items:center; gap:4px; font-size:11px; font-weight:500;
  color:var(--ip-faint); font-family:var(--ip-font-ui); }
.ip-tabs button.on { color:var(--ip-accent); }

/* VIP star (icon-only) */
.ip-person-head { display:flex; align-items:flex-start; justify-content:space-between;
  gap:10px; }
.ip-star { flex-shrink:0; background:none; border:0; padding:2px; line-height:0;
  color:#c2c8d2; cursor:pointer; transition:transform .08s ease, color .12s ease; }
.ip-star:active { transform:scale(.9); }
.ip-star.on { color:var(--ip-gold,#ba7517); }

/* onboarding coachmarks : ambient (the underlying UI stays clickable) */
.ip-onb { position:fixed; inset:0; z-index:58; pointer-events:none; }
.ip-onb-ring { border:2px solid var(--ip-accent); border-radius:14px; z-index:59;
  pointer-events:none; box-shadow:0 0 0 3px rgba(47,109,246,.18),
  0 0 0 9999px rgba(20,23,28,.12); animation:iponbpulse 1.6s ease-in-out infinite; }
@keyframes iponbpulse { 0%,100% { box-shadow:0 0 0 3px rgba(47,109,246,.18),
  0 0 0 9999px rgba(20,23,28,.12); } 50% { box-shadow:0 0 0 6px rgba(47,109,246,.10),
  0 0 0 9999px rgba(20,23,28,.12); } }
.ip-onb-card { pointer-events:auto; background:var(--ip-card); border:.5px solid var(--ip-line-2);
  border-radius:14px; padding:14px 15px 13px; box-shadow:0 12px 34px rgba(20,23,28,.18);
  font-family:var(--ip-font-ui); }
.ip-onb-card.floating { box-shadow:0 14px 40px rgba(20,23,28,.25); }
.ip-onb-top { display:flex; align-items:center; justify-content:space-between; }
.ip-onb-progress { display:inline-flex; align-items:center; gap:5px; font-size:11px;
  font-weight:600; color:var(--ip-accent); text-transform:uppercase; letter-spacing:.04em; }
.ip-onb-x { background:none; border:0; color:var(--ip-dim); cursor:pointer; padding:2px;
  line-height:0; border-radius:6px; }
.ip-onb-x:active { background:var(--ip-surface); }
.ip-onb-title { font-family:var(--ip-font-display); font-size:18px; font-weight:400;
  color:var(--ip-ink); margin:7px 0 4px; }
.ip-onb-body { font-size:13px; line-height:1.5; color:var(--ip-dim); }
.ip-onb-actions { display:flex; align-items:center; justify-content:space-between;
  margin-top:13px; gap:10px; }
.ip-onb-skip { background:none; border:0; color:var(--ip-faint); font-size:12px;
  cursor:pointer; padding:6px 2px; }
.ip-onb-nav { display:flex; align-items:center; gap:8px; }
.ip-onb-back { background:none; border:0; color:var(--ip-ink); font-size:13px;
  font-weight:500; cursor:pointer; padding:8px 6px; }
.ip-onb-next { display:inline-flex; align-items:center; gap:5px; background:var(--ip-accent);
  color:var(--ip-accent-ink); border:0; border-radius:10px; padding:9px 14px;
  font-size:13px; font-weight:500; cursor:pointer; }
.ip-onb-next:active { transform:scale(.98); }
`;
