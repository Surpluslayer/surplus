// Side panel logic: load the surplus book in the iframe, and keep a live
// "who you're looking at" context bar in sync with the LinkedIn content script.

// The relationship/book surface. (surplus serves surfaces by host:
// event.surpluslayer.com -> the phone-first book; the apex -> the desktop
// prospecting pipeline.) Override via chrome.storage if needed later.
const BOOK_URL = 'https://event.surpluslayer.com';

const book = document.getElementById('book');
const loading = document.getElementById('loading');
const ctx = document.getElementById('context');
const ctxName = document.getElementById('ctx-name');
const ctxHeadline = document.getElementById('ctx-headline');
const captureBtn = document.getElementById('capture');

let current = null; // the profile currently shown in the context bar

book.src = BOOK_URL;
book.addEventListener('load', () => {
  loading.style.display = 'none';
});

document.getElementById('reload').addEventListener('click', () => {
  loading.style.display = 'flex';
  book.src = BOOK_URL;
});

function renderProfile(p) {
  current = p && p.name ? p : null;
  // The bar is ALWAYS present (fixed footprint) to avoid layout shift; we only
  // swap its contents. Empty state = muted placeholder, Capture hidden.
  if (!current) {
    ctx.classList.add('empty');
    ctxName.textContent = 'Open a LinkedIn profile';
    ctxHeadline.textContent = '';
    captureBtn.disabled = true;
    return;
  }
  ctx.classList.remove('empty');
  ctxName.textContent = current.name;
  ctxHeadline.textContent = current.headline || '';
  captureBtn.disabled = false;
  captureBtn.textContent = 'Capture to surplus';
}

// Live updates pushed from the background relay as you browse LinkedIn.
chrome.runtime.onMessage.addListener((msg) => {
  console.log('[surplus] panel got message:', msg);
  if (msg?.type === 'surplus:profile:update') renderProfile(msg.profile);
});

// On open, show any cached profile immediately, then actively rescan the
// current tab so the bar reflects the LinkedIn page that's in front right now.
chrome.runtime.sendMessage({ type: 'surplus:profile:get' }, (p) => {
  if (!chrome.runtime.lastError && p) renderProfile(p);
});
chrome.runtime.sendMessage({ type: 'surplus:scan-active' });

// Capture the person into surplus via the background service worker (which
// calls the in-person scan API with the session cookie). On success, reload
// the book so the fresh capture + draft show up.
captureBtn.addEventListener('click', () => {
  if (!current) return;
  captureBtn.disabled = true;
  captureBtn.textContent = 'Capturing…';
  chrome.runtime.sendMessage(
    { type: 'surplus:capture', profile: current },
    (resp) => {
      if (chrome.runtime.lastError || !resp?.ok) {
        captureBtn.disabled = false;
        captureBtn.textContent = 'Retry capture';
        console.warn(
          '[surplus] capture failed',
          chrome.runtime.lastError || resp?.error,
        );
        return;
      }
      captureBtn.textContent = 'Captured ✓';
      // Show the new capture/draft in the book.
      loading.style.display = 'flex';
      book.src = BOOK_URL;
    },
  );
});
