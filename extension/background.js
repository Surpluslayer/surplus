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
});
