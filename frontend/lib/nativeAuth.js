// Native (iOS Capacitor) sign-in helpers.
//
// LinkedIn login can't complete inside the app's embedded WebView, and a cookie
// set during an external-browser login lands in the wrong cookie jar. So in the
// native app we run sign-in in the SYSTEM browser and the backend hands the
// session token back via the surplus://auth?token=... deep link. We then load
// /api/auth/mobile-adopt?token=... inside the WebView, which sets the
// surplus_session cookie there — authenticating the whole web app.
//
// On the web these helpers are inert (isNativeApp() is false), so the normal
// browser redirect flow is used unchanged.

function cap() {
  return typeof window !== "undefined" ? window.Capacitor : undefined;
}

export function isNativeApp() {
  const c = cap();
  return !!(c && typeof c.isNativePlatform === "function" && c.isNativePlatform());
}

function plugin(name) {
  return cap()?.Plugins?.[name];
}

// Wait for the surplus://auth?token=... deep link and resolve the token.
function waitForAuthDeepLink(timeoutMs = 180000) {
  const App = plugin("App");
  return new Promise((resolve) => {
    let handle;
    const timer = setTimeout(() => finish(null), timeoutMs);
    async function finish(token) {
      clearTimeout(timer);
      try {
        (await handle)?.remove?.();
      } catch {
        /* ignore */
      }
      resolve(token);
    }
    handle = App?.addListener?.("appUrlOpen", (evt) => {
      try {
        const u = new URL(evt.url);
        if (u.protocol === "surplus:") {
          finish(u.searchParams.get("token"));
        }
      } catch {
        /* not our link */
      }
    });
  });
}

// Core native OAuth runner: get a consent URL, open it in the SYSTEM browser,
// await the surplus://auth?token deep link, and adopt the session into the
// WebView. `getUrl` is a function returning a Promise<{url}>.
async function runNativeOAuth(getUrl) {
  const Browser = plugin("Browser");
  if (!Browser) return false; // plugin missing: let caller fall back

  const res = await getUrl();
  const url = res?.url;
  if (!url) throw new Error("no auth url");

  // Start listening BEFORE opening the browser so we can't miss the deep link.
  const tokenPromise = waitForAuthDeepLink();
  await Browser.open({ url });

  const token = await tokenPromise;
  try {
    await Browser.close();
  } catch {
    /* already closed */
  }
  if (!token) throw new Error("sign-in did not complete");

  // Adopt the session into the WebView (sets the cookie), then land in-app.
  window.location.assign(
    `/api/auth/mobile-adopt?token=${encodeURIComponent(token)}`,
  );
  return true;
}

// LinkedIn native sign-in (Unipile hosted-auth via the mobile-flagged start).
export async function nativeLinkedInLogin(api) {
  if (!isNativeApp()) return false;
  return runNativeOAuth(() => api.startLinkedinAuthMobile());
}

// Google / Microsoft native sign-in. `startFn(client)` is api.startGoogleAuth /
// api.startMicrosoftAuth, called with client="ios" so the backend deep-links
// the session token back instead of setting a web cookie.
export async function nativeOAuthLogin(startFn) {
  if (!isNativeApp()) return false;
  return runNativeOAuth(() => startFn("ios"));
}
