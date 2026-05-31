// Browser-notification helpers, shared by the desktop pipeline (App.jsx) and
// the phone-first in-person surface (InPersonApp.jsx).
//
// Best-effort: never throws, never blocks. The Notification API only fires on
// https / localhost, so these silently no-op on http deploys / insecure
// contexts (e.g. an iframe or an http:// LAN address on a phone).

// Returns the granted permission string (or "unsupported" when the API isn't
// there at all). Call once on mount to prompt for permission.
export async function ensureNotifyPermission() {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  try {
    return await Notification.requestPermission();
  } catch {
    return "default";
  }
}

// Fire a device notification. Suppressed when the tab is already focused : the
// in-app UI already conveys the event in that case.
export function notifyDevice(title, options = {}) {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  if (typeof document !== "undefined" && document.visibilityState === "visible"
      && document.hasFocus && document.hasFocus()) return;
  try {
    const n = new Notification(title, { icon: "/surplus-logo.png", ...options });
    n.onclick = () => { try { window.focus(); n.close(); } catch {} };
  } catch {
    // Some browsers throw on iframe / insecure contexts : just swallow.
  }
}
