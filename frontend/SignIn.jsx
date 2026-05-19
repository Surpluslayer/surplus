import React, { useState, useEffect } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { api } from "./lib/api.js";

// LinkedIn mark : official brand glyph, white-on-blue. Lucide doesn't ship it.
const LinkedInIcon = ({ size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M20.45 20.45h-3.55v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.13 1.45-2.13 2.94v5.67H9.37V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.38-1.85 3.61 0 4.28 2.38 4.28 5.47v6.27ZM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13ZM7.12 20.45H3.56V9h3.56v11.45ZM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.22 0Z"/>
  </svg>
);

// ============================================================
// Sign in with LinkedIn : surplus auth
//
// surplus has no separate email/password layer. The user's
// LinkedIn account IS their identity. They click "Sign in with
// LinkedIn", we redirect them to Unipile's hosted page (which
// handles 2FA + captcha), and on return they're authenticated.
//
// The same Unipile connection that auth uses is the connection
// surplus sends DMs through downstream : one consent, one cost.
// ============================================================

const ERROR_MESSAGES = {
  linkedin_auth_failed: "LinkedIn rejected the connection. Please try again.",
  linkedin_callback_failed: "Sign-in didn't complete. Please try again.",
  linkedin_pending:
    "LinkedIn is still finishing the connection : give it a few seconds and refresh.",
};

export default function SignIn() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // Triage-only signup state. The LinkedIn flow is the primary path; this
  // is the secondary "I just want to review applicants" path that doesn't
  // need a Unipile connection.
  const [showTriageForm, setShowTriageForm] = useState(false);
  const [triageName, setTriageName] = useState("");
  const [triageEmail, setTriageEmail] = useState("");
  const [triageBusy, setTriageBusy] = useState(false);

  // Pick up error code from the redirect URL after a failed flow
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("error");
    if (code) {
      setError(ERROR_MESSAGES[code] || "Something went wrong : please try again.");
      // Clean the URL so the error doesn't stick around on subsequent renders
      const url = new URL(window.location.href);
      url.searchParams.delete("error");
      window.history.replaceState({}, "", url.pathname + url.search);
    }
  }, []);

  const handleSignIn = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.startLinkedinAuth();
      if (!res?.url) throw new Error("Backend didn't return a hosted-auth URL");
      // Top-level navigation : the cookie set on /api/auth/linkedin/callback
      // needs to be set during a top-level redirect, not a fetch.
      window.location.href = res.url;
    } catch (e) {
      setLoading(false);
      // Most likely cause: server missing UNIPILE_DSN / UNIPILE_API_KEY
      const detail = e.message?.includes("503")
        ? "LinkedIn auth isn't configured on this server yet. Reach out to the operator."
        : e.message || "Could not start sign-in.";
      setError(detail);
    }
  };

  const handleTriageSignup = async (e) => {
    e.preventDefault();
    setError(null);
    setTriageBusy(true);
    try {
      await api.triageSignup({ name: triageName.trim(), email: triageEmail.trim() });
      // Session cookie is set on the response. Drop into the app.
      window.location.href = "/";
    } catch (err) {
      setTriageBusy(false);
      setError(err.message || "Could not create your account.");
    }
  };

  return (
    <div className="signin-root">
      <style>{SIGNIN_CSS}</style>
      <div className="signin-card">
        <div className="signin-brand">
          <img className="signin-logo" src="/surplus-logo.png" alt="Surplus" />
          <span className="signin-name">surplus</span>
        </div>

        <h1 className="signin-h1">
          Sign in with <em>LinkedIn</em>
        </h1>
        <p className="signin-sub">
          Surplus runs on the connections you already have. Sign in with
          LinkedIn once : we use that same connection to draft and send
          intros on your behalf.
        </p>

        {error && (
          <div className="signin-error" role="alert">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}

        <button
          className="signin-cta"
          onClick={handleSignIn}
          disabled={loading}
        >
          {loading ? (
            <>
              <Loader2 className="spin" size={18} />
              <span>Redirecting to LinkedIn…</span>
            </>
          ) : (
            <>
              <LinkedInIcon size={18} />
              <span>Sign in with LinkedIn</span>
            </>
          )}
        </button>

        <ul className="signin-bullets">
          <li>Handled by Unipile : 2FA, captcha, and unusual-sign-in prompts all work.</li>
          <li>Your credentials never touch surplus.</li>
          <li>Disconnect any time from Settings.</li>
        </ul>

        <div className="signin-divider"><span>or</span></div>

        {!showTriageForm ? (
          <button
            type="button"
            className="signin-secondary"
            onClick={() => setShowTriageForm(true)}
          >
            Just want to review applicants? Skip LinkedIn →
          </button>
        ) : (
          <form className="signin-triage" onSubmit={handleTriageSignup}>
            <p className="signin-triage-hint">
              For Applicant Triage only. You can connect LinkedIn later if you
              decide to use outbound prospecting too.
            </p>
            <input
              type="text"
              placeholder="Your name"
              value={triageName}
              onChange={(e) => setTriageName(e.target.value)}
              required
              autoFocus
              className="signin-input"
            />
            <input
              type="email"
              placeholder="you@example.com"
              value={triageEmail}
              onChange={(e) => setTriageEmail(e.target.value)}
              required
              className="signin-input"
            />
            <button
              type="submit"
              className="signin-cta signin-cta-triage"
              disabled={triageBusy || !triageName.trim() || !triageEmail.trim()}
            >
              {triageBusy ? (
                <>
                  <Loader2 className="spin" size={18} />
                  <span>Creating your account…</span>
                </>
              ) : (
                <span>Create triage-only account</span>
              )}
            </button>
            <button
              type="button"
              className="signin-cancel"
              onClick={() => setShowTriageForm(false)}
            >
              Cancel
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

const SIGNIN_CSS = `
.signin-root {
  --bg:#f6f7f9; --panel:#fff; --line:#e4e8ee;
  --ink:#1f1c2e; --ink-dim:#5f5b73; --ink-faint:#9b96ac;
  --acc:#6b46e0; --acc-deep:#5836c6; --acc-soft:#ede9fb;
  --li:#0a66c2; --li-deep:#084e96;
  font-family:'Plus Jakarta Sans',system-ui,-apple-system,sans-serif;
  background:var(--bg); color:var(--ink);
  min-height:100vh; display:flex; align-items:center; justify-content:center;
  padding:24px;
}
.signin-card {
  background:var(--panel); border:1px solid var(--line);
  border-radius:16px; box-shadow:0 4px 16px rgba(15,15,30,0.05);
  max-width:440px; width:100%; padding:36px 32px;
}
.signin-brand { display:flex; align-items:center; gap:10px; margin-bottom:32px; }
.signin-logo { width:28px; height:28px; }
.signin-name {
  font-family:'Inter',system-ui,sans-serif; font-weight:800;
  letter-spacing:-0.05em; font-size:1.4rem; color:var(--ink);
}
.signin-h1 {
  font-family:'Playfair Display',Georgia,serif; font-weight:600;
  font-size:34px; line-height:1.15; letter-spacing:-0.01em;
  margin-bottom:14px; color:var(--ink);
}
.signin-h1 em { color:var(--acc); font-style:italic; }
.signin-sub {
  font-size:14.5px; line-height:1.6; color:var(--ink-dim);
  margin-bottom:24px;
}
.signin-cta {
  display:inline-flex; align-items:center; justify-content:center; gap:10px;
  width:100%; padding:14px 22px; border-radius:999px; border:0;
  background:var(--li); color:white; font-family:inherit;
  font-weight:600; font-size:15px; cursor:pointer;
  transition:background 0.15s, transform 0.15s, box-shadow 0.15s;
  box-shadow:0 1px 2px rgba(10,102,194,0.25);
}
.signin-cta:hover:not(:disabled) {
  background:var(--li-deep);
  transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(10,102,194,0.3);
}
.signin-cta:disabled { opacity:0.7; cursor:wait; }
.signin-error {
  display:flex; align-items:flex-start; gap:8px;
  padding:11px 14px; margin-bottom:16px;
  background:#fff5f5; color:#b03030;
  border:1px solid #ffd6d6; border-radius:10px;
  font-size:13px; line-height:1.45;
}
.signin-bullets {
  list-style:none; padding:0; margin:24px 0 0; display:flex;
  flex-direction:column; gap:6px;
}
.signin-bullets li {
  font-size:12.5px; color:var(--ink-faint); line-height:1.5;
  padding-left:14px; position:relative;
}
.signin-bullets li::before {
  content:""; position:absolute; left:0; top:8px;
  width:4px; height:4px; border-radius:50%; background:var(--ink-faint);
}
.spin { animation:signin-spin 0.8s linear infinite; }
@keyframes signin-spin { to { transform:rotate(360deg); } }
.signin-divider {
  display:flex; align-items:center; gap:12px; margin:24px 0 16px;
  color:var(--ink-faint); font-size:12px; text-transform:uppercase;
  letter-spacing:0.08em;
}
.signin-divider::before, .signin-divider::after {
  content:""; flex:1; height:1px; background:var(--line);
}
.signin-secondary {
  display:block; width:100%; padding:11px 14px; border-radius:10px;
  background:transparent; color:var(--ink-dim); border:1px dashed var(--line);
  font-family:inherit; font-size:13.5px; cursor:pointer;
  transition:background 0.15s, color 0.15s, border-color 0.15s;
}
.signin-secondary:hover { background:var(--acc-soft); color:var(--acc-deep); border-color:var(--acc); }
.signin-triage { display:flex; flex-direction:column; gap:10px; }
.signin-triage-hint {
  font-size:12.5px; color:var(--ink-faint); line-height:1.5;
  margin:0 0 4px;
}
.signin-input {
  width:100%; padding:11px 14px; border-radius:10px;
  border:1px solid var(--line); background:var(--panel);
  font-family:inherit; font-size:14px; color:var(--ink);
}
.signin-input:focus { outline:none; border-color:var(--acc); }
.signin-cta-triage { background:var(--acc); box-shadow:0 1px 2px rgba(107,70,224,0.25); }
.signin-cta-triage:hover:not(:disabled) {
  background:var(--acc-deep);
  box-shadow:0 4px 12px rgba(107,70,224,0.3);
}
.signin-cancel {
  background:none; border:0; padding:8px; cursor:pointer;
  font-family:inherit; font-size:12.5px; color:var(--ink-faint);
  text-decoration:underline;
}
.signin-cancel:hover { color:var(--ink-dim); }
`;
