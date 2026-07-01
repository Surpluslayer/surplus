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

// Run LinkedIn sign-in natively. Returns true if it handled the flow (native),
// false on web (caller should fall back to the normal redirect).
export async function nativeLinkedInLogin(api) {
  if (!isNativeApp()) return false;
  const Browser = plugin("Browser");
  if (!Browser) return false; // plugin missing: let caller fall back

  // 1. Mint a mobile-flagged hosted-auth link.
  const { url } = await api.startLinkedinAuthMobile();
  if (!url) throw new Error("no auth url");

  // 2. Start listening for the deep link, then open login in the system browser.
  const tokenPromise = waitForAuthDeepLink();
  await Browser.open({ url });

  // 3. Wait for the token, close the browser.
  const token = await tokenPromise;
  try {
    await Browser.close();
  } catch {
    /* already closed */
  }
  if (!token) throw new Error("sign-in did not complete");

  // 4. Adopt the session into the WebView (sets the cookie), then land in-app.
  window.location.assign(
    `/api/auth/mobile-adopt?token=${encodeURIComponent(token)}`,
  );
  return true;
}
