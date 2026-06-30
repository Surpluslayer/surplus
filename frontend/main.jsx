import React from "react";
import ReactDOM from "react-dom/client";

import { ErrorBoundary, installPreloadRecovery } from "./lib/resilience.jsx";

installPreloadRecovery();

// Boot PostHog before React mounts so autocapture + session replay catch
// the very first interactions. No-op when no key is configured.
// Analytics (PostHog, ~390KB) loads lazily after first paint : capture-on-
// event-wifi should never wait on a telemetry bundle.
const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 1500));
idle(() => import("./lib/analytics.js").then((m) => m.initAnalytics()).catch(() => {}));

// ?fresh=true (or ?fresh=1) escape hatch : wipe the cached unified
// session so a returning user with a stale eventId lands on the
// intake screen instead of being resumed past it. Needed because the
// hydration effect in App.jsx resumes the last saved stage from
// localStorage, which hides the SharedIntake Luma row from anyone
// who completed intake once before.
//
// Runs synchronously before React mounts so the App constructor never
// sees the stale keys. Strip the param from the URL afterwards so a
// page reload doesn't keep nuking state.
(function maybeFreshReset() {
  try {
    const params = new URLSearchParams(window.location.search);
    const fresh = params.get("fresh");
    if (fresh !== "true" && fresh !== "1") return;
    try { localStorage.removeItem("surplus_unified_session"); } catch {}
    try { localStorage.removeItem("surplus_mode"); } catch {}
    try { sessionStorage.clear(); } catch {}
    params.delete("fresh");
    const qs = params.toString();
    const next = window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash;
    window.history.replaceState({}, "", next);
  } catch {
    // localStorage / history unavailable (private mode, sandboxed
    // iframe). Nothing to do : the worst case is the user sees
    // their cached session, same as before this code existed.
  }
})();

// The www host (index.html) serves ONLY the desktop pipeline App. The book and
// in-person surfaces live on event.surpluslayer.com (the inperson.html entry),
// so this entry no longer routes /book, /inperson, or ?surface= overrides.
// One exception: the password-reset page is a standalone surface (the target of the
// reset email link), independent of the main app's auth state.
const load = () => (
  window.location.pathname === "/reset-password"
    ? import("./components/ResetPasswordPage.jsx")
    : import("./App.jsx")
);

load().then(({ default: Root }) => {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <ErrorBoundary>
        <Root />
      </ErrorBoundary>
    </React.StrictMode>
  );
}).catch(() => {
  // Chunk import failed (a deploy replaced the hashed files under us) and the
  // one-shot reload in installPreloadRecovery already ran : leave a usable
  // fallback instead of a silently blank page.
  const el = document.getElementById("root");
  if (el) {
    el.innerHTML =
      '<div style="min-height:100vh;display:flex;align-items:center;' +
      'justify-content:center;font-family:Inter,system-ui,sans-serif">' +
      '<button onclick="window.location.reload()" style="font-size:15px;' +
      'padding:10px 22px;border-radius:999px;border:0.5px solid #d6dae1;' +
      'background:#14171c;color:#fff;cursor:pointer">Reload</button></div>';
  }
});
