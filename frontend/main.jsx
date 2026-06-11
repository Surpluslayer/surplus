import React from "react";
import ReactDOM from "react-dom/client";

import { ErrorBoundary, installPreloadRecovery } from "./lib/resilience.jsx";

installPreloadRecovery();

// Phone-first in-person surface lives at /inperson (or ?surface=inperson). It's
// a separate root from the desktop pipeline App : same origin, same session
// cookie, but a one-handed capture UI.
// In prod the server picks the shell by Host (event.surpluslayer.com serves the
// dedicated inperson.html entry), so this index entry normally renders the
// desktop App. We still detect it here for local dev / preview, where one Vite
// server has no host routing : /inperson, ?surface=inperson, or an event.* host.
function isInPersonSurface() {
  try {
    const { pathname, search, hostname } = window.location;
    if (hostname.startsWith("event.")) return true;
    if (pathname === "/inperson" || pathname.startsWith("/inperson/")) return true;
    return new URLSearchParams(search).get("surface") === "inperson";
  } catch { return false; }
}

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

// Advisor "Your book today" surface lives at /book (or ?surface=book). Like the
// in-person surface it's a separate root from the desktop pipeline App : same
// origin + session cookie, different one-handed shell (the relationship-led
// "keep my book warm" home).
function isBookSurface() {
  try {
    const { pathname, search, hostname } = window.location;
    if (hostname.startsWith("book.")) return true;
    if (pathname === "/book" || pathname.startsWith("/book/")) return true;
    return new URLSearchParams(search).get("surface") === "book";
  } catch { return false; }
}

// Dynamic import so the phone surface (incl. the jsQR decoder) and the desktop
// pipeline ship as separate chunks : a phone never downloads the desktop App,
// and desktop never downloads jsQR.
const load = isBookSurface()
  ? () => import("./BookApp.jsx")
  : isInPersonSurface()
  ? () => import("./InPersonApp.jsx")
  : () => import("./App.jsx");

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
