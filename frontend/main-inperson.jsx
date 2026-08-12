// Entry for the phone-first in-person surface (inperson.html). Served by
// FastAPI for the event.surpluslayer.com host. A dedicated entry means the phone
// bundle never pulls the desktop pipeline App, and vice versa.
import React, { useState, useCallback } from "react";
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
// existing product" constraint).
//
// The left column's width is PINNED to BOOK_COLUMN_PX rather than left to
// `flex: 0 0 auto`. With auto, the column sizes to its content, and
// .ip-root (CaptureShared.jsx's IP_CSS) is `width:100%; max-width:430px` --
// a percentage width against an auto-sized flex item resolves to
// fit-content, so any screen that renders something narrow (the loading
// spinner, the sign-in bounce) collapsed the whole column and the layout
// jumped on every screen change. A fixed basis is independent of whatever
// BookApp happens to be rendering, so the frame stays put.
//
// Each column scrolls independently (own height + overflowY) so paging
// through a long trace on the right doesn't scroll the product off-screen
// on the left -- the two panes are meant to be read against each other.
const BOOK_COLUMN_PX = 430;  // matches .ip-root's max-width

// Click delegation: Observe has no navigation of its own (components/
// ObservePanel.jsx). Its subject is whatever was last clicked in the real
// Book product to its left. BookApp.jsx carries additive data-observe-type
// / data-observe-id / data-observe-contact-id / data-observe-name
// attributes on its card elements (updates, needs-outreach, roster rows,
// the relationship detail screen, ask-bar results) -- plain HTML
// attributes nothing else reads, so this changes none of BookApp's own
// behavior. ONE delegated listener here reads them; BookApp never calls
// into Observe directly.
//
// Capture phase (onClickCapture, not onClick): fires before any handler
// BookApp itself attaches to the same click, so this always sees the
// click even if a future BookApp change were to stopPropagation() on it --
// this listener only READS the event, never calls preventDefault/
// stopPropagation, so BookApp's own handling is completely unaffected.
function ObserveSplitView() {
  const [selection, setSelection] = useState(null);

  const handleBookClick = useCallback((e) => {
    const el = e.target.closest && e.target.closest("[data-observe-type]");
    if (!el) return;
    const id = el.getAttribute("data-observe-id");
    if (!id) return;
    setSelection({
      type: el.getAttribute("data-observe-type"),
      id,
      // No `|| id` fallback: `id` (data-observe-id) is an INTERACTION id on
      // a signal card, not a contact id -- falling back to it would trace
      // the wrong entity with no indication anything went wrong. Every real
      // data-observe-* site in BookApp.jsx sets data-observe-contact-id
      // explicitly; a missing one means "no contact to trace", which
      // ObservePanel already handles (it just doesn't open a stream).
      contactId: el.getAttribute("data-observe-contact-id") || null,
      name: el.getAttribute("data-observe-name") || null,
      at: Date.now(),
    });
  }, []);

  return (
    <div style={{ display: "flex", height: "100dvh", overflow: "hidden",
                  background: "#f4f5f7" }}>
      <div style={{ flex: `0 0 ${BOOK_COLUMN_PX}px`, width: BOOK_COLUMN_PX,
                    height: "100dvh", overflowY: "auto",
                    // `transform` on this wrapper creates a new CSS containing
                    // block for any `position: fixed` descendant -- BookApp
                    // uses position:fixed for its compose/send modal (built
                    // for a full-viewport phone-first surface), which without
                    // this escaped the 430px column and covered the whole
                    // split view including Observe. translateZ(0) is a
                    // no-op transform chosen purely for this containment
                    // side effect; nothing about BookApp itself changes.
                    transform: "translateZ(0)" }}
           onClickCapture={handleBookClick}>
        <BookApp />
      </div>
      <div style={{ flex: "1 1 auto", minWidth: 0, height: "100dvh", overflowY: "auto",
                    borderLeft: "1px solid #e6e8ee" }}>
        <ObservePanel selection={selection} />
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
