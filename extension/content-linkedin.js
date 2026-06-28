// Reads the LinkedIn profile you're currently viewing and reports it to the
// surplus side panel. LinkedIn is a single-page app, so we watch for URL
// changes and re-scrape after the new page renders.

function text(el) {
  return (el?.innerText || el?.textContent || '').trim();
}

function meta(prop) {
  const el = document.querySelector(
    `meta[property="${prop}"], meta[name="${prop}"]`,
  );
  return el?.getAttribute('content')?.trim() || null;
}

function parseProfile() {
  const url = location.href;
  if (!/linkedin\.com\/in\//.test(url)) return null;

  // Primary source: og:title, which LinkedIn renders into the page head as
  // "Name - Headline - Company | LinkedIn" (or just "Name | LinkedIn").
  // This avoids LinkedIn's constantly-changing CSS class names.
  let name = null;
  let headline = null;

  const ogTitle = meta('og:title');
  if (ogTitle) {
    const cleaned = ogTitle.replace(/\s*[|｜]\s*LinkedIn\s*$/i, '').trim();
    const parts = cleaned.split(' - ');
    name = parts[0]?.trim() || null;
    if (parts.length > 1) headline = parts.slice(1).join(' - ').trim();
  }

  // Fallbacks for name: a visible h1, then the tab title.
  if (!name) {
    for (const h of document.querySelectorAll('h1')) {
      const t = text(h);
      if (t) { name = t; break; }
    }
  }
  if (!name) {
    name = document.title
      .replace(/^\(\d+\+?\)\s*/, '')
      .replace(/\s*[|｜].*$/, '')
      .trim() || null;
  }

  // Headline fallback: og:description, then the body-medium block.
  if (!headline) {
    headline =
      meta('og:description') ||
      text(document.querySelector('.text-body-medium.break-words')) ||
      text(document.querySelector('.text-body-medium')) ||
      null;
  }

  const location_ =
    text(document.querySelector('.text-body-small.inline.t-black--light.break-words')) ||
    null;

  if (!name) return null;
  return { url, name, headline, location: location_, source: 'linkedin' };
}

let lastReportedUrl = '';

function report(attempt = 0) {
  const profile = parseProfile();
  if (profile) {
    console.log('[surplus] scraped:', profile);
    chrome.runtime
      .sendMessage({ type: 'surplus:profile', profile })
      .then(() => console.log('[surplus] sent profile to extension'))
      .catch((e) => console.warn('[surplus] sendMessage failed', e));
  } else if (attempt < 10) {
    // Profile DOM may still be lazy-loading; retry with backoff.
    setTimeout(() => report(attempt + 1), 700);
  } else {
    console.log('[surplus] gave up. diagnostics:', {
      title: document.title,
      ogTitle: meta('og:title'),
      h1count: document.querySelectorAll('h1').length,
    });
  }
}

function checkUrl() {
  if (location.href !== lastReportedUrl) {
    lastReportedUrl = location.href;
    report();
  }
}

function startSurplus() {
  // Poll for client-side navigations (pushState doesn't fire a load event).
  setInterval(checkUrl, 1000);
  checkUrl();
}

// Guard against double-injection (manifest content-script + background inject).
if (window.__surplusLoaded) {
  console.log('[surplus] already loaded, skipping');
} else {
  window.__surplusLoaded = true;
  console.log('[surplus] content script loaded on', location.href);
  startSurplus();
}
