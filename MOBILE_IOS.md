# surplus → iOS app (App Store)

This turns the existing React/Vite frontend into a native iOS app with
**[Capacitor](https://capacitorjs.com/)** — no rewrite. Capacitor wraps the web
app in a native WebView, gives it an Xcode project you can sign and submit, and
lets you reach native APIs (camera, push, etc.) when you want them.

> **Why Capacitor and not React Native?** surplus already has a mature, polished
> web frontend (the phone-first `event.surpluslayer.com` "Book" surface). A
> React Native rewrite would throw most of that away. Capacitor reuses 100% of
> it and still ships a real `.ipa` to the App Store.

---

## What's already wired in this repo

| File | What it does |
|------|--------------|
| `frontend/capacitor.config.ts` | App id (`com.surpluslayer.app`), name, and the live-vs-bundled server toggle. |
| `frontend/package.json` scripts | `ios:add`, `ios:sync`, `ios:open`, `ios:assets`. |
| `frontend/package.json` deps | `@capacitor/core`, `ios`, `app`, `status-bar`, `splash-screen`, `cli`. |

The native `ios/` Xcode project is **not** in the repo yet — it's generated on a
Mac in step 2 below (it can't be created on Linux/CI).

---

## Prerequisites (all macOS-only)

- A **Mac** with **Xcode** (latest, from the App Store) + Command Line Tools.
- **CocoaPods**: `sudo gem install cocoapods` (or `brew install cocoapods`).
- An **Apple Developer Program** membership ($99/year) — required to run on a
  physical device and to submit to the App Store.
- Node 18+ and this repo cloned locally.

---

## Step 1 — install JS deps

```bash
cd frontend
npm install
```

## Step 2 — generate the native iOS project (first time only)

```bash
cd frontend
npm run build        # produces dist/ (Capacitor needs it present)
npm run ios:add      # creates frontend/ios/  (runs `cap add ios`)
```

Commit the new `frontend/ios/` folder. `.gitignore` already excludes the
generated/secret bits (`Pods/`, `build/`, provisioning profiles, …).

## Step 3 — app icon + splash screen

`frontend/public/surplus-logo.png` is the source art. Generate every iOS size:

```bash
cd frontend
npm run ios:assets   # uses npx @capacitor/assets (downloads on first run)
```

> This needs the native image lib `sharp`, which couldn't be installed in the
> sandboxed CI environment — it installs fine on a normal Mac. If it ever
> balks, the manual fallback is dragging icons into
> `ios/App/App/Assets.xcassets` in Xcode.

## Step 4 — run it

```bash
cd frontend
npm run ios:sync     # builds the web app + copies it into the iOS project
npm run ios:open     # opens ios/App/App.xcworkspace in Xcode
```

In Xcode: pick a simulator (or your plugged-in iPhone) and press **▶︎ Run**.

By default (see `capacitor.config.ts`) the app loads the **live phone surface**
`https://event.surpluslayer.com`. Because that's the same origin the backend
already serves, sign-in (the `surplus_session` cookie) and every API call work
with **zero code changes**.

---

## Step 5 — ship to the App Store

1. In Xcode → target **App** → **Signing & Capabilities**: select your Team and
   let Xcode manage signing. Set the Bundle Identifier to `com.surpluslayer.app`
   (must match `capacitor.config.ts` and your App Store Connect record).
2. Create the app in [App Store Connect](https://appstoreconnect.apple.com)
   with that same bundle id.
3. In Xcode: **Product → Destination → Any iOS Device**, then
   **Product → Archive**.
4. In the Organizer window: **Distribute App → App Store Connect → Upload**.
5. Back in App Store Connect: add screenshots, description, privacy details,
   then submit for review (or push to **TestFlight** first for beta testers).

Bump the build/version each submission in Xcode (**General → Identity**).

---

## Two server modes (important)

Set by `CAP_SERVER_URL` when you run `npm run ios:sync`:

### LIVE mode — default, recommended to start
The WebView loads `https://event.surpluslayer.com` directly. Same-origin, so
cookie auth + the whole existing app Just Work. The app needs a network
connection at launch (true of nearly every networked app).

Point at a different backend:
```bash
CAP_SERVER_URL="https://event.staging.surpluslayer.com" npm run ios:sync
```

### Bundled mode — ships the web assets inside the .ipa
```bash
CAP_SERVER_URL= npm run ios:sync   # empty value → no server.url → bundled
```
The app loads from `capacitor://localhost`, so assets are offline and launch is
instant. **But that origin is cross-origin to the backend**, which breaks the
current cookie session. Before using bundled mode the backend must do one of:

- **Token auth** (recommended): issue a bearer token at login, send it as an
  `Authorization: Bearer …` header from the app, stop relying on the cookie. The
  app would also need an absolute API base URL instead of the relative paths in
  `frontend/lib/api.js`.
- **Credentialed CORS**: set `allow_credentials=True` with an explicit
  `allow_origins=["capacitor://localhost", "https://localhost"]` (the wildcard
  `"*"` in `backend/main.py` is incompatible with credentials) and reissue the
  session cookie with `SameSite=None; Secure`.

Until then, **stay in LIVE mode** — it's fully functional today.

> **App Review note (Guideline 4.2):** Apple rejects apps that are just a
> website in a WebView with no native value. surplus has legitimate native hooks
> to lean on — the QR scan-to-connect flow (already using `jsqr`) maps cleanly to
> the native camera via `@capacitor/camera`, plus push notifications for
> follow-ups. Wiring at least one native capability before submission makes the
> review smooth.

---

## Suggested next steps (native polish)

- **Camera QR scan**: replace the web `getUserMedia` path with
  `@capacitor/camera` / a barcode plugin for a faster, native scanner.
- **Push notifications**: `@capacitor/push-notifications` for follow-up nudges.
- **Status bar / splash**: tune via `@capacitor/status-bar` and
  `@capacitor/splash-screen` (already installed).
- **Android**: the same project ships to Google Play — `npm i @capacitor/android`
  then `npx cap add android`. Everything above applies symmetrically.
