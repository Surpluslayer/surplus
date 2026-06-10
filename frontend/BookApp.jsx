// ── BookApp : the advisor "Your book today" surface ─────────────────────────
// Phone-first home for a relationship-led professional (wealth advisor / lawyer)
// whose income depends on keeping an existing book warm. It opens on Today : a
// time-ordered "Updates" feed (noteworthy events worth a personal note) and a
// priority-ranked "Needs outreach" list (relationships going quiet). Every
// "Draft" generates the note on tap; the ask bar answers questions over the
// book. Backed by /api/book/* (agents/book.py).
//
// Five screens, navigation model:
//   Today  · Add · Book   (bottom nav; Add is the centered accent button)
//   Today  -> tap the avatar  -> Account   (back arrow returns to Today)
//   Today/Book -> tap a person -> Relationship (back arrow returns to Book)
//   Add is a bottom sheet over the current screen.
//
// Self-contained (own CSS, design tokens) so it stays isolated from the event
// flow and each app's own shell, same pattern as InPersonApp.
import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  Sparkles, ArrowUp, ArrowUpRight, Star, LayoutGrid, Plus, BookText,
  Loader2, X, Copy, Check, ChevronLeft, ChevronRight, ChevronDown,
  MapPin, QrCode, Link2, Search, Send, CreditCard, Mail, Calendar,
  CheckCircle2, LogOut,
} from "lucide-react";
import { api } from "./lib/api.js";

// Relationship health : a colored dot + word.
//   active = green · warm/quiet = amber · cooling/dormant = red · new = blue
const HEALTH = {
  active:  { word: "Active",  cls: "active" },
  warm:    { word: "Warm",    cls: "warm" },
  quiet:   { word: "Quiet",   cls: "warm" },
  cooling: { word: "Cooling", cls: "cooling" },
  dormant: { word: "Dormant", cls: "dormant" },
  new:     { word: "New",     cls: "new" },
};
function health(status) {
  return HEALTH[status] || HEALTH.warm;
}

export default function BookApp() {
  const [user, setUser] = useState(null);        // null=loading, undefined=signed out
  const [feed, setFeed] = useState(null);        // null=loading
  const [err, setErr] = useState("");
  const [tab, setTab] = useState("today");       // "today" | "book"
  const [detail, setDetail] = useState(null);    // person -> Relationship screen
  const [account, setAccount] = useState(false); // Account screen
  const [addOpen, setAddOpen] = useState(false); // Add-contact sheet
  const [draftFor, setDraftFor] = useState(null);// {name, trigger, contact_id}

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then((u) => { if (!cancelled) setUser(u && u.id ? u : undefined); })
      .catch((e) => { if (!cancelled) setUser(e?.status === 401 ? undefined : {}); });
    return () => { cancelled = true; };
  }, []);

  const load = useCallback(() => {
    setErr("");
    api.bookToday()
      .then((f) => setFeed(f))
      .catch((e) => setErr(e.message || String(e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const openPerson = (p) => { setAccount(false); setDetail(p); };

  return (
    <div className="bk-root">
      <style>{BOOK_CSS}</style>
      <div className="bk-frame">
        {account ? (
          <AccountView user={user} onBack={() => setAccount(false)}
                       onConnected={() => api.me().then((u) => setUser(u)).catch(() => {})} />
        ) : detail ? (
          <RelationshipView person={detail} onBack={() => setDetail(null)} />
        ) : tab === "today" ? (
          <TodayView feed={feed} err={err} user={user}
                     onAvatar={() => setAccount(true)}
                     onPerson={openPerson}
                     onDraft={(d) => setDraftFor(d)} onReload={load} />
        ) : (
          <BookView feed={feed} onPerson={openPerson} onDraft={(d) => setDraftFor(d)} />
        )}

        <nav className="bk-nav">
          <button className={"bk-nav-item" + (!detail && !account && tab === "today" ? " on" : "")}
                  onClick={() => { setDetail(null); setAccount(false); setTab("today"); }}>
            <LayoutGrid size={19} /><span>Today</span>
          </button>
          <button className="bk-nav-add" onClick={() => setAddOpen(true)} aria-label="Add contact">
            <span className="bk-fab"><Plus size={22} /></span><span>Add</span>
          </button>
          <button className={"bk-nav-item" + ((detail || tab === "book") && !account ? " on" : "")}
                  onClick={() => { setDetail(null); setAccount(false); setTab("book"); }}>
            <BookText size={19} /><span>Book</span>
          </button>
        </nav>
      </div>

      {addOpen && <AddSheet onClose={() => setAddOpen(false)} />}
      {draftFor && <DraftSheet draft={draftFor} onClose={() => setDraftFor(null)} />}
    </div>
  );
}

// ── Today ────────────────────────────────────────────────────────────────────

function TodayView({ feed, err, user, onAvatar, onPerson, onDraft, onReload }) {
  const updates = feed?.updates || [];
  const needs = feed?.needs_outreach || [];
  const initials = _initials(user?.name || feed?.advisor_name);

  return (
    <div className="bk-scroll">
      <header className="bk-topbar">
        <div>
          <p className="bk-eyebrow">{_today_long()}</p>
          <p className="bk-h-display">Your book today</p>
        </div>
        <button className="bk-avatar" title={user?.name || "Account"} onClick={onAvatar}>
          {initials}
        </button>
      </header>

      <AskBar onDraft={onDraft} />

      {err && <div className="bk-err">{err} <button className="bk-link" onClick={onReload}>Retry</button></div>}
      {!feed && !err && <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Reading your book…</div>}

      {feed && (
        <>
          <SectionHead label="Updates" count={updates.length} />
          <div className="bk-group">
            {updates.map((u, i) => (
              <PersonRow key={`u${i}`}
                onOpen={() => onPerson(_personFromUpdate(u))}
                name={u.name} vip={u.vip} sub={u.headline}
                aside={<>
                  <p className="bk-time">{_rel_time(u.detected_at)}</p>
                  {u.can_draft && (
                    <button className="bk-draft" onClick={(e) => { e.stopPropagation();
                      onDraft({ name: u.name, contact_id: u.contact_id, trigger: u.trigger || u.headline }); }}>
                      Draft <ArrowUpRight size={13} />
                    </button>
                  )}
                </>} />
            ))}
            {updates.length === 0 && <Empty text="No new updates today." />}
          </div>

          <SectionHead label="Needs outreach" count={needs.length} />
          <div className="bk-group">
            {needs.map((n, i) => (
              <PersonRow key={`n${i}`}
                onOpen={() => onPerson(_personFromNeed(n))}
                name={n.name} vip={n.vip} sub={n.reason}
                aside={
                  <button className="bk-draft" onClick={(e) => { e.stopPropagation();
                    onDraft({ name: n.name, contact_id: n.contact_id, trigger: n.trigger || n.reason }); }}>
                    Draft <ArrowUpRight size={13} />
                  </button>
                } />
            ))}
            {needs.length === 0 && <Empty text="Everyone's warm. Nothing overdue." />}
          </div>
        </>
      )}
    </div>
  );
}

function SectionHead({ label, count, link, onLink }) {
  return (
    <div className="bk-sec">
      <span className="bk-sec-label">{label} <span className="bk-count">· {count}</span></span>
      {link && <span className="bk-sec-link" onClick={onLink}>{link}</span>}
    </div>
  );
}

// A grouped-list row : tappable to open the Relationship screen. `aside` holds
// the right-hand content (timestamp + Draft, a health pill, etc.).
function PersonRow({ name, vip, sub, meta, aside, onOpen }) {
  return (
    <div className="bk-row" onClick={onOpen} role="button" tabIndex={0}>
      <div className="bk-main">
        <p className="bk-name">{name}{vip && <Star size={13} className="bk-star" fill="currentColor" />}</p>
        {sub && <p className="bk-sub">{sub}</p>}
        {meta && <p className="bk-meta">{meta}</p>}
      </div>
      {aside && <div className="bk-aside">{aside}</div>}
    </div>
  );
}

function HealthPill({ status }) {
  const h = health(status);
  return (
    <span className={`bk-health ${h.cls}`}>
      {h.cls !== "new" && <span className="bk-hdot" />}{h.word}
    </span>
  );
}

function Empty({ text }) {
  return <div className="bk-empty">{text}</div>;
}

// ── Ask bar (agent) ──────────────────────────────────────────────────────────

const CHIPS = ["Who's cooling?", "Reviews due", "Quiet 30+ days"];

function AskBar({ onDraft }) {
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

  return (
    <div className="bk-ask-wrap">
      <div className="bk-agent-bar">
        <Sparkles size={17} className="bk-agent-spark" />
        <input className="bk-agent-input" placeholder="Ask your agent anything…"
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
        <button className="bk-agent-go" onClick={() => ask()} disabled={busy || !q.trim()} aria-label="Ask">
          {busy ? <Loader2 size={16} className="bk-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>

      {!res && !busy && (
        <div className="bk-chips">
          {CHIPS.map((c) => <button key={c} className="bk-chip" onClick={() => ask(c)}>{c}</button>)}
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
                  <button className="bk-draft"
                          onClick={() => onDraft({ name: p.name, trigger: p.reason || "catch up" })}>
                    Draft <ArrowUpRight size={13} />
                  </button>
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

// ── Book (roster) ─────────────────────────────────────────────────────────────

const FILTERS = ["All", "Starred", "Cooling", "Prospects"];

function BookView({ feed, onPerson, onDraft }) {
  const [filter, setFilter] = useState("All");
  const [expanded, setExpanded] = useState(false);

  // Compose a roster from the feed : everyone with an update or overdue. Newest
  // updates read as active; overdue rows carry their scored health.
  const rows = [];
  (feed?.updates || []).forEach((u) => rows.push({ ..._personFromUpdate(u), status: "active" }));
  (feed?.needs_outreach || []).forEach((n) => rows.push(_personFromNeed(n)));

  const filtered = rows.filter((r) => {
    if (filter === "Starred") return r.vip;
    if (filter === "Cooling") return r.status === "cooling" || r.status === "dormant";
    if (filter === "Prospects") return r.status === "new";
    return true;
  });
  const visible = expanded ? filtered : filtered.slice(0, 6);
  const hidden = filtered.length - visible.length;

  return (
    <div className="bk-scroll">
      <header className="bk-topbar bk-topbar--center">
        <span className="bk-h-display bk-book-title">
          Your book <span className="bk-book-count">{rows.length}</span>
        </span>
      </header>

      <div className="bk-assistant">
        <div className="bk-assistant-head"><Sparkles size={16} /><span>Relationship assistant</span></div>
        <BookAsk onDraft={onDraft} />
      </div>

      <div className="bk-pills">
        {FILTERS.map((f) => (
          <button key={f} className={"bk-pill" + (filter === f ? " active" : "")}
                  onClick={() => { setFilter(f); setExpanded(false); }}>{f}</button>
        ))}
      </div>
      <p className="bk-hint">Sorted by who needs attention</p>

      <div className="bk-group">
        {visible.length === 0 && <Empty text="No one here yet." />}
        {visible.map((r, i) => (
          <PersonRow key={i} onOpen={() => onPerson(r)}
            name={r.name} vip={r.vip} sub={r.sub} meta={r.meta}
            aside={<HealthPill status={r.status} />} />
        ))}
      </div>
      {hidden > 0 && (
        <p className="bk-more" onClick={() => setExpanded(true)}>Show {hidden} more</p>
      )}
    </div>
  );
}

// Compact ask field used inside the Book assistant card.
function BookAsk({ onDraft }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");

  const ask = async (query) => {
    const text = (query ?? q).trim();
    if (!text || busy) return;
    setBusy(true); setErr(""); setRes(null); setQ(text);
    try { setRes(await api.bookAsk(text)); }
    catch (e) { setErr(e.message || "Couldn't ask the agent"); }
    finally { setBusy(false); }
  };

  return (
    <>
      <div className="bk-field">
        <input placeholder="Ask about anyone, or who to follow up with…"
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
        <button className="bk-send" onClick={() => ask()} disabled={busy || !q.trim()} aria-label="Ask">
          {busy ? <Loader2 size={16} className="bk-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>
      {!res && !busy && (
        <div className="bk-chips" style={{ marginTop: 10 }}>
          {["Who's cooling?", "Reviews due", "Quiet 30+ days"].map((c) => (
            <button key={c} className="bk-chip" onClick={() => ask(c)}>{c}</button>
          ))}
        </div>
      )}
      {err && <div className="bk-err" style={{ marginTop: 8 }}>{err}</div>}
      {res && (
        <div className="bk-answer" style={{ marginTop: 10 }}>
          <div className="bk-answer-text">{res.answer}</div>
          {(res.people || []).length > 0 && (
            <div className="bk-answer-people">
              {res.people.map((p, i) => (
                <div key={i} className="bk-answer-person">
                  <div className="bk-ap-main">
                    <div className="bk-ap-name">{p.name}</div>
                    {p.reason && <div className="bk-ap-reason">{p.reason}</div>}
                  </div>
                  <button className="bk-draft"
                          onClick={() => onDraft({ name: p.name, trigger: p.reason || "catch up" })}>
                    Draft <ArrowUpRight size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <button className="bk-link" onClick={() => { setRes(null); setQ(""); }}>Clear</button>
        </div>
      )}
    </>
  );
}

// ── Relationship (detail) ──────────────────────────────────────────────────────

function RelationshipView({ person, onBack }) {
  const [contact, setContact] = useState(null);
  const h = health(person.status);

  useEffect(() => {
    if (!person.contact_id) return;
    let cancelled = false;
    api.getContact(person.contact_id)
      .then((c) => { if (!cancelled) setContact(c); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [person.contact_id]);

  const timeline = _timeline(contact);
  const stateWord = h.word.toLowerCase();
  const possessive = "they're"; // gender-neutral; no record of pronouns

  return (
    <div className="bk-scroll">
      <div className="bk-detail-head" onClick={onBack} role="button" tabIndex={0}>
        <ChevronLeft size={20} /><span className="bk-crumb">Your book</span>
      </div>

      <div className="bk-subhead">
        <p className="bk-h-display bk-h-lg">
          {person.name}{person.vip && <Star size={16} className="bk-star" fill="currentColor" style={{ verticalAlign: 1 }} />}
        </p>
        {person.sub && <p className="bk-role">{person.sub}</p>}
        <div className="bk-stat">
          <HealthPill status={person.status} />
          <span>{_statLine(person, contact)}</span>
        </div>
      </div>

      {person.reason && (
        <div className="bk-panel">
          <div className="bk-panel-head"><Sparkles size={16} /><span>Why {possessive} {stateWord}</span></div>
          <p>{person.reason_long || person.reason}</p>
        </div>
      )}

      <DraftPanel person={person} />

      {timeline.length > 0 && (
        <>
          <p className="bk-sec-label" style={{ margin: "0 18px 8px" }}>Timeline</p>
          <div className="bk-tl">
            {timeline.map((t, i) => (
              <div key={i} className="bk-tl-item">
                <span className={"bk-tl-dot" + (t.warn ? " warn" : "")} />
                <div><p className="bk-tl-t">{t.text}</p><p className="bk-tl-d">{t.date}</p></div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// Drafted re-engagement panel : generates the note on mount, in the user's
// voice (Newsreader). Refine re-runs; Send copies the note ready to paste
// (the book surface has no direct-send endpoint yet); Snooze dismisses.
function DraftPanel({ person }) {
  const [busy, setBusy] = useState(true);
  const [body, setBody] = useState("");
  const [subject, setSubject] = useState("");
  const [err, setErr] = useState("");
  const [dismissed, setDismissed] = useState(false);
  const [copied, setCopied] = useState(false);

  const run = useCallback(() => {
    setBusy(true); setErr("");
    api.bookDraft({ name: person.name, contact_id: person.contact_id,
                    trigger: person.trigger || person.reason, channel: "email" })
      .then((r) => { setSubject(r.subject || ""); setBody(r.body || ""); })
      .catch((e) => setErr(e.message || "Couldn't draft"))
      .finally(() => setBusy(false));
  }, [person]);
  useEffect(() => { run(); }, [run]);

  const copy = async () => {
    const text = subject ? `Subject: ${subject}\n\n${body}` : body;
    try { await navigator.clipboard.writeText(text); setCopied(true);
          setTimeout(() => setCopied(false), 1600); } catch {}
  };

  if (dismissed) return null;

  return (
    <div className="bk-panel">
      <p className="bk-panel-label">Drafted re-engagement</p>
      {busy ? (
        <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Writing in your voice…</div>
      ) : err ? (
        <div className="bk-err">{err} <button className="bk-link" onClick={run}>Retry</button></div>
      ) : (
        <>
          <div className="bk-quote"><p>{body}</p></div>
          <div className="bk-actions">
            <button className="bk-btn bk-btn--primary" onClick={copy}>
              {copied ? <><Check size={13} style={{ marginRight: 4, verticalAlign: -1 }} />Copied</>
                      : <><Send size={13} style={{ marginRight: 4, verticalAlign: -1 }} />Send</>}
            </button>
            <button className="bk-btn" onClick={run}>Refine</button>
            <button className="bk-btn" onClick={() => setDismissed(true)}>Snooze</button>
          </div>
        </>
      )}
    </div>
  );
}

// ── Account ────────────────────────────────────────────────────────────────────

function AccountView({ user, onBack, onConnected }) {
  const name = user?.name || "Your account";
  const email = user?.email || "";
  const initials = _initials(user?.name);
  const liConnected = user?.linkedin_status === "active";
  const emConnected = user?.email_status === "active";
  const plan = user?.paid_at ? "Pro" : "Individual";

  const connectLinkedin = () =>
    api.startLinkedinAuth().then((r) => { if (r?.url) window.location = r.url; }).catch(() => {});
  const connectEmail = () =>
    api.startEmailAuth().then((r) => { if (r?.url) window.location = r.url; }).catch(() => {});
  const signOut = () =>
    api.logout().then(() => window.location.reload()).catch(() => {});

  return (
    <div className="bk-scroll">
      <div className="bk-detail-head" onClick={onBack} role="button" tabIndex={0}>
        <ChevronLeft size={20} /><span className="bk-crumb">Today</span>
      </div>

      <div className="bk-acct-head">
        <div className="bk-avatar-lg">{initials}</div>
        <div><p className="bk-acct-name">{name}</p>{email && <p className="bk-acct-email">{email}</p>}</div>
      </div>

      <div className="bk-set-group">
        <div className="bk-set-row">
          <span className="bk-set-lead"><CreditCard size={19} /><span className="bk-set-lbl">Plan</span></span>
          <span className="bk-set-right"><span className="bk-set-val">{plan}</span><ChevronRight size={17} className="bk-chev" /></span>
        </div>

        <div className="bk-set-row">
          <span className="bk-set-lead"><img className="bk-set-brand" src="/linkedin-icon.png" alt="" /><span className="bk-set-lbl">LinkedIn</span></span>
          {liConnected
            ? <span className="bk-conn-status"><CheckCircle2 size={14} />Connected</span>
            : <button className="bk-btn bk-btn--primary" onClick={connectLinkedin}>Connect</button>}
        </div>

        <div className="bk-set-row">
          <span className="bk-set-lead"><Mail size={19} /><span className="bk-set-lbl">Gmail</span></span>
          {emConnected
            ? <span className="bk-conn-status"><CheckCircle2 size={14} />Connected</span>
            : <button className="bk-btn bk-btn--primary" onClick={connectEmail}>Connect</button>}
        </div>

        <div className="bk-set-row">
          <span className="bk-set-lead"><Calendar size={19} /><span className="bk-set-lbl">Google Calendar</span></span>
          <button className="bk-btn bk-btn--primary" title="Coming soon" disabled>Connect</button>
        </div>
      </div>

      <div className="bk-set-group">
        <button className="bk-set-row danger" onClick={signOut}>
          <span className="bk-set-lead"><LogOut size={19} /><span className="bk-set-lbl">Sign out</span></span>
        </button>
      </div>
    </div>
  );
}

// ── Add contact (bottom sheet) ──────────────────────────────────────────────────

const ADD_TABS = [
  { id: "qr", label: "Scan QR", icon: QrCode },
  { id: "link", label: "Paste link", icon: Link2 },
  { id: "name", label: "By name", icon: Search },
];

function AddSheet({ onClose }) {
  const [event, setEvent] = useState("Founders Inc");
  const [draftEvent, setDraftEvent] = useState("");
  const [tab, setTab] = useState("qr");
  const recents = ["NYC Tech Week", "SALT Conference"];

  return (
    <div className="bk-sheet-scrim" onClick={onClose}>
      <div className="bk-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="bk-grabber"><span /></div>
        <div className="bk-sheet-title">
          <span className="bk-h-display">Add contact</span>
          <button className="bk-sheet-x" onClick={onClose} aria-label="Close"><X size={20} /></button>
        </div>

        <div className="bk-event">
          <div className="bk-event-current">
            <span className="bk-event-name"><MapPin size={18} />{event}</span>
            <ChevronDown size={18} className="bk-chev" />
          </div>
          <div className="bk-field" style={{ marginTop: 11 }}>
            <input placeholder="e.g. NYC Tech Week, Founders Inc"
                   value={draftEvent} onChange={(e) => setDraftEvent(e.target.value)} />
            <button className="bk-btn bk-btn--primary" style={{ height: 36 }}
                    onClick={() => { if (draftEvent.trim()) { setEvent(draftEvent.trim()); setDraftEvent(""); } }}>
              Set
            </button>
          </div>
          <div className="bk-chips" style={{ marginTop: 11 }}>
            <button className="bk-pill active">{event}</button>
            {recents.filter((r) => r !== event).map((r) => (
              <button key={r} className="bk-chip" onClick={() => setEvent(r)}>{r}</button>
            ))}
          </div>
        </div>

        <div className="bk-banner"><b>1.</b> Add the person · <b>2.</b> Connect. That's it.</div>

        <div className="bk-tabs">
          {ADD_TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.id} className={"bk-tab" + (tab === t.id ? " active" : "")} onClick={() => setTab(t.id)}>
                <Icon size={16} /> {t.label}
              </button>
            );
          })}
        </div>

        {tab === "qr" && (
          <div className="bk-scan">
            <div className="bk-scan-target"><QrCode size={42} /></div>
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
            <p className="bk-scan-sub" style={{ marginTop: 12 }}>We enrich the profile and drop them into your book.</p>
          </div>
        )}
        {tab === "name" && (
          <div className="bk-scan">
            <div className="bk-field">
              <input placeholder="Search by name, title, or firm" />
              <button className="bk-btn bk-btn--primary" style={{ height: 36 }}>Find</button>
            </div>
            <p className="bk-scan-sub" style={{ marginTop: 12 }}>Pick the right match and we fill in the rest.</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Draft sheet (quick draft from a list row) ───────────────────────────────────

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
            <div className="bk-sheet-to">To {draft.name}</div>
            <div className="bk-sheet-trigger">{draft.trigger}</div>
          </div>
          <button className="bk-sheet-x" onClick={onClose} aria-label="Close"><X size={18} /></button>
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
              <button className="bk-btn" onClick={copy}>
                {copied ? <><Check size={15} style={{ verticalAlign: -2, marginRight: 4 }} /> Copied</>
                        : <><Copy size={15} style={{ verticalAlign: -2, marginRight: 4 }} /> Copy</>}
              </button>
              <button className="bk-btn bk-btn--primary" onClick={onClose}>Looks good</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── helpers ───────────────────────────────────────────────────────────────────

function _personFromUpdate(u) {
  return {
    name: u.name, vip: u.vip, sub: u.headline,
    status: "active", reason: u.headline,
    contact_id: u.contact_id, trigger: u.trigger || u.headline,
    meta: u.met_at ? `Met at ${u.met_at}` : undefined,
  };
}
function _personFromNeed(n) {
  return {
    name: n.name, vip: n.vip, sub: n.sub || n.reason,
    status: n.status || "cooling", reason: n.reason, reason_long: n.reason_long,
    contact_id: n.contact_id, trigger: n.trigger || n.reason,
    meta: n.meta || (n.met_at ? `Met at ${n.met_at}` : undefined),
  };
}

function _initials(name) {
  if (!name) return "•";
  const parts = String(name).trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() || "").join("") || "•";
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

// Build the relationship stat line ("last spoke 18 days ago · $40M relationship")
// from whatever the contact record carries, degrading gracefully.
function _statLine(person, contact) {
  const bits = [];
  const days = contact?.days_since ?? person?.days_since;
  if (typeof days === "number") bits.push(`last spoke ${days} ${days === 1 ? "day" : "days"} ago`);
  const value = contact?.relationship_value || contact?.value;
  if (value) bits.push(`${value} relationship`);
  return bits.length ? `· ${bits.join(" · ")}` : "";
}

// Map a contact's interaction history into timeline rows. Returns [] when the
// record has nothing to show, so the section hides cleanly.
function _timeline(contact) {
  const hist = contact?.interaction_history || contact?.timeline || [];
  if (!Array.isArray(hist)) return [];
  return hist.slice(0, 6).map((h, i) => ({
    text: h.text || h.summary || h.title || String(h),
    date: h.date || h.when || (h.at ? _rel_time(h.at) : ""),
    warn: !!h.warn || h.kind === "no_reply",
  }));
}

const BOOK_CSS = `
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500&family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap');

/* ============================================================
   SURPLUS DESIGN TOKENS  (single source of truth)
   ============================================================ */
.bk-root{
  --ink:#1b1e22; --muted:#5b616a; --faint:#99a0a8;
  --bg:#ffffff; --surface:#f4f5f7;
  --line:rgba(20,23,28,.08); --line-2:rgba(20,23,28,.16);
  --accent:#2f6df6; --accent-bg:#eaf1fe;
  --success:#1f9d62; --success-bg:#e7f5ee;
  --warning:#b07210; --warning-bg:#fbf1e1;
  --danger:#c0433d;  --danger-bg:#fbeceb;
  --gold:#ba7517;
  --r-sm:8px; --r-md:10px; --r-lg:14px;
  --font-ui:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-display:'Newsreader',Georgia,'Times New Roman',serif;
}

.bk-root{ min-height:100dvh; background:#e9ebee; display:flex; justify-content:center;
  font-family:var(--font-ui); color:var(--ink); font-size:14px; line-height:1.5;
  -webkit-font-smoothing:antialiased; }
.bk-root *{ box-sizing:border-box; }
.bk-frame{ width:100%; max-width:430px; min-height:100dvh; background:var(--bg);
  display:flex; flex-direction:column; position:relative; }
.bk-spin{ animation:bkspin 1s linear infinite; }
@keyframes bkspin{ to{ transform:rotate(360deg); } }

.bk-scroll{ flex:1; overflow-y:auto; padding:0 0 96px; }

/* top bar / headings */
.bk-topbar{ display:flex; align-items:flex-start; justify-content:space-between; padding:20px 18px 14px; }
.bk-topbar--center{ align-items:center; padding-bottom:12px; }
.bk-eyebrow{ font-size:12px; color:var(--faint); margin:0 0 2px; }
.bk-h-display{ font-family:var(--font-display); font-size:23px; font-weight:400; margin:0; }
.bk-h-lg{ font-size:24px; }
.bk-book-title{ display:inline-flex; align-items:center; gap:10px; }
.bk-book-count{ font-size:13px; color:var(--faint); font-family:var(--font-ui); }
.bk-avatar{ width:32px; height:32px; border-radius:50%; background:var(--accent-bg);
  color:var(--accent); display:flex; align-items:center; justify-content:center;
  font-size:12px; font-weight:500; flex:none; border:0; cursor:pointer; font-family:var(--font-ui); }

/* agent ask bar */
.bk-ask-wrap{ margin:0 18px 20px; }
.bk-agent-bar{ display:flex; align-items:center; gap:10px; background:var(--surface);
  border:.5px solid var(--line); border-radius:999px; padding:7px 8px 7px 15px; }
.bk-agent-spark{ color:var(--accent); flex:none; }
.bk-agent-input{ flex:1; border:0; background:none; outline:none; font-size:13px;
  color:var(--ink); font-family:var(--font-ui); min-width:0; }
.bk-agent-input::placeholder{ color:var(--faint); }
.bk-agent-go{ flex:none; width:30px; height:30px; border-radius:50%; border:0;
  background:var(--accent); color:#fff; display:flex; align-items:center;
  justify-content:center; cursor:pointer; }
.bk-agent-go:disabled{ background:#b9cdf9; cursor:default; }

.bk-chips{ display:flex; flex-wrap:wrap; gap:6px; }
.bk-chip{ font-size:11px; color:var(--ink); background:var(--bg); border:.5px solid var(--line-2);
  border-radius:var(--r-md); padding:5px 10px; cursor:pointer; font-family:var(--font-ui); }

/* section label + count */
.bk-sec{ padding:0 18px 6px; display:flex; align-items:baseline; justify-content:space-between; }
.bk-sec-label{ font-size:13px; font-weight:500; }
.bk-count{ color:var(--faint); font-weight:400; }
.bk-sec-link{ font-size:12px; color:var(--accent); cursor:pointer; }

/* grouped list */
.bk-group{ margin:0 18px 20px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); overflow:hidden; }
.bk-row{ display:flex; align-items:center; justify-content:space-between; gap:8px;
  padding:11px 14px; cursor:pointer; }
.bk-row + .bk-row{ border-top:.5px solid var(--line); }
.bk-row:active{ background:rgba(20,23,28,.02); }
.bk-main{ min-width:0; }
.bk-name{ font-size:14px; font-weight:500; margin:0; display:flex; align-items:center; gap:6px; }
.bk-sub{ font-size:12px; color:var(--muted); margin:2px 0 0; }
.bk-meta{ font-size:11px; color:var(--faint); margin:3px 0 0; }
.bk-aside{ text-align:right; white-space:nowrap; display:flex; flex-direction:column;
  align-items:flex-end; gap:3px; }
.bk-time{ font-size:11px; color:var(--faint); margin:0; }
.bk-star{ color:var(--gold); flex:none; }
.bk-draft{ display:inline-flex; align-items:center; gap:2px; font-size:12px; color:var(--accent);
  cursor:pointer; white-space:nowrap; background:none; border:0; font-family:var(--font-ui);
  font-weight:500; padding:0; }
.bk-draft:active{ opacity:.6; }
.bk-empty{ padding:18px 14px; text-align:center; color:var(--faint); font-size:13px; }

/* health pip + word */
.bk-health{ display:inline-flex; align-items:center; gap:5px; font-size:11px; white-space:nowrap; }
.bk-hdot{ width:7px; height:7px; border-radius:50%; }
.bk-health.cooling,.bk-health.dormant{ color:var(--danger); } .bk-health.cooling .bk-hdot,.bk-health.dormant .bk-hdot{ background:var(--danger); }
.bk-health.warm{ color:var(--warning); } .bk-health.warm .bk-hdot{ background:var(--warning); }
.bk-health.active{ color:var(--success); } .bk-health.active .bk-hdot{ background:var(--success); }
.bk-health.new{ color:var(--accent); }

/* filter pills */
.bk-pills{ display:flex; gap:7px; flex-wrap:wrap; padding:0 18px 12px; }
.bk-pill{ font-size:12px; color:var(--muted); background:var(--surface); padding:5px 12px;
  border-radius:999px; cursor:pointer; border:0; font-family:var(--font-ui); }
.bk-pill.active{ background:var(--accent-bg); color:var(--accent); font-weight:500; }
.bk-hint{ font-size:11px; color:var(--faint); margin:0 18px 8px; }
.bk-more{ text-align:center; font-size:12px; color:var(--accent); margin:0 0 16px; cursor:pointer; }

/* assistant card (Book) */
.bk-assistant{ margin:0 18px 14px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); padding:13px 14px; }
.bk-assistant-head{ display:flex; align-items:center; gap:7px; margin-bottom:10px; }
.bk-assistant-head svg{ color:var(--accent); }
.bk-assistant-head span{ font-size:13px; font-weight:500; }
.bk-field{ display:flex; align-items:center; gap:8px; }
.bk-field input{ flex:1; height:36px; border:.5px solid var(--line-2); border-radius:var(--r-md);
  padding:0 12px; font:inherit; font-size:13px; background:var(--bg); color:var(--ink);
  font-family:var(--font-ui); }
.bk-field input::placeholder{ color:var(--faint); }
.bk-field input:focus{ outline:none; border-color:var(--accent); }
.bk-send{ width:36px; height:36px; border:.5px solid var(--accent); background:var(--accent-bg);
  color:var(--accent); border-radius:var(--r-md); display:flex; align-items:center;
  justify-content:center; cursor:pointer; flex:none; }
.bk-send:disabled{ opacity:.5; cursor:default; }

.bk-answer{ background:var(--accent-bg); border-radius:var(--r-md); padding:11px 13px; }
.bk-answer-text{ font-size:13px; color:var(--ink); line-height:1.5; }
.bk-answer-people{ margin-top:9px; display:flex; flex-direction:column; gap:7px; }
.bk-answer-person{ display:flex; align-items:flex-start; justify-content:space-between; gap:10px;
  background:var(--bg); border:.5px solid var(--line); border-radius:var(--r-md); padding:8px 10px; }
.bk-ap-name{ font-size:13px; font-weight:500; color:var(--ink); }
.bk-ap-reason{ font-size:12px; color:var(--muted); margin-top:1px; }
.bk-ap-draft{ font-size:12px; color:var(--muted); font-family:var(--font-display); margin-top:4px; line-height:1.45; }

/* buttons */
.bk-btn{ font:inherit; font-size:13px; border:.5px solid var(--line-2); background:var(--bg);
  color:var(--ink); border-radius:var(--r-md); padding:7px 13px; cursor:pointer;
  font-family:var(--font-ui); display:inline-flex; align-items:center; justify-content:center; }
.bk-btn--primary{ background:var(--accent-bg); color:var(--accent); border-color:var(--accent); }
.bk-btn:disabled{ opacity:.5; cursor:default; }

.bk-loading{ display:flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; padding:14px 18px; }
.bk-err{ background:var(--danger-bg); color:var(--danger); border:.5px solid #f0c9c6;
  border-radius:var(--r-md); padding:10px 13px; font-size:13px; margin:0 18px 12px; }
.bk-link{ background:none; border:0; color:var(--accent); font-weight:500; cursor:pointer;
  font-size:13px; font-family:var(--font-ui); padding:4px 0; }

/* bottom nav (Today · Add · Book) */
.bk-nav{ position:sticky; bottom:0; display:flex; align-items:center; background:var(--bg);
  border-top:.5px solid var(--line); padding:8px 0 calc(8px + env(safe-area-inset-bottom)); }
.bk-nav-item{ flex:1; text-align:center; color:var(--faint); cursor:pointer; border:0;
  background:none; display:flex; flex-direction:column; align-items:center; gap:2px;
  font-family:var(--font-ui); }
.bk-nav-item span{ font-size:11px; }
.bk-nav-item.on{ color:var(--accent); }
.bk-nav-add{ flex:1; display:flex; flex-direction:column; align-items:center; gap:2px;
  cursor:pointer; border:0; background:none; color:var(--accent); font-family:var(--font-ui); }
.bk-fab{ width:44px; height:44px; border-radius:50%; background:var(--accent-bg); color:var(--accent);
  border:.5px solid var(--accent); display:flex; align-items:center; justify-content:center; }
.bk-nav-add span{ font-size:11px; color:var(--accent); }

/* Add-contact sheet */
.bk-sheet-scrim{ position:fixed; inset:0; background:rgba(18,22,34,.42); display:flex;
  align-items:flex-end; justify-content:center; z-index:50; }
.bk-sheet{ width:100%; max-width:430px; background:var(--bg); border-radius:20px 20px 0 0;
  padding:0 0 calc(20px + env(safe-area-inset-bottom)); box-shadow:0 -10px 40px rgba(18,22,34,.18);
  animation:bksheet .18s ease-out; max-height:92dvh; overflow-y:auto; }
@keyframes bksheet{ from{ transform:translateY(20px); opacity:.6; } to{ transform:none; opacity:1; } }
.bk-grabber{ display:flex; justify-content:center; padding:12px 0 2px; }
.bk-grabber span{ width:40px; height:4px; border-radius:999px; background:var(--line-2); }
.bk-sheet-title{ display:flex; align-items:center; justify-content:space-between; padding:8px 18px 12px; }
.bk-sheet-x{ background:none; border:0; color:var(--faint); cursor:pointer; padding:2px; }

.bk-event{ margin:0 18px 14px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); padding:12px 14px; }
.bk-event-current{ display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding-bottom:11px; border-bottom:.5px solid var(--line); }
.bk-event-name{ display:inline-flex; align-items:center; gap:8px; font-size:16px; font-weight:500; }
.bk-event-name svg{ color:var(--accent); }
.bk-chev{ color:var(--faint); }

.bk-banner{ margin:0 18px 14px; background:var(--accent-bg); color:var(--accent);
  border-radius:var(--r-md); padding:9px 12px; text-align:center; font-size:12px; }
.bk-banner b{ font-weight:500; }
.bk-tabs{ display:flex; gap:4px; margin:0 18px 16px; background:var(--surface);
  border-radius:var(--r-md); padding:4px; }
.bk-tab{ flex:1; display:flex; align-items:center; justify-content:center; gap:6px; padding:8px 0;
  font-size:13px; color:var(--muted); border-radius:var(--r-md); cursor:pointer; border:0;
  background:none; font-family:var(--font-ui); }
.bk-tab.active{ background:var(--bg); color:var(--accent); font-weight:500; }
.bk-scan{ margin:0 18px 20px; border:1.5px dashed var(--line-2); border-radius:var(--r-lg);
  padding:28px 20px; text-align:center; }
.bk-scan-target{ width:92px; height:92px; margin:0 auto 16px; border-radius:var(--r-md);
  background:var(--surface); display:flex; align-items:center; justify-content:center; color:var(--accent); }
.bk-scan-lead{ font-size:15px; font-weight:500; margin:0; }
.bk-scan-sub{ font-size:12px; color:var(--muted); margin:7px 0 0; }
.bk-scan-sub b{ color:var(--ink); font-weight:500; }

/* relationship detail */
.bk-detail-head{ display:flex; align-items:center; gap:8px; padding:16px 18px 6px;
  cursor:pointer; color:var(--muted); }
.bk-crumb{ font-size:13px; color:var(--faint); }
.bk-subhead{ padding:2px 18px 14px; }
.bk-role{ font-size:13px; color:var(--muted); margin:4px 0 0; }
.bk-stat{ display:flex; align-items:center; gap:8px; margin-top:8px; font-size:12px; color:var(--faint); }
.bk-panel{ margin:0 18px 12px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); padding:13px 15px; }
.bk-panel-head{ display:flex; align-items:center; gap:7px; margin-bottom:8px; }
.bk-panel-head svg{ color:var(--accent); }
.bk-panel-head span{ font-size:13px; font-weight:500; }
.bk-panel p{ font-size:13px; color:var(--muted); line-height:1.55; margin:0; }
.bk-panel-label{ font-size:12px; color:var(--faint); margin:0 0 9px; }
.bk-quote{ background:var(--bg); border:.5px solid var(--line); border-radius:var(--r-md); padding:11px 13px; }
.bk-quote p{ font-family:var(--font-display); font-size:14px; color:var(--ink); line-height:1.55; margin:0; }
.bk-actions{ margin-top:10px; display:flex; gap:8px; }
.bk-tl{ margin:0 18px 14px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); overflow:hidden; }
.bk-tl-item{ display:flex; align-items:flex-start; gap:10px; padding:11px 14px; }
.bk-tl-item + .bk-tl-item{ border-top:.5px solid var(--line); }
.bk-tl-dot{ width:7px; height:7px; border-radius:50%; background:var(--faint); margin-top:5px; flex:none; }
.bk-tl-dot.warn{ background:var(--warning); }
.bk-tl-t{ font-size:13px; margin:0; }
.bk-tl-d{ font-size:11px; color:var(--faint); margin:2px 0 0; }

/* draft sheet */
.bk-sheet-to{ font-size:16px; font-weight:500; color:var(--ink); }
.bk-sheet-trigger{ font-size:13px; color:var(--muted); margin-top:2px; }
.bk-sheet-subject{ width:calc(100% - 36px); margin:0 18px 9px; border:.5px solid var(--line-2);
  border-radius:var(--r-md); padding:10px 12px; font-size:14px; font-weight:500; color:var(--ink);
  font-family:var(--font-ui); }
.bk-sheet-body{ width:calc(100% - 36px); margin:0 18px; border:.5px solid var(--line-2);
  border-radius:var(--r-md); padding:11px 13px; font-size:14px; line-height:1.5; color:var(--ink);
  font-family:var(--font-ui); resize:vertical; }
.bk-sheet-subject:focus, .bk-sheet-body:focus{ outline:none; border-color:var(--accent); }
.bk-sheet .bk-actions{ margin:14px 18px 0; }
.bk-sheet .bk-btn{ flex:1; padding:12px; font-size:14px; font-weight:500; }

/* account */
.bk-acct-head{ display:flex; align-items:center; gap:13px; padding:8px 18px 18px; }
.bk-avatar-lg{ width:48px; height:48px; border-radius:50%; background:var(--accent-bg);
  color:var(--accent); display:flex; align-items:center; justify-content:center; font-size:17px;
  font-weight:500; flex:none; }
.bk-acct-name{ font-family:var(--font-display); font-size:22px; font-weight:400; margin:0; }
.bk-acct-email{ font-size:12px; color:var(--muted); margin:3px 0 0; }
.bk-set-group{ margin:0 18px 16px; background:var(--surface); border:.5px solid var(--line);
  border-radius:var(--r-lg); overflow:hidden; }
.bk-set-row{ display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding:13px 14px; width:100%; background:none; border:0; font-family:var(--font-ui);
  text-align:left; cursor:pointer; }
.bk-set-row + .bk-set-row{ border-top:.5px solid var(--line); }
.bk-set-lead{ display:inline-flex; align-items:center; gap:11px; }
.bk-set-lead svg{ color:var(--muted); }
.bk-set-brand{ width:19px; height:19px; border-radius:3px; object-fit:contain; }
.bk-set-lbl{ font-size:14px; color:var(--ink); }
.bk-set-right{ display:inline-flex; align-items:center; gap:8px; }
.bk-set-val{ font-size:12px; color:var(--faint); }
.bk-chev{ color:var(--faint); }
.bk-set-row.danger .bk-set-lead svg,.bk-set-row.danger .bk-set-lbl{ color:var(--danger); }
.bk-conn-status{ display:inline-flex; align-items:center; gap:5px; font-size:11px;
  color:var(--success); white-space:nowrap; }
`;
