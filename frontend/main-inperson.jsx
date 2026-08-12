// Entry for the phone-first in-person surface (inperson.html). Served by
// FastAPI for the event.surpluslayer.com host. A dedicated entry means the phone
// bundle never pulls the desktop pipeline App, and vice versa.
import React from "react";
import ReactDOM from "react-dom/client";

import BookApp from "./BookApp.jsx";
import ObservePanel from "./components/ObservePanel.jsx";
import { ErrorBoundary, installPreloadRecovery } from "./lib/resilience.jsx";
import { api } from "./lib/api.js";
import { saveActiveEvent } from "./CaptureShared.jsx";

// Analytics (PostHog) loads lazily after first paint — event wifi should never
// wait on a telemetry bundle.
const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 1500));
idle(() => import("./lib/analytics.js").then((m) => m.initAnalytics()).catch(() => {}));
installPreloadRecovery();

// The event host serves BookApp (Today · Add · Book) for every path except the
// public /demo walkthrough below. The legacy in-person surface (/legacy, /guest
// → InPersonApp) has been removed — event.surpluslayer.com is Book-only now.

// The /demo link drops the visitor straight into the REAL Book surface as an
// isolated, seeded demo session (like the old www demo) — not a separate tour.
function wantsDemo() {
  try {
    const p = window.location.pathname || "";
    return p === "/demo" || p.startsWith("/demo/");
  } catch { return false; }
}

// Surplus Observe: the account-aware "open the hood" debugger
// (components/ObservePanel.jsx -> /api/observe/*). Requires a real signed-in
// session same as BookApp — no separate token/query-param auth, unlike the
// older RankingTrace.jsx demo surface.
function wantsObserve() {
  try {
    const p = window.location.pathname || "";
    return p === "/observe" || p.startsWith("/observe/");
  } catch { return false; }
}

function mountBook() {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <ErrorBoundary>
        <BookApp />
      </ErrorBoundary>
    </React.StrictMode>
  );
}

// Side by side with the REAL, unmodified Surplus product -- not a
// standalone replacement page. BookApp mounts exactly as it does on every
// other path (same session, same behavior; nothing about it is changed for
// this or any other user, per this feature's own "do not modify the
// existing product" constraint). ip-root already self-constrains to a
// 430px phone-frame width (CaptureShared.jsx's IP_CSS), so it renders as a
// natural left column; Observe fills the remaining width as an independent
// right-hand panel with its own object picker -- there is no live
// click-through wiring FROM Book INTO Observe yet (that would require
// touching BookApp.jsx itself), so pick the same lawyer/contact in both.
function ObserveSplitView() {
  return (
    <div style={{ display: "flex", minHeight: "100dvh", background: "#f4f5f7" }}>
      <div style={{ flex: "0 0 auto" }}>
        <BookApp />
      </div>
      <div style={{ flex: "1 1 auto", minWidth: 0, borderLeft: "1px solid #e6e8ee",
                    overflowY: "auto" }}>
        <ObservePanel />
      </div>
    </div>
  );
}

function mountObserve() {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <ErrorBoundary>
        <ObserveSplitView />
      </ErrorBoundary>
    </React.StrictMode>
  );
}

if (wantsObserve()) {
  mountObserve();
} else if (wantsDemo()) {
  // Start the demo session first (mints an isolated demo user + cookie + seed)
  // so BookApp's first /me + /book/today calls are authenticated, then mount
  // Book. BookApp shows the "exploring with sample data / sign in" banner for
  // demo users. .finally so a start hiccup still renders (Book gates to sign-in
  // if there's no session).
  api.demoStart()
    .then((r) => {
      // Pre-select the demo's seeded event as the active in-person event so the
      // Add flow captures into a valid, OWNED event. Without this the Add screen
      // falls back to a stale/foreign sessionStorage event and every capture
      // 404s ("Event not found") for the fresh demo user.
      if (r && r.event && r.event.event_id) saveActiveEvent(r.event);
    })
    .catch(() => {})
    .finally(mountBook);
} else {
  mountBook();
}
