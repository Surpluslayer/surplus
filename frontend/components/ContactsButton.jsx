// ── Relationship CRM peek (temporary, self-contained, shared) ────────────
// A single "Contacts" button that opens a slide-in panel listing the durable
// cross-event Contact spine (GET /api/relationships/contacts) and, on click,
// one person's full rollup + cross-event timeline (GET .../contacts/{id}).
//
// Imported by BOTH the desktop app (App.jsx) and the in-person app
// (InPersonApp.jsx) so the spine is reachable from either surface. Deliberately
// styled with INLINE styles so it stays isolated from each app's CSS and from
// the in-progress CRM UI work — this is a "does the spine actually populate?"
// peek, not the final screen.
import React, { useState, useEffect } from "react";
import { Users, X, ArrowRight } from "lucide-react";
import { api } from "../lib/api.js";

const _crmOverlay = {
  position: "fixed", inset: 0, background: "rgba(8,10,14,0.55)",
  display: "flex", justifyContent: "flex-end", zIndex: 1000,
};
const _crmPanel = {
  width: "min(460px, 100%)", height: "100%", background: "#0f1217",
  color: "#e6e9ef", borderLeft: "1px solid #232936", overflowY: "auto",
  padding: "18px 20px", boxShadow: "-12px 0 40px rgba(0,0,0,0.4)",
};
const _crmRow = {
  width: "100%", textAlign: "left", background: "#141923",
  border: "1px solid #232936", borderRadius: 10, padding: "10px 12px",
  marginBottom: 8, color: "#e6e9ef", cursor: "pointer",
};
const _crmTag = {
  fontSize: 11, padding: "2px 7px", borderRadius: 999,
  background: "#1d2633", color: "#8fb4ff", marginLeft: 6,
};

function ContactsPeek({ onClose }) {
  const [list, setList] = useState(null);   // null=loading, []=empty
  const [err, setErr] = useState(null);
  const [active, setActive] = useState(null); // selected contact detail
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.listContacts()
      .then((r) => { if (!cancelled) setList(r.contacts || []); })
      .catch((e) => { if (!cancelled) setErr(e.message || String(e)); });
    return () => { cancelled = true; };
  }, []);

  const openContact = async (id) => {
    setDetailLoading(true); setActive(null);
    try { setActive(await api.getContact(id)); }
    catch (e) { setErr(e.message || String(e)); }
    finally { setDetailLoading(false); }
  };

  const fmtDate = (s) => {
    if (!s) return "—";
    try { return new Date(s).toLocaleDateString(); } catch { return s; }
  };

  return (
    <div style={_crmOverlay} onClick={onClose}>
      <div style={_crmPanel} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "center", marginBottom: 14 }}>
          <strong style={{ fontSize: 15 }}>
            {active ? "Contact" : "Contacts (relationship spine)"}
          </strong>
          <button onClick={active ? () => setActive(null) : onClose}
                  style={{ background: "none", border: "none", color: "#8a93a6",
                           cursor: "pointer", display: "flex" }}>
            {active ? <ArrowRight size={18} style={{ transform: "rotate(180deg)" }} />
                    : <X size={18} />}
          </button>
        </div>

        {err && <div style={{ color: "#ff8a8a", fontSize: 13 }}>{err}</div>}

        {/* LIST VIEW */}
        {!active && !detailLoading && (
          <>
            {list === null && !err && <div style={{ color: "#8a93a6" }}>Loading…</div>}
            {list && list.length === 0 && (
              <div style={{ color: "#8a93a6", fontSize: 13, lineHeight: 1.5 }}>
                No durable contacts yet. The spine populates when you scan
                someone in person, they accept a LinkedIn invite, or you send
                them a message (and we can derive a strong identity).
              </div>
            )}
            {list && list.map((c) => (
              <button key={c.contact_id} style={_crmRow}
                      onClick={() => openContact(c.contact_id)}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontWeight: 600 }}>{c.name || "Unknown"}</span>
                  <span style={_crmTag}>{c.relationship_stage || "—"}</span>
                </div>
                <div style={{ fontSize: 12, color: "#8a93a6", marginTop: 3 }}>
                  {c.company || "—"} · {c.n_events} event{c.n_events === 1 ? "" : "s"}
                  {c.is_connection ? " · connected" : ""}
                </div>
                <div style={{ fontSize: 11, color: "#6b7384", marginTop: 2 }}>
                  first met {fmtDate(c.first_met_at)} · last touch {fmtDate(c.last_touch_at)}
                </div>
              </button>
            ))}
          </>
        )}

        {detailLoading && <div style={{ color: "#8a93a6" }}>Loading…</div>}

        {/* DETAIL VIEW */}
        {active && !detailLoading && (
          <div>
            <div style={{ fontWeight: 700, fontSize: 16 }}>
              {active.contact_summary.name || "Unknown"}
            </div>
            <div style={{ fontSize: 12, color: "#8a93a6", margin: "4px 0 14px" }}>
              {active.contact_summary.company || "—"} ·{" "}
              {active.contact_summary.relationship_stage || "—"} ·{" "}
              {active.contact_summary.n_events} event
              {active.contact_summary.n_events === 1 ? "" : "s"}
              {active.contact_summary.next_step
                ? ` · next: ${active.contact_summary.next_step}` : ""}
            </div>

            <div style={{ fontSize: 12, textTransform: "uppercase",
                          color: "#6b7384", margin: "10px 0 6px" }}>
              Events we've shared
            </div>
            {active.events.map((e) => (
              <div key={e.prospect_id} style={{ ..._crmRow, cursor: "default" }}>
                <div style={{ fontWeight: 600 }}>{e.event_title || "Untitled event"}</div>
                <div style={{ fontSize: 12, color: "#8a93a6" }}>
                  {e.relationship_stage} · captured {fmtDate(e.captured_at)}
                </div>
              </div>
            ))}

            <div style={{ fontSize: 12, textTransform: "uppercase",
                          color: "#6b7384", margin: "16px 0 6px" }}>
              Cross-event timeline
            </div>
            {active.timeline.map((it, i) => (
              <div key={i} style={{ borderLeft: "2px solid #232936",
                                    padding: "2px 0 10px 12px", marginLeft: 4 }}>
                <div style={{ fontSize: 13 }}>
                  {it.title}
                  {it.metadata?.event_title && (
                    <span style={_crmTag}>{it.metadata.event_title}</span>
                  )}
                </div>
                {it.summary && (
                  <div style={{ fontSize: 12, color: "#8a93a6" }}>{it.summary}</div>
                )}
                <div style={{ fontSize: 11, color: "#6b7384" }}>
                  {fmtDate(it.occurred_at)} · {it.channel || it.source_type}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// `variant` lets each host match its own chrome:
//   "desktop" (default) — the App.jsx topbar pill
//   "inperson"          — a compact icon-forward button for the phone surface
//
// Two modes:
//   • Controlled toggle — pass `onClick` (and `active`) and the button just
//     flips the host's view between the CRM page and the event flow. The
//     label reflects where a click takes you ("Contacts" ⇄ "Event flow").
//   • Self-contained peek — omit `onClick` and the button opens the legacy
//     dark slide-in ContactsPeek panel (kept for surfaces not yet on the
//     full-page CRM).
export default function ContactsButton({ variant = "desktop", active = false, onClick }) {
  const [open, setOpen] = useState(false);
  // The two surfaces have opposite chrome: the desktop topbar is light
  // (white pills, purple accent), the in-person bar is dark. Theme the
  // button to its host so it reads as native, and use the purple accent
  // to signal the active (CRM) state in both.
  const palette = variant === "inperson"
    ? { onBg: "#2f6df6", onInk: "#ffffff", offBg: "#141923",
        offInk: "#cfd6e4", offBorder: "#232936" }
    : { onBg: "#2f6df6", onInk: "#ffffff", offBg: "#ffffff",
        offInk: "#1a1d24", offBorder: "#e6e8ee" };
  const base = {
    display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
    fontFamily: "'Inter', system-ui, sans-serif",
    fontWeight: 600,
    background: active ? palette.onBg : palette.offBg,
    color: active ? palette.onInk : palette.offInk,
    border: `1px solid ${active ? palette.onBg : palette.offBorder}`,
    borderRadius: 999, fontSize: 13,
  };
  const style = variant === "inperson"
    ? { ...base, padding: "6px 10px" }
    : { ...base, padding: "7px 14px", marginRight: 8 };

  // Controlled toggle : the host owns the view; we just signal a flip.
  if (onClick) {
    const label = variant === "inperson"
      ? ""
      : (active ? "Event flow" : "Contacts");
    return (
      <button onClick={onClick} style={style}
              title={active ? "Back to the event flow" : "Your relationship spine"}>
        <Users size={15} /> {label}
      </button>
    );
  }

  // Self-contained peek (legacy).
  return (
    <>
      <button onClick={() => setOpen(true)} title="Your relationship spine"
              style={style}>
        <Users size={15} /> {variant === "inperson" ? "" : "Contacts"}
      </button>
      {open && <ContactsPeek onClose={() => setOpen(false)} />}
    </>
  );
}
