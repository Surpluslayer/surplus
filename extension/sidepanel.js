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
const openProfileBtn = document.getElementById('open-profile');

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
  current = p;
  if (!p || !p.name) {
    ctx.classList.remove('show');
    return;
  }
  ctxName.textContent = p.name;
  ctxHeadline.textContent = p.headline || '';
  captureBtn.disabled = false;
  captureBtn.textContent = 'Capture to surplus';
  ctx.classList.add('show');
}

// Live updates pushed from the background relay as you browse LinkedIn.
chrome.runtime.onMessage.addListener((msg) => {
  console.log('[surplus] panel got message:', msg);
  if (msg?.type === 'surplus:profile:update') renderProfile(msg.profile);
});

// On open, ask the background for the last-seen profile.
chrome.runtime.sendMessage({ type: 'surplus:profile:get' }, (p) => {
  if (!chrome.runtime.lastError && p) renderProfile(p);
});

// Hand the captured person to the book. The book can listen for this
// postMessage to kick off its workflow (search / add contact / draft).
// Until the book wires up a handler, this is a no-op on its side; the
// button still gives the user feedback.
captureBtn.addEventListener('click', () => {
  if (!current) return;
  book.contentWindow?.postMessage(
    { type: 'surplus:capture', profile: current },
    BOOK_URL,
  );
  captureBtn.disabled = true;
  captureBtn.textContent = 'Sent to surplus ✓';
});

// Open the LinkedIn profile in a normal tab (handy from the panel).
openProfileBtn.addEventListener('click', () => {
  if (current?.url) chrome.tabs.create({ url: current.url });
});
