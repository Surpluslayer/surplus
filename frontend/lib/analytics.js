// PostHog analytics : product analytics + autocapture + session replay.
//
// The key is the PostHog *Project API Key* (phc_…), which is a publishable
// client-side key by design : it's safe to ship in the browser bundle. Swap
// it via POSTHOG_KEY below. Leaving the placeholder disables analytics
// cleanly (init is skipped), so the app still runs without a key.
//
// "See everything" config: autocapture (clicks/inputs), pageviews +
// pageleave, and session replay. NOTE: session replay also has to be turned
// ON in PostHog → Settings → Replay ("Record user sessions") for recordings
// to actually appear : the SDK flag below only permits it.
import posthog from "posthog-js";

// ── Replace this with your PostHog Project API key (phc_…) ──────────────
const POSTHOG_KEY = "phc_PASTE_YOUR_PROJECT_KEY_HERE";
const POSTHOG_HOST = "https://us.i.posthog.com";
const POSTHOG_UI_HOST = "https://us.posthog.com";
// ────────────────────────────────────────────────────────────────────────

let started = false;

function configured() {
  return !!POSTHOG_KEY && !POSTHOG_KEY.startsWith("phc_PASTE");
}

export function initAnalytics() {
  if (started || !configured()) return;
  started = true;
  posthog.init(POSTHOG_KEY, {
    api_host: POSTHOG_HOST,
    ui_host: POSTHOG_UI_HOST,
    autocapture: true,
    capture_pageview: true,
    capture_pageleave: true,
    capture_performance: true,
    // Permit session replay; actual recording is gated by the project's
    // Replay setting in the PostHog dashboard.
    disable_session_recording: false,
    persistence: "localStorage+cookie",
  });
}

// Link events to the signed-in user so you can segment demo vs real traffic.
export function identifyUser(user) {
  if (!started || !user) return;
  posthog.identify(String(user.id), {
    email: user.email || undefined,
    name: user.name || undefined,
    is_demo: !!user.is_demo,
    linkedin_connected: !!user.unipile_account_id,
  });
}

// Call on logout so the next user isn't merged into the previous identity.
export function resetAnalytics() {
  if (started) posthog.reset();
}

// Thin wrapper for explicit custom events.
export function capture(event, props) {
  if (started) posthog.capture(event, props);
}
