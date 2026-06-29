import type { CapacitorConfig } from "@capacitor/cli";

// surplus · iOS native shell (Capacitor)
// ---------------------------------------------------------------------------
// This wraps the EXISTING Vite/React frontend in a native iOS app for the App
// Store — no rewrite. There are two ways to run it, switched by the
// CAP_SERVER_URL env var read at `cap sync` time:
//
//  • LIVE  (default) — the native WebView loads the deployed phone-first
//    surface (event.surpluslayer.com) directly. Because that's the SAME ORIGIN
//    the backend already serves, the existing `surplus_session` cookie and
//    every relative `/api/...` call in lib/api.js keep working UNCHANGED. This
//    is the fastest route to a build that actually logs in and runs. It needs
//    a network connection at launch (like most networked apps).
//
//  • BUNDLED — run `CAP_SERVER_URL= npm run ios:sync` (empty value) to ship the
//    Vite `dist/` assets INSIDE the .ipa. The app then loads from
//    capacitor://localhost, which is CROSS-ORIGIN to the backend. Cookie auth
//    will NOT flow in that mode until the backend moves to token auth (or
//    enables credentialed CORS for the capacitor origin + SameSite=None
//    cookies). See MOBILE_IOS.md → "Bundled mode" before flipping this.
//
// To point at a staging/preview backend, export CAP_SERVER_URL before syncing,
// e.g.  CAP_SERVER_URL="https://event.staging.surpluslayer.com" npm run ios:sync
// ---------------------------------------------------------------------------

const liveUrl = process.env.CAP_SERVER_URL ?? "https://event.surpluslayer.com";

const config: CapacitorConfig = {
  appId: "com.surpluslayer.app",
  appName: "surplus",
  // Required even in LIVE mode: `cap` copies this folder into the native
  // project as the offline fallback shell. Build it with `npm run build`.
  webDir: "dist",
  ios: {
    // Let the WebView lay out under the status bar / home indicator so the
    // app's own safe-area CSS (already present in inperson.html via
    // viewport-fit=cover) controls the insets.
    contentInset: "always",
  },
  // In LIVE mode, hand the WebView the deployed origin. Empty string → omit the
  // server block entirely so Capacitor serves the bundled dist/ assets instead.
  //
  // allowNavigation: keep the backend's OWN host inside the native WebView so
  // server-side redirects (e.g. the demo /api/demo/enter → /book hop, or the
  // LinkedIn hosted-auth return) don't get kicked out to an external Safari
  // sheet — which would drop the just-set session cookie and break the flow.
  // Derived from CAP_SERVER_URL so it tracks whatever backend you point at.
  ...(liveUrl
    ? {
        server: {
          url: liveUrl,
          cleartext: false,
          allowNavigation: [new URL(liveUrl).hostname],
        },
      }
    : {}),
};

export default config;
