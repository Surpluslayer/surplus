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

// The event host serves BookApp (Today · Add · Book) for every path except the
// public /demo walkthrough below. The legacy in-person surface (/legacy, /guest
// → InPersonApp) has been removed — event.surpluslayer.com is Book-only now.

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
} else {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <ErrorBoundary>
        <BookApp />
      </ErrorBoundary>
    </React.StrictMode>
  );
}
