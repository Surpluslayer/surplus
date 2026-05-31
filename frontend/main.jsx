import React from "react";
import ReactDOM from "react-dom/client";

import { initAnalytics } from "./lib/analytics.js";

// Phone-first in-person surface lives at /inperson (or ?surface=inperson). It's
// a separate root from the desktop pipeline App : same origin, same session
// cookie, but a one-handed capture UI.
// In prod the server picks the shell by Host (app.surpluslayer.com serves the
// dedicated inperson.html entry), so this index entry normally renders the
// desktop App. We still detect it here for local dev / preview, where one Vite
// server has no host routing : /inperson, ?surface=inperson, or an app.* host.
function isInPersonSurface() {
  try {
    const { pathname, search, hostname } = window.location;
    if (hostname.startsWith("app.")) return true;
    if (pathname === "/inperson" || pathname.startsWith("/inperson/")) return true;
    return new URLSearchParams(search).get("surface") === "inperson";
  } catch { return false; }
}

// Boot PostHog before React mounts so autocapture + session replay catch
// the very first interactions. No-op when no key is configured.
initAnalytics();

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

// Dynamic import so the phone surface (incl. the jsQR decoder) and the desktop
// pipeline ship as separate chunks : a phone never downloads the desktop App,
// and desktop never downloads jsQR.
const load = isInPersonSurface()
  ? () => import("./InPersonApp.jsx")
  : () => import("./App.jsx");

load().then(({ default: Root }) => {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <Root />
    </React.StrictMode>
  );
});
