// Service worker: opens the side panel from the toolbar icon and relays the
// LinkedIn profile the content script reads to whatever side panel is open.

chrome.runtime.onInstalled.addListener(() => {
  // Clicking the toolbar icon opens the side panel for the current tab.
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((e) => console.warn('[surplus] setPanelBehavior', e));

  injectIntoOpenLinkedInTabs();
});

chrome.runtime.onStartup.addListener(injectIntoOpenLinkedInTabs);

function injectIntoOpenLinkedInTabs() {
  // Content scripts only auto-inject into pages loaded AFTER install. Inject
  // into any LinkedIn tabs that are already open so they work immediately.
  chrome.tabs.query({ url: 'https://*.linkedin.com/*' }, (tabs) => {
    for (const tab of tabs) {
      if (tab.id == null) continue;
      inject(tab.id);
    }
  });
}

function inject(tabId) {
  chrome.scripting
    .executeScript({ target: { tabId }, files: ['content-linkedin.js'] })
    .then(() => console.log('[surplus][bg] injected into tab', tabId))
    .catch((e) => console.warn('[surplus][bg] inject failed', tabId, e));
}

const LINKEDIN_RE = /:\/\/[^/]*\.linkedin\.com\//;

// Inject whenever a LinkedIn tab finishes (re)loading. This is the reliable
// path: it does not depend on the manifest content-script registration.
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete' && tab.url && LINKEDIN_RE.test(tab.url)) {
    inject(tabId);
  }
});

// Ensure the content script is present in a tab, then ask it to scrape now.
// Used when the panel opens or the user switches tabs, so the bar reflects
// whatever LinkedIn page is currently in front without waiting for a navigation.
function scanTab(tabId) {
  chrome.scripting
    .executeScript({ target: { tabId }, files: ['content-linkedin.js'] })
    .then(() => chrome.tabs.sendMessage(tabId, { type: 'surplus:rescan' }).catch(() => {}))
    .catch(() => {});
}

function scanActiveTab() {
  chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
    const tab = tabs[0];
    if (tab?.id != null && tab.url && LINKEDIN_RE.test(tab.url)) {
      scanTab(tab.id);
    } else {
      // Active tab isn't LinkedIn: clear the bar.
      lastProfile = null;
      chrome.runtime
        .sendMessage({ type: 'surplus:profile:update', profile: null })
        .catch(() => {});
    }
  });
}

// When the user switches tabs, refresh the bar to match the new active tab.
chrome.tabs.onActivated.addListener(scanActiveTab);

const BOOK_ORIGIN = 'https://event.surpluslayer.com';

// --- Single shared plugin session token ----------------------------------
// Chrome partitions the cookie jar for the embedded Book iframe (it's a
// third-party frame under the extension origin), so the iframe and the
// service worker's own fetches can resolve to different surplus accounts than
// the user's first-party web tab. To make every extension context resolve to
// ONE account we hold a single client="plugin" session token and:
//   - send it as `Authorization: Bearer` on all service-worker API calls, and
//   - replay it into the Book iframe via /api/auth/token-bootstrap (sidepanel.js)
// so the iframe adopts the SAME session in its partitioned cookie jar.
//
// The token is minted from whatever session the browser already has (the
// cookie that rides our host permission), so it can only ever represent the
// user who is actually signed in. Cached in chrome.storage.local; never logged.

let pluginToken = null; // in-memory cache; chrome.storage.local is the source of truth

async function loadPluginToken() {
  if (pluginToken) return pluginToken;
  try {
    const got = await chrome.storage.local.get('surplus_plugin_token');
    pluginToken = got?.surplus_plugin_token || null;
  } catch (_) {
    pluginToken = null;
  }
  return pluginToken;
}

async function savePluginToken(token) {
  pluginToken = token || null;
  try {
    if (token) await chrome.storage.local.set({ surplus_plugin_token: token });
    else await chrome.storage.local.remove('surplus_plugin_token');
  } catch (_) {
    /* storage best-effort */
  }
}

// Authorization header for a known token (empty object when we have none, so
// callers can spread it unconditionally).
function authHeader(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Mint a fresh plugin token from the current (cookie) session and cache it.
// Returns the token, or null if the browser isn't signed in (401).
async function mintPluginToken() {
  const res = await fetch(`${BOOK_ORIGIN}/api/auth/plugin/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  });
  if (!res.ok) {
    if (res.status === 401) await savePluginToken(null);
    return null;
  }
  const data = await res.json().catch(() => null);
  const token = data?.token || null;
  if (token) await savePluginToken(token);
  return token;
}

// URL to load in the Book iframe. With a plugin token, route through the
// same-origin token-bootstrap endpoint so the iframe adopts OUR session in its
// partitioned cookie jar (and lands on "/" after). Without a token, the bare
// origin (the SPA shows its own sign-in screen).
function bookUrlFor(token) {
  if (!token) return BOOK_ORIGIN;
  return (
    `${BOOK_ORIGIN}/api/auth/token-bootstrap` +
    `?token=${encodeURIComponent(token)}&next=${encodeURIComponent('/')}`
  );
}

// Best-effort authenticated fetch: attaches the cached plugin token as a Bearer
// header (and keeps credentials:'include' so the partitioned cookie still works
// as a fallback). On a 401 with a stale token, re-mint once from the cookie
// session and retry, so a rotated/expired plugin token self-heals.
async function authedFetch(path, opts = {}) {
  const token = await loadPluginToken();
  const doFetch = (tok) =>
    fetch(`${BOOK_ORIGIN}${path}`, {
      ...opts,
      credentials: 'include',
      headers: { ...(opts.headers || {}), ...authHeader(tok) },
    });
  let res = await doFetch(token);
  if (res.status === 401) {
    const fresh = await mintPluginToken();
    if (fresh) res = await doFetch(fresh);
  }
  return res;
}

// Capture a LinkedIn profile into surplus using the existing in-person flow:
// get-or-create a "LinkedIn" event, then /scan the profile (which resolves +
// drafts). Runs from the service worker so the session cookie is sent via the
// extension's host permission for event.surpluslayer.com.
async function captureProfile(profile) {
  const evRes = await authedFetch(`/api/inperson/events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: 'LinkedIn', city: '' }),
  });
  if (!evRes.ok) throw new Error(`events ${evRes.status}`);
  const ev = await evRes.json();

  const scanRes = await authedFetch(`/api/inperson/scan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      event_id: ev.event_id,
      linkedin_url: profile.url,
      source: 'link',
      name: profile.name || null,
      role: profile.headline || null,
    }),
  });
  if (!scanRes.ok) throw new Error(`scan ${scanRes.status}`);
  return scanRes.json();
}

// Fire the LinkedIn connect request (with note) + DM for a captured prospect.
// note/message override the composed draft; the backend routes warm vs cold.
async function sendCapture(prospectId, note, message) {
  const res = await authedFetch(
    `/api/inperson/captures/${prospectId}/send`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: note ?? null, message: message ?? null }),
    },
  );
  if (!res.ok) throw new Error(`send ${res.status}`);
  return res.json();
}

// --- LinkedIn connect-cookie (v4): one-tap LinkedIn auto-connect ---------
// Read the user's li_at session cookie from linkedin.com and hand it to surplus
// so it can attach their LinkedIn via Unipile -- skipping the slow hosted-auth
// flow. All surplus calls reuse BOOK_ORIGIN + the session cookie (credentials:
// 'include') exactly like captureProfile/sendCapture above, so they're
// authenticated as the signed-in surplus user.

// Read the LinkedIn auth cookie. Resolves null if the user isn't logged into
// LinkedIn (cookie absent) so callers can prompt them to log in first.
function getLinkedInCookie() {
  return new Promise((resolve) => {
    try {
      chrome.cookies.get(
        { url: 'https://www.linkedin.com', name: 'li_at' },
        (cookie) => {
          if (chrome.runtime.lastError || !cookie || !cookie.value) {
            resolve(null);
            return;
          }
          resolve(cookie.value);
        },
      );
    } catch (e) {
      console.warn('[surplus][bg] cookies.get failed', e);
      resolve(null);
    }
  });
}

// Is LinkedIn already connected to this surplus account? Call FIRST so we never
// create a second Unipile account (the backend dedup guard).
async function linkedinStatus() {
  const res = await authedFetch(`/api/integrations/linkedin/status`, {});
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json(); // {connected, account_id, status}
}

// Hand the li_at cookie to surplus to attach LinkedIn via Unipile.
async function connectLinkedInCookie(liAt, userAgent) {
  const res = await authedFetch(
    `/api/integrations/linkedin/connect-cookie`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ li_at: liAt, user_agent: userAgent || undefined }),
    },
  );
  if (!res.ok) {
    let detail = '';
    try {
      const j = await res.json();
      detail = j?.detail || j?.error || j?.message || '';
    } catch (_) {
      /* non-JSON body */
    }
    throw new Error(detail || `connect-cookie ${res.status}`);
  }
  return res.json(); // {connected, account_id, reused}
}

// Orchestrate the one-tap flow: check status -> read cookie -> connect.
// Returns {connected, reused?, alreadyConnected?, needLinkedInLogin?}.
async function linkedinConnectFlow(userAgent) {
  const status = await linkedinStatus();
  if (status?.connected) {
    return { connected: true, alreadyConnected: true };
  }
  const liAt = await getLinkedInCookie();
  if (!liAt) {
    return { connected: false, needLinkedInLogin: true };
  }
  const res = await connectLinkedInCookie(liAt, userAgent);
  return { connected: !!res?.connected, reused: !!res?.reused };
}

// Remember the last profile so a side panel opened *after* navigation can
// ask for it (the panel may not have been listening when it was scraped).
let lastProfile = null;

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Fire-and-forget messages: do NOT return true (no response is sent, and
  // keeping the channel open just produces a "channel closed" warning).
  if (msg?.type === 'surplus:profile') {
    lastProfile = { ...msg.profile, tabId: sender.tab?.id, at: Date.now() };
    chrome.runtime
      .sendMessage({ type: 'surplus:profile:update', profile: lastProfile })
      .catch(() => {});
    return;
  }
  if (msg?.type === 'surplus:profile:clear') {
    lastProfile = null;
    chrome.runtime
      .sendMessage({ type: 'surplus:profile:update', profile: null })
      .catch(() => {});
    return;
  }
  if (msg?.type === 'surplus:profile:get') {
    sendResponse(lastProfile); // synchronous response
    return;
  }
  if (msg?.type === 'surplus:scan-active') {
    scanActiveTab(); // panel opened: scrape whatever's in front right now
    return;
  }
  if (msg?.type === 'surplus:auth-check') {
    // Are we signed in to surplus with a REAL account? /me returns 200 with a
    // valid session (Bearer plugin token, or the cookie via our host
    // permission), 401 without. A leftover demo session (anyone who opened the
    // /demo walkthrough in this browser) also returns 200 -- but with
    // is_demo:true and seeded sample data, not the user's real book. Treat demo
    // as signed-OUT so the panel shows its sign-in screen instead of the demo.
    //
    // On success we ensure we hold a plugin token (mint one from the cookie
    // session if we don't yet) and hand the panel a token-bootstrap URL so the
    // embedded Book iframe adopts the SAME session in its partitioned jar.
    (async () => {
      try {
        // authedFetch self-mints a token on 401-with-stale-token; but first
        // load whatever we have so /me can resolve via Bearer when the cookie
        // jar is partitioned away.
        const r = await authedFetch(`/api/auth/me`, {});
        if (!r.ok) return sendResponse({ authed: false });
        const me = await r.json().catch(() => null);
        const authed = !!(me && me.id && !me.is_demo);
        if (!authed) return sendResponse({ authed: false });
        // Make sure we have a plugin token to replay into the iframe. /me
        // resolved, so the session is valid; mint if we don't already hold one.
        let token = await loadPluginToken();
        if (!token) token = await mintPluginToken();
        sendResponse({ authed: true, bookUrl: bookUrlFor(token) });
      } catch (_) {
        sendResponse({ authed: false });
      }
    })();
    return true; // async
  }
  if (msg?.type === 'surplus:book-url') {
    // The panel asks for the URL to load in the Book iframe. With a plugin
    // token we hand back the token-bootstrap URL so the iframe adopts our
    // session; otherwise the bare origin (signed-out -> shows sign-in).
    loadPluginToken().then((token) =>
      sendResponse({ url: bookUrlFor(token) }),
    );
    return true; // async
  }
  if (msg?.type === 'surplus:signout') {
    // Clear any session on this origin before sending the user to sign in. The
    // case that matters: a leftover DEMO session (sample data) would otherwise
    // make the book tab re-open the demo instead of the real sign-in screen.
    // POST carries the cookie via our host permission; the Set-Cookie clears it
    // from the shared jar the panel iframe / book tab both use.
    // Revoke + clear BOTH transports: the plugin Bearer token (so the iframe
    // bootstrap can't re-adopt it) and the partitioned cookie. authedFetch
    // sends the Bearer so the backend revokes the plugin session we hold.
    authedFetch(`/api/auth/logout`, { method: 'POST' })
      .then(async () => {
        await savePluginToken(null);
        sendResponse({ ok: true });
      })
      .catch(async () => {
        await savePluginToken(null);
        sendResponse({ ok: false });
      });
    return true; // async
  }
  if (msg?.type === 'surplus:linkedin:status') {
    linkedinStatus()
      .then((res) => sendResponse({ ok: true, res }))
      .catch((e) => {
        console.warn('[surplus][bg] linkedin status failed', e);
        sendResponse({ ok: false, error: String(e) });
      });
    return true; // async
  }
  if (msg?.type === 'surplus:linkedin:connect') {
    linkedinConnectFlow(msg.userAgent)
      .then((res) => {
        console.log('[surplus][bg] linkedin connect', res);
        sendResponse({ ok: true, res });
      })
      .catch((e) => {
        console.warn('[surplus][bg] linkedin connect failed', e);
        sendResponse({ ok: false, error: String(e) });
      });
    return true; // async
  }
  if (msg?.type === 'surplus:capture') {
    captureProfile(msg.profile)
      .then((res) => {
        console.log('[surplus][bg] captured', res);
        sendResponse({ ok: true, res });
      })
      .catch((e) => {
        console.warn('[surplus][bg] capture failed', e);
        sendResponse({ ok: false, error: String(e) });
      });
    return true; // async response: keep the channel open
  }
  if (msg?.type === 'surplus:send') {
    sendCapture(msg.prospectId, msg.note, msg.message)
      .then((res) => {
        console.log('[surplus][bg] sent', res);
        sendResponse({ ok: true, res });
      })
      .catch((e) => {
        console.warn('[surplus][bg] send failed', e);
        sendResponse({ ok: false, error: String(e) });
      });
    return true; // async response: keep the channel open
  }
});
