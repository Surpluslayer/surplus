import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App.jsx";

// ?fresh=true demo reset : runs BEFORE React mounts so App's useState
// initializers don't re-read pre-reset localStorage. The token is read
// from ?key= and sent to POST /api/demo/start, which issues a fresh
// session cookie + placeholder event. The new event_id is seeded into
// the unified session key so App.jsx's normal hydration picks it up.
const DEMO_LS_KEYS = ["surplus_mode", "surplus_unified_session"];
const UNIFIED_SESSION_KEY = "surplus_unified_session";

async function maybeFreshReset() {
  let params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch {
    return;
  }
  if (params.get("fresh") !== "true") return;

  for (const k of DEMO_LS_KEYS) {
    try { localStorage.removeItem(k); } catch {}
  }
  try { sessionStorage.clear(); } catch {}

  const key = params.get("key") || "";
  try {
    const res = await fetch("/api/demo/start", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (res.ok) {
      const { event_id } = await res.json();
      if (event_id) {
        try {
          localStorage.setItem(
            UNIFIED_SESSION_KEY,
            JSON.stringify({ eventId: event_id, stage: "intake", committedPath: null }),
          );
        } catch {}
      }
    }
  } catch {}

  try {
    const url = new URL(window.location.href);
    url.searchParams.delete("fresh");
    url.searchParams.delete("key");
    window.history.replaceState({}, "", url.toString());
  } catch {}
}

function renderApp() {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

// Avoid top-level await : Vite's default prod target (es2020 + Safari 14)
// doesn't support it, so the build fails. Promise-then keeps the same
// ordering : React only mounts after the reset finishes.
maybeFreshReset().then(renderApp, renderApp);
