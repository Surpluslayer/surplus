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

// Capture a LinkedIn profile into surplus using the existing in-person flow:
// get-or-create a "LinkedIn" event, then /scan the profile (which resolves +
// drafts). Runs from the service worker so the session cookie is sent via the
// extension's host permission for event.surpluslayer.com.
async function captureProfile(profile) {
  const evRes = await fetch(`${BOOK_ORIGIN}/api/inperson/events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ label: 'LinkedIn', city: '' }),
  });
  if (!evRes.ok) throw new Error(`events ${evRes.status}`);
  const ev = await evRes.json();

  const scanRes = await fetch(`${BOOK_ORIGIN}/api/inperson/scan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
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
  const res = await fetch(
    `${BOOK_ORIGIN}/api/inperson/captures/${prospectId}/send`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
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
  const res = await fetch(
    `${BOOK_ORIGIN}/api/integrations/linkedin/status`,
    { credentials: 'include' },
  );
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json(); // {connected, account_id, status}
}

// Hand the li_at cookie to surplus to attach LinkedIn via Unipile.
async function connectLinkedInCookie(liAt, userAgent) {
  const res = await fetch(
    `${BOOK_ORIGIN}/api/integrations/linkedin/connect-cookie`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
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
    // Are we signed in to surplus with a REAL account? /me returns 200 with the
    // session cookie, 401 without. The cookie rides the fetch via our host
    // permission. A leftover demo session (anyone who opened the /demo
    // walkthrough in this browser) also returns 200 -- but with is_demo:true and
    // seeded sample data, not the user's real book. Treat demo as signed-OUT so
    // the panel shows its sign-in screen instead of loading the demo.
    fetch(`${BOOK_ORIGIN}/api/auth/me`, { credentials: 'include' })
      .then(async (r) => {
        if (!r.ok) return sendResponse({ authed: false });
        const me = await r.json().catch(() => null);
        sendResponse({ authed: !!(me && me.id && !me.is_demo) });
      })
      .catch(() => sendResponse({ authed: false }));
    return true; // async
  }
  if (msg?.type === 'surplus:signout') {
    // Clear any session on this origin before sending the user to sign in. The
    // case that matters: a leftover DEMO session (sample data) would otherwise
    // make the book tab re-open the demo instead of the real sign-in screen.
    // POST carries the cookie via our host permission; the Set-Cookie clears it
    // from the shared jar the panel iframe / book tab both use.
    fetch(`${BOOK_ORIGIN}/api/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    })
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
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
