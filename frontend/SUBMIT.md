# surplus iOS — submission checklist

How to get the Capacitor iOS app (`com.surpluslayer.app`) from this repo onto the
App Store. Setup/regeneration lives in [MOBILE_IOS.md](./MOBILE_IOS.md); this file
is just the ship path.

## Toolchain requirement (hard gate)

As of **Apple's April 28, 2026 rule**, every App Store upload must be built with
**Xcode 26 or later** against the **iOS 26 SDK**. Older Xcode (e.g. 16.x) is
rejected at upload. This repo's `ios/` project was generated and last built with
**Xcode 26.6** on **macOS Tahoe 26.5.1** — that satisfies the rule.

- Apple's notice: https://developer.apple.com/news/upcoming-requirements/
- Xcode/macOS compatibility: https://developer.apple.com/xcode/system-requirements/

## One-time prerequisites

1. **Apple Developer Program** membership ($99/yr). A free Apple ID can run the
   app on a simulator or a personal device, but **cannot upload** to App Store
   Connect. Decide whether this is enrolled under a **surpluslayer / company**
   Apple ID rather than a personal one.
2. **iOS Simulator runtime** — Xcode 26 ships without bundled simulators.
   Install one with:
   ```bash
   xcodebuild -downloadPlatform iOS      # ~8.5 GB, installs iOS 26.x simulator
   ```
3. A **publicly hosted privacy policy URL** (draft text in `../store/privacy-policy.md`).

## Build & run locally (sanity check before submitting)

```bash
cd frontend
npm install
npm run build          # produces dist/ (Capacitor copies this into the native shell)
npm run ios:sync       # cap sync + pod install
npm run ios:open       # opens ios/App/App.xcworkspace in Xcode
```

In Xcode: pick a simulator (e.g. *iPhone 16 Pro*, iOS 26.x) and hit **▶ Run**.
The app loads the live `event.surpluslayer.com` surface in a native WebView, so
login + the booking flow should work as-is (LIVE mode — see `capacitor.config.ts`).

## Signing

App target → **Signing & Capabilities** → check **Automatically manage signing**
→ select your **Team**. Bundle id is already `com.surpluslayer.app`.

## Archive & upload

1. Device target dropdown → **Any iOS Device (arm64)**.
2. **Product → Archive**.
3. In the Organizer that opens → **Distribute App → App Store Connect → Upload**.

## App Store Connect

1. At https://appstoreconnect.apple.com create the app record; register bundle id
   `com.surpluslayer.app`.
2. Fill metadata — name, description (draft in `../store/listing.md`), screenshots,
   and the **privacy policy URL**.
3. Attach the uploaded build, answer the review questions, **Submit for Review**
   (typically 1–3 days).

## Notes

- `Pods/`, `build/`, `DerivedData/`, and provisioning profiles are gitignored —
  only the project sources are committed. After `npm run ios:sync` on a fresh
  clone, CocoaPods regenerates `Pods/`.
- **Bundled mode** (shipping `dist/` inside the `.ipa` instead of loading the live
  origin) breaks cookie auth until the backend moves to token auth. Stay on LIVE
  mode for submission unless that's been addressed — see `capacitor.config.ts`.
