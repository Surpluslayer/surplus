// ResetPasswordPage : the target of the password-reset email link (/reset-password?token=).
// Standalone page (rendered by main.jsx when the path matches) so it doesn't depend on
// the main app's auth state. Reads the token from the URL, takes a new password, POSTs
// to /api/auth/reset-password, then sends the user to sign in.
import { useState } from "react";
import { Loader2, ArrowRight, AlertCircle, CheckCircle2 } from "lucide-react";
import { api } from "../lib/api.js";

export default function ResetPasswordPage() {
  const token = new URLSearchParams(window.location.search).get("token") || "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState(null);

  const canSubmit = password.length >= 8 && password === confirm && token;

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) { setError("Passwords don't match."); return; }
    setBusy(true);
    try {
      await api.resetPassword({ token, password });
      setDone(true);
    } catch (err) {
      setBusy(false);
      setError(err.message || "That reset link is invalid or expired.");
    }
  };

  return (
    <div className="rp-wrap">
      <style>{RP_CSS}</style>
      <div className="rp-card">
        <div className="rp-brand">
          <img className="rp-logo" src="/surplus-logo.png" alt="" />
          <span className="rp-name">surplus</span>
        </div>
        {!token ? (
          <>
            <h1 className="rp-h1">Reset link missing</h1>
            <p className="rp-sub">This page needs a valid reset link from your email.</p>
            <a className="rp-link" href="/">Back to sign in</a>
          </>
        ) : done ? (
          <>
            <div className="rp-ok"><CheckCircle2 size={32} /></div>
            <h1 className="rp-h1">Password updated</h1>
            <p className="rp-sub">You can now sign in with your new password.</p>
            <a className="rp-cta" href="/">Sign in <ArrowRight size={16} /></a>
          </>
        ) : (
          <>
            <h1 className="rp-h1">Set a new password</h1>
            <p className="rp-sub">Choose a new password for your surplus account.</p>
            <form onSubmit={submit} className="rp-form">
              <input className="rp-in" type="password" value={password} required
                     placeholder="New password (8+ characters)" autoFocus
                     onChange={(e) => setPassword(e.target.value)} />
              <input className="rp-in" type="password" value={confirm} required
                     placeholder="Confirm new password"
                     onChange={(e) => setConfirm(e.target.value)} />
              <button type="submit" className="rp-cta" disabled={busy || !canSubmit}>
                {busy ? <><Loader2 className="spin" size={16} /> Updating…</>
                      : <>Update password <ArrowRight size={16} /></>}
              </button>
            </form>
            {error && <div className="rp-error" role="alert"><AlertCircle size={14} /> {error}</div>}
          </>
        )}
      </div>
    </div>
  );
}

const RP_CSS = `
.rp-wrap { min-height:100vh; display:flex; align-items:center; justify-content:center;
  background:#f4f5f7; font-family:'Inter',system-ui,sans-serif; color:#1b1e22; padding:20px; }
.rp-card { width:100%; max-width:380px; background:#fff; border:1px solid #e6e8eb;
  border-radius:16px; padding:28px; box-shadow:0 8px 30px rgba(20,23,28,.08); text-align:center; }
.rp-brand { display:flex; align-items:center; justify-content:center; gap:8px; margin-bottom:18px; }
.rp-logo { width:24px; height:24px; border-radius:6px; }
.rp-name { font-weight:700; font-size:18px; }
.rp-h1 { font-size:20px; font-weight:700; margin:0 0 6px; }
.rp-sub { color:#5b616a; font-size:14px; margin:0 0 18px; }
.rp-form { display:flex; flex-direction:column; gap:8px; }
.rp-in { width:100%; padding:10px 12px; border:1px solid #e6e8eb; border-radius:10px;
  font:inherit; font-size:14px; box-sizing:border-box; }
.rp-in:focus { outline:none; border-color:#2f6df6; box-shadow:0 0 0 3px #eaf1fe; }
.rp-cta { display:inline-flex; align-items:center; justify-content:center; gap:8px;
  width:100%; padding:11px 14px; border:0; border-radius:10px; margin-top:4px;
  background:#2f6df6; color:#fff; font:inherit; font-weight:700; font-size:14px;
  cursor:pointer; text-decoration:none; }
.rp-cta:hover:not(:disabled) { background:#2257d6; }
.rp-cta:disabled { opacity:.5; cursor:default; }
.rp-link { color:#2f6df6; font-size:14px; font-weight:600; text-decoration:none; }
.rp-error { display:flex; align-items:center; gap:6px; justify-content:center; color:#c43146;
  background:#fce6ea; padding:8px 10px; border-radius:8px; font-size:13px; margin-top:12px; }
.rp-ok { color:#1f9d57; margin-bottom:8px; }
`;
