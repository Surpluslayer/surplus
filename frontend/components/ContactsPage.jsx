// ── Relationship CRM : full-page view ────────────────────────────────────
// The durable "who I've met" surface, rendered INSIDE the app canvas (not a
// slide-in). The topbar button toggles between this page and the event flow.
//
// Master/detail: a list of cross-event Contacts (GET /api/relationships/
// contacts) and, on click, one person's rollup + per-event breakdown + unified
// cross-event timeline (GET .../contacts/{id}). Styled light to match the
// surplus event flow. Self-contained so it stays isolated from the in-progress
// CRM work and from each app's own CSS.
import React, { useState, useEffect, useRef } from "react";
import { Users, ArrowLeft, Building2, CalendarDays, Activity, Sparkles,
         MessageSquare, Send, Check, Clock, Plug2, Mail, Calendar,
         MessageCircle, Link2, ArrowRight } from "lucide-react";
import { api } from "../lib/api.js";
import { UsageMeter, PaywallModal, paywallFromError } from "./UpgradePaywall.jsx";

const C = {
  ink: "#1a1d24", muted: "#6b7280", faint: "#9aa1ad",
  line: "#e6e8ee", card: "#ffffff", bg: "#f4f5f7",
  accent: "#2f6df6", chipBg: "#eaf1fe", chipInk: "#2f6df6",
};

// Match the surplus shell's typeface (set on `.root` in surplusTheme.js).
// The page renders via inline styles, so we set it explicitly on the two
// top-level containers rather than relying on inheritance.
const FONT = "'Inter', system-ui, sans-serif";

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
                  background: "#eaf1fe", border: "1px solid #cfe0fd" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6,
                    fontSize: 10.5, fontWeight: 700, letterSpacing: 0.4,
                    textTransform: "uppercase", color: "#2f6df6" }}>
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
  const [tab, setTab] = useState("contacts");   // "contacts" | "integrations"

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
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 26, fontWeight: 800, color: C.ink,
                      display: "flex", alignItems: "center", gap: 10 }}>
          <Users size={24} /> Relationships
        </div>
        <div style={{ fontSize: 14, color: C.muted, marginTop: 4 }}>
          Everyone you've met across your events — auto-populated as you scan,
          connect, and message.
        </div>
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 20 }}>
        {[["contacts", "Contacts", Users],
          ["integrations", "Integrations", Plug2]].map(([key, label, Icon]) => (
          <button key={key} onClick={() => setTab(key)}
                  style={{ display: "inline-flex", alignItems: "center", gap: 7,
                           background: tab === key ? C.chipBg : "transparent",
                           color: tab === key ? C.chipInk : C.muted,
                           border: `1px solid ${tab === key ? "transparent" : C.line}`,
                           borderRadius: 999, padding: "8px 15px", cursor: "pointer",
                           fontSize: 13.5, fontWeight: 700, fontFamily: FONT }}>
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === "integrations" ? <Integrations /> : (
      <>
      <FollowupChat />

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
      </>
      )}
    </div>
  );
}

// ── Integrations : connect the apps your contacts already live in ────────────
// The relationship engine's intake surface. Connecting an app is how your book
// fills itself — no manual entry. LinkedIn is wired to the real hosted-auth
// connect (and reflects live status off /me); Email / Calendar / WhatsApp are
// shown as the roadmap so the demo tells the whole story. Per the design: each
// app is its own progressive connect, and contacts sync in automatically (we
// pull who you actually talk to — you remove, never assemble).
const INTEGRATIONS = [
  {
    key: "linkedin", name: "LinkedIn", Icon: Link2, brand: "#0a66c2",
    blurb: "Imports the people you connect with and message.",
    real: true,
  },
  {
    key: "email", name: "Email", Icon: Mail, brand: "#ea4335",
    blurb: "Gmail or Outlook. Pulls who you actually correspond with — and when you last talked.",
    real: true,
  },
  {
    key: "calendar", name: "Calendar", Icon: Calendar, brand: "#4285f4",
    blurb: "Knows who you're meeting, and nudges a follow-up after.",
    real: false,
  },
  {
    key: "whatsapp", name: "WhatsApp", Icon: MessageCircle, brand: "#25d366",
    blurb: "Keeps the people you text in the loop too.",
    real: false,
  },
];

function Integrations() {
  const [user, setUser] = useState(null);   // null = loading
  const [connecting, setConnecting] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then((u) => { if (!cancelled) setUser(u || {}); })
      .catch(() => { if (!cancelled) setUser({}); });
    return () => { cancelled = true; };
  }, []);

  const linkedinConnected = !!(user && user.unipile_account_id
    && user.linkedin_status === "active");
  const emailConnected = !!(user && user.unipile_email_account_id
    && user.email_status === "active");

  // One generic starter for both hosted-auth connects: hit the start route,
  // redirect the browser to Unipile's hosted page. The webhook flips the
  // user row; we land back here with the tile reading Connected.
  const startConnect = (key, startFn) => async () => {
    setErr(""); setConnecting(key);
    try {
      const r = await startFn();
      if (r?.url) { window.location.href = r.url; return; }
      setConnecting("");
    } catch (e) {
      setErr(`Couldn't start ${key} connect: ` + (e.message || "unknown"));
      setConnecting("");
    }
  };
  const connectLinkedin = startConnect("linkedin", api.startLinkedinAuth);
  const connectEmail = startConnect("email", api.startEmailAuth);

  const statusFor = (it) => {
    if (!it.real) return { label: "Coming soon", connected: false, soon: true };
    if (user === null) return { label: "…", connected: false };
    if (it.key === "linkedin") {
      return linkedinConnected
        ? { label: "Connected", connected: true }
        : { label: "Not connected", connected: false };
    }
    if (it.key === "email") {
      return emailConnected
        ? { label: user.email_account_address
              ? `Connected · ${user.email_account_address}` : "Connected",
            connected: true }
        : { label: "Not connected", connected: false };
    }
    return { label: "Not connected", connected: false };
  };

  return (
    <div style={{ fontFamily: FONT }}>
      <div style={{ background: C.chipBg, border: `1px solid #e2d9fb`,
                    borderRadius: 14, padding: "14px 16px", marginBottom: 18,
                    display: "flex", gap: 10, alignItems: "flex-start" }}>
        <Sparkles size={18} color={C.accent} style={{ flexShrink: 0, marginTop: 1 }} />
        <div style={{ fontSize: 13.5, color: C.ink, lineHeight: 1.5 }}>
          <strong>Connect the apps you already use.</strong> Your contacts sync
          in automatically — we pull the people you actually talk to and how
          recently, so your book stays warm without you typing a thing.
        </div>
      </div>

      {err && (
        <div style={{ color: "#c0432f", background: "#fdeaea",
                      border: "1px solid #f3c9c2", borderRadius: 10,
                      padding: "10px 14px", fontSize: 13, marginBottom: 16 }}>{err}</div>
      )}

      <div style={{ display: "grid",
                    gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
                    gap: 12 }}>
        {INTEGRATIONS.map((it) => {
          const st = statusFor(it);
          const busy = connecting === it.key;
          return (
            <div key={it.key}
                 style={{ background: C.card, border: `1px solid ${C.line}`,
                          borderRadius: 14, padding: "16px 18px",
                          opacity: st.soon ? 0.85 : 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div style={{ width: 40, height: 40, borderRadius: 10,
                              background: `${it.brand}14`, color: it.brand,
                              display: "flex", alignItems: "center",
                              justifyContent: "center", flexShrink: 0 }}>
                  <it.Icon size={21} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 700, color: C.ink, fontSize: 15.5 }}>
                    {it.name}
                  </div>
                  <div style={{ fontSize: 12, marginTop: 2,
                                display: "inline-flex", alignItems: "center", gap: 5,
                                color: st.connected ? "#1c8c4e"
                                  : st.soon ? C.faint : C.muted, fontWeight: 600 }}>
                    {st.connected && <Check size={12} />}
                    {st.label}
                  </div>
                </div>
              </div>

              <div style={{ fontSize: 13, color: C.muted, marginTop: 12,
                            lineHeight: 1.5 }}>
                {it.blurb}
              </div>

              <div style={{ marginTop: 14 }}>
                {st.connected ? (
                  <button disabled
                          style={{ width: "100%", border: `1px solid ${C.line}`,
                                   background: C.bg, color: C.muted, borderRadius: 10,
                                   padding: "10px", fontSize: 13.5, fontWeight: 700,
                                   fontFamily: FONT, cursor: "default",
                                   display: "inline-flex", alignItems: "center",
                                   justifyContent: "center", gap: 7 }}>
                    <Check size={15} /> Connected
                  </button>
                ) : st.soon ? (
                  <button disabled title="On the roadmap"
                          style={{ width: "100%", border: `1px dashed ${C.line}`,
                                   background: "transparent", color: C.faint,
                                   borderRadius: 10, padding: "10px", fontSize: 13.5,
                                   fontWeight: 700, fontFamily: FONT,
                                   cursor: "not-allowed" }}>
                    Coming soon
                  </button>
                ) : (
                  <button onClick={it.key === "linkedin" ? connectLinkedin
                                   : it.key === "email" ? connectEmail : undefined}
                          disabled={busy}
                          style={{ width: "100%", border: "none",
                                   background: C.accent, color: "#fff", borderRadius: 10,
                                   padding: "10px", fontSize: 13.5, fontWeight: 700,
                                   fontFamily: FONT, cursor: busy ? "default" : "pointer",
                                   display: "inline-flex", alignItems: "center",
                                   justifyContent: "center", gap: 7 }}>
                    {busy ? "Connecting…" : <>Connect <ArrowRight size={15} /></>}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ fontSize: 12, color: C.faint, marginTop: 16, lineHeight: 1.5 }}>
        We only ever sync people you genuinely correspond with, and you can hide
        anyone — you remove, you never have to build the list yourself.
      </div>
    </div>
  );
}

// ── Follow-up assistant : a chat over the propose-only relationship agent ──
// The host asks ("who should I follow up with?"); the agent surveys the spine
// and replies with a paragraph + per-contact drafted follow-ups. Each draft is
// editable, and Approve routes through /contacts/{id}/followup — which honors
// the host's auto-send toggle server-side (sends when on, stages a draft when
// off). Nothing sends without the host clicking Approve.
function FollowupChat() {
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState([]);   // {role:"host"|"agent", text, proposals?, auto?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // Relationship-layer metered usage (from /me) + the active 402 paywall, if
  // the agent run hit the cap.
  const [user, setUser] = useState(null);
  const [billing, setBilling] = useState(null);
  const [paywall, setPaywall] = useState(null);
  const scrollRef = useRef(null);

  const refreshUsage = async () => {
    try {
      const me = await api.me();
      setUser(me);
      setBilling(me.billing || null);
    } catch { /* signed-out / offline : just skip the meter */ }
  };
  useEffect(() => { refreshUsage(); }, []);

  useEffect(() => {
    if (scrollRef.current)
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns, busy]);

  const ask = async (text) => {
    const q = (text ?? input).trim();
    if (!q || busy) return;
    setInput("");
    setTurns((t) => [...t, { role: "host", text: q }]);
    setBusy(true);
    // auto-send pref arrives in the `meta` frame before any card; hold it so
    // each streamed proposal card labels its button correctly.
    let auto = false;

    // Reveal pacing. The backend drafts up to RELATIONSHIP_DRAFT_CONCURRENCY
    // people in PARALLEL, so their cards resolve in a near-simultaneous burst
    // and would otherwise all pop at once. Buffer the streamed proposals and
    // release them into the chat one at a time on a fixed cadence, so they
    // animate in one-by-one regardless of how bursty the delivery is. The
    // "busy" indicator stays on until the buffer drains.
    const REVEAL_MS = 700;
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const pending = [];          // proposals streamed but not yet shown
    let streamDone = false;      // true once the SSE stream closes
    let summaryText = null;      // closing line, shown after the last card

    const pump = (async () => {
      // First card lands fast; subsequent ones are spaced by REVEAL_MS.
      let first = true;
      for (;;) {
        if (pending.length) {
          if (!first) await sleep(REVEAL_MS);
          first = false;
          const p = pending.shift();
          setTurns((t) => [...t, { role: "agent", proposals: [p], auto }]);
        } else if (streamDone) {
          break;
        } else {
          await sleep(120);      // wait for the next streamed card
        }
      }
    })();

    try {
      await api.relationshipChatStream(q, {
        onMeta: (m) => { auto = !!m.auto_send_enabled; },
        // Queue each staged person; the pump reveals them one-by-one.
        onProposal: (p) => { if (p.kind === "draft_message") pending.push(p); },
        // Closing line lands last, under the cards it summarizes.
        onDone: (d) => { summaryText = d.summary || null; },
        onError: (e) => {
          setTurns((t) => [...t, { role: "agent",
            text: `Sorry — ${e.message || "something went wrong"}`, proposals: [] }]);
        },
      });
      streamDone = true;
      await pump;                // drain the remaining cards one-by-one
      if (summaryText)
        setTurns((t) => [...t, { role: "agent", text: summaryText }]);
    } catch (e) {
      streamDone = true;
      await pump.catch(() => {});
      // A 402 from the relationship-quota gate means the free cap is hit:
      // open the upgrade paywall instead of dumping a raw error in the chat.
      const pw = paywallFromError(e);
      if (pw) {
        if (pw.billing) setBilling(pw.billing);
        setPaywall(pw);
        setTurns((t) => t.slice(0, -1));  // drop the unanswered host bubble
      } else {
        setTurns((t) => [...t, { role: "agent",
          text: `Sorry — ${e.message || String(e)}`, proposals: [] }]);
      }
    } finally {
      setBusy(false);
      refreshUsage();  // reflect the drafts/contacts this run consumed
    }
  };

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
              style={{ display: "flex", alignItems: "center", gap: 8,
                       width: "100%", marginBottom: 18, cursor: "pointer",
                       background: "#eaf1fe", border: "1px solid #cfe0fd",
                       borderRadius: 14, padding: "14px 18px", color: C.accent,
                       fontSize: 14.5, fontWeight: 700, fontFamily: FONT }}>
        <MessageSquare size={18} />
        Ask who to follow up with
        <span style={{ marginLeft: "auto", fontWeight: 500, color: C.muted,
                       fontSize: 12.5 }}>
          AI drafts a message for each →
        </span>
      </button>
    );
  }

  return (
   <>
    {paywall && (
      <PaywallModal user={user} reason={paywall.reason}
                    message={paywall.message}
                    onClose={() => { setPaywall(null); refreshUsage(); }} />
    )}
    <div style={{ marginBottom: 20, background: C.card,
                  border: `1px solid ${C.line}`, borderRadius: 16,
                  overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8,
                    padding: "12px 16px", borderBottom: `1px solid ${C.line}`,
                    background: "#faf9ff" }}>
        <MessageSquare size={16} color={C.accent} />
        <span style={{ fontWeight: 700, color: C.ink, fontSize: 14 }}>
          Follow-up assistant
        </span>
        <UsageMeter billing={billing} style={{ marginLeft: "auto" }} />
        <button onClick={() => setOpen(false)}
                style={{ background: "none", border: "none",
                         color: C.muted, cursor: "pointer", fontSize: 13 }}>
          Hide
        </button>
      </div>

      <div ref={scrollRef}
           style={{ maxHeight: 420, overflowY: "auto", padding: "14px 16px" }}>
        {turns.length === 0 && (
          <div style={{ color: C.muted, fontSize: 13.5, lineHeight: 1.5 }}>
            Ask me who's gone quiet, who just changed jobs, or who's worth a
            ping — I'll read your history and draft a message for each.
            <div style={{ display: "flex", gap: 8, marginTop: 12,
                          flexWrap: "wrap" }}>
              {["Who should I follow up with?",
                "Who recently changed roles?",
                "Draft pings for anyone going cold"].map((s) => (
                <button key={s} onClick={() => ask(s)}
                        style={{ background: C.chipBg, color: C.chipInk,
                                 border: "none", borderRadius: 999,
                                 padding: "6px 12px", fontSize: 12.5,
                                 fontWeight: 600, cursor: "pointer",
                                 fontFamily: FONT }}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((t, i) => (
          <div key={i} style={{ marginBottom: 14 }}>
            {t.role === "host" ? (
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <div style={{ background: C.accent, color: "#fff",
                              borderRadius: "14px 14px 4px 14px",
                              padding: "8px 13px", fontSize: 13.5,
                              maxWidth: "80%" }}>
                  {t.text}
                </div>
              </div>
            ) : (
              <div>
                {t.text && (
                  <div style={{ background: C.bg, color: C.ink, borderRadius: 12,
                                padding: "10px 14px", fontSize: 13.5,
                                lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
                    {t.text}
                  </div>
                )}
                {(t.proposals || []).map((p) => (
                  <ProposalCard key={`${p.contact_id}-${p.text.slice(0,12)}`}
                                proposal={p} auto={t.auto} />
                ))}
              </div>
            )}
          </div>
        ))}

        {busy && (
          <div style={{ color: C.faint, fontSize: 13, fontStyle: "italic" }}>
            {turns.some((t) => t.role === "agent" && t.proposals?.length)
              ? "Drafting the next one…"
              : "Reading your relationship history…"}
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 8, padding: "12px 16px",
                    borderTop: `1px solid ${C.line}` }}>
        <input value={input} onChange={(e) => setInput(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
               placeholder="Ask who to follow up with…"
               disabled={busy}
               style={{ flex: 1, border: `1px solid ${C.line}`, borderRadius: 10,
                        padding: "10px 13px", fontSize: 14, fontFamily: FONT,
                        outline: "none" }} />
        <button onClick={() => ask()} disabled={busy || !input.trim()}
                style={{ display: "flex", alignItems: "center", gap: 6,
                         background: busy || !input.trim() ? "#c9bdf8" : C.accent,
                         color: "#fff", border: "none", borderRadius: 10,
                         padding: "0 16px", fontSize: 14, fontWeight: 600,
                         cursor: busy || !input.trim() ? "default" : "pointer",
                         fontFamily: FONT }}>
          <Send size={15} /> Ask
        </button>
      </div>
    </div>
   </>
  );
}

// ── send-time helpers (Gmail-style schedule-send) ──
const _pad = (n) => String(n).padStart(2, "0");
// Local time -> the value a <input type="datetime-local"> expects.
function _toLocalInput(d) {
  return `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}` +
         `T${_pad(d.getHours())}:${_pad(d.getMinutes())}`;
}
function _tomorrow9am() {
  const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return d;
}
// Human label for a chosen/returned time, e.g. "Jun 9, 9:00 AM".
function _fmtWhen(d) {
  return d.toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

// One drafted follow-up the host can edit, time, and approve. Approve schedules
// a real send (or sends now) via the ScheduledFollowup queue — never a dead-end
// draft. The auto-send toggle still gates whether a scheduled row auto-fires.
function ProposalCard({ proposal, auto }) {
  const [draft, setDraft] = useState(proposal.text || "");
  const [choice, setChoice] = useState("tomorrow"); // now | 2h | tomorrow | custom
  const [customVal, setCustomVal] = useState(() =>
    _toLocalInput(proposal.suggested_send_at
      ? new Date(proposal.suggested_send_at) : _tomorrow9am()));
  const [state, setState] = useState("idle");   // idle | sending | done | error
  const [result, setResult] = useState(null);   // { sent, when }
  const [err, setErr] = useState("");

  // Resolve the current choice to a send_at Date (null = send now).
  const resolveSendAt = () => {
    if (choice === "now") return null;
    if (choice === "2h") return new Date(Date.now() + 2 * 3600 * 1000);
    if (choice === "custom") {
      const d = new Date(customVal);
      return isNaN(d.getTime()) ? null : d;
    }
    return _tomorrow9am();
  };

  const approve = async () => {
    setState("sending"); setErr("");
    const at = resolveSendAt();
    try {
      const r = await api.scheduleContactFollowup(
        proposal.contact_id, draft.trim(), at ? at.toISOString() : null);
      setResult({
        sent: r.status === "sent",
        when: r.send_at ? new Date(r.send_at) : null,
      });
      setState("done");
    } catch (e) {
      setState("error"); setErr(e.message || String(e));
    }
  };

  const done = state === "done";
  const sending = state === "sending";
  const PRESETS = [
    ["now", "Send now"], ["2h", "In 2 hours"],
    ["tomorrow", "Tomorrow 9am"], ["custom", "Custom…"],
  ];

  return (
    <div style={{ marginTop: 10, border: `1px solid ${C.line}`,
                  borderRadius: 12, padding: "12px 14px", background: "#fff" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8,
                    marginBottom: 8 }}>
        <span style={{ fontWeight: 700, color: C.ink, fontSize: 13.5 }}>
          {proposal.contact_name || "Contact"}
        </span>
        <span style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 0.3,
                       textTransform: "uppercase", color: C.accent }}>
          draft follow-up
        </span>
      </div>
      <textarea value={draft} onChange={(e) => setDraft(e.target.value)}
                disabled={done || sending} rows={3}
                style={{ width: "100%", border: `1px solid ${C.line}`,
                         borderRadius: 8, padding: "8px 10px", fontSize: 13,
                         fontFamily: FONT, resize: "vertical", color: C.ink,
                         boxSizing: "border-box", outline: "none",
                         background: done ? "#f7f7fa" : "#fff" }} />
      {proposal.rationale && !done && (
        <div style={{ fontSize: 11.5, color: C.faint, marginTop: 5 }}>
          Why: {proposal.rationale}
        </div>
      )}

      {!done && (
        <>
          {/* Send-time presets : Gmail-style schedule-send. */}
          <div style={{ display: "flex", alignItems: "center", gap: 6,
                        flexWrap: "wrap", marginTop: 10 }}>
            <Clock size={13} color={C.faint} />
            {PRESETS.map(([key, label]) => (
              <button key={key} onClick={() => setChoice(key)} disabled={sending}
                      style={{ border: `1px solid ${choice === key ? C.accent : C.line}`,
                               background: choice === key ? C.chipBg : "#fff",
                               color: choice === key ? C.accent : C.muted,
                               borderRadius: 999, padding: "4px 11px",
                               fontSize: 12, fontWeight: 600, cursor: "pointer",
                               fontFamily: FONT }}>
                {label}
              </button>
            ))}
          </div>
          {choice === "custom" && (
            <input type="datetime-local" value={customVal} disabled={sending}
                   onChange={(e) => setCustomVal(e.target.value)}
                   min={_toLocalInput(new Date())}
                   style={{ marginTop: 8, border: `1px solid ${C.line}`,
                            borderRadius: 8, padding: "7px 10px", fontSize: 13,
                            fontFamily: FONT, color: C.ink, outline: "none" }} />
          )}
        </>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10,
                    marginTop: 10 }}>
        {!done ? (
          <button onClick={approve} disabled={sending || !draft.trim()}
                  style={{ display: "flex", alignItems: "center", gap: 6,
                           background: C.accent, color: "#fff", border: "none",
                           borderRadius: 8, padding: "7px 14px", fontSize: 13,
                           fontWeight: 600, cursor: "pointer", fontFamily: FONT,
                           opacity: sending || !draft.trim() ? 0.6 : 1 }}>
            {choice === "now" ? <Send size={14} /> : <Clock size={14} />}
            {sending ? "Working…" : choice === "now" ? "Send now" : "Schedule send"}
          </button>
        ) : (
          <span style={{ display: "flex", alignItems: "center", gap: 6,
                         color: result?.sent ? "#1c8c4e" : C.accent,
                         fontSize: 13, fontWeight: 600 }}>
            <Check size={15} />
            {result?.sent ? "Sent"
              : `Scheduled for ${result?.when ? _fmtWhen(result.when) : "later"}`}
          </span>
        )}
        {!done && (
          <span style={{ fontSize: 11.5, color: C.faint }}>
            {choice === "now"
              ? "Sends immediately"
              : auto ? "Will send automatically at the chosen time"
                     : "Auto-send is off, so you'll confirm it then"}
          </span>
        )}
        {done && !result?.sent && !auto && (
          <span style={{ fontSize: 11.5, color: C.faint }}>
            auto-send is off, confirm it from your follow-up queue
          </span>
        )}
        {state === "error" && (
          <span style={{ fontSize: 12, color: "#c0432f" }}>{err}</span>
        )}
      </div>
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
