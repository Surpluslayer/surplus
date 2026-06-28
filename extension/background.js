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

// Inject whenever a LinkedIn tab finishes (re)loading. This is the reliable
// path: it does not depend on the manifest content-script registration.
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete' && tab.url && /:\/\/[^/]*\.linkedin\.com\//.test(tab.url)) {
    inject(tabId);
  }
});

// Remember the last profile so a side panel opened *after* navigation can
// ask for it (the panel may not have been listening when it was scraped).
let lastProfile = null;

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  console.log('[surplus][bg] got', msg?.type, 'from', sender.tab?.url || 'extension');
  if (msg?.type === 'surplus:profile') {
    lastProfile = { ...msg.profile, tabId: sender.tab?.id, at: Date.now() };
    // Broadcast to any open side panel. Ignore "no receiver" errors.
    chrome.runtime
      .sendMessage({ type: 'surplus:profile:update', profile: lastProfile })
      .catch(() => {});
  } else if (msg?.type === 'surplus:profile:get') {
    sendResponse(lastProfile);
  }
  return true; // keep the message channel open for async sendResponse
});
