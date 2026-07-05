// ── AccountsTab : the Accounts surface of the Book app ──────────────────────
// Peer of the People/Book tab (docs/accounts-architecture.md §4): the list of
// companies the owner's contacts roll up into, then a per-account detail
// panel — who we know there (warmth-sorted with health chips), who left
// ("now at X"), the merged interaction timeline, coverage, and an editable
// objective. Backed by /api/accounts/* (routes/accounts.py → accounts_read.py).
//
// Rendered inside BookApp's bk-root frame, so it reuses the Book design
// tokens + bk-* classes rather than shipping its own CSS.
import React, { useState, useEffect, useCallback } from "react";
import { ChevronLeft, Loader2, Search, Star, X } from "lucide-react";

// Thin local fetch wrapper, same conventions as lib/api.js (same-origin
// cookies, throw on non-2xx). Local because lib/api.js is shared surface and
// the accounts endpoints live only on this tab.
async function req(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const err = new Error(`${res.status} : ${(text || "").slice(0, 200)}`);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

const WORD = { active: "Active", warm: "Warm", cooling: "Cooling",
               dormant: "Dormant", new: "New" };

// Same chip as BookApp's Health (not exported from there).
function HealthChip({ status }) {
  const s = WORD[status] ? status : "new";
  return (
    <span className={`bk-health ${s}`}>
      {s !== "new" && <span className="bk-health-dot" />}
      {WORD[s]}
    </span>
  );
}

// Account warmth word from the cached strength rollup (0-100 = mean member
// touch freshness — see accounts_read.recompute_rollups).
function strengthStatus(score) {
  if (score == null) return "new";
  if (score >= 70) return "active";
  if (score >= 45) return "warm";
  if (score >= 20) return "cooling";
  return "dormant";
}

function _when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function AccountsTab() {
  const [accounts, setAccounts] = useState(null);   // null = loading
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [openId, setOpenId] = useState(null);

  const load = useCallback(() => {
    setErr("");
    req("/api/accounts")
      .then((r) => setAccounts(r?.accounts || []))
      .catch((e) => setErr(e.message || String(e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const toggleStar = (a) => {
    const next = !a.starred;
    setAccounts((rows) => rows.map((r) => (r.id === a.id ? { ...r, starred: next } : r)));
    req(`/api/accounts/${a.id}`, { method: "PATCH", body: JSON.stringify({ starred: next }) })
      .catch(() => setAccounts((rows) =>
        rows.map((r) => (r.id === a.id ? { ...r, starred: !next } : r))));
  };

  if (openId != null) {
    return <AccountDetail id={openId}
                          onBack={() => { setOpenId(null); load(); }} />;
  }

  const needle = q.trim().toLowerCase();
  const shown = (accounts || []).filter((a) => {
    if (!needle) return true;
    const hay = [a.company?.canonical_name, ...(a.member_preview || [])];
    return hay.some((v) => (v || "").toLowerCase().includes(needle));
  });

  return (
    <div className="bk-scroll">
      <header className="bk-topbar">
        <span className="bk-display bk-display--row">
          Accounts <span className="bk-count-lg">{(accounts || []).length}</span>
        </span>
      </header>

      <div className="bk-ask-wrap">
        <div className="bk-ask">
          <Search size={17} className="bk-ask-spark" />
          <input className="bk-ask-input" placeholder="Search accounts…"
                 value={q} onChange={(e) => setQ(e.target.value)} />
          {q && (
            <button className="bk-ask-go" onClick={() => setQ("")} aria-label="Clear">
              <X size={14} />
            </button>
          )}
        </div>
      </div>
      <p className="bk-hint">Companies your relationships roll up into</p>

      {err && <div className="bk-err">{err} <button className="bk-link" onClick={load}>Retry</button></div>}
      {!accounts && !err && (
        <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Loading accounts…</div>
      )}

      {accounts && (
        <div className="bk-group">
          {shown.map((a) => (
            <div key={a.id} className="bk-row bk-row--tap" role="button" tabIndex={0}
                 onClick={() => setOpenId(a.id)}
                 onKeyDown={(e) => { if (e.key === "Enter") setOpenId(a.id); }}>
              <div className="bk-main">
                <p className="bk-name">
                  {a.company?.canonical_name}
                  <button type="button" className="bk-starbtn"
                          aria-label={a.starred ? "Unstar account" : "Star account"}
                          onClick={(e) => { e.stopPropagation(); toggleStar(a); }}
                          style={{ marginLeft: 6, border: 0, background: "none",
                                   cursor: "pointer", verticalAlign: "middle", padding: 0 }}>
                    <Star size={14} className="bk-star"
                          fill={a.starred ? "currentColor" : "none"}
                          style={{ opacity: a.starred ? 1 : 0.4 }} />
                  </button>
                </p>
                <p className="bk-sub">
                  {a.rollups?.contact_count === 1 ? "1 contact"
                    : `${a.rollups?.contact_count ?? 0} contacts`}
                  {(a.member_preview || []).length > 0 && ` · ${a.member_preview.join(", ")}`}
                </p>
                {a.objective && <p className="bk-meta">{a.objective}</p>}
              </div>
              <div style={{ display: "flex", flexDirection: "column",
                            alignItems: "flex-end", gap: 4, flex: "none" }}>
                <HealthChip status={strengthStatus(a.rollups?.strength_score)} />
                <span className="bk-chip">{a.tier}</span>
              </div>
            </div>
          ))}
          {shown.length === 0 && (accounts || []).length > 0 && (
            <div className="bk-empty">No account matches that search.</div>
          )}
          {(accounts || []).length === 0 && (
            <div className="bk-empty">
              <p>No accounts yet.</p>
              <p className="bk-hint">Accounts appear as your contacts are linked to their companies.</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── detail panel ─────────────────────────────────────────────────────────────

function AccountDetail({ id, onBack }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [objective, setObjective] = useState("");
  const [saveNote, setSaveNote] = useState("");

  useEffect(() => {
    let cancelled = false;
    setD(null); setErr("");
    req(`/api/accounts/${id}`)
      .then((r) => { if (!cancelled) { setD(r); setObjective(r?.objective || ""); } })
      .catch((e) => { if (!cancelled) setErr(e.message || "Couldn't load"); });
    return () => { cancelled = true; };
  }, [id]);

  // Objective saves on blur — only when it actually changed.
  const saveObjective = () => {
    if (!d || objective === (d.objective || "")) return;
    setSaveNote("Saving…");
    req(`/api/accounts/${id}`, { method: "PATCH",
                                 body: JSON.stringify({ objective }) })
      .then(() => { setD((prev) => (prev ? { ...prev, objective } : prev));
                    setSaveNote("Saved");
                    setTimeout(() => setSaveNote(""), 1500); })
      .catch(() => setSaveNote("Couldn't save — try again."));
  };

  const cov = d?.coverage;
  const covLine = cov && [
    cov.total === 1 ? "1 contact" : `${cov.total} contacts`,
    `${cov.warm} warm`,
    cov.cooling ? `${cov.cooling} cooling` : null,
    cov.dormant ? `${cov.dormant} dormant` : null,
    cov.single_threaded ? "single-threaded" : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className="bk-scroll">
      <div className="bk-detail-head">
        <button className="bk-back" onClick={onBack} aria-label="Back to accounts">
          <ChevronLeft size={20} />
        </button>
        <span className="bk-crumb">Accounts</span>
      </div>

      <div className="bk-subhead">
        <p className="bk-display bk-display--lg">{d?.company?.canonical_name || "…"}</p>
        {d && (
          <div className="bk-stat">
            <HealthChip status={strengthStatus(d.rollups?.strength_score)} />
            {covLine && <span className="bk-stat-sep">· {covLine}</span>}
          </div>
        )}
      </div>

      {err && <div className="bk-err">{err}</div>}
      {!d && !err && (
        <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Reading the account…</div>
      )}

      {d && (
        <>
          <div className="bk-panel">
            <div className="bk-panel-head"><span>Objective</span></div>
            <textarea className="bk-sheet-body" rows={3}
                      placeholder="What do you want from this account? e.g. intro to their platform team"
                      value={objective}
                      onChange={(e) => setObjective(e.target.value)}
                      onBlur={saveObjective}
                      style={{ width: "100%", resize: "vertical" }} />
            {saveNote && <p className="bk-hint" style={{ padding: 0 }}>{saveNote}</p>}
          </div>

          <p className="bk-sec-label bk-sec-label--tl">People here</p>
          <div className="bk-group">
            {d.members.map((m) => (
              <div key={m.contact_id} className="bk-row">
                <div className="bk-main">
                  <p className="bk-name">{m.name}</p>
                  <p className="bk-sub">{[m.role_title || m.title].filter(Boolean).join(" · ")}</p>
                  <p className="bk-meta">
                    {m.last_touch_at ? `Last touch ${_when(m.last_touch_at)}` : "No interactions yet"}
                  </p>
                </div>
                <HealthChip status={m.health?.status} />
              </div>
            ))}
            {d.members.length === 0 && <div className="bk-empty">No current contacts here.</div>}
          </div>

          {d.former_members.length > 0 && (
            <>
              <p className="bk-sec-label bk-sec-label--tl">Former</p>
              <div className="bk-group">
                {d.former_members.map((m) => (
                  <div key={m.contact_id} className="bk-row">
                    <div className="bk-main">
                      <p className="bk-name" style={{ textDecoration: "line-through", opacity: 0.6 }}>
                        {m.name}
                      </p>
                      <p className="bk-sub">{m.now_at ? `now at ${m.now_at}` : "moved on"}</p>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          <p className="bk-sec-label bk-sec-label--tl">Timeline</p>
          <div className="bk-tl">
            {d.timeline.map((t, i) => (
              <div className="bk-tl-item" key={i}>
                <span className="bk-tl-dot" />
                <div>
                  <p className="bk-tl-t">
                    {[t.contact_name, t.title || t.interaction_type].filter(Boolean).join(" — ")}
                  </p>
                  <p className="bk-tl-d">
                    {[_when(t.occurred_at), t.summary].filter(Boolean).join(" · ")}
                  </p>
                </div>
              </div>
            ))}
            {d.timeline.length === 0 && <div className="bk-empty">No history yet.</div>}
          </div>
        </>
      )}
    </div>
  );
}
