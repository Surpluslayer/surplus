// Shared resilience plumbing for the SPA entries: a deploy, a flaky network,
// or a render crash must never strand a live phone on a raw error.
//
// Two layers:
//  - installPreloadRecovery(): a deploy replaces the hashed chunk files; a tab
//    that loaded the old HTML then fails to lazy-import a chunk that no longer
//    exists. Vite surfaces that as "vite:preloadError" — reload once to pick up
//    the new build instead of dying mid-navigation.
//  - <ErrorBoundary>: any uncaught render/runtime throw (e.g. an
//    engine-specific DOMException) renders a friendly reload card instead of a
//    white screen or a cryptic one-liner in the feed.
import React from "react";

const RELOADED_KEY = "surplus_preload_reloaded";

export function installPreloadRecovery() {
  window.addEventListener("vite:preloadError", (event) => {
    // Reload at most once per session so a genuinely broken build can't
    // reload-loop the phone.
    let already = false;
    try {
      already = sessionStorage.getItem(RELOADED_KEY) === "1";
      sessionStorage.setItem(RELOADED_KEY, "1");
    } catch { /* private mode : still reload, just without the guard */ }
    if (!already) {
      event.preventDefault(); // suppress the throw : we're handling it
      window.location.reload();
    }
  });
}

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { crashed: false };
  }
  static getDerivedStateFromError() { return { crashed: true }; }
  componentDidCatch(error, info) {
    try { console.error("ErrorBoundary:", error, info?.componentStack); } catch {}
  }
  render() {
    if (!this.state.crashed) return this.props.children;
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center",
                    justifyContent: "center", padding: 24,
                    fontFamily: "'Inter',system-ui,sans-serif", textAlign: "center" }}>
        <div>
          <p style={{ fontSize: 17, fontWeight: 500, margin: "0 0 6px" }}>
            Something went wrong
          </p>
          <p style={{ fontSize: 14, color: "#5b6472", margin: "0 0 16px" }}>
            Reload to pick up where you left off.
          </p>
          <button onClick={() => window.location.reload()}
                  style={{ fontSize: 15, fontWeight: 500, padding: "10px 22px",
                           borderRadius: 999, border: "0.5px solid #d6dae1",
                           background: "#14171c", color: "#fff", cursor: "pointer" }}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
