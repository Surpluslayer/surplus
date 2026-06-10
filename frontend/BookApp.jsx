// ── BookApp : the advisor "Your book today" surface ─────────────────────────
// Phone-first home for a relationship-led professional (wealth advisor / lawyer)
// whose income depends on keeping an existing book warm. Six screens, matching
// the Surplus design reference (surplus-design.html):
//
//   Today        — dated "Your book today", the agent ask bar, then two lists:
//                  Updates (prospecting signals) + Needs outreach.
//   Book         — the full roster: assistant card, filter pills, attention-
//                  sorted list, "Show N more".
//   Add contact  — a bottom sheet: event picker, two-step banner, capture tabs.
//   Relationship — name + health, a "Why she's …" reasoning panel, a drafted
//                  message (Send / Refine / Snooze), and a timeline.
//   Account      — profile, Connections + Plan, Sign out. (JL avatar → here.)
//   Connections  — LinkedIn / Gmail / Google Calendar, with live status.
//
// Backed by /api/book/* (routes/book.py → agents/book.py) and /api/auth/me.
// Self-contained (own CSS + design tokens) so it stays isolated from the event
// flow — same pattern as InPersonApp.
import React, { useState, useEffect, useCallback } from "react";
import {
  Sparkles, ArrowUp, Star, LayoutDashboard, Plus, BookText, Loader2, X,
  ChevronLeft, ChevronRight, ChevronDown, MapPin, QrCode, Link2, Search, Send,
  Mail, Calendar, Plug, CreditCard, LogOut, CheckCircle2,
} from "lucide-react";
import { api } from "./lib/api.js";

// Health word + colour token by relationship status.
const HEALTH = {
  active: "active", warm: "warm", cooling: "cooling", dormant: "dormant", new: "new",
};
const HEALTH_WORD = {
  active: "Active", warm: "Warm", cooling: "Cooling", dormant: "Dormant", new: "New",
};

export default function BookApp() {
  const [user, setUser] = useState(null);       // null=loading, undefined=signed out
  const [feed, setFeed] = useState(null);        // null=loading
  const [err, setErr] = useState("");
  const [tab, setTab] = useState("today");       // "today" | "book"
  const [route, setRoute] = useState(null);      // {name:"detail",row} | {name:"account"} | {name:"connections"} | null
  const [addOpen, setAddOpen] = useState(false);
  const [draftFor, setDraftFor] = useState(null);// {name, contact_id, trigger}

  // Fonts: load Inter + Newsreader only for this surface (the desktop App ships
  // its own type), injected once so the design tokens resolve.
  useEffect(() => { _ensureFonts(); }, []);

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then((u) => { if (!cancelled) setUser(u && u.id ? u : undefined); })
      .catch((e) => { if (!cancelled) setUser(e?.status === 401 ? undefined : {}); });
    return () => { cancelled = true; };
  }, []);

  const load = useCallback(() => {
    setErr("");
    api.bookToday().then(setFeed).catch((e) => setErr(e.message || String(e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const openDetail = (row) => setRoute({ name: "detail", row });
  const openDraft = (d) => setDraftFor(d);
  const goTab = (t) => { setRoute(null); setTab(t); };

  // Which bottom-nav item reads as active.
  const activeNav = route?.name === "detail" ? "book"
    : route ? "" : tab;

  let screen;
  if (route?.name === "detail") {
    screen = <RelationshipScreen row={route.row} onBack={() => goTab("book")}
                                 onDraftDone={() => {}} />;
  } else if (route?.name === "account") {
    screen = <AccountScreen user={user} onBack={() => goTab("today")}
                            onConnections={() => setRoute({ name: "connections" })} />;
  } else if (route?.name === "connections") {
    screen = <ConnectionsScreen user={user}
                                onBack={() => setRoute({ name: "account" })} />;
  } else if (tab === "book") {
    screen = <BookView feed={feed} err={err} onReload={load}
                       onOpen={openDetail} onDraft={openDraft} />;
  } else {
    screen = <TodayView feed={feed} err={err} user={user} onReload={load}
                        onAccount={() => setRoute({ name: "account" })}
                        onOpen={openDetail} onDraft={openDraft} />;
  }

  return (
    <div className="bk-root">
      <style>{BOOK_CSS}</style>
      <div className="bk-frame">
        {screen}
        <nav className="bk-nav">
          <button className={"bk-nav-item" + (activeNav === "today" ? " on" : "")}
                  onClick={() => goTab("today")}>
            <LayoutDashboard size={19} /><span>Today</span>
          </button>
          <button className="bk-nav-add" onClick={() => setAddOpen(true)}
                  aria-label="Add contact">
            <span className="bk-fab"><Plus size={22} /></span><span>Add</span>
          </button>
          <button className={"bk-nav-item" + (activeNav === "book" ? " on" : "")}
                  onClick={() => goTab("book")}>
            <BookText size={19} /><span>Book</span>
          </button>
        </nav>
      </div>

      {addOpen && <AddSheet feed={feed} onClose={() => setAddOpen(false)} />}
      {draftFor && <DraftSheet draft={draftFor} onClose={() => setDraftFor(null)} />}
    </div>
  );
}

// ── Today ────────────────────────────────────────────────────────────────────

function TodayView({ feed, err, user, onReload, onAccount, onOpen, onDraft }) {
  const updates = feed?.updates || [];
  const needs = feed?.needs_outreach || [];
  const initials = _initials(user?.name || feed?.advisor_name);

  return (
    <div className="bk-scroll">
      <header className="bk-topbar">
        <div>
          <p className="bk-eyebrow">{_today_long()}</p>
          <p className="bk-display">Your book today</p>
        </div>
        <button className="bk-avatar" onClick={onAccount} aria-label="Account"
                title={user?.name || ""}>{initials}</button>
      </header>

      <AskBar variant="bar" onOpen={onOpen} onDraft={onDraft} />

      {err && <div className="bk-err">{err} <button className="bk-link" onClick={onReload}>Retry</button></div>}
      {!feed && !err && <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Reading your book…</div>}

      {feed && (
        <>
          <SectionHead label="Updates" count={updates.length} />
          <div className="bk-group">
            {updates.map((u, i) => (
              <Row key={`u${i}`} onOpen={u.contact_id ? () => onOpen(u) : null}>
                <div className="bk-main">
                  <p className="bk-name">{u.name}{u.vip && <Star size={13} className="bk-star" fill="currentColor" />}</p>
                  <p className="bk-sub">{u.headline}</p>
                </div>
                <div className="bk-aside">
                  <p className="bk-time">{_rel_time(u.detected_at)}</p>
                  {u.can_draft && <DraftLink onClick={() => onDraft({ name: u.name, contact_id: u.contact_id, trigger: u.trigger || u.headline })} />}
                </div>
              </Row>
            ))}
            {updates.length === 0 && <Empty text="No new updates today." />}
          </div>

          <SectionHead label="Needs outreach" count={needs.length} />
          <div className="bk-group">
            {needs.map((n, i) => (
              <Row key={`n${i}`} onOpen={n.contact_id ? () => onOpen(n) : null}>
                <div className="bk-main">
                  <p className="bk-name">{n.name}{n.vip && <Star size={13} className="bk-star" fill="currentColor" />}</p>
                  <p className="bk-sub">{n.reason}</p>
                </div>
                <DraftLink onClick={() => onDraft({ name: n.name, contact_id: n.contact_id, trigger: n.trigger || n.reason })} />
              </Row>
            ))}
            {needs.length === 0 && <Empty text="Everyone's warm. Nothing overdue." />}
          </div>
        </>
      )}
    </div>
  );
}

// ── Book (roster) ─────────────────────────────────────────────────────────────

const FILTERS = [
  { key: "all", label: "All" },
  { key: "starred", label: "Starred" },
  { key: "cooling", label: "Cooling" },
  { key: "prospects", label: "Prospects" },
];

function BookView({ feed, err, onReload, onOpen, onDraft }) {
  const [filter, setFilter] = useState("all");
  const [expanded, setExpanded] = useState(false);
  const roster = feed?.roster || [];

  const shown = roster.filter((r) => {
    if (filter === "starred") return r.vip;
    if (filter === "cooling") return r.status === "cooling" || r.status === "dormant";
    if (filter === "prospects") return r.is_prospect;
    return true;
  });
  const cap = expanded ? shown.length : 6;
  const visible = shown.slice(0, cap);
  const more = shown.length - visible.length;

  return (
    <div className="bk-scroll">
      <header className="bk-topbar bk-topbar--center">
        <span className="bk-display bk-display--row">
          Your book <span className="bk-count-lg">{roster.length}</span>
        </span>
      </header>

      <AskBar variant="card" onOpen={onOpen} onDraft={onDraft} />

      <div className="bk-pills">
        {FILTERS.map((f) => (
          <button key={f.key}
                  className={"bk-pill" + (filter === f.key ? " on" : "")}
                  onClick={() => { setFilter(f.key); setExpanded(false); }}>
            {f.label}
          </button>
        ))}
      </div>
      <p className="bk-hint">Sorted by who needs attention</p>

      {err && <div className="bk-err">{err} <button className="bk-link" onClick={onReload}>Retry</button></div>}
      {!feed && !err && <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Loading your book…</div>}

      {feed && (
        <>
          <div className="bk-group">
            {visible.map((r, i) => (
              <Row key={i} onOpen={() => onOpen(r)}>
                <div className="bk-main">
                  <p className="bk-name">{r.name}{r.vip && <Star size={13} className="bk-star" fill="currentColor" />}</p>
                  <p className="bk-sub">{[r.title, r.firm].filter(Boolean).join(" · ")}</p>
                  <p className="bk-meta">{_book_meta(r)}</p>
                </div>
                <Health status={r.is_prospect ? "new" : r.status} />
              </Row>
            ))}
            {visible.length === 0 && <Empty text="No one matches this filter." />}
          </div>
          {more > 0 && (
            <p className="bk-more" onClick={() => setExpanded(true)}>Show {more} more</p>
          )}
        </>
      )}
    </div>
  );
}

// ── Relationship detail ───────────────────────────────────────────────────────

function RelationshipScreen({ row, onBack }) {
  const id = row?.contact_id;
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!id) { setErr("This contact isn't in your book yet."); return; }
    let cancelled = false;
    setD(null); setErr("");
    api.bookRelationship(id)
      .then((r) => { if (!cancelled) setD(r); })
      .catch((e) => { if (!cancelled) setErr(e.message || "Couldn't load"); });
    return () => { cancelled = true; };
  }, [id]);

  const status = d?.is_prospect ? "new" : d?.status;
  const stat = d && [
    d.days_since > 0 ? `last spoke ${d.days_since} days ago` : "just met",
    d.value,
  ].filter(Boolean).join(" · ");

  return (
    <div className="bk-scroll">
      <div className="bk-detail-head">
        <button className="bk-back" onClick={onBack} aria-label="Back to book"><ChevronLeft size={20} /></button>
        <span className="bk-crumb">Your book</span>
      </div>

      <div className="bk-subhead">
        <p className="bk-display bk-display--lg">
          {row?.name || d?.name}
          {(row?.vip || d?.vip) && <Star size={16} className="bk-star" fill="currentColor" style={{ marginLeft: 6 }} />}
        </p>
        <p className="bk-role">{[d?.title || row?.title, d?.firm || row?.firm].filter(Boolean).join(" · ")}</p>
        {d && (
          <div className="bk-stat">
            <Health status={status} />
            {stat && <span className="bk-stat-sep">· {stat}</span>}
          </div>
        )}
      </div>

      {err && <div className="bk-err">{err}</div>}
      {!d && !err && <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Reading the relationship…</div>}

      {d && (
        <>
          <div className="bk-panel">
            <div className="bk-panel-head"><Sparkles size={16} /><span>Why {_first(d.name)}'s {HEALTH_WORD[status]?.toLowerCase() || "here"}</span></div>
            <p className="bk-panel-p">{d.why}</p>
          </div>

          <DraftPanel detail={d} />

          <p className="bk-sec-label bk-sec-label--tl">Timeline</p>
          <div className="bk-tl">
            {(d.timeline || []).map((t, i) => (
              <div className="bk-tl-item" key={i}>
                <span className={"bk-tl-dot" + (t.warn ? " warn" : "")} />
                <div>
                  <p className="bk-tl-t">{t.t}</p>
                  {t.d && <p className="bk-tl-d">{t.d}</p>}
                </div>
              </div>
            ))}
            {(d.timeline || []).length === 0 && <Empty text="No history yet." />}
          </div>
        </>
      )}
    </div>
  );
}

function DraftPanel({ detail }) {
  const [busy, setBusy] = useState(true);
  const [body, setBody] = useState("");
  const [err, setErr] = useState("");
  const [sent, setSent] = useState(false);

  const fetchDraft = useCallback(() => {
    setBusy(true); setErr(""); setSent(false);
    api.bookDraft({ contact_id: detail.contact_id, name: detail.name,
                    trigger: detail.reason || "catching up", channel: "email" })
      .then((r) => setBody(r.body || ""))
      .catch((e) => setErr(e.message || "Couldn't draft"))
      .finally(() => setBusy(false));
  }, [detail]);
  useEffect(() => { fetchDraft(); }, [fetchDraft]);

  const send = async () => {
    try { await navigator.clipboard.writeText(body); setSent(true);
          setTimeout(() => setSent(false), 1800); } catch {}
  };

  return (
    <div className="bk-panel">
      <p className="bk-panel-label">Drafted re-engagement</p>
      {busy ? (
        <div className="bk-loading bk-loading--tight"><Loader2 className="bk-spin" size={16} /> Writing in your voice…</div>
      ) : err ? (
        <div className="bk-err">{err}</div>
      ) : (
        <>
          <div className="bk-quote"><p>{body}</p></div>
          <div className="bk-actions">
            <button className="bk-btn bk-btn--primary" onClick={send}>
              <Send size={13} style={{ marginRight: 5, verticalAlign: -1 }} />{sent ? "Copied" : "Send"}
            </button>
            <button className="bk-btn" onClick={fetchDraft}>Refine</button>
            <button className="bk-btn" onClick={() => {}}>Snooze</button>
          </div>
        </>
      )}
    </div>
  );
}

// ── Account ───────────────────────────────────────────────────────────────────

function AccountScreen({ user, onBack, onConnections }) {
  const initials = _initials(user?.name);
  const calendarOff = true; // No calendar backend yet — surfaced as the hint.
  const plan = user?.billing?.plan_label || (user?.paid_at ? "Pro" : "Individual");

  const signOut = async () => {
    try { await api.logout(); } catch {}
    window.location.reload();
  };

  return (
    <div className="bk-scroll">
      <div className="bk-detail-head">
        <button className="bk-back" onClick={onBack} aria-label="Back to Today"><ChevronLeft size={20} /></button>
        <span className="bk-crumb">Today</span>
      </div>

      <div className="bk-acct-head">
        <div className="bk-avatar-lg">{initials}</div>
        <div>
          <p className="bk-acct-name">{user?.name || "Your account"}</p>
          {user?.email && <p className="bk-acct-email">{user.email}</p>}
        </div>
      </div>

      <div className="bk-set-group">
        <button className="bk-set-row" onClick={onConnections}>
          <span className="bk-set-lead"><Plug size={19} /><span className="bk-set-lbl">Connections</span></span>
          <span className="bk-set-right">
            {calendarOff && <Health status="warm" word="Calendar off" />}
            <ChevronRight size={17} className="bk-chev" />
          </span>
        </button>
        <div className="bk-set-row">
          <span className="bk-set-lead"><CreditCard size={19} /><span className="bk-set-lbl">Plan</span></span>
          <span className="bk-set-right"><span className="bk-set-val">{plan}</span><ChevronRight size={17} className="bk-chev" /></span>
        </div>
      </div>

      <div className="bk-set-group">
        <button className="bk-set-row bk-set-row--danger" onClick={signOut}>
          <span className="bk-set-lead"><LogOut size={19} /><span className="bk-set-lbl">Sign out</span></span>
        </button>
      </div>
    </div>
  );
}

// ── Connections ───────────────────────────────────────────────────────────────

function ConnectionsScreen({ user, onBack }) {
  const [note, setNote] = useState("");
  const liOn = user?.linkedin_status === "active";
  const emailOn = user?.email_status === "active";

  const connect = async (starter, label) => {
    try {
      const { url } = await starter();
      if (url) window.location.assign(url);
      else setNote(`Couldn't start ${label} — try again.`);
    } catch (e) { setNote(e.message || `Couldn't start ${label}.`); }
  };

  return (
    <div className="bk-scroll">
      <div className="bk-detail-head">
        <button className="bk-back" onClick={onBack} aria-label="Back to Account"><ChevronLeft size={20} /></button>
        <span className="bk-crumb">Account</span>
      </div>
      <div className="bk-subhead"><p className="bk-display">Connections</p></div>

      <div className="bk-set-group">
        <ConnRow icon={<LinkedinGlyph size={21} />} name="LinkedIn"
                 sub="Enrichment & job-change updates"
                 connected={liOn}
                 onConnect={() => connect(api.startLinkedinAuth, "LinkedIn")} />
        <ConnRow icon={<Mail size={21} />} name="Gmail"
                 sub={emailOn && user?.email_account_address
                   ? `Connected as ${user.email_account_address}`
                   : "Tracks replies, sends your drafts"}
                 connected={emailOn}
                 onConnect={() => connect(api.startEmailAuth, "Gmail")} />
        <ConnRow icon={<Calendar size={21} />} name="Google Calendar"
                 sub="Logs meetings, books reviews"
                 connected={false}
                 onConnect={() => setNote("Calendar sync is coming soon.")} />
      </div>

      {note && <p className="bk-note bk-note--warn">{note}</p>}
      <p className="bk-note">Surplus reads these to keep your book current — it never posts or emails without you.</p>
    </div>
  );
}

function ConnRow({ icon, name, sub, connected, onConnect }) {
  return (
    <div className="bk-conn-row">
      <span className="bk-tile">{icon}</span>
      <div className="bk-main">
        <p className="bk-name">{name}</p>
        <p className="bk-sub">{sub}</p>
      </div>
      {connected ? (
        <span className="bk-conn-status"><CheckCircle2 size={14} />Connected</span>
      ) : (
        <button className="bk-btn bk-btn--primary" onClick={onConnect}>Connect</button>
      )}
    </div>
  );
}

// ── Add contact (bottom sheet) ────────────────────────────────────────────────

const CAPTURE_TABS = [
  { key: "qr", label: "Scan QR", icon: QrCode },
  { key: "link", label: "Paste link", icon: Link2 },
  { key: "name", label: "By name", icon: Search },
];

function AddSheet({ feed, onClose }) {
  const [event, setEvent] = useState("Founders Inc");
  const [draftEvent, setDraftEvent] = useState("");
  const [tab, setTab] = useState("qr");
  const recents = ["Founders Inc", "NYC Tech Week", "SALT Conference"];

  return (
    <div className="bk-sheet-scrim" onClick={onClose}>
      <div className="bk-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="bk-grabber"><span /></div>
        <div className="bk-sheet-title">
          <span className="bk-display">Add contact</span>
          <button className="bk-sheet-x" onClick={onClose} aria-label="Close"><X size={20} /></button>
        </div>

        <div className="bk-event">
          <div className="bk-event-current">
            <span className="bk-event-name"><MapPin size={18} />{event}</span>
            <ChevronDown size={18} className="bk-faint" />
          </div>
          <div className="bk-field" style={{ marginTop: 11 }}>
            <input value={draftEvent} onChange={(e) => setDraftEvent(e.target.value)}
                   placeholder="e.g. NYC Tech Week — Founders Inc" />
            <button className="bk-btn bk-btn--primary" style={{ height: 36 }}
                    onClick={() => { if (draftEvent.trim()) { setEvent(draftEvent.trim()); setDraftEvent(""); } }}>Set</button>
          </div>
          <div className="bk-chips bk-recents">
            {recents.map((r) => (
              <button key={r} className={"bk-pill" + (event === r ? " on" : "")}
                      onClick={() => setEvent(r)}>{r}</button>
            ))}
          </div>
        </div>

        <div className="bk-banner"><b>1.</b> Add the person · <b>2.</b> Connect. That's it.</div>

        <div className="bk-tabs">
          {CAPTURE_TABS.map((t) => {
            const I = t.icon;
            return (
              <button key={t.key} className={"bk-tab" + (tab === t.key ? " on" : "")}
                      onClick={() => setTab(t.key)}><I size={16} /> {t.label}</button>
            );
          })}
        </div>

        {tab === "qr" && (
          <div className="bk-scan">
            <div className="bk-target"><QrCode size={42} /></div>
            <p className="bk-scan-lead">Point at their badge or QR</p>
            <p className="bk-scan-sub">It lands in your book in seconds. No camera? Switch to <b>Paste link</b>.</p>
          </div>
        )}
        {tab === "link" && (
          <div className="bk-scan">
            <div className="bk-field">
              <input placeholder="Paste a LinkedIn URL" />
              <button className="bk-btn bk-btn--primary" style={{ height: 36 }}>Add</button>
            </div>
            <p className="bk-scan-sub" style={{ marginTop: 14 }}>We'll enrich name, title and firm from the profile.</p>
          </div>
        )}
        {tab === "name" && (
          <div className="bk-scan">
            <div className="bk-field">
              <input placeholder="Full name" />
              <button className="bk-btn bk-btn--primary" style={{ height: 36 }}>Add</button>
            </div>
            <p className="bk-scan-sub" style={{ marginTop: 14 }}>Best when you already know who they are.</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Ask bar / assistant card (agent) ──────────────────────────────────────────

const CHIPS = ["Who's cooling?", "Reviews due", "Quiet 30+ days"];

function AskBar({ variant, onOpen, onDraft }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);   // {answer, people}
  const [err, setErr] = useState("");

  const ask = async (query) => {
    const text = (query ?? q).trim();
    if (!text || busy) return;
    setBusy(true); setErr(""); setRes(null); setQ(text);
    try { setRes(await api.bookAsk(text)); }
    catch (e) { setErr(e.message || "Couldn't ask the agent"); }
    finally { setBusy(false); }
  };

  const input = (
    <input className="bk-ask-input"
      placeholder={variant === "card" ? "Ask about anyone, or who to follow up with…" : "Ask your agent anything…"}
      value={q} onChange={(e) => setQ(e.target.value)}
      onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
  );
  const go = (
    <button className={variant === "card" ? "bk-send" : "bk-ask-go"} onClick={() => ask()}
            disabled={busy || !q.trim()} aria-label="Ask">
      {busy ? <Loader2 size={16} className="bk-spin" /> : <ArrowUp size={16} />}
    </button>
  );

  return (
    <div className={variant === "card" ? "bk-assistant" : "bk-ask-wrap"}>
      {variant === "card" ? (
        <>
          <div className="bk-assistant-head"><Sparkles size={16} /><span>Relationship assistant</span></div>
          <div className="bk-field">{input}{go}</div>
        </>
      ) : (
        <div className="bk-ask">
          <Sparkles size={17} className="bk-ask-spark" />
          {input}
          {go}
        </div>
      )}

      {!res && !busy && (
        <div className="bk-chips" style={variant === "card" ? { marginTop: 10 } : undefined}>
          {CHIPS.map((c) => (
            <button key={c} className="bk-chip" onClick={() => ask(c)}>{c}</button>
          ))}
        </div>
      )}

      {err && <div className="bk-err" style={{ marginTop: 8 }}>{err}</div>}

      {res && (
        <div className="bk-answer">
          <div className="bk-answer-text">{res.answer}</div>
          {(res.people || []).length > 0 && (
            <div className="bk-answer-people">
              {res.people.map((p, i) => (
                <div key={i} className="bk-answer-person">
                  <div className="bk-ap-main">
                    <div className="bk-ap-name">{p.name}</div>
                    {p.reason && <div className="bk-ap-reason">{p.reason}</div>}
                    {p.draft && <div className="bk-ap-draft">"{p.draft}"</div>}
                  </div>
                  <DraftLink onClick={() => onDraft({ name: p.name, trigger: p.reason || "catch up" })} />
                </div>
              ))}
            </div>
          )}
          <button className="bk-link" onClick={() => { setRes(null); setQ(""); }}>Clear</button>
        </div>
      )}
    </div>
  );
}

// ── Draft sheet (Draft → tap) ──────────────────────────────────────────────────

function DraftSheet({ draft, onClose }) {
  const [busy, setBusy] = useState(true);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBusy(true); setErr("");
    api.bookDraft({ name: draft.name, contact_id: draft.contact_id,
                    trigger: draft.trigger, channel: "email" })
      .then((r) => { if (!cancelled) { setSubject(r.subject || ""); setBody(r.body || ""); } })
      .catch((e) => { if (!cancelled) setErr(e.message || "Couldn't draft"); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [draft]);

  const copy = async () => {
    const text = subject ? `Subject: ${subject}\n\n${body}` : body;
    try { await navigator.clipboard.writeText(text); setCopied(true);
          setTimeout(() => setCopied(false), 1600); } catch {}
  };

  return (
    <div className="bk-sheet-scrim" onClick={onClose}>
      <div className="bk-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="bk-grabber"><span /></div>
        <div className="bk-sheet-title">
          <div>
            <span className="bk-display" style={{ fontSize: 20 }}>To {draft.name}</span>
            <p className="bk-sub" style={{ marginTop: 2 }}>{draft.trigger}</p>
          </div>
          <button className="bk-sheet-x" onClick={onClose} aria-label="Close"><X size={20} /></button>
        </div>

        {busy ? (
          <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Writing in your voice…</div>
        ) : err ? (
          <div className="bk-err">{err}</div>
        ) : (
          <>
            {subject !== "" && (
              <input className="bk-sheet-subject" value={subject}
                     onChange={(e) => setSubject(e.target.value)} placeholder="Subject" />
            )}
            <textarea className="bk-sheet-body" value={body}
                      onChange={(e) => setBody(e.target.value)} rows={6} />
            <div className="bk-actions" style={{ marginTop: 14 }}>
              <button className="bk-btn bk-btn--block" onClick={copy}>
                {copied ? "Copied" : "Copy message"}
              </button>
              <button className="bk-btn bk-btn--primary bk-btn--block" onClick={onClose}>Looks good</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── shared bits ────────────────────────────────────────────────────────────────

function Row({ children, onOpen }) {
  return (
    <div className={"bk-row" + (onOpen ? " bk-row--tap" : "")}
         onClick={onOpen || undefined} role={onOpen ? "button" : undefined}>
      {children}
    </div>
  );
}

function SectionHead({ label, count }) {
  return (
    <div className="bk-sec">
      <span className="bk-sec-label">{label} <span className="bk-count">· {count}</span></span>
    </div>
  );
}

function Health({ status, word }) {
  const s = HEALTH[status] || "warm";
  return (
    <span className={`bk-health ${s}`}>
      {s !== "new" && <span className="bk-health-dot" />}
      {word || HEALTH_WORD[status] || ""}
    </span>
  );
}

function DraftLink({ onClick }) {
  return (
    <button className="bk-draft" onClick={(e) => { e.stopPropagation(); onClick(); }}>
      Draft <span aria-hidden>→</span>
    </button>
  );
}

function Empty({ text }) { return <div className="bk-empty">{text}</div>; }

// lucide dropped brand icons — render the LinkedIn mark inline so the tile
// matches the design's ti-brand-linkedin.
function LinkedinGlyph({ size = 21 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.13 1.45-2.13 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.45v6.29zM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13zM7.12 20.45H3.56V9h3.56v11.45zM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.22.79 24 1.77 24h20.45c.98 0 1.78-.78 1.78-1.73V1.73C24 .77 23.2 0 22.22 0z"/>
    </svg>
  );
}

// ── helpers ─────────────────────────────────────────────────────────────────────

function _ensureFonts() {
  if (typeof document === "undefined") return;
  if (document.getElementById("bk-fonts")) return;
  const l = document.createElement("link");
  l.id = "bk-fonts";
  l.rel = "stylesheet";
  l.href = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500&family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap";
  document.head.appendChild(l);
}

function _initials(name) {
  if (!name) return "•";
  const parts = String(name).trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() || "").join("") || "•";
}

function _first(name) { return String(name || "they").trim().split(/\s+/)[0]; }

function _book_meta(r) {
  const bits = [];
  if (r.met_at) bits.push(`Met at ${r.met_at}`);
  if (r.is_prospect) bits.push("moments ago");
  else if (r.review_due) bits.push(r.days_since > 0 ? `review overdue ${r.days_since}d` : "review due");
  else if (r.days_since > 0) bits.push(`last spoke ${r.days_since}d ago`);
  return bits.join(" · ");
}

function _today_long() {
  try {
    return new Date().toLocaleDateString(undefined,
      { weekday: "long", month: "long", day: "numeric" });
  } catch { return ""; }
}

function _rel_time(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const ms = Date.now() - d.getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 2) return "Yesterday";
  if (day < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ── styles (ported from surplus-design.html design tokens) ────────────────────

const BOOK_CSS = `
.bk-root{
  --ink:#1b1e22; --muted:#5b616a; --faint:#99a0a8;
  --bg:#ffffff; --surface:#f4f5f7;
  --line:rgba(20,23,28,.08); --line-2:rgba(20,23,28,.16);
  --accent:#2f6df6; --accent-bg:#eaf1fe;
  --success:#1f9d62; --success-bg:#e7f5ee;
  --warning:#b07210; --warning-bg:#fbf1e1;
  --danger:#c0433d; --danger-bg:#fbeceb;
  --gold:#ba7517;
  --r-sm:8px; --r-md:10px; --r-lg:14px;
  --font-ui:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-display:'Newsreader',Georgia,'Times New Roman',serif;
  min-height:100dvh; background:#e9ebee; display:flex; justify-content:center;
  font-family:var(--font-ui); font-size:14px; line-height:1.5; color:var(--ink);
  -webkit-font-smoothing:antialiased;
}
.bk-root *{box-sizing:border-box;}
.bk-frame{width:100%; max-width:430px; min-height:100dvh; background:var(--bg);
  display:flex; flex-direction:column; position:relative;}
.bk-spin{animation:bkspin 1s linear infinite;}
@keyframes bkspin{to{transform:rotate(360deg);}}

.bk-scroll{flex:1; overflow-y:auto; padding-bottom:20px;}

/* topbar / headings */
.bk-topbar{display:flex; align-items:flex-start; justify-content:space-between; padding:18px 18px 14px;}
.bk-topbar--center{align-items:center; padding-bottom:12px;}
.bk-eyebrow{font-size:12px; color:var(--faint); margin:0 0 2px;}
.bk-display{font-family:var(--font-display); font-size:23px; font-weight:400; margin:0; color:var(--ink);}
.bk-display--lg{font-size:24px;}
.bk-display--row{display:inline-flex; align-items:center; gap:10px;}
.bk-count-lg{font-size:13px; color:var(--faint); font-family:var(--font-ui);}
.bk-avatar{width:28px; height:28px; border-radius:50%; background:var(--accent-bg);
  color:var(--accent); display:flex; align-items:center; justify-content:center;
  font-size:12px; font-weight:500; flex:none; border:0; cursor:pointer; font-family:var(--font-ui);}

/* agent ask bar (Today) */
.bk-ask-wrap{padding:0 18px; margin-bottom:20px;}
.bk-ask{display:flex; align-items:center; gap:10px; background:var(--surface);
  border:.5px solid var(--line); border-radius:999px; padding:9px 11px 9px 15px;}
.bk-ask-spark{color:var(--accent); flex:none;}
.bk-ask-input{flex:1; border:0; background:none; outline:none; font-size:13px;
  color:var(--ink); font-family:var(--font-ui); min-width:0;}
.bk-ask-input::placeholder{color:var(--faint);}
.bk-ask-go{flex:none; width:28px; height:28px; border-radius:50%; border:0;
  background:var(--accent); color:#fff; display:flex; align-items:center;
  justify-content:center; cursor:pointer;}
.bk-ask-go:disabled{opacity:.4; cursor:default;}

/* assistant card (Book) */
.bk-assistant{margin:0 18px 14px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); padding:13px 14px;}
.bk-assistant-head{display:flex; align-items:center; gap:7px; margin-bottom:10px;}
.bk-assistant-head svg{color:var(--accent);}
.bk-assistant-head span{font-size:13px; font-weight:500;}
.bk-field{display:flex; align-items:center; gap:8px;}
.bk-field input{flex:1; height:36px; border:.5px solid var(--line-2); border-radius:var(--r-md);
  padding:0 12px; font:inherit; font-size:13px; background:var(--bg); color:var(--ink); min-width:0;}
.bk-field input::placeholder{color:var(--faint);}
.bk-field input:focus{outline:none; border-color:var(--accent);}
.bk-send{width:36px; height:36px; flex:none; border:.5px solid var(--accent);
  background:var(--accent-bg); color:var(--accent); border-radius:var(--r-md);
  display:flex; align-items:center; justify-content:center; cursor:pointer;}
.bk-send:disabled{opacity:.5; cursor:default;}

/* chips */
.bk-chips{display:flex; flex-wrap:wrap; gap:6px;}
.bk-chip{font-size:11px; color:var(--ink); background:var(--bg); border:.5px solid var(--line-2);
  border-radius:var(--r-md); padding:5px 10px; cursor:pointer; font-family:var(--font-ui);}

/* filter pills */
.bk-pills{display:flex; gap:7px; flex-wrap:wrap; padding:0 18px 12px;}
.bk-pill{font-size:12px; color:var(--muted); background:var(--surface); padding:5px 12px;
  border-radius:999px; cursor:pointer; border:0; font-family:var(--font-ui);}
.bk-pill.on{background:var(--accent-bg); color:var(--accent); font-weight:500;}
.bk-hint{font-size:11px; color:var(--faint); margin:0 18px 8px;}
.bk-more{text-align:center; font-size:12px; color:var(--accent); margin:0 0 8px; cursor:pointer;}

/* section label + count */
.bk-sec{padding:0 18px 6px; display:flex; align-items:baseline; justify-content:space-between;}
.bk-sec-label{font-size:13px; font-weight:500;}
.bk-sec-label .bk-count{color:var(--faint); font-weight:400;}
.bk-sec-label--tl{margin:4px 18px 8px;}

/* grouped list */
.bk-group{margin:0 18px 20px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); overflow:hidden;}
.bk-row{display:flex; align-items:center; justify-content:space-between; gap:8px; padding:11px 14px;}
.bk-row + .bk-row{border-top:.5px solid var(--line);}
.bk-row--tap{cursor:pointer;}
.bk-row--tap:active{background:rgba(20,23,28,.03);}
.bk-main{min-width:0; flex:1;}
.bk-name{font-size:14px; font-weight:500; margin:0; display:flex; align-items:center; gap:6px;}
.bk-sub{font-size:12px; color:var(--muted); margin:2px 0 0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.bk-meta{font-size:11px; color:var(--faint); margin:3px 0 0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.bk-aside{text-align:right; white-space:nowrap; display:flex; flex-direction:column; align-items:flex-end; gap:3px; flex:none;}
.bk-time{font-size:11px; color:var(--faint); margin:0;}
.bk-star{color:var(--gold); flex:none;}
.bk-draft{font-size:12px; color:var(--accent); cursor:pointer; white-space:nowrap; border:0;
  background:none; font-family:var(--font-ui); padding:0;}
.bk-empty{padding:18px 14px; text-align:center; color:var(--faint); font-size:13px;}

/* health pip + word */
.bk-health{display:inline-flex; align-items:center; gap:5px; font-size:11px; white-space:nowrap; flex:none;}
.bk-health-dot{width:7px; height:7px; border-radius:50%;}
.bk-health.cooling, .bk-health.dormant{color:var(--danger);}
.bk-health.cooling .bk-health-dot, .bk-health.dormant .bk-health-dot{background:var(--danger);}
.bk-health.warm{color:var(--warning);}
.bk-health.warm .bk-health-dot{background:var(--warning);}
.bk-health.active{color:var(--success);}
.bk-health.active .bk-health-dot{background:var(--success);}
.bk-health.new{color:var(--accent);}

/* states */
.bk-loading{display:flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; padding:18px;}
.bk-loading--tight{padding:8px 0;}
.bk-err{margin:0 18px 14px; background:var(--danger-bg); color:var(--danger);
  border:.5px solid rgba(192,67,61,.2); border-radius:var(--r-md); padding:10px 13px; font-size:13px;}
.bk-link{background:none; border:0; color:var(--accent); font-weight:500; cursor:pointer;
  font-size:13px; font-family:var(--font-ui); padding:2px 0;}

/* answer (agent) */
.bk-answer{margin-top:12px; background:var(--accent-bg); border:.5px solid rgba(47,109,246,.2);
  border-radius:var(--r-lg); padding:13px 15px;}
.bk-answer-text{font-size:14px; color:var(--ink); line-height:1.5;}
.bk-answer-people{margin-top:10px; display:flex; flex-direction:column; gap:8px;}
.bk-answer-person{display:flex; align-items:flex-start; justify-content:space-between; gap:10px;
  background:var(--bg); border:.5px solid var(--line); border-radius:var(--r-md); padding:9px 11px;}
.bk-ap-name{font-size:13px; font-weight:500; color:var(--ink);}
.bk-ap-reason{font-size:12px; color:var(--muted); margin-top:1px;}
.bk-ap-draft{font-size:12px; color:var(--muted); font-style:italic; margin-top:4px; line-height:1.4;}

/* bottom nav */
.bk-nav{display:flex; align-items:center; border-top:.5px solid var(--line); padding:8px 0
  calc(8px + env(safe-area-inset-bottom)); background:var(--bg); position:sticky; bottom:0;}
.bk-nav-item{flex:1; text-align:center; color:var(--faint); cursor:pointer; border:0; background:none;
  font-family:var(--font-ui); display:flex; flex-direction:column; align-items:center; gap:2px;}
.bk-nav-item svg{display:block;}
.bk-nav-item span{font-size:11px;}
.bk-nav-item.on{color:var(--accent);}
.bk-nav-add{flex:1; display:flex; flex-direction:column; align-items:center; gap:2px; cursor:pointer;
  border:0; background:none; font-family:var(--font-ui);}
.bk-fab{width:44px; height:44px; border-radius:50%; background:var(--accent-bg); color:var(--accent);
  border:.5px solid var(--accent); display:flex; align-items:center; justify-content:center;}
.bk-nav-add span{font-size:11px; color:var(--accent);}

/* buttons */
.bk-btn{font:inherit; font-size:13px; border:.5px solid var(--line-2); background:var(--bg);
  color:var(--ink); border-radius:var(--r-md); padding:7px 13px; cursor:pointer; font-family:var(--font-ui);}
.bk-btn--primary{background:var(--accent-bg); color:var(--accent); border-color:var(--accent);}
.bk-btn--block{flex:1; display:flex; align-items:center; justify-content:center; gap:8px;
  font-size:15px; font-weight:500; padding:14px;}

/* add-contact sheet */
.bk-sheet-scrim{position:fixed; inset:0; background:rgba(18,22,34,.42); display:flex;
  align-items:flex-end; justify-content:center; z-index:50;}
.bk-sheet{width:100%; max-width:430px; background:var(--bg); border-radius:18px 18px 0 0;
  padding-bottom:calc(18px + env(safe-area-inset-bottom)); animation:bksheet .18s ease-out;
  max-height:92dvh; overflow-y:auto;}
@keyframes bksheet{from{transform:translateY(20px); opacity:.6;} to{transform:none; opacity:1;}}
.bk-grabber{display:flex; justify-content:center; padding:12px 0 2px;}
.bk-grabber span{width:40px; height:4px; border-radius:999px; background:var(--line-2);}
.bk-sheet-title{display:flex; align-items:center; justify-content:space-between; padding:8px 18px 12px;}
.bk-sheet-x{background:none; border:0; color:var(--faint); cursor:pointer; padding:2px;}
.bk-event{margin:0 18px 14px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); padding:12px 14px;}
.bk-event-current{display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding-bottom:11px; border-bottom:.5px solid var(--line);}
.bk-event-name{display:inline-flex; align-items:center; gap:8px; font-size:16px; font-weight:500;}
.bk-event-name svg{color:var(--accent);}
.bk-faint{color:var(--faint);}
.bk-recents{margin-top:11px;}
.bk-banner{margin:0 18px 14px; background:var(--accent-bg); color:var(--accent);
  border-radius:var(--r-md); padding:9px 12px; text-align:center; font-size:12px;}
.bk-banner b{font-weight:500;}
.bk-tabs{display:flex; gap:4px; margin:0 18px 16px; background:var(--surface);
  border-radius:var(--r-md); padding:4px;}
.bk-tab{flex:1; display:flex; align-items:center; justify-content:center; gap:6px; padding:8px 0;
  font-size:13px; color:var(--muted); border-radius:var(--r-md); cursor:pointer; border:0;
  background:none; font-family:var(--font-ui);}
.bk-tab.on{background:var(--bg); color:var(--accent); font-weight:500;}
.bk-scan{margin:0 18px 20px; border:1.5px dashed var(--line-2); border-radius:var(--r-lg);
  padding:28px 20px; text-align:center;}
.bk-target{width:92px; height:92px; margin:0 auto 16px; border-radius:var(--r-md);
  background:var(--surface); display:flex; align-items:center; justify-content:center; color:var(--accent);}
.bk-scan-lead{font-size:15px; font-weight:500; margin:0;}
.bk-scan-sub{font-size:12px; color:var(--muted); margin:7px 0 0;}
.bk-scan-sub b{color:var(--ink); font-weight:500;}

/* relationship detail */
.bk-detail-head{display:flex; align-items:center; gap:8px; padding:16px 18px 6px;}
.bk-back{border:0; background:none; color:var(--muted); cursor:pointer; padding:0; display:flex;}
.bk-crumb{font-size:13px; color:var(--faint);}
.bk-subhead{padding:2px 18px 14px;}
.bk-role{font-size:13px; color:var(--muted); margin:4px 0 0;}
.bk-stat{display:flex; align-items:center; gap:8px; margin-top:8px; font-size:12px; color:var(--faint);}
.bk-panel{margin:0 18px 12px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); padding:13px 15px;}
.bk-panel-head{display:flex; align-items:center; gap:7px; margin-bottom:8px;}
.bk-panel-head svg{color:var(--accent);}
.bk-panel-head span{font-size:13px; font-weight:500;}
.bk-panel-p{font-size:13px; color:var(--muted); line-height:1.55; margin:0;}
.bk-panel-label{font-size:12px; color:var(--faint); margin:0 0 9px;}
.bk-quote{background:var(--bg); border:.5px solid var(--line); border-radius:var(--r-md); padding:11px 13px;}
.bk-quote p{font-family:var(--font-display); font-size:14px; color:var(--ink); line-height:1.55; margin:0;}
.bk-actions{margin-top:10px; display:flex; gap:8px;}
.bk-tl{margin:0 18px 16px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); overflow:hidden;}
.bk-tl-item{display:flex; align-items:flex-start; gap:10px; padding:11px 14px;}
.bk-tl-item + .bk-tl-item{border-top:.5px solid var(--line);}
.bk-tl-dot{width:7px; height:7px; border-radius:50%; background:var(--faint); margin-top:5px; flex:none;}
.bk-tl-dot.warn{background:var(--warning);}
.bk-tl-t{font-size:13px; margin:0;}
.bk-tl-d{font-size:11px; color:var(--faint); margin:2px 0 0;}

/* account / settings */
.bk-acct-head{display:flex; align-items:center; gap:13px; padding:8px 18px 18px;}
.bk-avatar-lg{width:48px; height:48px; border-radius:50%; background:var(--accent-bg);
  color:var(--accent); display:flex; align-items:center; justify-content:center; font-size:17px;
  font-weight:500; flex:none;}
.bk-acct-name{font-family:var(--font-display); font-size:22px; font-weight:400; margin:0;}
.bk-acct-email{font-size:12px; color:var(--muted); margin:3px 0 0;}
.bk-set-group{margin:0 18px 16px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); overflow:hidden;}
.bk-set-row{display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding:13px 14px; width:100%; border:0; background:none; font-family:var(--font-ui);
  cursor:pointer; text-align:left; color:var(--ink);}
.bk-set-row + .bk-set-row{border-top:.5px solid var(--line);}
.bk-set-lead{display:inline-flex; align-items:center; gap:11px;}
.bk-set-lead svg{color:var(--muted);}
.bk-set-lbl{font-size:14px;}
.bk-set-right{display:inline-flex; align-items:center; gap:8px;}
.bk-set-val{font-size:12px; color:var(--faint);}
.bk-chev{color:var(--faint);}
.bk-set-row--danger .bk-set-lead svg, .bk-set-row--danger .bk-set-lbl{color:var(--danger);}

/* connections */
.bk-conn-row{display:flex; align-items:center; gap:12px; padding:13px 14px;}
.bk-conn-row + .bk-conn-row{border-top:.5px solid var(--line);}
.bk-tile{width:38px; height:38px; border-radius:var(--r-md); background:var(--bg);
  border:.5px solid var(--line); display:flex; align-items:center; justify-content:center;
  flex:none; color:var(--accent);}
.bk-conn-status{display:inline-flex; align-items:center; gap:5px; font-size:11px;
  color:var(--success); white-space:nowrap;}
.bk-note{font-size:11px; color:var(--faint); margin:0 18px 14px; line-height:1.5;}
.bk-note--warn{color:var(--warning);}
`;
