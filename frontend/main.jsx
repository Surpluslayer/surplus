import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App.jsx";

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

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
