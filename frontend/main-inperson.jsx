// Entry for the phone-first in-person surface (inperson.html). Served by
// FastAPI for the event.surpluslayer.com host. A dedicated entry means the phone
// bundle never pulls the desktop pipeline App, and vice versa.
import React from "react";
import ReactDOM from "react-dom/client";

import BookApp from "./BookApp.jsx";
import { ErrorBoundary, installPreloadRecovery } from "./lib/resilience.jsx";

// Analytics (PostHog) loads lazily after first paint — event wifi should never
// wait on a telemetry bundle.
const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 1500));
idle(() => import("./lib/analytics.js").then((m) => m.initAnalytics()).catch(() => {}));
installPreloadRecovery();

// The event hosts serve BookApp (Today · Add · Book). The legacy in-person
// surface stays at /legacy and /guest. InPersonApp is lazy — it's 186KB and
// 99% of users never need it; BookApp is always the fast path.
function wantsLegacy() {
  try {
    const p = window.location.pathname || "";
    return p === "/legacy" || p.startsWith("/legacy/")
        || p === "/guest" || p.startsWith("/guest/");
  } catch { return false; }
}

// The public, no-sign-in walkthrough lives at /demo. It's its own lazy chunk
// so the default BookApp path never downloads the guided-tour bundle.
function wantsDemo() {
  try {
    const p = window.location.pathname || "";
    return p === "/demo" || p.startsWith("/demo/");
  } catch { return false; }
}

function mountLazy(loader) {
  loader().then(({ default: App }) => {
    ReactDOM.createRoot(document.getElementById("root")).render(
      <React.StrictMode>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </React.StrictMode>
    );
  }).catch(() => {
    const el = document.getElementById("root");
    if (el) el.innerHTML =
      '<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:Inter,system-ui,sans-serif">' +
      '<button onclick="window.location.reload()" style="font-size:15px;padding:10px 22px;border-radius:999px;border:0.5px solid #d6dae1;background:#14171c;color:#fff;cursor:pointer">Reload</button></div>';
  });
}

if (wantsDemo()) {
  // Plain dynamic import so Vite code-splits DemoApp into its own hashed chunk
  // (loaded only on /demo) and rewrites the path for production.
  mountLazy(() => import("./DemoApp.jsx"));
} else if (wantsLegacy()) {
  // @vite-ignore prevents Vite from statically analysing this path and adding
  // InPersonApp to the preload graph — 99% of users are on BookApp and should
  // never download the legacy surface's 186KB chunk.
  const _legacy = /* @vite-ignore */ "./InPersonApp.jsx";
  import(/* @vite-ignore */ _legacy).then(({ default: InPersonApp }) => {
    ReactDOM.createRoot(document.getElementById("root")).render(
      <React.StrictMode>
        <ErrorBoundary>
          <InPersonApp />
        </ErrorBoundary>
      </React.StrictMode>
    );
  }).catch(() => {
    const el = document.getElementById("root");
    if (el) el.innerHTML =
      '<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:Inter,system-ui,sans-serif">' +
      '<button onclick="window.location.reload()" style="font-size:15px;padding:10px 22px;border-radius:999px;border:0.5px solid #d6dae1;background:#14171c;color:#fff;cursor:pointer">Reload</button></div>';
  });
} else {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <ErrorBoundary>
        <BookApp />
      </ErrorBoundary>
    </React.StrictMode>
  );
}
