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
import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  Sparkles, ArrowUp, ArrowRight, Star, LayoutDashboard, Plus, BookText, Loader2, X,
  ChevronLeft, ChevronRight, ChevronDown, MapPin, QrCode, Link2, Search, Send,
  Mail, Calendar, Plug, CreditCard, LogOut, CheckCircle2,
} from "lucide-react";
import { api } from "./lib/api.js";
import {
  CaptureScreen, ScanResult, SignInBounce, IP_CSS,
  loadActiveEvent, saveActiveEvent, loadRecentLabels, pushRecentLabel,
} from "./CaptureShared.jsx";
import { StageChip } from "./components/ContactsPage.jsx";

// Demo → real conversion: send the visitor into the connect-first LinkedIn
// flow (same entry the send-gate uses). The callback returns them to the real
// event.surpluslayer.com app with onboarding armed.
function signInWithLinkedIn() {
  window.location.href = "/api/auth/linkedin/start-redirect";
}

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
  const [tab, setTab] = useState("today");       // "today" | "add" | "book"
  const [route, setRoute] = useState(null);      // {name:"detail",row} | {name:"account"} | {name:"connections"} | null
  const [draftFor, setDraftFor] = useState(null);// {name, contact_id, trigger}

  // Fonts: load Inter + Newsreader only for this surface (the desktop App ships
  // its own type), injected once so the design tokens resolve.
  useEffect(() => { _ensureFonts(); }, []);

  // Fire me + bookToday in parallel — no reason to wait for auth before
  // starting the book fetch; both resolve independently.
  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([api.me(), api.bookToday()]).then(([meRes, todayRes]) => {
      if (cancelled) return;
      if (meRes.status === "fulfilled") {
        const u = meRes.value;
        setUser(u && u.id ? u : undefined);
      } else {
        setUser(meRes.reason?.status === 401 ? undefined : {});
      }
      if (todayRes.status === "fulfilled") {
        setFeed(todayRes.value);
      } else {
        setErr(todayRes.reason?.message || String(todayRes.reason));
      }
    });
    return () => { cancelled = true; };
  }, []);

  const load = useCallback(() => {
    setErr("");
    api.bookToday().then(setFeed).catch((e) => setErr(e.message || String(e)));
  }, []);

  // ── Demo onboarding coach ─────────────────────────────────────────────────
  // The public /demo session (user.is_demo) gets a guided six-step tour that
  // pops up over the real Book surface: add a contact, find them, send a
  // message, ask the agent a question, send a message, then check the
  // relationship list. We persist a dismissal flag in localStorage so it shows
  // once per browser rather than on every reload of the seeded demo.
  const [onbStep, setOnbStep] = useState(0);
  const [onbOn, setOnbOn] = useState(false);
  useEffect(() => {
    if (!user || typeof user !== "object" || !user.is_demo) return;
    let dismissed = false;
    try { dismissed = !!localStorage.getItem("surplus_demo_onb"); } catch {}
    if (dismissed) return;
    setOnbStep(0);
    setOnbOn(true);
  }, [user]);
  const onbGo = (i) => {
    const next = Math.min(Math.max(i, 0), BK_ONB_STEPS.length - 1);
    setOnbStep(next);
    // Put the screen the step points at in front of the visitor.
    setRoute(null);
    setTab(BK_ONB_STEPS[next].tab);
  };
  const onbClose = (reason) => {
    setOnbOn(false);
    try { localStorage.setItem("surplus_demo_onb", reason || "done"); } catch {}
  };

  // Signed out → the same LinkedIn sign-in bounce as the event surface (this
  // is the shell event hosts serve, so it must gate, not error).
  if (user === undefined) return <SignInBounce />;

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
    screen = <BookView feed={feed} err={err} user={user} onReload={load}
                       onAccount={() => setRoute({ name: "account" })}
                       onOpen={openDetail} onDraft={openDraft} />;
  } else if (tab === "add") {
    screen = <AddScreen user={user}
                        onAccount={() => setRoute({ name: "account" })}
                        onAdded={() => { load(); goTab("book"); }} />;
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
          <button data-onb="add"
                  className={"bk-nav-add" + (activeNav === "add" ? " on" : "")}
                  onClick={() => goTab("add")} aria-label="Add contact">
            <span className="bk-fab"><Plus size={22} /></span><span>Add</span>
          </button>
          <button data-onb="book"
                  className={"bk-nav-item" + (activeNav === "book" ? " on" : "")}
                  onClick={() => goTab("book")}>
            <BookText size={19} /><span>Book</span>
          </button>
        </nav>
      </div>

      {draftFor && <DraftSheet draft={draftFor} onClose={() => setDraftFor(null)} />}

      {onbOn && <BookOnboarding step={onbStep} onGo={onbGo} onClose={onbClose} />}
    </div>
  );
}

// ── Today ────────────────────────────────────────────────────────────────────

// The JL/DW avatar — the entry to Account, present in every screen's topbar.
function Avatar({ user, feed, onAccount }) {
  return (
    <button className="bk-avatar" onClick={onAccount} aria-label="Account"
            title={user?.name || ""}>
      {_initials(user?.name || feed?.advisor_name)}
    </button>
  );
}

function TodayView({ feed, err, user, onReload, onAccount, onOpen, onDraft }) {
  const updates = feed?.updates || [];
  const needs = feed?.needs_outreach || [];

  return (
    <div className="bk-scroll">
      <header className="bk-topbar">
        <div>
          <p className="bk-eyebrow">
            {_today_long()}
            {/* Deploy-pipeline probe: a deliberately visible, harmless marker so
                we can confirm a frontend change actually shipped to
                event.surpluslayer.com. Safe to remove once the deploy is verified. */}
            <span style={{ marginLeft: 8, opacity: 0.5, fontWeight: 600 }}>
              · deploy check ✓
            </span>
          </p>
          <p className="bk-display">Your book today</p>
        </div>
        <Avatar user={user} feed={feed} onAccount={onAccount} />
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

// Relationship-type filters = the capture "This person is…" tags.
const FILTERS = [
  { key: "all", label: "All" },
  { key: "sales", label: "Sales" },
  { key: "hiring", label: "Hiring" },
  { key: "investor", label: "Investor" },
  { key: "partner", label: "Partner" },
  { key: "follow_up", label: "Follow-up" },
];
const TAG_LABEL = { sales: "Sales", hiring: "Hiring", investor: "Investor",
                    partner: "Partner", follow_up: "Follow-up" };

function BookView({ feed, err, user, onReload, onAccount, onOpen, onDraft }) {
  const [filter, setFilter] = useState("all");
  const [expanded, setExpanded] = useState(false);
  const [q, setQ] = useState("");
  const roster = feed?.roster || [];

  const needle = q.trim().toLowerCase();
  const shown = roster.filter((r) => {
    const tags = r.tags || [];
    // Search matches name / title / firm / event AND the relationship-type
    // tags (so "follow-up", "sales", or an event name all find people).
    if (needle) {
      const hay = [r.name, r.title, r.firm, r.met_at,
                   ...tags.map((t) => TAG_LABEL[t] || t)];
      if (!hay.some((v) => (v || "").toLowerCase().includes(needle))) return false;
    }
    // Pills filter by relationship type.
    if (filter !== "all" && !tags.includes(filter)) return false;
    return true;
  });
  // A live search shows every hit; the capped view is for browsing.
  const cap = (expanded || needle) ? shown.length : 6;
  const visible = shown.slice(0, cap);
  const more = shown.length - visible.length;

  return (
    <div className="bk-scroll">
      <header className="bk-topbar">
        <span className="bk-display bk-display--row">
          Your book <span className="bk-count-lg">{roster.length}</span>
        </span>
        <Avatar user={user} feed={feed} onAccount={onAccount} />
      </header>

      <div className="bk-ask-wrap" data-onb="search">
        <div className="bk-ask">
          <Search size={17} className="bk-ask-spark" />
          <input className="bk-ask-input" placeholder="Search your book…"
                 value={q} onChange={(e) => setQ(e.target.value)} />
          {q && (
            <button className="bk-ask-go" onClick={() => setQ("")} aria-label="Clear">
              <X size={14} />
            </button>
          )}
        </div>
      </div>

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
                {r.stage
                  ? <StageChip stage={r.stage} />
                  : <Health status={r.is_prospect ? "new" : r.status} />}
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
  const [working, setWorking] = useState("");      // "send" | "schedule" | ""
  const [done, setDone] = useState("");
  const [showSched, setShowSched] = useState(false);
  const [sendAt, setSendAt] = useState("");

  // Real Send/Schedule need a numeric contact id; demo-book slugs get Copy.
  const canSend = !!detail.contact_id && /^\d+$/.test(String(detail.contact_id));

  const fetchDraft = useCallback(() => {
    setBusy(true); setErr(""); setDone("");
    api.bookDraft({ contact_id: detail.contact_id, name: detail.name,
                    trigger: detail.reason || "catching up", channel: "email" })
      .then((r) => setBody(r.body || ""))
      .catch((e) => setErr(e.message || "Couldn't draft"))
      .finally(() => setBusy(false));
  }, [detail]);
  useEffect(() => { fetchDraft(); }, [fetchDraft]);

  const copy = async () => {
    try { await navigator.clipboard.writeText(body); setDone("Copied");
          setTimeout(() => setDone(""), 1600); } catch {}
  };
  const sendNow = async () => {
    if (!canSend || working) return;
    setWorking("send"); setErr(""); setDone("");
    try {
      // An explicit Send click means SEND NOW, regardless of the auto-send
      // toggle (that toggle only governs the unattended cron). The schedule
      // path with send_at=null sends immediately (send_and_log) -> status "sent".
      const r = await api.scheduleContactFollowup(detail.contact_id, body, null);
      setDone(r.status === "sent" ? "Sent" : "Saved as draft");
    } catch (e) { setErr(e.message || "Couldn't send"); }
    finally { setWorking(""); }
  };
  const schedule = async () => {
    if (!canSend || !sendAt || working) return;
    setWorking("schedule"); setErr(""); setDone("");
    try {
      const iso = new Date(sendAt).toISOString();
      const r = await api.scheduleContactFollowup(detail.contact_id, body, iso);
      const when = new Date(r.send_at || iso).toLocaleString([],
        { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      setDone(r.status === "sent" ? "Sent" : `Scheduled for ${when}`);
      setShowSched(false);
    } catch (e) { setErr(e.message || "Couldn't schedule"); }
    finally { setWorking(""); }
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
          <textarea className="bk-quote-edit" value={body}
                    onChange={(e) => setBody(e.target.value)} rows={6} />
          {done && <div className="bk-done bk-done--tight"><CheckCircle2 size={15} /> {done}</div>}
          {showSched && canSend && (
            <div className="bk-sched-row">
              <input type="datetime-local" value={sendAt}
                     onChange={(e) => setSendAt(e.target.value)} />
              <button className="bk-btn bk-btn--primary" disabled={!sendAt || !!working}
                      onClick={schedule}>{working === "schedule" ? "…" : "Schedule"}</button>
            </div>
          )}
          <div className="bk-actions">
            {canSend ? (
              <button className="bk-btn bk-btn--primary" disabled={!!working} onClick={sendNow}>
                <Send size={13} style={{ marginRight: 5, verticalAlign: -1 }} />
                {working === "send" ? "Sending…" : "Send"}
              </button>
            ) : (
              <button className="bk-btn bk-btn--primary" onClick={copy}>
                <Send size={13} style={{ marginRight: 5, verticalAlign: -1 }} />
                {done === "Copied" ? "Copied" : "Copy"}
              </button>
            )}
            {canSend && (
              <button className="bk-btn" onClick={() => setShowSched((v) => !v)}>
                {showSched ? "Cancel" : "Schedule"}
              </button>
            )}
            <button className="bk-btn" onClick={fetchDraft}>Refine</button>
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

function AddScreen({ user, onAccount, onAdded }) {
  // Real capture flow — shares the active event + capture/send components with
  // InPersonApp so a contact added here is the same as one scanned at the door.
  const [event, setEvent] = useState(() => loadActiveEvent());
  const [draftEvent, setDraftEvent] = useState("");
  const [creating, setCreating] = useState(false);
  const [evErr, setEvErr] = useState("");
  const [result, setResult] = useState(null);   // scan result → ScanResult screen
  const recents = loadRecentLabels();

  const createEvent = async (label) => {
    const name = (label || "").trim();
    if (!name || creating) return;
    setCreating(true); setEvErr("");
    try {
      const ev = await api.inpersonCreateEvent(name);
      saveActiveEvent(ev); pushRecentLabel(ev.label);
      setEvent(ev); setDraftEvent("");
    } catch (e) { setEvErr(e.message || "Couldn't set the event"); }
    finally { setCreating(false); }
  };

  return (
    <div className="bk-scroll">
      <style>{IP_CSS}</style>
      <header className="bk-topbar">
        <div>
          <p className="bk-eyebrow">Capture someone you just met</p>
          <p className="bk-display">Add contact</p>
        </div>
        <Avatar user={user} onAccount={onAccount} />
      </header>
      <div className="bk-addbody">
        {result ? (
          <ScanResult event={event} result={result}
                      onDone={() => { setResult(null); onAdded && onAdded(); }}
                      onCancel={() => setResult(null)}
                      canSend={!!user?.unipile_account_id}
                      savedLink={(user && user.saved_send_link) || ""} />
        ) : (
          <>
            <div className="bk-event">
              {event && (
                <div className="bk-event-current">
                  <span className="bk-event-name"><MapPin size={18} />{event.label}</span>
                  <ChevronDown size={18} className="bk-faint" />
                </div>
              )}
              <div className="bk-field" style={{ marginTop: event ? 11 : 0 }}>
                <input value={draftEvent} onChange={(e) => setDraftEvent(e.target.value)}
                       placeholder="e.g. NYC Tech Week — Founders Inc"
                       onKeyDown={(e) => { if (e.key === "Enter") createEvent(draftEvent); }} />
                <button className="bk-btn bk-btn--primary" style={{ height: 36 }}
                        disabled={creating || !draftEvent.trim()}
                        onClick={() => createEvent(draftEvent)}>
                  {creating ? <Loader2 size={15} className="bk-spin" /> : "Set"}
                </button>
              </div>
              {recents.length > 0 && (
                <div className="bk-chips bk-recents">
                  {recents.map((r) => (
                    <button key={r} className={"bk-pill" + (event?.label === r ? " on" : "")}
                            onClick={() => createEvent(r)}>{r}</button>
                  ))}
                </div>
              )}
              {evErr && <p className="bk-scan-sub" style={{ color: "#c0433d", marginTop: 8 }}>{evErr}</p>}
            </div>

            {event ? (
              <CaptureScreen event={event} onResult={setResult} />
            ) : (
              <div className="bk-scan">
                <div className="bk-target"><QrCode size={42} /></div>
                <p className="bk-scan-lead">Set the event first</p>
                <p className="bk-scan-sub">Name where you are — everyone you add gets filed under it.</p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── Ask bar / assistant card (agent) ──────────────────────────────────────────

// Match the relationship-agent chat's suggested "bubbles" (event-host framing).
const CHIPS = ["Who should I follow up with?",
               "Who recently changed roles?",
               "Draft pings for anyone going cold"];

function AskBar({ variant, onOpen, onDraft }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);   // {answer, people}
  const [err, setErr] = useState("");
  const [phase, setPhase] = useState("");  // live "thinking / drafting X…" label

  const ask = async (query) => {
    const text = (query ?? q).trim();
    if (!text || busy) return;
    setBusy(true); setErr(""); setRes(null); setQ(text); setPhase("Thinking…");
    try {
      // Streamed: the ranked people show the instant selection finishes, then
      // each draft fills in as it lands. A heartbeat keeps the connection alive
      // so a slow moment shows "drafting…" instead of a 524 "server took too long".
      await api.bookAskStream(text, {
        onStatus: ({ phase: ph, name }) =>
          setPhase(ph === "drafting" ? `Drafting ${name || "…"}` :
                   ph === "selecting" ? "Finding who to follow up with…" : "Thinking…"),
        onPeople: ({ people, answer }) =>
          setRes({ answer: answer || "", people: people || [] }),
        onToken: ({ index, t }) =>     // each card types out live
          setRes((r) => {
            if (!r || !r.people[index]) return r;
            const people = r.people.slice();
            people[index] = { ...people[index], draft: (people[index].draft || "") + t };
            return { ...r, people };
          }),
        onError: ({ detail }) => setErr(detail || "Couldn't ask the agent"),
      });
    } catch (e) { setErr(e.message || "Couldn't ask the agent"); }
    finally { setBusy(false); setPhase(""); }
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
    <div className={variant === "card" ? "bk-assistant" : "bk-ask-wrap"}
         data-onb={variant === "bar" ? "ask" : undefined}>
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

      {busy && phase && (
        <div className="bk-ap-reason" style={{ marginTop: 10, display: "flex",
             alignItems: "center", gap: 6 }}>
          <Loader2 size={13} className="bk-spin" /> {phase}
        </div>
      )}

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
                  <DraftLink onClick={() => onDraft({ name: p.name, contact_id: p.contact_id, trigger: p.reason || "catch up", body: p.draft })} />
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
  const hasInline = !!(draft.body && draft.body.trim());
  const [busy, setBusy] = useState(!hasInline);   // reuse the card's draft if present
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState(draft.body || "");
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const [working, setWorking] = useState("");      // "send" | "schedule" | ""
  const [done, setDone] = useState("");            // success line
  const [showSched, setShowSched] = useState(false);
  const [sendAt, setSendAt] = useState("");

  // Send / Schedule are keyed on a real numeric contact id; demo-book slugs
  // can't send, so we only offer Copy for those.
  const canSend = !!draft.contact_id && /^\d+$/.test(String(draft.contact_id));

  const generate = useCallback(() => {
    // Token-level streaming: the message types out live (like Claude) instead of
    // a blank spinner then a sudden block of text. Falls back to the non-stream
    // endpoint if the stream can't open.
    setBusy(true); setErr(""); setDone(""); setBody("");
    let acc = "";
    api.bookDraftStream(
      { name: draft.name, contact_id: draft.contact_id,
        trigger: draft.trigger, channel: "email" },
      {
        onToken: (t) => { acc += t; setBody(acc); },
        onDone: () => setBusy(false),
        onError: (e) => { setErr(e.detail || "Couldn't draft"); setBusy(false); },
      },
    ).catch(() => {
      // Stream failed to open : fall back to the one-shot draft.
      api.bookDraft({ name: draft.name, contact_id: draft.contact_id,
                      trigger: draft.trigger, channel: "email" })
        .then((r) => { setSubject(r.subject || ""); setBody(r.body || ""); })
        .catch((e) => setErr(e.message || "Couldn't draft"))
        .finally(() => setBusy(false));
    });
  }, [draft]);

  useEffect(() => {
    // Instant: the /ask card already composed this through the shared composer.
    if (hasInline) { setBody(draft.body); setBusy(false); }
    else generate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  const copy = async () => {
    const text = subject ? `Subject: ${subject}\n\n${body}` : body;
    try { await navigator.clipboard.writeText(text); setCopied(true);
          setTimeout(() => setCopied(false), 1600); } catch {}
  };

  const sendNow = async () => {
    if (!canSend || working) return;
    setWorking("send"); setErr(""); setDone("");
    try {
      // Explicit Send = send NOW, regardless of the auto-send toggle (which only
      // governs the unattended cron). schedule(send_at=null) sends immediately.
      const r = await api.scheduleContactFollowup(draft.contact_id, body, null);
      setDone(r.status === "sent" ? "Sent" : "Saved as draft");
    } catch (e) {
      const code = e?.body?.detail?.code || e?.body?.code;
      if (e?.status === 402 || code === "linkedin_send_locked" || code === "payment_required") {
        // Sending is gated for demo / not-signed-in users : take them to sign in.
        window.location.href = "/api/auth/linkedin/start-redirect"; return;
      }
      setErr(e.message || "Couldn't send");
    }
    finally { setWorking(""); }
  };

  const schedule = async () => {
    if (!canSend || !sendAt || working) return;
    setWorking("schedule"); setErr(""); setDone("");
    try {
      const iso = new Date(sendAt).toISOString();
      const r = await api.scheduleContactFollowup(draft.contact_id, body, iso);
      const when = new Date(r.send_at || iso).toLocaleString([],
        { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      setDone(r.status === "sent" ? "Sent" : `Scheduled for ${when}`);
      setShowSched(false);
    } catch (e) { setErr(e.message || "Couldn't schedule"); }
    finally { setWorking(""); }
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
        ) : done ? (
          <>
            <div className="bk-done"><CheckCircle2 size={16} /> {done}</div>
            <div className="bk-sheet-actions">
              <button className="bk-btn bk-btn--primary bk-btn--block" onClick={onClose}>Done</button>
            </div>
          </>
        ) : (
          <>
            {subject !== "" && (
              <input className="bk-sheet-subject" value={subject}
                     onChange={(e) => setSubject(e.target.value)} placeholder="Subject" />
            )}
            <textarea className="bk-sheet-body" value={body}
                      onChange={(e) => setBody(e.target.value)} rows={6} />

            <div className="bk-sheet-minor">
              <button className="bk-link-btn" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
              <button className="bk-link-btn" onClick={generate}>Rewrite</button>
              {canSend && (
                <button className="bk-link-btn" onClick={() => setShowSched((v) => !v)}>
                  {showSched ? "Cancel schedule" : "Schedule for later"}
                </button>
              )}
            </div>

            {showSched && canSend && (
              <div className="bk-sched-row">
                <input type="datetime-local" value={sendAt}
                       onChange={(e) => setSendAt(e.target.value)} />
                <button className="bk-btn bk-btn--primary" disabled={!sendAt || !!working}
                        onClick={schedule}>
                  {working === "schedule" ? "…" : "Schedule"}
                </button>
              </div>
            )}

            <div className="bk-sheet-actions">
              {canSend ? (
                <button className="bk-btn bk-btn--primary bk-btn--block"
                        disabled={!!working} onClick={sendNow}>
                  <Send size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
                  {working === "send" ? "Sending…" : "Send now"}
                </button>
              ) : (
                <button className="bk-btn bk-btn--block" onClick={copy}>
                  {copied ? "Copied" : "Copy message"}
                </button>
              )}
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
    <button data-onb="draft" className="bk-draft"
            onClick={(e) => { e.stopPropagation(); onClick(); }}>
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

// ── demo onboarding coach ─────────────────────────────────────────────────────
//
// A guided six-step tour that pops up over the real Book surface for /demo
// visitors. Each step anchors to a live control by [data-onb] selector,
// highlights it with a pulsing ring, and explains the next thing to do. The
// card is ambient (the underlying UI stays clickable) and the BookApp shell
// switches to the right tab as the visitor advances, so the highlighted
// control is always on screen. Mirrors the in-person OnboardingCoach pattern.

const BK_ONB_STEPS = [
  {
    key: "add", tab: "today", anchor: "add", place: "top",
    title: "Add contacts",
    body: "Start by capturing someone you met. Tap Add to scan their LinkedIn QR or paste their profile.",
  },
  {
    key: "find", tab: "book", anchor: "search", place: "bottom",
    title: "Find them",
    body: "Everyone you capture lands in your book. Search by name, firm, or event to find anyone fast.",
  },
  {
    key: "send", tab: "today", anchor: "draft", place: "bottom",
    title: "Send a message",
    body: "Surplus drafts a follow-up in your voice. Tap Draft on anyone who needs outreach to review it.",
  },
  {
    key: "ask", tab: "today", anchor: "ask", place: "bottom",
    title: "Ask the agent a question",
    body: "Ask your agent anything — like who to follow up with. It reads your whole book to answer.",
  },
  {
    key: "send2", tab: "today", anchor: "draft", place: "bottom",
    title: "Send a message",
    body: "Happy with the draft? Hit Send and Surplus delivers it for you — no copy-paste.",
  },
  {
    key: "list", tab: "book", anchor: "book", place: "top",
    title: "Check your relationship list",
    body: "Open Book any time to see every relationship, sorted by who needs attention.",
    final: true, cta: "Got it",
  },
];

const BK_ONB_CARD_W = 300;

function bkOnbCardStyle(rect, place) {
  const vw = typeof window !== "undefined" ? window.innerWidth : 380;
  const vh = typeof window !== "undefined" ? window.innerHeight : 720;
  const w = Math.min(BK_ONB_CARD_W, vw - 24);
  const base = { position: "fixed", width: w, zIndex: 60 };
  if (!rect) {
    // No live anchor yet : float as a toast above the bottom tab bar.
    return { ...base, left: "50%", bottom: 96, transform: "translateX(-50%)" };
  }
  let left = rect.left + rect.width / 2 - w / 2;
  left = Math.max(10, Math.min(left, vw - w - 10));
  const NEED = 200;
  const spaceBelow = vh - rect.bottom;
  const spaceAbove = rect.top;
  let above;
  if (place === "top") above = spaceAbove >= NEED || spaceAbove >= spaceBelow;
  else above = !(spaceBelow >= NEED || spaceBelow >= spaceAbove);
  const style = { ...base, left };
  if (above) style.bottom = vh - rect.top + 12;
  else style.top = rect.bottom + 12;
  return style;
}

function BookOnboarding({ step, onGo, onClose }) {
  const total = BK_ONB_STEPS.length;
  const idx = Math.min(Math.max(step | 0, 0), total - 1);
  const def = BK_ONB_STEPS[idx];
  const [rect, setRect] = useState(null);
  const selector = `[data-onb="${def.anchor}"]`;

  // Poll the anchor's rect — the underlying app re-renders as the visitor acts.
  useEffect(() => {
    const measure = () => {
      const el = document.querySelector(selector);
      if (el) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) { setRect(r); return; }
      }
      setRect(null);
    };
    measure();
    const id = setInterval(measure, 250);
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      clearInterval(id);
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [selector]);

  const next = () => { if (def.final) onClose("done"); else onGo(idx + 1); };
  const back = () => { if (idx > 0) onGo(idx - 1); };

  return (
    <div className="bk-onb" role="dialog" aria-label="Getting started">
      {rect && (
        <div className="bk-onb-ring" style={{
          position: "fixed", top: rect.top - 6, left: rect.left - 6,
          width: rect.width + 12, height: rect.height + 12,
        }} />
      )}
      <div className={"bk-onb-card" + (rect ? "" : " floating")}
           style={bkOnbCardStyle(rect, def.place)}>
        <div className="bk-onb-top">
          <span className="bk-onb-progress"><Sparkles size={13} /> Step {idx + 1} of {total}</span>
          <button className="bk-onb-x" onClick={() => onClose("skipped")} aria-label="Skip the tour">
            <X size={15} />
          </button>
        </div>
        <div className="bk-onb-title">{def.title}</div>
        <div className="bk-onb-body">{def.body}</div>
        <div className="bk-onb-actions">
          {/* Skipping the tour is a conversion moment, not a dead end: drop the
              visitor straight into LinkedIn sign-in to use it for real. The
              corner ✕ remains a plain dismiss for anyone who just wants to keep
              poking around the demo. */}
          <button className="bk-onb-skip" onClick={signInWithLinkedIn}>
            Skip &amp; sign in
          </button>
          <div className="bk-onb-nav">
            {idx > 0 && <button className="bk-onb-back" onClick={back}>Back</button>}
            <button className="bk-onb-next" onClick={next}>
              {def.final ? def.cta : "Next"} <ArrowRight size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
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
.bk-sheet-subject{display:block; box-sizing:border-box; width:calc(100% - 36px); margin:0 18px 8px;
  padding:10px 12px; border:.5px solid var(--line-2); border-radius:var(--r-md);
  font-family:var(--font-ui); font-size:14px; font-weight:500; color:var(--ink); background:var(--surface);}
.bk-sheet-body{display:block; box-sizing:border-box; width:calc(100% - 36px); margin:0 18px;
  padding:13px 14px; border:.5px solid var(--line-2); border-radius:var(--r-md);
  font-family:var(--font-ui); font-size:14px; line-height:1.55; color:var(--ink);
  background:var(--surface); resize:vertical; min-height:132px;}
.bk-sheet-body:focus, .bk-sheet-subject:focus{outline:none; border-color:var(--accent);}
.bk-sheet-minor{display:flex; gap:4px; justify-content:center; margin:10px 18px 2px; flex-wrap:wrap;}
.bk-link-btn{background:none; border:0; color:var(--muted); font-size:13px; cursor:pointer;
  padding:6px 10px; border-radius:var(--r-md); font-family:var(--font-ui);}
.bk-link-btn:hover{background:var(--surface); color:var(--ink);}
.bk-sched-row{display:flex; gap:8px; margin:6px 18px 2px;}
.bk-sched-row input{flex:1; min-width:0; box-sizing:border-box; padding:9px 11px;
  border:.5px solid var(--line-2); border-radius:var(--r-md); font-family:var(--font-ui);
  font-size:13px; color:var(--ink); background:var(--surface);}
.bk-sheet-actions{margin:12px 18px 4px; display:flex; flex-direction:column; gap:8px;}
.bk-done{display:flex; align-items:center; justify-content:center; gap:7px; margin:22px 18px 6px;
  color:var(--accent); font-size:15px; font-weight:500;}
.bk-done--tight{justify-content:flex-start; margin:8px 0 2px; font-size:13px;}
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
.bk-quote-edit{display:block; box-sizing:border-box; width:100%; background:var(--bg);
  border:.5px solid var(--line); border-radius:var(--r-md); padding:11px 13px;
  font-family:var(--font-display); font-size:14px; color:var(--ink); line-height:1.55;
  resize:vertical; min-height:118px;}
.bk-quote-edit:focus{outline:none; border-color:var(--accent);}
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

/* demo onboarding coach : ambient (the underlying UI stays clickable) */
.bk-onb{position:fixed; inset:0; z-index:58; pointer-events:none;}
.bk-onb-ring{border:2px solid var(--accent); border-radius:14px; z-index:59;
  pointer-events:none; box-shadow:0 0 0 3px rgba(47,109,246,.18),
  0 0 0 9999px rgba(20,23,28,.12); animation:bkonbpulse 1.6s ease-in-out infinite;}
@keyframes bkonbpulse{0%,100%{box-shadow:0 0 0 3px rgba(47,109,246,.18),
  0 0 0 9999px rgba(20,23,28,.12);} 50%{box-shadow:0 0 0 6px rgba(47,109,246,.10),
  0 0 0 9999px rgba(20,23,28,.12);}}
.bk-onb-card{pointer-events:auto; background:var(--bg); border:.5px solid var(--line-2);
  border-radius:var(--r-lg); padding:14px 15px 13px; box-shadow:0 12px 34px rgba(20,23,28,.18);
  font-family:var(--font-ui);}
.bk-onb-card.floating{box-shadow:0 14px 40px rgba(20,23,28,.25);}
.bk-onb-top{display:flex; align-items:center; justify-content:space-between;}
.bk-onb-progress{display:inline-flex; align-items:center; gap:5px; font-size:11px;
  font-weight:600; color:var(--accent); text-transform:uppercase; letter-spacing:.04em;}
.bk-onb-x{background:none; border:0; color:var(--muted); cursor:pointer; padding:2px;
  line-height:0; border-radius:6px;}
.bk-onb-x:active{background:var(--surface);}
.bk-onb-title{font-family:var(--font-display); font-size:18px; font-weight:400;
  color:var(--ink); margin:7px 0 4px;}
.bk-onb-body{font-size:13px; line-height:1.5; color:var(--muted);}
.bk-onb-actions{display:flex; align-items:center; justify-content:space-between;
  margin-top:13px; gap:10px;}
.bk-onb-skip{background:none; border:0; color:var(--faint); font-size:12px;
  cursor:pointer; padding:6px 2px; font-family:var(--font-ui);}
.bk-onb-nav{display:flex; align-items:center; gap:8px;}
.bk-onb-back{background:none; border:0; color:var(--ink); font-size:13px;
  font-weight:500; cursor:pointer; padding:8px 6px; font-family:var(--font-ui);}
.bk-onb-next{display:inline-flex; align-items:center; gap:5px; background:var(--accent);
  color:#fff; border:0; border-radius:var(--r-md); padding:9px 14px;
  font-size:13px; font-weight:500; cursor:pointer; font-family:var(--font-ui);}
.bk-onb-next:active{transform:scale(.98);}
`;
