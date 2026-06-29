// Reads the LinkedIn profile you're currently viewing and reports it to the
// surplus side panel. LinkedIn is a single-page app, so we watch for URL
// changes and re-scrape after the new page renders.
//
// Wrapped in a guarded IIFE: the background may executeScript this file into a
// tab more than once, and bare top-level `let`/`function` would throw
// "already declared" on re-injection. The guard makes re-injection a no-op.
(function () {
  if (window.__surplusLoaded) return;
  window.__surplusLoaded = true;
  console.log('[surplus] content script loaded on', location.href);

  function text(el) {
    return (el?.innerText || el?.textContent || '').trim();
  }

  function meta(prop) {
    const el = document.querySelector(
      `meta[property="${prop}"], meta[name="${prop}"]`,
    );
    return el?.getAttribute('content')?.trim() || null;
  }

  function titleName() {
    // The tab title DOES update on LinkedIn's client-side navigation, unlike
    // the og:title meta tag. Format: "(12) Sonia Kastner | LinkedIn".
    return (
      document.title
        .replace(/^\(\d+\+?\)\s*/, '')
        .replace(/\s*[|｜].*$/, '')
        .trim() || null
    );
  }

  function domName() {
    // Visible profile name: the h1 in the main column. Scope to <main> first to
    // avoid picking up an h1 from the feed/sidebar.
    const t = text(document.querySelector('main h1'));
    if (t) return t;
    for (const h of document.querySelectorAll('h1')) {
      const v = text(h);
      if (v) return v;
    }
    return null;
  }

  // Generic tab titles LinkedIn shows on non-profile views or mid-transition.
  // If we see one of these, it's not a person's name: keep waiting.
  const GENERIC = new Set([
    'linkedin', 'feed', 'notifications', 'messaging', 'search',
    'my network', 'jobs', 'home', 'profile',
  ]);

  function isGeneric(n) {
    return !n || GENERIC.has(n.trim().toLowerCase());
  }

  function parseProfile() {
    const url = location.href;
    if (!/linkedin\.com\/in\//.test(url)) return null;

    // Prefer the visible profile h1 (always current). Fall back to the tab
    // title only if it's a real name, not a generic view label. og:title is
    // deliberately NOT used: it goes stale on LinkedIn's client routing.
    let name = domName();
    if (isGeneric(name)) {
      const t = titleName();
      name = isGeneric(t) ? null : t;
    }
    if (!name) return null; // mid-transition: report() will retry

    const headline =
      text(document.querySelector('.text-body-medium.break-words')) ||
      text(document.querySelector('.text-body-medium')) ||
      null;
    const location_ =
      text(document.querySelector('.text-body-small.inline.t-black--light.break-words')) ||
      null;

    return { url, name, headline, location: location_, source: 'linkedin' };
  }

  let lastReportedUrl = '';

  function report(attempt = 0) {
    const profile = parseProfile();
    if (profile) {
      console.log('[surplus] scraped:', profile);
      chrome.runtime
        .sendMessage({ type: 'surplus:profile', profile })
        .catch(() => {});
    } else if (attempt < 10) {
      setTimeout(() => report(attempt + 1), 700);
    } else {
      console.log('[surplus] gave up. diagnostics:', {
        title: document.title,
        ogTitle: meta('og:title'),
        h1count: document.querySelectorAll('h1').length,
      });
    }
  }

  function clearBar() {
    chrome.runtime.sendMessage({ type: 'surplus:profile:clear' }).catch(() => {});
  }

  function checkUrl() {
    if (location.href === lastReportedUrl) return;
    lastReportedUrl = location.href;
    if (/linkedin\.com\/in\//.test(location.href)) {
      // Going to another profile: keep the current person shown and just
      // re-scrape, so the bar updates in place (no flash). Settle-delay lets
      // LinkedIn swap the DOM/title first so we don't read the old person.
      setTimeout(report, 600);
    } else {
      // Left profiles entirely (feed, search, etc.): clear the bar.
      clearBar();
    }
  }

  // On-demand rescan (e.g. when the side panel opens or the tab is focused).
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.type === 'surplus:rescan') {
      if (/linkedin\.com\/in\//.test(location.href)) report();
      else clearBar();
    }
  });

  // Poll for client-side navigations (pushState doesn't fire a load event).
  setInterval(checkUrl, 1000);
  checkUrl();
})();
