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
const POSTHOG_KEY = "phc_tepkkBdxdHbsTNU6mXygDzgRfTYHYWWNKWNo6sNrGUiB";
const POSTHOG_HOST = "https://us.i.posthog.com";
const POSTHOG_UI_HOST = "https://us.posthog.com";
// ────────────────────────────────────────────────────────────────────────

let started = false;

function configured() {
  return !!POSTHOG_KEY && !POSTHOG_KEY.startsWith("phc_PASTE");
}

// ── Runtime platform detection ────────────────────────────────────────────
// The SAME deployed web app (event.surpluslayer.com) runs in three runtimes:
//   • web       — a normal browser tab / standalone
//   • ios       — wrapped in a Capacitor native WebView (the iOS App-Store app)
//   • extension — loaded as the iframe inside the Chrome side-panel extension
// PostHog's built-in `$lib` is always "web" in all three (it's the JS SDK), so
// we attach an explicit `platform` super-property to distinguish them.
const PLATFORM_KEY = "surplus_platform"; // sessionStorage / URL-param key

// The extension can't set window globals on this origin, so it hands us an
// explicit hint via the iframe URL (?surplus_platform=extension). We read it
// once and persist in sessionStorage so it survives in-app navigations.
function readPlatformHint() {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get(PLATFORM_KEY);
    if (fromUrl) {
      try { sessionStorage.setItem(PLATFORM_KEY, fromUrl); } catch { /* no-op */ }
      return fromUrl;
    }
    const stored = sessionStorage.getItem(PLATFORM_KEY);
    if (stored) return stored;
  } catch { /* no-op */ }
  return null;
}

// Heuristic fallback for the extension iframe when no explicit hint is present:
// we're framed (self !== top) and the embedder is a chrome-extension. The
// ancestorOrigins / referrer reads are best-effort (cross-origin frames may
// hide them), hence the explicit URL hint above is the primary signal.
function looksLikeExtensionFrame() {
  try {
    if (window.self === window.top) return false;
    const ao = window.location.ancestorOrigins;
    if (ao) {
      for (let i = 0; i < ao.length; i++) {
        if (String(ao[i]).startsWith("chrome-extension://")) return true;
      }
    }
    if (document.referrer && document.referrer.startsWith("chrome-extension://")) {
      return true;
    }
  } catch { /* no-op */ }
  return false;
}

export function detectPlatform() {
  const hint = readPlatformHint();
  if (hint === "extension" || hint === "ios" || hint === "web") return hint;
  // iOS: Capacitor injects a global in the native WebView.
  try {
    const cap = window.Capacitor;
    if (cap && (typeof cap.isNativePlatform !== "function" || cap.isNativePlatform())) {
      return "ios";
    }
  } catch { /* no-op */ }
  if (looksLikeExtensionFrame()) return "extension";
  return "web";
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
  // Tag EVERY event (autocapture, pageviews, custom) with the runtime platform
  // so web / ios / extension traffic is distinguishable in PostHog. $lib stays
  // "web" for all three (same JS SDK); `platform` is the discriminator.
  try { posthog.register({ platform: detectPlatform() }); } catch { /* no-op */ }
  // Publish a synchronous tracking hook so surfaces that don't statically import
  // this module (e.g. BookApp, to keep PostHog off their critical bundle) can
  // fire events at click time via window.__surplusTrack without an async import.
  try { window.__surplusTrack = capture; } catch { /* no-op */ }
}

// Link events to the signed-in user so you can segment demo vs real traffic.
export function identifyUser(user) {
  if (!started || !user) return;
  posthog.identify(String(user.id), {
    email: user.email || undefined,
    name: user.name || undefined,
    is_demo: !!user.is_demo,
    linkedin_connected: !!user.unipile_account_id,
    platform: detectPlatform(),
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
