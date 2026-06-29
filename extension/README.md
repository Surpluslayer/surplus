# surplus Chrome extension

A Chrome side panel that pins your surplus **book** (the relationship CRM at
`event.surpluslayer.com`) beside whatever you're browsing, and reads the
LinkedIn profile you're viewing so you can watch your relationship workflows
fire in real time.

## Load it (unpacked, dev)

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top-right)
3. **Load unpacked** -> select this `extension/` folder
4. Pin the surplus icon, click it -> the side panel opens with your book
5. Visit a LinkedIn profile (`linkedin.com/in/...`) -> the context bar at the
   top of the panel shows who you're viewing

## Pieces

- `manifest.json` - MV3 config (side panel, LinkedIn content script, header rule)
- `sidepanel.html` / `sidepanel.js` - the panel: book iframe + live context bar
- `content-linkedin.js` - scrapes name/headline/location off LinkedIn profiles
- `background.js` - relays the scraped profile to the open side panel
- `rules.json` - strips `x-frame-options` on surpluslayer.com sub-frames (so the
  book can be embedded even if a sub-route sets it)

## Known open items

- **Embedded login:** the surplus session cookie is `SameSite=Lax`, which a
  browser will not send in a cross-site iframe. The book may render
  logged-out inside the panel. Fix is a backend change to `SameSite=None;
  Secure` on the session cookie (affects the deployed web app, so get sign-off).
- **Capture button:** currently `postMessage`s the profile to the book; the
  book has no handler yet, so it is a stub. Next step is either a book-side
  listener or a direct surplus API call.
- **LinkedIn selectors** are best-effort and may need updating when LinkedIn
  changes its DOM.
