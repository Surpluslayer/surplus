// ── Relationship CRM : full-page view ────────────────────────────────────
// The durable "who I've met" surface, rendered INSIDE the app canvas (not a
// slide-in). The topbar button toggles between this page and the event flow.
//
// Master/detail: a list of cross-event Contacts (GET /api/relationships/
// contacts) and, on click, one person's rollup + per-event breakdown + unified
// cross-event timeline (GET .../contacts/{id}). Styled light to match the
// surplus event flow. Self-contained so it stays isolated from the in-progress
// CRM work and from each app's own CSS.
import React, { useState, useEffect } from "react";
import { Users, ArrowLeft, Building2, CalendarDays, Activity, Sparkles } from "lucide-react";
import { api } from "../lib/api.js";

const C = {
  ink: "#1a1d24", muted: "#6b7280", faint: "#9aa1ad",
  line: "#e6e8ee", card: "#ffffff", bg: "#f4f5f7",
  accent: "#6d4df6", chipBg: "#efeafe", chipInk: "#6d4df6",
};

// Match the surplus shell's typeface (set on `.root` in surplusTheme.js).
// The page renders via inline styles, so we set it explicitly on the two
// top-level containers rather than relying on inheritance.
const FONT = "'Plus Jakarta Sans', system-ui, sans-serif";

const STAGE_COLORS = {
  converted: { bg: "#e7f7ee", ink: "#1c8c4e" },
  replied:   { bg: "#e8f0ff", ink: "#2f6df0" },
  contacted: { bg: "#fff3e0", ink: "#b9731a" },
  captured:  { bg: "#eef0f3", ink: "#5b6472" },
  stale:     { bg: "#fdeaea", ink: "#c0432f" },
};

// What's-new (relationship-watch) labels for the contact card.
const UPDATE_LABEL = {
  job_change:     "Changed roles",
  profile_update: "Updated profile",
  new_post:       "New post",
};

// The "what's new about them" highlight on a contact card : the freshest
// external change the watch-poller found (job move / profile edit / new post).
// Absent when we've seen nothing, so quiet contacts stay plain.
function WhatsNew({ update }) {
  if (!update) return null;
  return (
    <div style={{ marginTop: 10, padding: "8px 11px", borderRadius: 10,
                  background: "#f3f0ff", border: "1px solid #e3dcff" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6,
                    fontSize: 10.5, fontWeight: 700, letterSpacing: 0.4,
                    textTransform: "uppercase", color: "#6d4df6" }}>
        <Sparkles size={12} /> {UPDATE_LABEL[update.type] || "Update"}
      </div>
      <div style={{ fontSize: 12.5, color: "#3a3550", marginTop: 3,
                    lineHeight: 1.4 }}>
        {update.summary}
      </div>
    </div>
  );
}

function StageChip({ stage }) {
  const c = STAGE_COLORS[stage] || STAGE_COLORS.captured;
  return (
    <span style={{ fontSize: 11, fontWeight: 600, padding: "2px 9px",
                   borderRadius: 999, background: c.bg, color: c.ink }}>
      {stage || "—"}
    </span>
  );
}

const fmtDate = (s) => {
  if (!s) return "—";
  try { return new Date(s).toLocaleDateString(undefined,
    { month: "short", day: "numeric", year: "numeric" }); }
  catch { return s; }
};

export default function ContactsPage() {
  const [list, setList] = useState(null);   // null=loading, []=empty
  const [err, setErr] = useState(null);
  const [active, setActive] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.listContacts()
      .then((r) => { if (!cancelled) setList(r.contacts || []); })
      .catch((e) => { if (!cancelled) setErr(e.message || String(e)); });
    return () => { cancelled = true; };
  }, []);

  const open = async (id) => {
    setDetailLoading(true); setActive({ loading: true });
    try { setActive(await api.getContact(id)); }
    catch (e) { setErr(e.message || String(e)); setActive(null); }
    finally { setDetailLoading(false); }
  };

  // ── detail view ────────────────────────────────────────────────────
  if (active && !active.loading && !detailLoading) {
    const s = active.contact_summary;
    return (
      <div style={{ maxWidth: 820, margin: "0 auto", fontFamily: FONT }}>
        <button onClick={() => setActive(null)}
                style={{ display: "flex", alignItems: "center", gap: 6,
                         background: "none", border: "none", color: C.accent,
                         cursor: "pointer", fontSize: 14, padding: "4px 0",
                         marginBottom: 14 }}>
          <ArrowLeft size={16} /> All contacts
        </button>

        <div style={{ background: C.card, border: `1px solid ${C.line}`,
                      borderRadius: 16, padding: "22px 24px" }}>
          <div style={{ display: "flex", justifyContent: "space-between",
                        alignItems: "flex-start" }}>
            <div>
              <div style={{ fontSize: 22, fontWeight: 700, color: C.ink }}>
                {s.name || "Unknown"}
              </div>
              <div style={{ fontSize: 14, color: C.muted, marginTop: 4,
                            display: "flex", alignItems: "center", gap: 6 }}>
                {s.company && <><Building2 size={14} /> {s.company}</>}
              </div>
            </div>
            <StageChip stage={s.relationship_stage} />
          </div>

          <div style={{ display: "flex", gap: 28, marginTop: 18,
                        flexWrap: "wrap" }}>
            <Stat label="Events shared" value={s.n_events} />
            <Stat label="First met" value={fmtDate(s.first_met_at)} />
            <Stat label="Last touch" value={fmtDate(s.last_touch_at)} />
            <Stat label="Connection"
                  value={s.is_connection ? "Connected" : "—"} />
          </div>
          {s.next_step && (
            <div style={{ marginTop: 16, padding: "10px 14px",
                          background: C.chipBg, borderRadius: 10,
                          color: C.chipInk, fontSize: 13 }}>
              <strong>Next step:</strong> {s.next_step}
            </div>
          )}
        </div>

        <SectionLabel icon={CalendarDays} text="Events we've shared" />
        <div style={{ display: "grid", gap: 10 }}>
          {active.events.map((e) => (
            <div key={e.prospect_id}
                 style={{ background: C.card, border: `1px solid ${C.line}`,
                          borderRadius: 12, padding: "12px 16px",
                          display: "flex", justifyContent: "space-between",
                          alignItems: "center" }}>
              <div>
                <div style={{ fontWeight: 600, color: C.ink }}>
                  {e.event_title || "Untitled event"}
                </div>
                <div style={{ fontSize: 12, color: C.muted, marginTop: 2 }}>
                  {e.event_city ? `${e.event_city} · ` : ""}
                  captured {fmtDate(e.captured_at)}
                </div>
              </div>
              <StageChip stage={e.relationship_stage} />
            </div>
          ))}
        </div>

        <SectionLabel icon={Activity} text="Cross-event timeline" />
        <div style={{ background: C.card, border: `1px solid ${C.line}`,
                      borderRadius: 12, padding: "8px 18px 14px" }}>
          {active.timeline.length === 0 && (
            <div style={{ color: C.faint, fontSize: 13, padding: "10px 0" }}>
              No touches recorded yet.
            </div>
          )}
          {active.timeline.map((it, i) => (
            <div key={i} style={{ display: "flex", gap: 12, padding: "10px 0",
                                  borderBottom: i < active.timeline.length - 1
                                    ? `1px solid ${C.line}` : "none" }}>
              <div style={{ width: 8, height: 8, borderRadius: 999,
                            background: C.accent, marginTop: 6,
                            flexShrink: 0 }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 14, color: C.ink, fontWeight: 600 }}>
                  {it.title}
                  {it.metadata?.event_title && (
                    <span style={{ marginLeft: 8, fontSize: 11, fontWeight: 600,
                                   padding: "1px 8px", borderRadius: 999,
                                   background: C.chipBg, color: C.chipInk }}>
                      {it.metadata.event_title}
                    </span>
                  )}
                </div>
                {it.summary && (
                  <div style={{ fontSize: 13, color: C.muted, marginTop: 2 }}>
                    {it.summary}
                  </div>
                )}
                <div style={{ fontSize: 11, color: C.faint, marginTop: 3 }}>
                  {fmtDate(it.occurred_at)} · {it.channel || it.source_type}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ── list view ──────────────────────────────────────────────────────
  return (
    <div style={{ maxWidth: 980, margin: "0 auto", fontFamily: FONT }}>
      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 26, fontWeight: 800, color: C.ink,
                      display: "flex", alignItems: "center", gap: 10 }}>
          <Users size={24} /> Relationships
        </div>
        <div style={{ fontSize: 14, color: C.muted, marginTop: 4 }}>
          Everyone you've met across your events — auto-populated as you scan,
          connect, and message.
        </div>
      </div>

      {err && (
        <div style={{ color: "#c0432f", background: "#fdeaea",
                      border: "1px solid #f3c9c2", borderRadius: 10,
                      padding: "10px 14px", fontSize: 13 }}>{err}</div>
      )}

      {detailLoading && <div style={{ color: C.muted }}>Loading…</div>}

      {!detailLoading && list === null && !err && (
        <div style={{ color: C.muted }}>Loading contacts…</div>
      )}

      {!detailLoading && list && list.length === 0 && (
        <div style={{ background: C.card, border: `1px dashed ${C.line}`,
                      borderRadius: 16, padding: "40px 28px",
                      textAlign: "center", color: C.muted }}>
          <Users size={28} style={{ opacity: 0.5 }} />
          <div style={{ fontWeight: 700, color: C.ink, marginTop: 10,
                        fontSize: 16 }}>No contacts yet</div>
          <div style={{ fontSize: 13, marginTop: 6, maxWidth: 420,
                        marginInline: "auto", lineHeight: 1.5 }}>
            Your relationship spine fills itself when you scan someone in
            person, they accept a LinkedIn invite, or you send them a message
            (and we can derive a strong identity).
          </div>
        </div>
      )}

      {!detailLoading && list && list.length > 0 && (
        <div style={{ display: "grid",
                      gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
                      gap: 12 }}>
          {list.map((c) => (
            <button key={c.contact_id} onClick={() => open(c.contact_id)}
                    style={{ textAlign: "left", background: C.card,
                             border: `1px solid ${C.line}`, borderRadius: 14,
                             padding: "16px 18px", cursor: "pointer",
                             transition: "border-color .12s, box-shadow .12s" }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.borderColor = C.accent;
                      e.currentTarget.style.boxShadow =
                        "0 4px 16px rgba(109,77,246,0.10)";
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.borderColor = C.line;
                      e.currentTarget.style.boxShadow = "none";
                    }}>
              <div style={{ display: "flex", justifyContent: "space-between",
                            alignItems: "flex-start" }}>
                <span style={{ fontWeight: 700, color: C.ink, fontSize: 15 }}>
                  {c.name || "Unknown"}
                </span>
                <StageChip stage={c.relationship_stage} />
              </div>
              <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>
                {c.company || "—"}
              </div>
              <WhatsNew update={c.latest_update} />
              <div style={{ fontSize: 12, color: C.faint, marginTop: 10,
                            display: "flex", gap: 10, flexWrap: "wrap" }}>
                <span>{c.n_events} event{c.n_events === 1 ? "" : "s"}</span>
                {c.is_connection && <span>· connected</span>}
                <span>· last {fmtDate(c.last_touch_at)}</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4,
                    color: "#9aa1ad", fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color: "#1a1d24",
                    marginTop: 2 }}>{value}</div>
    </div>
  );
}

function SectionLabel({ icon: Icon, text }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 7,
                  margin: "22px 0 10px", color: "#6b7280", fontSize: 12,
                  fontWeight: 700, textTransform: "uppercase",
                  letterSpacing: 0.5 }}>
      <Icon size={14} /> {text}
    </div>
  );
}
