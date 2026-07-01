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
  Mail, Calendar, Plug, CreditCard, LogOut, CheckCircle2, Mic, Video, MessageCircle,
  KeyRound,
} from "lucide-react";
import { api } from "./lib/api.js";
import {
  CaptureScreen, ScanResult, SignInBounce, IP_CSS,
  loadActiveEvent, saveActiveEvent, loadRecentLabels, pushRecentLabel,
} from "./CaptureShared.jsx";
import { StageChip } from "./components/ContactsPage.jsx";
import AuthOptions from "./components/AuthOptions.jsx";
import LinkedInMark from "./components/LinkedInMark.jsx";

// Demo → real conversion: send the visitor into the connect-first LinkedIn
// flow (same entry the send-gate uses). The callback returns them to the real
// event.surpluslayer.com app with onboarding armed.
// Best-effort analytics. `lib/analytics.js` (lazy-loaded on idle by
// main-inperson.jsx) publishes its `capture` fn on window.__surplusTrack after
// init, so we call it synchronously here WITHOUT statically importing analytics
// (keeps PostHog off BookApp's critical bundle). No-ops before init / never
// throws, so it can't break or block anything.
function track(event, props) {
  try { window.__surplusTrack && window.__surplusTrack(event, props); } catch { /* no-op */ }
}

// Demo -> real conversion now lands on the in-app SIGN-UP screen (AuthOptions,
// "Create account" with email / Google / Microsoft), NOT LinkedIn OAuth. The
// `?signup` param on the event host forces that screen to render over the demo
// (see SIGNUP_PARAM handling in BookApp). LinkedIn stays a CONNECT option once
// the user is signed in; it is no longer the sign-in door.
function goToSignup(source) {
  // `source` labels which conversion CTA was tapped (banner / tour_final /
  // tour_skip / draft_send / ...). When called as a bare onClick handler the
  // arg is a DOM event, so only strings count.
  track("demo_signin_click", { source: typeof source === "string" ? source : "unknown" });
  // Sign-in now happens on the sign-up screen (email/password + Google/
  // Microsoft via AuthOptions, which is native-aware for the iOS app).
  window.location.href = "/?signup";
}

// True when the URL asks for the sign-up screen (?signup). This is the single
// shared target every "Sign up now" CTA + the landing "Try now" button point
// at. Read once at module scope so the value is stable for the initial render.
function wantsSignup() {
  try {
    const params = new URLSearchParams(window.location.search);
    return params.has("signup");
  } catch { return false; }
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
  // relationship list.
  //
  // The tour is recurring: it re-arms every time the demo page is shown — a
  // fresh load AND a back/forward (bfcache) restore, e.g. when a visitor
  // bounces out to LinkedIn sign-in, cancels, and hits back. Dismiss / skip
  // only hides it for the current view; reopening the demo always brings it
  // back. The only thing that truly ends it is signing in — once they have a
  // real account is_demo is false, so the popups never show again.
  const [onbStep, setOnbStep] = useState(0);
  const [onbOn, setOnbOn] = useState(false);
  useEffect(() => {
    if (!user || typeof user !== "object" || !user.is_demo) return;
    const arm = () => { setOnbStep(0); setOnbOn(true); };
    arm();
    window.addEventListener("pageshow", arm);
    return () => window.removeEventListener("pageshow", arm);
  }, [user]);
  const onbGo = (i) => {
    const next = Math.min(Math.max(i, 0), BK_ONB_STEPS.length - 1);
    setOnbStep(next);
    // Put the screen the step points at in front of the visitor.
    setRoute(null);
    setTab(BK_ONB_STEPS[next].tab);
  };
  const onbClose = () => setOnbOn(false);

  // Conversion funnel denominator: fire once when a demo visitor lands, so the
  // `demo_signin_click` events (by source) can be measured against it.
  useEffect(() => {
    if (user && typeof user === "object" && user.is_demo) {
      track("demo_signin_shown", { surface: "book" });
    }
  }, [user && typeof user === "object" ? user.is_demo : false]);

  // Auth still resolving (user === null): brief neutral loading, NOT the book
  // shell and NOT the sign-in screen, so a returning user with a valid session
  // lands straight in the book once /me resolves (no login flash).
  if (user === null) {
    return (
      <div className="bk-root">
        <style>{BOOK_CSS}</style>
        <div className="bk-frame">
          <div className="bk-loading" style={{ minHeight: "60vh" }}>
            <Loader2 className="bk-spin" size={18} /> Loading…
          </div>
        </div>
      </div>
    );
  }

  // ?signup → force the in-app sign-up screen (AuthOptions, "Create account")
  // regardless of demo state. Shared target for every "Sign up now" CTA and the
  // landing "Try now". A real signed-in (non-demo) user falls through to their app.
  if (wantsSignup() && (user === undefined || (user && typeof user === "object" && user.is_demo))) {
    return <BookSignupScreen />;
  }

  // Signed out (real 401, user === undefined) → the sign-in bounce.
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
                                 onDraftDone={() => {}} isDemo={!!user?.is_demo} />;
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
        {user?.is_demo ? (
          <div className="bk-demobar">
            <span><b>Demo</b> · sample data. Sign in to use it for real, or skip the tour.</span>
            <button className="bk-demobar-cta" data-onb="signin" onClick={() => goToSignup("banner")}>Sign up now</button>
          </div>
        ) : !(user?.unipile_account_id && user?.linkedin_status === "active") ? (
          // Real user without LinkedIn connected: nudge them into the connectors
          // screen (the one place connect lives) instead of a standalone CTA.
          <div className="bk-demobar">
            <span><b>Connect LinkedIn</b> to enrich your book and catch job changes.</span>
            <button className="bk-demobar-cta" onClick={() => setRoute({ name: "connections" })}>Connect</button>
          </div>
        ) : null}
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

      {draftFor && <DraftSheet draft={draftFor} onClose={() => setDraftFor(null)}
                               isDemo={!!user?.is_demo} />}

      {onbOn && <BookOnboarding step={onbStep} onGo={onbGo} onClose={onbClose} />}
    </div>
  );
}

// ── Sign-up screen (the ?signup target) ──────────────────────────────────────
// Renders AuthOptions in "Create account" mode over the event host. Reached by
// every "Sign up now" CTA and the landing "Try now" button (both navigate to
// /?signup). On success we drop the param and reload into the real signed-in
// Book, so the new account sees their own book, not the demo.
function BookSignupScreen() {
  const onSignedIn = () => {
    try {
      const params = new URLSearchParams(window.location.search);
      params.delete("signup");
      const qs = params.toString();
      window.location.href = window.location.pathname + (qs ? `?${qs}` : "");
    } catch {
      window.location.href = "/";
    }
  };
  return (
    <div className="ip-root">
      <style>{IP_CSS}</style>
      <div className="ip-centered">
        <div className="ip-empty">
          <Sparkles size={36} />
          <p className="ip-empty-title">Create your surplus account</p>
          <p>Sign up now to turn this into your real book, with your own contacts, your voice, and your follow-ups.</p>
          <div style={{ width: "100%", maxWidth: 340, marginTop: 18, textAlign: "left" }}>
            <AuthOptions defaultMode="signup" onSignedIn={onSignedIn} />
          </div>
        </div>
      </div>
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

  // "New since you last looked": capture the prior seen-at ONCE, pop updates
  // detected after it, then persist now so each new update only pops once.
  const seenAtRef = useRef(null);
  if (seenAtRef.current === null) {
    const v = Number((typeof localStorage !== "undefined"
      && localStorage.getItem("bk_updates_seen_at")) || 0);
    seenAtRef.current = Number.isFinite(v) ? v : 0;
  }
  useEffect(() => {
    if (updates.length && typeof localStorage !== "undefined") {
      try { localStorage.setItem("bk_updates_seen_at", String(Date.now())); } catch {}
    }
  }, [updates.length]);

  return (
    <div className="bk-scroll">
      <header className="bk-topbar">
        <div>
          <p className="bk-eyebrow">{_today_long()}</p>
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
              <div key={`u${i}`}
                   className={"bk-upd" + (_is_new(u.detected_at, seenAtRef.current) ? " bk-upd--pop" : "")}>
                <Row onOpen={u.contact_id ? () => onOpen(u) : null}>
                  <div className="bk-main">
                    <p className="bk-name">{u.name}<StarToggle contactId={u.contact_id} vip={u.vip} />
                      {u.has_draft && <span className="bk-readytag">Draft ready</span>}</p>
                    <p className="bk-sub">{u.headline}</p>
                  </div>
                  <div className="bk-aside">
                    <p className="bk-time">{_rel_time(u.detected_at)}</p>
                    {u.can_draft && <DraftLink onClick={() => onDraft({ name: u.name, contact_id: u.contact_id, trigger: u.trigger || u.headline, body: u.draft, subject: u.draft_subject })} />}
                  </div>
                </Row>
                {u.draft && (
                  <div className="bk-draftbox" onClick={() => onDraft({ name: u.name, contact_id: u.contact_id, trigger: u.trigger || u.headline, body: u.draft, subject: u.draft_subject })}>
                    {u.draft_subject && <p className="bk-draftsub">{u.draft_subject}</p>}
                    <p className="bk-drafttext">{u.draft}</p>
                  </div>
                )}
              </div>
            ))}
            {updates.length === 0 && <Empty text="No new updates today." />}
          </div>

          <SectionHead label="Needs outreach" count={needs.length} />
          <div className="bk-group">
            {needs.map((n, i) => (
              <Row key={`n${i}`} onOpen={n.contact_id ? () => onOpen(n) : null}>
                <div className="bk-main">
                  <p className="bk-name">{n.name}<StarToggle contactId={n.contact_id} vip={n.vip} /></p>
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
  const [importing, setImporting] = useState(false);
  const [importNote, setImportNote] = useState("");
  const roster = feed?.roster || [];

  const runImport = async () => {
    // The import runs as a background job now (the chat walk is minutes of
    // Unipile paging) : queue it, then poll for progress until it lands.
    setImporting(true); setImportNote("");
    try {
      const start = await api.importConversations();
      const jobId = start?.job_id;
      if (!jobId) throw new Error("Couldn't start the import.");
      const startedAt = Date.now();
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      for (;;) {
        if (Date.now() - startedAt > 240000) {
          setImportNote("Still importing in the background. Check back in a minute.");
          return;
        }
        await sleep(2000);
        let s;
        try { s = await api.importConversationsStatus(jobId); }
        catch { continue; }   // transient poll error : keep waiting
        if (s.status === "done") {
          const n = s.result?.imported ?? 0;
          setImportNote(n > 0 ? `Imported ${n} from your LinkedIn chats.`
                              : "No new conversations to import yet.");
          if (n > 0) onReload?.();
          return;
        }
        if (s.status === "error") {
          throw new Error(s.error || "Import failed.");
        }
        if (s.progress) {
          setImportNote(`Importing… checked ${s.progress.scanned} chats, `
                        + `found ${s.progress.found} people.`);
        }
      }
    } catch (e) {
      setImportNote(e?.message || "Couldn't import. Try again.");
    } finally {
      setImporting(false);
    }
  };

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
                  <p className="bk-name">{r.name}<StarToggle contactId={r.contact_id} vip={r.vip} /></p>
                  <p className="bk-sub">{[r.title, r.firm].filter(Boolean).join(" · ")}</p>
                  <p className="bk-meta">{_book_meta(r)}</p>
                </div>
                {r.stage
                  ? <StageChip stage={r.stage} />
                  : <Health status={r.is_prospect ? "new" : r.status} />}
              </Row>
            ))}
            {visible.length === 0 && roster.length === 0 && (
              <div className="bk-empty">
                <p>Your book is empty.</p>
                <button className="bk-import" onClick={runImport} disabled={importing}>
                  {importing
                    ? <><Loader2 className="bk-spin" size={15} /> Importing…</>
                    : <>Import from LinkedIn chats</>}
                </button>
                <p className="bk-hint">Pulls people you've had real conversations with.</p>
                {importNote && <p className="bk-hint">{importNote}</p>}
              </div>
            )}
            {visible.length === 0 && roster.length > 0 && <Empty text="No one matches this filter." />}
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

function RelationshipScreen({ row, onBack, isDemo = false }) {
  const id = row?.contact_id;
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [starred, setStarred] = useState(!!row?.vip);

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
          {id && (
            <button type="button" className="bk-starbtn"
              aria-label={starred ? "Unstar — stop close monitoring" : "Star — monitor closely"}
              title={starred ? "Starred — monitored closely for updates" : "Star to monitor closely for updates"}
              onClick={() => {
                const next = !starred;
                setStarred(next);                              // optimistic
                api.starContact(id, next).catch(() => setStarred(!next));
              }}
              style={{ marginLeft: 8, border: 0, background: "none", cursor: "pointer",
                       verticalAlign: "middle", padding: 0 }}>
              <Star size={18} className="bk-star" fill={starred ? "currentColor" : "none"}
                    style={{ opacity: starred ? 1 : 0.45 }} />
            </button>
          )}
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

          <DraftPanel detail={d} isDemo={isDemo} />

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

function DraftPanel({ detail, isDemo = false }) {
  const [busy, setBusy] = useState(true);
  const [body, setBody] = useState("");
  const [err, setErr] = useState("");
  const [working, setWorking] = useState("");      // "send" | "schedule" | ""
  const [done, setDone] = useState("");
  const [showSched, setShowSched] = useState(false);
  const [sendAt, setSendAt] = useState("");

  // Real Send/Schedule need a numeric contact id; demo-book slugs get Copy.
  // Demo users can never really send -> always show the "Sign up now"
  // conversion CTA at the moment of intent (no 402-then-redirect detour).
  const canSend = !isDemo && !!detail.contact_id && /^\d+$/.test(String(detail.contact_id));

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
            ) : isDemo ? (
              <button className="bk-btn bk-btn--primary" onClick={() => goToSignup("draft_send")}>
                <Send size={13} style={{ marginRight: 5, verticalAlign: -1 }} />
                Sign up now
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
            <ChevronRight size={17} className="bk-chev" />
          </span>
        </button>
        <div className="bk-set-row">
          <span className="bk-set-lead"><CreditCard size={19} /><span className="bk-set-lbl">Plan</span></span>
          <span className="bk-set-right"><span className="bk-set-val">{plan}</span><ChevronRight size={17} className="bk-chev" /></span>
        </div>
      </div>

      <PasswordSection user={user} />

      <div className="bk-set-group">
        <button className="bk-set-row bk-set-row--danger" onClick={signOut}>
          <span className="bk-set-lead"><LogOut size={19} /><span className="bk-set-lbl">Sign out</span></span>
        </button>
      </div>
    </div>
  );
}

// Set or change the account password. For an OAuth-only account (Google/
// LinkedIn) this adds email+password as an ALTERNATIVE sign-in method; for an
// account that already has one it changes it (current password required).
function PasswordSection({ user }) {
  const has = !!user?.has_password;
  const [open, setOpen] = useState(false);
  const [cur, setCur] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const save = async (e) => {
    e.preventDefault();
    setBusy(true); setMsg(null);
    try {
      await api.setPassword(pw, has ? cur : undefined);
      setMsg({ ok: true, text: has
        ? "Password changed."
        : "Password set — you can now sign in with email + password too." });
      setPw(""); setCur("");
    } catch (err) {
      setMsg({ ok: false, text: err.message || "Couldn't save password." });
    } finally { setBusy(false); }
  };

  const inp = { font: "inherit", fontSize: 14, padding: "9px 11px", borderRadius: 10,
    border: ".5px solid var(--line)", background: "var(--surface)", color: "var(--ink)" };

  return (
    <div className="bk-set-group">
      {!open ? (
        <button className="bk-set-row" onClick={() => setOpen(true)}>
          <span className="bk-set-lead"><KeyRound size={19} />
            <span className="bk-set-lbl">{has ? "Change password" : "Set a password"}</span></span>
          <span className="bk-set-right"><ChevronRight size={17} className="bk-chev" /></span>
        </button>
      ) : (
        <form onSubmit={save} style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 9 }}>
          <span className="bk-set-lbl" style={{ fontWeight: 600 }}>{has ? "Change password" : "Set a password"}</span>
          {!has && (
            <p style={{ fontSize: 12.5, color: "var(--ink-dim)", margin: 0, lineHeight: 1.45 }}>
              Add a password so you can sign in with email + password too — not just Google.
            </p>
          )}
          {has && (
            <input type="password" placeholder="Current password" value={cur}
              onChange={(e) => setCur(e.target.value)} autoComplete="current-password" style={inp} />
          )}
          <input type="password" placeholder="New password (8+ characters)" value={pw}
            onChange={(e) => setPw(e.target.value)} autoComplete="new-password" style={inp} />
          {msg && (
            <p style={{ fontSize: 12.5, margin: 0, color: msg.ok ? "#1f9d62" : "#c0433d" }}>{msg.text}</p>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
            <button type="button" onClick={() => { setOpen(false); setMsg(null); }}
              style={{ font: "inherit", fontSize: 13, fontWeight: 600, border: ".5px solid var(--line)",
                borderRadius: 999, padding: "8px 16px", background: "transparent", color: "var(--ink-dim)" }}>
              Cancel
            </button>
            <button type="submit" disabled={busy || pw.length < 8}
              style={{ font: "inherit", fontSize: 13, fontWeight: 600, border: 0, borderRadius: 999,
                padding: "8px 16px", background: "#2f6df6", color: "#fff", flex: 1,
                opacity: (busy || pw.length < 8) ? 0.6 : 1 }}>
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

// ── Connections ───────────────────────────────────────────────────────────────

function ConnectionsScreen({ user, onBack }) {
  const [note, setNote] = useState("");
  // A FRESH /me snapshot, refetched on mount + window focus + tab-visible, so the
  // rows update after the user connects something elsewhere (e.g. LinkedIn via the
  // extension's connect-cookie, or Gmail/Google in the hosted-auth tab) without a
  // full app reload. The BookApp-level `user` prop is loaded once at app start and
  // would otherwise keep showing "Connect". Falls back to the prop until the first
  // fetch resolves so there's no flicker.
  const [me, setMe] = useState(null);
  const [integrations, setIntegrations] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      api.me()
         .then((d) => { if (!cancelled && d) setMe(d); })
         .catch(() => {});
      api.listIntegrations()
         .then((d) => { if (!cancelled && d) setIntegrations(d); })
         .catch(() => {});
    };
    refresh();
    const onFocus = () => refresh();
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  // Prefer the freshly-fetched /me; fall back to the (stale) prop until it lands.
  const u = me || user;
  // LinkedIn is connected only when an actual Unipile account is tied -- linkedin_status
  // defaults to "active" even with none, so check unipile_account_id too.
  const liOn = !!u?.unipile_account_id && u?.linkedin_status === "active";
  const emailOn = u?.email_status === "active";
  // WhatsApp is a CLOUD seat (Unipile), like email -- connected when the
  // account is tied AND active.
  const whatsappOn = !!u?.unipile_whatsapp_account_id
    && u?.whatsapp_status === "active";
  // All connected mailboxes. Falls back to the legacy single-account view when
  // the API doesn't return the array (older session / pre-feature backend).
  const emailAccounts = Array.isArray(u?.email_accounts)
    ? u.email_accounts : null;
  // Prefer the stored provider; fall back to the address domain (webhook rows
  // don't carry a provider) so a connected mailbox reads "Gmail"/"Outlook", not
  // a bare "Mailbox".
  const providerLabel = (acct) => {
    const p = acct?.provider;
    if (p === "outlook") return "Outlook";
    if (p === "google") return "Gmail";
    const dom = (acct?.address || "").split("@")[1]?.toLowerCase() || "";
    if (/gmail|googlemail/.test(dom)) return "Gmail";
    if (/outlook|hotmail|live|microsoft|office365/.test(dom)) return "Outlook";
    return "Mailbox";
  };
  // Google (calendar + contacts) -- from /me (instant on/off, no separate fetch).
  const googleOn = !!u?.google_connected;
  // Zoom -- shown only when the server has Zoom creds (available.zoom), connected
  // when an active zoom account is tied. From the /api/integrations list.
  const zoomAvail = !!integrations?.available?.zoom;
  const zoomOn = Array.isArray(integrations?.connected)
    && integrations.connected.some((a) => a.provider === "zoom" && a.status === "active");

  const connect = async (starter, label) => {
    try {
      const { url } = await starter();
      if (url) window.location.assign(url);
      else setNote(`Couldn't start ${label}. Try again.`);
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
        <ConnRow icon={<LinkedInMark size={21} />} name="LinkedIn"
                 sub="Enrichment & job-change updates"
                 connected={liOn}
                 onConnect={() => connect(api.startLinkedinAuth, "LinkedIn")} />
        {emailAccounts && emailAccounts.length > 0 ? (
          // One row per connected mailbox (personal Gmail + work Outlook ...),
          // plus an "Add another email" row to connect one more.
          // NOTE: fragment shorthand <>, NOT <React.Fragment> -- the `React`
          // namespace binding is not present in this code-split chunk (Vite's
          // automatic JSX runtime), so `React.Fragment` throws
          // "React is not defined" at render for any user WITH email accounts.
          <>
            {emailAccounts.map((acct) => (
              <ConnRow key={acct.unipile_account_id}
                       icon={<Mail size={21} />}
                       name={providerLabel(acct)}
                       sub={acct.address
                         ? `Connected as ${acct.address}`
                         : "Connected"}
                       connected={acct.status === "active"}
                       onConnect={() => connect(api.startEmailAuth, "email")} />
            ))}
            <ConnRow icon={<Mail size={21} />} name="Add another email"
                     sub="Gmail or Outlook"
                     connected={false}
                     onConnect={() => connect(api.startEmailAuth, "email")} />
          </>
        ) : (
          // Legacy / no-mailbox fallback : the original single Gmail row.
          <ConnRow icon={<Mail size={21} />} name="Gmail"
                   sub={emailOn && user?.email_account_address
                     ? `Connected as ${user.email_account_address}`
                     : "Tracks replies, sends your drafts"}
                   connected={emailOn}
                   onConnect={() => connect(api.startEmailAuth, "Gmail")} />
        )}
        <ConnRow icon={<MessageCircle size={21} />} name="WhatsApp"
                 sub={whatsappOn
                   ? "Connected"
                   : "Tracks chats, sends your drafts"}
                 connected={whatsappOn}
                 onConnect={() => connect(api.startWhatsappAuth, "WhatsApp")} />
        <ConnRow icon={<Calendar size={21} />} name="Google Calendar & Contacts"
                 sub={googleOn ? "Connected" : "Logs meetings, syncs contacts"}
                 connected={googleOn}
                 onConnect={() => connect(api.connectGoogle, "Google")} />
        {integrations === null ? (
          // /api/integrations hasn't resolved yet: render the row in its
          // loading state instead of hiding it, so it doesn't pop into the
          // list a few seconds after the screen opens (layout jump).
          <ConnRow icon={<Video size={21} />} name="Zoom"
                   sub="Add Zoom links to your bookings"
                   loading
                   onConnect={() => connect(api.connectZoom, "Zoom")} />
        ) : zoomAvail && (
          <ConnRow icon={<Video size={21} />} name="Zoom"
                   sub={zoomOn ? "Connected" : "Add Zoom links to your bookings"}
                   connected={zoomOn}
                   onConnect={() => connect(api.connectZoom, "Zoom")} />
        )}
      </div>

      {note && <p className="bk-note bk-note--warn">{note}</p>}
      <p className="bk-note">Surplus reads these to keep your book current. It never posts or emails without you.</p>
    </div>
  );
}

function ConnRow({ icon, name, sub, connected, loading, onConnect }) {
  return (
    <div className="bk-conn-row">
      <span className="bk-tile">{icon}</span>
      <div className="bk-main">
        <p className="bk-name">{name}</p>
        <p className="bk-sub">{sub}</p>
      </div>
      {loading ? (
        // Status not yet known -- show a neutral placeholder, NOT "Connect"
        // (defaulting to Connect makes a connected account flicker Connect->Connected).
        <span className="bk-conn-status" style={{ opacity: 0.4 }}>…</span>
      ) : connected ? (
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
const CHIPS = ["Reach out to my sales prospects",
               "Schedule calls with leads",
               "Follow up with people from the event"];

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
          <div className="bk-assistant-head"><Mic size={16} /><span>Relationship assistant</span></div>
          <div className="bk-field">{input}{go}</div>
        </>
      ) : (
        <div className="bk-ask">
          <Mic size={17} className="bk-ask-spark" />
          {input}
          {go}
        </div>
      )}

      {!res && !busy && (
        <div className="bk-chips" style={{ marginTop: 10 }}>
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

function DraftSheet({ draft, onClose, isDemo = false }) {
  const hasInline = !!(draft.body && draft.body.trim());
  const [busy, setBusy] = useState(!hasInline);   // reuse the card's draft if present
  const [subject, setSubject] = useState(draft.subject || "");
  const [body, setBody] = useState(draft.body || "");
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const [working, setWorking] = useState("");      // "send" | "schedule" | ""
  const [done, setDone] = useState("");            // success line
  const [showSched, setShowSched] = useState(false);
  const [sendAt, setSendAt] = useState("");

  // Send / Schedule are keyed on a real numeric contact id; demo-book slugs
  // can't send, so we only offer Copy for those. Demo users never really send
  // -> always the "Sign up now" conversion CTA (no 402-then-redirect).
  const canSend = !isDemo && !!draft.contact_id && /^\d+$/.test(String(draft.contact_id));

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
        // Sending is gated for demo / not-signed-in users : take them to the
        // sign-up screen (they connect LinkedIn later, after they have an account).
        window.location.href = "/?signup"; return;
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
              ) : isDemo ? (
                <button className="bk-btn bk-btn--primary bk-btn--block" onClick={() => goToSignup("draft_send")}>
                  <Send size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
                  Sign up now
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

// Inline star toggle for list rows. ⭐ = "monitor closely" -> the updates engine
// checks starred contacts DAILY (others weekly). stopPropagation so tapping the
// star doesn't open the row. Optimistic; reverts on API failure (a demo-book
// slug isn't a real contact, so its star just won't persist -- that's fine).
function StarToggle({ contactId, vip }) {
  const [on, setOn] = useState(!!vip);
  const numeric = /^\d+$/.test(String(contactId || ""));
  const toggle = (e) => {
    e.stopPropagation();
    const next = !on;
    setOn(next);
    if (numeric) api.starContact(contactId, next).catch(() => setOn(!next));
  };
  return (
    <button type="button" className="bk-startoggle" onClick={toggle}
            aria-label={on ? "Unstar" : "Star to monitor closely"}
            title={on ? "Starred — checked daily for updates"
                      : "Star to monitor closely (checked daily)"}>
      <Star size={13} className="bk-star" fill={on ? "currentColor" : "none"}
            style={{ opacity: on ? 1 : 0.4 }} />
    </button>
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

// An update is "new" (worth a pop animation) if it was detected AFTER the user
// last looked, bounded to the last 7 days so a first-ever visit doesn't flash the
// whole backlog. seenMs is the prior persisted seen-at (0 on first ever).
function _is_new(iso, seenMs) {
  const t = iso ? new Date(iso).getTime() : 0;
  if (!t || isNaN(t)) return false;
  const floor = Math.max(Number(seenMs) || 0, Date.now() - 7 * 24 * 3600 * 1000);
  return t > floor;
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
    body: "Tap + to scan a LinkedIn QR or paste a profile.",
  },
  {
    key: "find", tab: "book", anchor: "search", place: "bottom",
    title: "Find them",
    body: "Search by name, firm, or event.",
  },
  {
    key: "send", tab: "today", anchor: "draft", place: "bottom",
    title: "Draft a follow-up",
    body: "Tap Draft to review a message in your voice.",
  },
  {
    key: "ask", tab: "today", anchor: "ask", place: "bottom",
    title: "Ask the agent",
    body: "It reads your whole book to answer.",
  },
  {
    key: "send2", tab: "today", anchor: "draft", place: "bottom",
    title: "Send it",
    body: "Hit Send, no copy-paste.",
  },
  {
    key: "list", tab: "book", anchor: "book", place: "top",
    title: "Your relationship list",
    body: "Open Book to see everyone, sorted by who needs attention.",
  },
  {
    key: "signin", tab: "today", anchor: "signin", place: "bottom",
    title: "Make it yours",
    body: "Sign up now to turn this into your real book, "
        + "with your own contacts, your voice, and your follow-ups.",
    final: true, cta: "Sign up now", convert: true,
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

  const next = () => {
    if (def.convert) { goToSignup("tour_final"); return; }  // final step = convert
    if (def.final) onClose(); else onGo(idx + 1);
  };
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
          <span className="bk-onb-progress">Step {idx + 1} of {total}</span>
          <button className="bk-onb-x" onClick={() => onClose()} aria-label="Dismiss the tour">
            <X size={15} />
          </button>
        </div>
        <div className="bk-onb-title">{def.title}</div>
        <div className="bk-onb-body">{def.body}</div>
        <div className="bk-onb-actions">
          {/* Skipping the tour is a conversion moment, not a dead end: drop the
              visitor straight into the sign-up screen to use it for real. The
              corner ✕ remains a plain dismiss for anyone who just wants to keep
              poking around the demo. */}
          <button className="bk-onb-skip" onClick={() => goToSignup("tour_skip")}>
            Skip tour &amp; sign up
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
.bk-demobar{display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding:8px 14px; background:var(--accent,#2563eb); color:#fff;
  font-size:12.5px; line-height:1.3; position:sticky; top:0; z-index:50;}
.bk-demobar b{font-weight:700;}
.bk-demobar-cta{flex:none; border:none; cursor:pointer; background:#fff;
  color:var(--accent,#2563eb); font-size:12px; font-weight:700; border-radius:999px;
  padding:5px 12px;}
.bk-spin{animation:bkspin 1s linear infinite;}
@keyframes bkspin{to{transform:rotate(360deg);}}
/* A freshly-detected update card pops in: slides up with a green highlight flash
   that fades, so a new update draws the eye without disturbing the rest. */
.bk-upd--pop{animation:bkpop 1.1s cubic-bezier(.2,.85,.25,1) both; border-radius:12px;}
@keyframes bkpop{
  0%{opacity:0; transform:translateY(12px) scale(.985); background:rgba(47,210,122,.22);}
  35%{opacity:1; transform:translateY(0) scale(1.012); background:rgba(47,210,122,.16);}
  70%{transform:translateY(0) scale(1); background:rgba(47,210,122,.10);}
  100%{opacity:1; transform:none; background:transparent;}
}

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
.bk-startoggle{border:0; background:none; cursor:pointer; padding:0 0 0 6px;
  vertical-align:middle; line-height:0; flex:none;}
.bk-startoggle:hover .bk-star{opacity:1 !important;}
.bk-draft{font-size:12px; color:var(--accent); cursor:pointer; white-space:nowrap; border:0;
  background:none; font-family:var(--font-ui); padding:0;}
.bk-empty{padding:18px 14px; text-align:center; color:var(--faint); font-size:13px;}
.bk-upd{display:flex; flex-direction:column;}
.bk-readytag{margin-left:8px; font-size:10px; font-weight:700; letter-spacing:.02em;
  text-transform:uppercase; color:#fff; background:var(--accent,#2563eb);
  padding:2px 7px; border-radius:999px; vertical-align:middle;}
.bk-draftbox{margin:-2px 2px 10px; padding:10px 12px; border-radius:10px; cursor:pointer;
  background:var(--card-soft,rgba(37,99,235,.06)); border:1px solid rgba(37,99,235,.18);}
.bk-draftsub{font-size:12px; font-weight:600; margin:0 0 3px; color:var(--ink,#111);}
.bk-drafttext{font-size:13px; line-height:1.45; margin:0; color:var(--muted,#374151); white-space:pre-wrap;}
.bk-import{margin:10px auto 4px; display:inline-flex; align-items:center; gap:7px;
  padding:9px 16px; border-radius:10px; border:none; cursor:pointer;
  background:var(--accent,#2563eb); color:#fff; font-size:13px; font-weight:600;}
.bk-import:disabled{opacity:.6; cursor:default;}

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
.bk-onb-skip{background:none; border:0; color:var(--accent); font-size:13px;
  font-weight:500; cursor:pointer; padding:6px 2px; font-family:var(--font-ui);
  text-decoration:underline; text-underline-offset:2px;}
.bk-onb-nav{display:flex; align-items:center; gap:8px;}
.bk-onb-back{background:none; border:0; color:var(--ink); font-size:13px;
  font-weight:500; cursor:pointer; padding:8px 6px; font-family:var(--font-ui);}
.bk-onb-next{display:inline-flex; align-items:center; gap:5px; background:var(--accent);
  color:#fff; border:0; border-radius:var(--r-md); padding:9px 14px;
  font-size:13px; font-weight:500; cursor:pointer; font-family:var(--font-ui);}
.bk-onb-next:active{transform:scale(.98);}
`;
