// AuthOptions : the email/password + Google/Microsoft sign-in block.
//
// Drop-in for any signed-out surface (the SignInModal today). Email+password works
// for ANY address (Gmail/Outlook/Yahoo/custom); the provider buttons are one-tap.
// LinkedIn is intentionally NOT here -- each surface owns its LinkedIn CTA (the modal
// has demo/paid pre-flight). On success the session cookie is set; we reload so
// /api/auth/me re-renders into the signed-in app.
import React, { useState } from "react";
import { Loader2, ArrowRight, AlertCircle } from "lucide-react";
import { api } from "../lib/api.js";
import { isNativeApp, nativeOAuthLogin } from "../lib/nativeAuth.js";

function GoogleMark({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5Z"/>
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65Z"/>
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19Z"/>
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48Z"/>
    </svg>
  );
}
function MicrosoftMark({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 23 23" aria-hidden="true">
      <path fill="#F25022" d="M1 1h10v10H1z"/><path fill="#7FBA00" d="M12 1h10v10H12z"/>
      <path fill="#00A4EF" d="M1 12h10v10H1z"/><path fill="#FFB900" d="M12 12h10v10H12z"/>
    </svg>
  );
}

export default function AuthOptions({ onSignedIn, defaultMode = "signup" }) {
  const [mode, setMode] = useState(defaultMode);   // "signup" | "login"
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [oauthBusy, setOauthBusy] = useState(null); // "google" | "microsoft"
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [verifying, setVerifying] = useState(false);   // after signup: enter the email PIN
  const [code, setCode] = useState("");
  const [forgot, setForgot] = useState(false);         // dedicated "reset your password" step

  const done = () => { if (onSignedIn) onSignedIn(); else window.location.reload(); };

  const verifyEmailCode = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.verifyCode(code.trim());
      done();                                          // verified -> into the app
    } catch (err) {
      setBusy(false);
      setError(err.message || "That code is invalid or expired.");
    }
  };

  const resendCode = async () => {
    setError(null); setNotice(null);
    try {
      const r = await api.sendCode();
      setNotice(r?.sent ? "Sent a new code." : "Code refreshed (email isn't configured here).");
    } catch (err) {
      setError(err.message || "Couldn't resend the code.");
    }
  };

  const handleForgot = async () => {
    setError(null); setNotice(null);
    const addr = email.trim();
    if (!addr.includes("@")) { setError("Enter your email."); return; }
    setBusy(true);
    try {
      await api.forgotPassword(addr);
      setBusy(false);
      setNotice("If that email has an account, a reset link is on its way.");
    } catch {
      // forgot-password is always 200; on a network blip, show the same neutral note.
      setBusy(false);
      setNotice("If that email has an account, a reset link is on its way.");
    }
  };

  const startOAuth = (provider, fn) => async () => {
    setError(null);
    setOauthBusy(provider);
    try {
      // Native app: Google/Microsoft block OAuth in the embedded WebView, so
      // run sign-in in the system browser and adopt the session via deep link.
      // Returns false on an OLD app build without the Browser plugin — fall
      // through to the web redirect so the button never dead-ends.
      if (isNativeApp()) {
        const handled = await nativeOAuthLogin(fn); // navigates to mobile-adopt on success
        if (handled) return;
      }
      const r = await fn();
      if (!r?.url) throw new Error("Backend didn't return a sign-in URL");
      window.location.href = r.url;          // top-level nav; cookie set on callback
    } catch (err) {
      setOauthBusy(null);
      setError(err.message || `Could not start ${provider} sign-in.`);
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "signup") {
        const res = await api.signup({ name: name.trim(), email: email.trim(), password });
        setBusy(false);
        // Require the emailed PIN ONLY when one actually went out (email provider live).
        // If it didn't (dormant provider), proceed -- never lock a new user out.
        if (res?.verification_required) { setVerifying(true); return; }
        done();
        return;
      }
      await api.login({ email: email.trim(), password });
      done();
    } catch (err) {
      setBusy(false);
      setError(err.message || "Could not sign you in.");
    }
  };

  const canSubmit = email.trim().includes("@") && password.length >= 8
    && (mode === "login" || name.trim());

  // After signup: collect the 6-digit email PIN. REQUIRED (no skip) -- this view only
  // shows when a code actually went out (verification_required), so it never dead-ends.
  if (verifying) {
    return (
      <div className="authopts">
        <style>{AUTHOPTS_CSS}</style>
        <div className="authopts-divider"><span>verify your email</span></div>
        <p style={{ fontSize: 13, color: "#5b616a", margin: "0 0 4px" }}>
          We emailed a 6-digit code to <b>{email.trim()}</b>. Enter it to confirm your address.
        </p>
        <form onSubmit={verifyEmailCode} className="authopts-form">
          <input className="authopts-in" inputMode="numeric" maxLength={6} value={code}
                 placeholder="123456" autoFocus
                 onChange={(e) => setCode(e.target.value.replace(/[^0-9]/g, ""))} />
          <button type="submit" className="authopts-submit"
                  disabled={busy || code.trim().length < 6}>
            {busy ? <><Loader2 className="spin" size={16} /> Verifying…</>
                  : <>Verify email <ArrowRight size={16} /></>}
          </button>
        </form>
        {error && <div className="authopts-error" role="alert"><AlertCircle size={14} /> {error}</div>}
        {notice && <div className="authopts-notice">{notice}</div>}
        <button type="button" className="authopts-forgot" onClick={resendCode}>Resend code</button>
      </div>
    );
  }

  // "Forgot password?" -> a dedicated step: enter email -> send reset link -> confirmation.
  if (forgot) {
    return (
      <div className="authopts">
        <style>{AUTHOPTS_CSS}</style>
        <div className="authopts-divider"><span>reset your password</span></div>
        <p style={{ fontSize: 13, color: "#5b616a", margin: "0 0 4px" }}>
          Enter your email and we'll send you a link to reset your password.
        </p>
        <form onSubmit={(e) => { e.preventDefault(); handleForgot(); }} className="authopts-form">
          <input className="authopts-in" type="email" value={email} required autoFocus
                 placeholder="you@anywhere.com"
                 onChange={(e) => setEmail(e.target.value)} />
          <button type="submit" className="authopts-submit"
                  disabled={busy || !email.trim().includes("@")}>
            {busy ? <><Loader2 className="spin" size={16} /> Sending…</>
                  : <>Send reset link <ArrowRight size={16} /></>}
          </button>
        </form>
        {error && <div className="authopts-error" role="alert"><AlertCircle size={14} /> {error}</div>}
        {notice && <div className="authopts-notice">{notice}</div>}
        <button type="button" className="authopts-switch"
                onClick={() => { setError(null); setNotice(null); setForgot(false); }}>
          Back to sign in
        </button>
      </div>
    );
  }

  return (
    <div className="authopts">
      <style>{AUTHOPTS_CSS}</style>
      <div className="authopts-oauth">
        <button type="button" className="authopts-oauth-btn"
                onClick={startOAuth("google", api.startGoogleAuth)} disabled={!!oauthBusy || busy}>
          {oauthBusy === "google" ? <Loader2 className="spin" size={16} /> : <GoogleMark />}
          <span>Continue with Google</span>
        </button>
        {/* Microsoft sign-in is built + provider-agnostic, but hidden until MS OAuth
            creds exist (no Azure app yet) -- showing it would 409 on click. Re-enable
            by restoring this button once MICROSOFT_CLIENT_ID/SECRET are set. */}
      </div>

      <div className="authopts-divider"><span>or use email</span></div>

      <form onSubmit={submit} className="authopts-form">
        {mode === "signup" && (
          <input className="authopts-in" value={name} required placeholder="Your name"
                 onChange={(e) => setName(e.target.value)} />
        )}
        <input className="authopts-in" type="email" value={email} required placeholder="you@anywhere.com"
               onChange={(e) => setEmail(e.target.value)} />
        <input className="authopts-in" type="password" value={password} required
               placeholder={mode === "signup" ? "Password (8+ characters)" : "Password"}
               onChange={(e) => setPassword(e.target.value)} />
        <button type="submit" className="authopts-submit" disabled={busy || !!oauthBusy || !canSubmit}>
          {busy ? <><Loader2 className="spin" size={16} /> {mode === "signup" ? "Creating account…" : "Signing in…"}</>
                : <>{mode === "signup" ? "Create account" : "Sign in"} <ArrowRight size={16} /></>}
        </button>
      </form>

      {error && <div className="authopts-error" role="alert"><AlertCircle size={14} /> {error}</div>}
      {notice && <div className="authopts-notice">{notice}</div>}

      {mode === "login" && (
        <button type="button" className="authopts-forgot"
                onClick={() => { setError(null); setNotice(null); setForgot(true); }}>
          Forgot password?
        </button>
      )}
      <button type="button" className="authopts-switch"
              onClick={() => { setError(null); setNotice(null); setMode(mode === "signup" ? "login" : "signup"); }}>
        {mode === "signup" ? "Already have an account? Sign in" : "New here? Create an account"}
      </button>
    </div>
  );
}

const AUTHOPTS_CSS = `
.authopts { display:flex; flex-direction:column; gap:10px; text-align:left; }
.authopts-oauth { display:flex; flex-direction:column; gap:8px; }
.authopts-oauth-btn {
  display:flex; align-items:center; justify-content:center; gap:8px;
  width:100%; padding:10px 14px; border:1px solid #e6e8eb; border-radius:10px;
  background:#fff; color:#1b1e22; font:inherit; font-weight:600; font-size:14px; cursor:pointer;
  transition:background .12s ease, border-color .12s ease, box-shadow .12s ease;
}
.authopts-oauth-btn:hover:not(:disabled) { background:#fbfcfd; border-color:#d3d7dc; box-shadow:0 3px 14px rgba(20,23,28,.06); }
.authopts-oauth-btn:disabled { opacity:.55; cursor:default; }
.authopts-divider { display:flex; align-items:center; gap:10px; color:#99a0a8; font-size:12px; margin:2px 0; }
.authopts-divider::before, .authopts-divider::after { content:""; flex:1; height:1px; background:#e6e8eb; }
.authopts-form { display:flex; flex-direction:column; gap:8px; }
.authopts-in {
  width:100%; padding:10px 12px; border:1px solid #e6e8eb; border-radius:10px;
  background:#fff; color:#1b1e22; font:inherit; font-size:14px; box-sizing:border-box;
}
.authopts-in:focus { outline:none; border-color:#2f6df6; box-shadow:0 0 0 3px #eaf1fe; }
.authopts-submit {
  display:flex; align-items:center; justify-content:center; gap:8px;
  width:100%; padding:11px 14px; border:0; border-radius:10px; margin-top:2px;
  background:#2f6df6; color:#fff; font:inherit; font-weight:700; font-size:14px; cursor:pointer;
}
.authopts-submit:hover:not(:disabled) { background:#2257d6; }
.authopts-submit:disabled { opacity:.5; cursor:default; }
.authopts-error { display:flex; align-items:center; gap:6px; color:#c43146; background:#fce6ea; padding:8px 10px; border-radius:8px; font-size:13px; }
.authopts-switch { width:100%; margin-top:4px; padding:6px; border:0; background:none; color:#2f6df6; font:inherit; font-size:13px; font-weight:600; cursor:pointer; }
.authopts-switch:hover { text-decoration:underline; }
.authopts-forgot { width:100%; padding:2px; border:0; background:none; color:#5b616a; font:inherit; font-size:12px; cursor:pointer; }
.authopts-forgot:hover { text-decoration:underline; }
.authopts-notice { color:#1f6f43; background:#e7f6ee; padding:8px 10px; border-radius:8px; font-size:13px; }
`;
