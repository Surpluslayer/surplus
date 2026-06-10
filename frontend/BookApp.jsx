// ── BookApp : the advisor "Your book today" surface ─────────────────────────
// Phone-first home for a relationship-led professional (wealth advisor / lawyer)
// whose income depends on keeping an existing book warm. It opens on Today : a
// time-ordered "Updates" feed (noteworthy events worth a personal note) and a
// priority-ranked "Needs outreach" list (relationships going quiet). Every
// "Draft" generates the note on tap; the ask bar answers questions over the
// book. Backed by /api/book/* (agents/book.py).
//
// Self-contained (own CSS, inline styles) so it stays isolated from the event
// flow and each app's own shell — same pattern as InPersonApp.
import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  Sparkles, ArrowUp, ArrowUpRight, Star, LayoutGrid, Plus, BookText,
  Loader2, X, Copy, Check, RefreshCw,
} from "lucide-react";
import { api } from "./lib/api.js";

const C = {
  ink: "#1c2330", muted: "#6b7280", faint: "#9aa1ad",
  line: "#e9ebf0", card: "#f6f7f9", bg: "#ffffff", page: "#eef0f3",
  accent: "#6d4df6", accentSoft: "#efeafe", star: "#e8a93b",
  ok: "#1c8c4e", warn: "#b9731a", danger: "#c0432f",
};
const SANS = "'Plus Jakarta Sans', system-ui, -apple-system, sans-serif";
const SERIF = "'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif";

// status dot color by relationship health.
const DOT = { active: "#2bb673", warm: "#e8a93b", cooling: "#e0792b", dormant: "#c0432f" };

export default function BookApp() {
  const [user, setUser] = useState(null);        // null=loading, undefined=signed out
  const [feed, setFeed] = useState(null);        // null=loading
  const [err, setErr] = useState("");
  const [tab, setTab] = useState("today");       // "today" | "book" | "add"
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

  return (
    <div className="bk-root">
      <style>{BOOK_CSS}</style>
      <div className="bk-frame">
        {tab === "today" && (
          <TodayView feed={feed} err={err} user={user}
                     onDraft={(d) => setDraftFor(d)} onReload={load} />
        )}
        {tab === "book" && <BookView feed={feed} onDraft={(d) => setDraftFor(d)} />}
        {tab === "add" && <AddView onBack={() => setTab("today")} />}

        <nav className="bk-tabs">
          <button className={tab === "today" ? "on" : ""} onClick={() => setTab("today")}>
            <LayoutGrid size={20} /><span>Today</span>
          </button>
          <button className="bk-add" onClick={() => setTab("add")} aria-label="Add a client">
            <span className="bk-add-circle"><Plus size={22} /></span><span>Add</span>
          </button>
          <button className={tab === "book" ? "on" : ""} onClick={() => setTab("book")}>
            <BookText size={20} /><span>Book</span>
          </button>
        </nav>
      </div>

      {draftFor && (
        <DraftSheet draft={draftFor} onClose={() => setDraftFor(null)} />
      )}
    </div>
  );
}

// ── Today ────────────────────────────────────────────────────────────────────

function TodayView({ feed, err, user, onDraft, onReload }) {
  const updates = feed?.updates || [];
  const needs = feed?.needs_outreach || [];
  const initials = _initials(user?.name || feed?.advisor_name);

  return (
    <div className="bk-scroll">
      <header className="bk-head">
        <div>
          <div className="bk-date">{_today_long()}</div>
          <div className="bk-title">Your book today</div>
        </div>
        <div className="bk-avatar" title={user?.name || ""}>{initials}</div>
      </header>

      <AskBar onDraft={onDraft} />

      {err && <div className="bk-err">{err} <button className="bk-link" onClick={onReload}>Retry</button></div>}
      {!feed && !err && <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Reading your book…</div>}

      {feed && (
        <>
          <SectionHead label="Updates" count={updates.length} />
          <div className="bk-list">
            {updates.map((u, i) => (
              <FeedRow key={`u${i}`}
                name={u.name} vip={u.vip} sub={u.headline}
                meta={_rel_time(u.detected_at)}
                canDraft={u.can_draft}
                onDraft={() => onDraft({ name: u.name, contact_id: u.contact_id,
                                        trigger: u.trigger || u.headline })} />
            ))}
            {updates.length === 0 && <Empty text="No new updates today." />}
          </div>

          <SectionHead label="Needs outreach" count={needs.length} />
          <div className="bk-list">
            {needs.map((n, i) => (
              <FeedRow key={`n${i}`}
                name={n.name} vip={n.vip} sub={n.reason}
                dot={DOT[n.status]}
                canDraft
                onDraft={() => onDraft({ name: n.name, contact_id: n.contact_id,
                                        trigger: n.trigger || n.reason })} />
            ))}
            {needs.length === 0 && <Empty text="Everyone's warm. Nothing overdue." />}
          </div>
        </>
      )}
    </div>
  );
}

function SectionHead({ label, count }) {
  return (
    <div className="bk-sechead">
      {label} <span className="bk-dot-sep">·</span> <span className="bk-count">{count}</span>
    </div>
  );
}

function FeedRow({ name, vip, sub, meta, dot, canDraft, onDraft }) {
  return (
    <div className="bk-row">
      <div className="bk-row-main">
        <div className="bk-row-name">
          {dot && <span className="bk-statusdot" style={{ background: dot }} />}
          {name}
          {vip && <Star size={14} className="bk-star" fill="currentColor" />}
        </div>
        <div className="bk-row-sub">{sub}</div>
      </div>
      <div className="bk-row-right">
        {meta && <div className="bk-row-meta">{meta}</div>}
        {canDraft && (
          <button className="bk-draft" onClick={onDraft}>
            Draft <ArrowUpRight size={13} />
          </button>
        )}
      </div>
    </div>
  );
}

function Empty({ text }) {
  return <div className="bk-empty">{text}</div>;
}

// ── Ask bar (agent) ──────────────────────────────────────────────────────────

const CHIPS = ["Who's cooling?", "Reviews due", "Who should I follow up with?"];

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
      <div className="bk-ask">
        <Sparkles size={17} className="bk-ask-spark" />
        <input className="bk-ask-input" placeholder="Ask your agent anything…"
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
        <button className="bk-ask-go" onClick={() => ask()} disabled={busy || !q.trim()}
                aria-label="Ask">
          {busy ? <Loader2 size={16} className="bk-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>

      {!res && !busy && (
        <div className="bk-chips">
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

// ── Draft sheet ──────────────────────────────────────────────────────────────

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
        <div className="bk-sheet-head">
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
            <div className="bk-sheet-actions">
              <button className="bk-btn-ghost" onClick={copy}>
                {copied ? <><Check size={15} /> Copied</> : <><Copy size={15} /> Copy</>}
              </button>
              <button className="bk-btn-primary" onClick={onClose}>Looks good</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Book (roster) ─────────────────────────────────────────────────────────────

function BookView({ feed, onDraft }) {
  // Compose a simple roster from the feed: everyone with an update or overdue.
  const rows = [];
  (feed?.updates || []).forEach((u) =>
    rows.push({ name: u.name, vip: u.vip, sub: u.headline, dot: "#2bb673" }));
  (feed?.needs_outreach || []).forEach((n) =>
    rows.push({ name: n.name, vip: n.vip, sub: n.reason, dot: DOT[n.status],
                trigger: n.trigger || n.reason, contact_id: n.contact_id, draftable: true }));

  return (
    <div className="bk-scroll">
      <header className="bk-head">
        <div>
          <div className="bk-date">Your roster</div>
          <div className="bk-title">The book</div>
        </div>
      </header>
      <div className="bk-list" style={{ marginTop: 8 }}>
        {rows.length === 0 && <Empty text="Your book is empty." />}
        {rows.map((r, i) => (
          <FeedRow key={i} name={r.name} vip={r.vip} sub={r.sub} dot={r.dot}
                   canDraft={!!r.draftable}
                   onDraft={() => onDraft({ name: r.name, contact_id: r.contact_id,
                                            trigger: r.trigger })} />
        ))}
      </div>
    </div>
  );
}

// ── Add (placeholder) ─────────────────────────────────────────────────────────

function AddView({ onBack }) {
  return (
    <div className="bk-scroll">
      <header className="bk-head">
        <div>
          <div className="bk-date">New</div>
          <div className="bk-title">Add a client</div>
        </div>
      </header>
      <div className="bk-add-card">
        <Plus size={28} className="bk-add-icon" />
        <div className="bk-add-title">Bring a client into your book</div>
        <p className="bk-add-copy">
          Connect your inbox &amp; calendar to pull your whole roster automatically —
          or add someone by hand. Auto-import is coming to this surface next.
        </p>
        <button className="bk-btn-primary" onClick={onBack}>Back to today</button>
      </div>
    </div>
  );
}

// ── helpers ───────────────────────────────────────────────────────────────────

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

const BOOK_CSS = `
.bk-root { min-height:100dvh; background:var(--bk-page,#eef0f3); display:flex;
  justify-content:center; font-family:${SANS}; color:${C.ink}; }
.bk-root * { box-sizing:border-box; }
.bk-frame { width:100%; max-width:430px; min-height:100dvh; background:${C.bg};
  display:flex; flex-direction:column; position:relative;
  box-shadow:0 0 40px rgba(20,24,40,.06); }
.bk-spin { animation:bkspin 1s linear infinite; }
@keyframes bkspin { to { transform:rotate(360deg); } }

.bk-scroll { flex:1; overflow-y:auto; padding:22px 20px 96px; }

.bk-head { display:flex; align-items:flex-start; justify-content:space-between;
  margin-bottom:16px; }
.bk-date { font-size:13px; color:${C.muted}; font-weight:500; }
.bk-title { font-family:${SERIF}; font-size:27px; font-weight:600; color:${C.ink};
  margin-top:2px; letter-spacing:-.01em; }
.bk-avatar { width:40px; height:40px; border-radius:999px; background:#dfe3ee;
  color:#3c4660; display:flex; align-items:center; justify-content:center;
  font-size:13px; font-weight:800; flex-shrink:0; }

/* ask bar */
.bk-ask-wrap { margin-bottom:22px; }
.bk-ask { display:flex; align-items:center; gap:10px; background:${C.card};
  border:1px solid ${C.line}; border-radius:14px; padding:12px 12px 12px 14px; }
.bk-ask-spark { color:${C.accent}; flex-shrink:0; }
.bk-ask-input { flex:1; border:0; background:none; outline:none; font-size:15px;
  color:${C.ink}; font-family:${SANS}; min-width:0; }
.bk-ask-input::placeholder { color:${C.faint}; }
.bk-ask-go { flex-shrink:0; width:30px; height:30px; border-radius:9px; border:0;
  background:${C.accent}; color:#fff; display:flex; align-items:center;
  justify-content:center; cursor:pointer; }
.bk-ask-go:disabled { background:#c7c1ec; cursor:default; }
.bk-chips { display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; }
.bk-chip { border:1px solid ${C.line}; background:${C.bg}; border-radius:999px;
  padding:6px 12px; font-size:12.5px; color:${C.muted}; cursor:pointer;
  font-family:${SANS}; font-weight:600; }
.bk-chip:active { background:${C.card}; }
.bk-answer { margin-top:12px; background:${C.accentSoft}; border:1px solid #e2d9fb;
  border-radius:14px; padding:13px 15px; }
.bk-answer-text { font-size:14px; color:${C.ink}; line-height:1.5; }
.bk-answer-people { margin-top:10px; display:flex; flex-direction:column; gap:8px; }
.bk-answer-person { display:flex; align-items:flex-start; justify-content:space-between;
  gap:10px; background:${C.bg}; border:1px solid ${C.line}; border-radius:10px;
  padding:9px 11px; }
.bk-ap-name { font-size:13.5px; font-weight:700; color:${C.ink}; }
.bk-ap-reason { font-size:12px; color:${C.muted}; margin-top:1px; }
.bk-ap-draft { font-size:12px; color:${C.muted}; font-style:italic; margin-top:4px;
  line-height:1.4; }

/* sections + rows */
.bk-sechead { font-size:14px; font-weight:800; color:${C.ink}; margin:18px 2px 9px; }
.bk-dot-sep { color:${C.faint}; font-weight:600; }
.bk-count { color:${C.muted}; font-weight:700; }
.bk-list { display:flex; flex-direction:column; gap:1px; background:${C.line};
  border-radius:14px; overflow:hidden; border:1px solid ${C.line}; }
.bk-row { display:flex; align-items:center; justify-content:space-between; gap:12px;
  background:${C.card}; padding:14px 15px; }
.bk-row-main { min-width:0; flex:1; }
.bk-row-name { font-size:15.5px; font-weight:700; color:${C.ink};
  display:flex; align-items:center; gap:7px; }
.bk-statusdot { width:8px; height:8px; border-radius:999px; flex-shrink:0; }
.bk-star { color:${C.star}; flex-shrink:0; }
.bk-row-sub { font-size:13px; color:${C.muted}; margin-top:3px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.bk-row-right { display:flex; flex-direction:column; align-items:flex-end; gap:4px;
  flex-shrink:0; }
.bk-row-meta { font-size:12px; color:${C.faint}; }
.bk-draft { display:inline-flex; align-items:center; gap:3px; background:none;
  border:0; color:${C.accent}; font-size:13.5px; font-weight:700; cursor:pointer;
  font-family:${SANS}; padding:0; }
.bk-draft:active { opacity:.6; }
.bk-empty { background:${C.card}; padding:20px 15px; text-align:center;
  color:${C.faint}; font-size:13.5px; }

.bk-loading { display:flex; align-items:center; gap:8px; color:${C.muted};
  font-size:14px; padding:18px 2px; }
.bk-err { background:#fdeaea; color:${C.danger}; border:1px solid #f3c9c2;
  border-radius:10px; padding:10px 13px; font-size:13px; }
.bk-link { background:none; border:0; color:${C.accent}; font-weight:700;
  cursor:pointer; font-size:13px; font-family:${SANS}; padding:4px 0; }

/* bottom tabs */
.bk-tabs { position:sticky; bottom:0; display:flex; align-items:center;
  justify-content:space-around; background:${C.bg}; border-top:1px solid ${C.line};
  padding:8px 6px calc(8px + env(safe-area-inset-bottom)); }
.bk-tabs button { border:0; background:none; display:flex; flex-direction:column;
  align-items:center; gap:3px; font-size:11px; font-weight:700; color:${C.faint};
  cursor:pointer; font-family:${SANS}; padding:4px 18px; }
.bk-tabs button.on { color:${C.accent}; }
.bk-add { color:${C.accent} !important; }
.bk-add-circle { width:44px; height:44px; border-radius:999px; background:${C.accentSoft};
  color:${C.accent}; display:flex; align-items:center; justify-content:center;
  margin-top:-14px; box-shadow:0 4px 14px rgba(109,77,246,.22); }

/* draft sheet */
.bk-sheet-scrim { position:fixed; inset:0; background:rgba(18,22,34,.42);
  display:flex; align-items:flex-end; justify-content:center; z-index:50; }
.bk-sheet { width:100%; max-width:430px; background:${C.bg};
  border-radius:20px 20px 0 0; padding:18px 18px calc(20px + env(safe-area-inset-bottom));
  box-shadow:0 -10px 40px rgba(18,22,34,.25); animation:bksheet .18s ease-out; }
@keyframes bksheet { from { transform:translateY(20px); opacity:.6; } to { transform:none; opacity:1; } }
.bk-sheet-head { display:flex; align-items:flex-start; justify-content:space-between;
  margin-bottom:14px; }
.bk-sheet-to { font-size:16px; font-weight:800; color:${C.ink}; }
.bk-sheet-trigger { font-size:13px; color:${C.muted}; margin-top:2px; }
.bk-sheet-x { background:none; border:0; color:${C.muted}; cursor:pointer; padding:2px; }
.bk-sheet-subject { width:100%; border:1px solid ${C.line}; border-radius:10px;
  padding:11px 12px; font-size:14px; font-weight:600; color:${C.ink}; margin-bottom:9px;
  font-family:${SANS}; }
.bk-sheet-body { width:100%; border:1px solid ${C.line}; border-radius:12px;
  padding:12px 13px; font-size:14.5px; line-height:1.5; color:${C.ink};
  font-family:${SANS}; resize:vertical; }
.bk-sheet-subject:focus, .bk-sheet-body:focus { outline:none; border-color:${C.accent};
  box-shadow:0 0 0 3px rgba(109,77,246,.12); }
.bk-sheet-actions { display:flex; gap:10px; margin-top:14px; }
.bk-btn-ghost, .bk-btn-primary { flex:1; display:inline-flex; align-items:center;
  justify-content:center; gap:6px; border-radius:12px; padding:13px; font-size:14.5px;
  font-weight:700; cursor:pointer; font-family:${SANS}; }
.bk-btn-ghost { border:1px solid ${C.line}; background:${C.bg}; color:${C.ink}; }
.bk-btn-primary { border:0; background:${C.accent}; color:#fff; }

/* add view */
.bk-add-card { background:${C.card}; border:1px solid ${C.line}; border-radius:16px;
  padding:30px 22px; text-align:center; margin-top:10px; }
.bk-add-icon { color:${C.accent}; }
.bk-add-title { font-size:17px; font-weight:800; color:${C.ink}; margin:12px 0 6px; }
.bk-add-copy { font-size:13.5px; color:${C.muted}; line-height:1.55; margin:0 0 16px; }
`;
