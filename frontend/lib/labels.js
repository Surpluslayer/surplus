// Status / action label helpers shared by the desktop pipeline (App.jsx) and
// the phone-first in-person surface (InPersonApp.jsx). Keeping these in one
// place means the warm/cold button label and the funnel status chips read
// identically across both surfaces.

// The "reach out" button label, driven by the live connection_status the
// /invite + /scan + /captures endpoints return.
export function actionLabel(connectionStatus, sending) {
  if (connectionStatus === "connected") return sending ? "Sending message…" : "Send message";
  if (connectionStatus === "not_connected") return sending ? "Sending invite…" : "Send invite";
  return sending ? "Sending…" : "Reach out";
}

// Prospect.status -> chip label + css class (the funnel mapping).
export function statusMeta(s) {
  if (s === "rsvp") return { label: "RSVP'd", cls: "st-rsvp" };
  if (s === "contacted") return { label: "Awaiting", cls: "st-contacted" };
  if (s === "below") return { label: "Below threshold", cls: "st-below" };
  if (s === "pending") return { label: "Pending", cls: "st-pending" };
  return { label: s, cls: "" };
}

// Last-OutreachLog state -> short human label for the in-person CRM timeline.
// Mirrors the canonical states the backend records (providers/base.py).
export function outreachStateLabel(state) {
  switch (state) {
    case "invite_sent":     return "Invite sent";
    case "invite_accepted": return "Accepted";
    case "message_sent":    return "DM sent";
    case "message_replied": return "Replied";
    case "auto_reply_sent": return "Auto-replied";
    case "follow_up_sent":  return "Followed up";
    case "dry_run_queued":  return "Queued (dry-run)";
    case "failed":          return "Failed";
    default:                return state || "";
  }
}
