# Backlog

Captured-but-not-yet-built work. Each entry is a design that's been thought
through and is ready to pick up — it is intentionally NOT implemented yet.

---

## Notification steps in first-time-user onboarding

**Status:** designed, not implemented (parked 2026-06-16 at the user's request).
**Goal:** a first-time user should be prompted — in context, during onboarding —
to turn on browser/device notifications, and see a sample of what they'll get,
so they grant permission and don't miss accepts/replies.

### Where onboarding lives today
- The first-time tour is a 7-step coachmark flow in `frontend/InPersonApp.jsx`
  (`ONB_STEPS`): `event → contact → notes → classify → link → send → hub`. Each
  step anchors to a live control via a `[data-onb="…"]` selector; state is
  `{status, step}` mirrored from `/me` and persisted via `PUT /api/auth/onboarding`.
  `onboarding_status` is armed to `active` on first LinkedIn connect
  (`routes/auth.py:_arm_onboarding_if_first_connect`).
- Notification helpers already exist: `frontend/lib/notify.js`
  (`ensureNotifyPermission`, `notifyDevice`). Today the permission prompt fires
  **silently on mount** (`InPersonApp.jsx` `useEffect(() => ensureNotifyPermission())`),
  with no context.

### ⚠️ Key open decision (blocks implementation)
The default `event.surpluslayer.com` surface is **BookApp**, which has **no
onboarding tour and no notifications at all**. The 7-step tour only renders in
InPersonApp (`/legacy`, `/guest`). So a real first-time user lands on BookApp and
sees neither. Pick the target surface before building:
- **(A)** Add the step to the existing InPersonApp tour only — small/fast, but
  most first-time users (on BookApp) won't see it.
- **(B)** Bring the onboarding engine to BookApp too — reaches first-timers, but
  BookApp has no coachmark scaffold yet (bigger lift: port the engine + add
  `[data-onb]` anchors).
- **(C)** A surface-agnostic one-off notification prompt on first connect —
  smallest way to reach everyone; not part of a guided tour.
- _Recommendation:_ **(C) now**, **(B) later** if the full tour is wanted on BookApp.

### Proposed UX (two-beat single step)
Insert before the final `hub` step:
- **Beat 1 — the ask** (anchored to the bottom tab bar / a bell control):
  > 🔔 **Never miss a reply** — Turn on notifications and we'll ping you the
  > moment someone accepts your invite or replies, so you can follow up while
  > it's warm. **[ Turn on notifications ]** · *Not now*
  - "Turn on notifications" → `ensureNotifyPermission()` (now in context).
  - "Not now" → advance without prompting.
- **Beat 2 — the proof** (only if `granted`): fire a sample
  `notifyDevice("Maya accepted your invite", { body: "Tap to send your follow-up" })`,
  then flip the card to a ✅ confirm + Next.

### State handling (reuse existing helpers, no new mechanism)
- `granted` → Beat 2 + sample notification.
- `denied` → don't dead-end: show an **in-app mock** of the notification card +
  "Notifications are blocked in your browser settings — here's what you'd get",
  then advance.
- `unsupported` / insecure context (iframe, http) → self-skip the step entirely
  (`ensureNotifyPermission` already returns `"unsupported"`; `notifyDevice` no-ops).
- Note: `notifyDevice` suppresses the OS notification while the tab is focused, so
  during the tour the **in-app sample card is the reliable visual**; the real OS
  notification is what they get later when backgrounded.

### Persistence
None new. It's another `ONB_STEPS` entry, so `onboarding_step` already tracks and
resumes it. Browser `Notification.permission` records grant/deny, so we never
re-prompt. (Optional later: a "% enabled notifications" analytics counter.)

### Implementation sketch (when greenlit)
1. Add one (or two) entries to `ONB_STEPS` (or the chosen surface's tour).
2. Remove the silent `ensureNotifyPermission()` on mount; move it into the step.
3. Add the `granted/denied/unsupported` branch + sample-notification render in
   `OnboardingCoach`.
4. Decide one-beat (ask only) vs two-beat (ask + sample).
