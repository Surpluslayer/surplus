// ── Relationship-layer paywall : usage meter + Stripe pricing table ──────────
//
// The relationship agent (follow-up drafts + contact scanning) is metered per
// billing period (see backend/billing_plans.py). When a free-tier user hits
// the cap the backend returns 402 {detail:{error:"LIMIT_REACHED"|
// "CONTACT_LIMIT_REACHED", ...}}; the caller pops <PaywallModal/> which embeds
// the Stripe pricing table so they can upgrade in place.
//
// The publishable key + pricing-table id below are PUBLIC by design (Stripe's
// pricing-table embed is a client-side widget). The secret key never leaves
// the server. client-reference-id={user.id} is what the webhook reads to map
// the resulting subscription back to this user row.
import React, { useEffect } from "react";

// Public Stripe identifiers — safe to ship to the browser.
const STRIPE_PRICING_TABLE_ID = "prctbl_1TgJTaBCTXVW8E0Q27EzcbLA";
const STRIPE_PUBLISHABLE_KEY =
  "pk_live_51TaTZnBCTXVW8E0QARWE3yrEBdRexqpG8jvT09y1WVPhWHJwruFf0Pf5xOv7YJFgJWZWgcBMOREqWSyPYCClqaKG005IdEOAAj";

const FONT = "'Inter', system-ui, sans-serif";
const ACCENT = "#2f6df6";

// Load Stripe's pricing-table web component once, idempotently.
function useStripePricingScript() {
  useEffect(() => {
    const SRC = "https://js.stripe.com/v3/pricing-table.js";
    if (document.querySelector(`script[src="${SRC}"]`)) return;
    const s = document.createElement("script");
    s.src = SRC;
    s.async = true;
    document.head.appendChild(s);
  }, []);
}

// The embedded Stripe pricing table, tagged with the signed-in user so the
// checkout the user completes resolves back to their row in the webhook.
export function StripePricingTable({ user }) {
  useStripePricingScript();
  const email = user?.email && user.email.includes("@") &&
    !user.email.endsWith("@anonymous.surplus") &&
    !user.email.endsWith("@demo.surpluslayer.com")
      ? user.email : undefined;
  // React passes through unknown (hyphenated) attributes to custom elements.
  return React.createElement("stripe-pricing-table", {
    "pricing-table-id": STRIPE_PRICING_TABLE_ID,
    "publishable-key": STRIPE_PUBLISHABLE_KEY,
    ...(user?.id != null ? { "client-reference-id": String(user.id) } : {}),
    ...(email ? { "customer-email": email } : {}),
  });
}

// Compact "3 / 5 drafts left" meter for the relationship surface. `billing`
// is the usage_snapshot block from GET /api/auth/me. Renders nothing for
// unlimited (demo / allowlisted) accounts.
export function UsageMeter({ billing, style }) {
  if (!billing || billing.unlimited) return null;
  const drafts = billing.remaining?.drafts;
  const contacts = billing.remaining?.contacts;
  if (drafts == null && contacts == null) return null;
  const lim = billing.limits || {};
  const low = (drafts != null && drafts <= 1) || (contacts != null && contacts <= 3);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10,
                  fontSize: 12, fontWeight: 600, fontFamily: FONT,
                  color: low ? "#c0432f" : "#6b7280", ...style }}>
      {drafts != null && (
        <span>{drafts}/{lim.drafts} follow-up drafts left</span>)}
      {drafts != null && contacts != null && <span style={{ opacity: 0.5 }}>·</span>}
      {contacts != null && (
        <span>{contacts}/{lim.contacts} contact scans left</span>)}
    </div>
  );
}

// Full-screen modal wrapping the pricing table. Shown when the backend 402s
// a metered relationship action. `reason` is the backend error code so the
// copy can name the right cap.
export function PaywallModal({ user, reason, message, onClose }) {
  const title = reason === "CONTACT_LIMIT_REACHED"
    ? "You've scanned every contact on the free plan"
    : "You've used all your free follow-up drafts";
  const sub = message ||
    "Upgrade to keep the relationship agent drafting and scanning. Your usage resets each billing period.";
  return (
    <div onClick={onClose}
         style={{ position: "fixed", inset: 0, zIndex: 1000,
                  background: "rgba(20,18,40,0.55)", display: "flex",
                  alignItems: "center", justifyContent: "center",
                  padding: 20, fontFamily: FONT }}>
      <div onClick={(e) => e.stopPropagation()}
           style={{ background: "#fff", borderRadius: 20, maxWidth: 920,
                    width: "100%", maxHeight: "90vh", overflowY: "auto",
                    boxShadow: "0 24px 80px rgba(20,18,40,0.35)" }}>
        <div style={{ padding: "26px 30px 8px", position: "relative" }}>
          <button onClick={onClose}
                  style={{ position: "absolute", top: 18, right: 20,
                           background: "none", border: "none", fontSize: 22,
                           lineHeight: 1, color: "#9aa1ad", cursor: "pointer" }}>
            ×
          </button>
          <div style={{ fontSize: 12.5, fontWeight: 700, letterSpacing: 0.4,
                        textTransform: "uppercase", color: ACCENT }}>
            Upgrade
          </div>
          <h2 style={{ margin: "8px 0 6px", fontSize: 22, color: "#1a1d24" }}>
            {title}
          </h2>
          <p style={{ margin: 0, color: "#6b7280", fontSize: 14.5,
                      lineHeight: 1.5, maxWidth: 620 }}>
            {sub}
          </p>
        </div>
        <div style={{ padding: "10px 18px 26px" }}>
          <StripePricingTable user={user} />
        </div>
      </div>
    </div>
  );
}

// Pull the structured 402 paywall payload out of an api error, or null if the
// error isn't a relationship-quota block. Handles both the FastAPI envelope
// ({detail:{...}}) and a bare body.
export function paywallFromError(err) {
  if (!err || err.status !== 402) return null;
  const body = err.body;
  const d = (body && (body.detail ?? body)) || null;
  if (!d || typeof d !== "object") return null;
  if (d.error === "LIMIT_REACHED" || d.error === "CONTACT_LIMIT_REACHED") {
    return { reason: d.error, message: d.message, billing: d.billing };
  }
  return null;
}
