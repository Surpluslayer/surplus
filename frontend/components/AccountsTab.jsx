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

// Email-sync contacts sometimes carry the ADDRESS as their name; showing
// "jia@surpluslayer.com" as a person reads broken. Display the local part,
// title-cased, until a real name lands from enrichment.
function prettyName(name) {
  const s = (name || "").trim();
  const m = s.match(/^([a-z0-9._+-]+)@[a-z0-9.-]+\.[a-z]{2,}$/i);
  if (!m) return s;
  const local = m[1].replace(/[._+-]+/g, " ").trim();
  return local.replace(/\b\w/g, (c) => c.toUpperCase()) || s;
}

// ── team view : who on the org knows whom, per company ──────────────────────
// The Level-1 team plane (docs/accounts-architecture.md §6): each company row
// expands into ATTRIBUTED paths — which member owns which relationship —
// because a merged number without attribution ("2 paths") tells you nothing
// about who should make the intro. Metadata only: warmth + recency bands,
// never content.
function TeamAccounts({ team, q }) {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState("");
  const [openCid, setOpenCid] = useState(null);
  const [paths, setPaths] = useState({});   // company_id -> rows | "loading"
  const [showWalls, setShowWalls] = useState(false);
  const [showAudit, setShowAudit] = useState(false);

  // Reusable so the conflict-setup flow can refetch in place once the team
  // flips to live (confirm or skip), without remounting the tab.
  const load = useCallback(() => {
    setErr("");
    req(`/api/teams/${team.team_id}/accounts`)
      .then((r) => {
        // Strict-profile interlock: {"view_state":"pending"} must survive
        // normalization, not flatten into an empty list.
        if (r && !Array.isArray(r) && r.view_state === "pending") setRows(r);
        else setRows(Array.isArray(r) ? r : (r?.accounts || []));
      })
      .catch((e) => setErr(e.message || String(e)));
  }, [team.team_id]);
  useEffect(() => { load(); }, [load]);

  const toggle = (cid) => {
    if (openCid === cid) { setOpenCid(null); return; }
    setOpenCid(cid);
    if (!paths[cid]) {
      setPaths((p) => ({ ...p, [cid]: "loading" }));
      req(`/api/teams/${team.team_id}/companies/${cid}/paths`)
        .then((r) => setPaths((p) => ({ ...p, [cid]: Array.isArray(r) ? r : (r?.paths || []) })))
        .catch(() => setPaths((p) => ({ ...p, [cid]: [] })));
    }
  };

  if (err) return <div className="bk-err">{err}</div>;
  if (!rows) return (
    <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Loading team view…</div>
  );
  if (!Array.isArray(rows)) {
    // Pending team: admins get the conflict-setup flow (import list, confirm
    // walls, or skip with a reason); members keep the passive message.
    if (team.role === "admin") return <ConflictSetup team={team} onLive={load} />;
    return (
      <div className="bk-empty">Team view is pending until conflict setup is finished.</div>
    );
  }

  // The shared search box filters the team view too (company name — the
  // Level-1 list shape has no contact names to match, by design).
  const needle = (q || "").trim().toLowerCase();
  const shown = needle
    ? rows.filter((r) => (r.company_name || "").toLowerCase().includes(needle))
    : rows;

  return (
    <div className="bk-group">
      {team.role === "admin" && (
        <div style={{ padding: "4px 16px 8px", display: "flex", gap: 14 }}>
          <button type="button" className="bk-link"
                  style={{ border: 0, background: "none", cursor: "pointer",
                           padding: 0, fontSize: 13 }}
                  onClick={() => setShowWalls((v) => !v)}>
            {showWalls ? "Hide walls" : "Manage walls"}
          </button>
          <button type="button" className="bk-link"
                  style={{ border: 0, background: "none", cursor: "pointer",
                           padding: 0, fontSize: 13 }}
                  onClick={() => setShowAudit((v) => !v)}>
            {showAudit ? "Hide audit" : "Audit"}
          </button>
        </div>
      )}
      {showWalls && <WallsPanel team={team} />}
      {showAudit && team.role === "admin" && <AuditPanel team={team} />}
      {shown.map((r) => (
        <React.Fragment key={r.company_id}>
          <div className="bk-row bk-row--tap" role="button" tabIndex={0}
               onClick={() => toggle(r.company_id)}
               onKeyDown={(e) => { if (e.key === "Enter") toggle(r.company_id); }}>
            <div className="bk-main">
              <p className="bk-name">{r.company_name}</p>
              <p className="bk-sub">
                {r.member_count === 1 ? "1 of you" : `${r.member_count} of you`}
                {" · "}
                {r.path_count === 1 ? "1 path" : `${r.path_count} paths`}
              </p>
            </div>
            <HealthChip status={r.warmth} />
          </div>
          {openCid === r.company_id && (
            <div style={{ padding: "2px 16px 10px" }}>
              {paths[r.company_id] === "loading" && (
                <p className="bk-hint"><Loader2 className="bk-spin" size={13} /> Loading paths…</p>
              )}
              {Array.isArray(paths[r.company_id]) && paths[r.company_id].map((p, i) => (
                <p key={i} className="bk-sub" style={{ margin: "6px 0" }}>
                  <strong>{prettyName(p.member_name)}</strong>
                  {" knows "}
                  <strong>{prettyName(p.contact_name)}</strong>
                  {p.contact_title ? ` (${p.contact_title})` : ""}
                  {" · "}
                  <HealthChip status={p.warmth_band} />
                  {p.last_touch_band && p.last_touch_band !== "never"
                    ? ` · ${p.last_touch_band}` : ""}
                </p>
              ))}
              {Array.isArray(paths[r.company_id]) && paths[r.company_id].length === 0 && (
                <p className="bk-hint">No visible paths.</p>
              )}
            </div>
          )}
        </React.Fragment>
      ))}
      {shown.length === 0 && rows.length > 0 && (
        <div className="bk-empty">No team account matches that search.</div>
      )}
      {rows.length === 0 && (
        <div className="bk-empty">No team accounts yet.</div>
      )}
    </div>
  );
}

// ── walls admin : who is screened from which company ────────────────────────
// The conflicts-of-interest surface (docs/accounts-architecture.md §6). A wall
// makes its subject cease to exist on the team plane for the excluded members,
// in both directions. Admin-only — even READING the wall list is restricted,
// since who-is-conflicted is itself sensitive.
function WallsPanel({ team }) {
  const [walls, setWalls] = useState(null);
  const [roster, setRoster] = useState([]);
  const [err, setErr] = useState("");
  const [name, setName] = useState("");          // company to wall (by name)
  const [reason, setReason] = useState("");
  const [excluded, setExcluded] = useState([]);  // user ids; [] = everyone
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    req(`/api/teams/${team.team_id}/walls`)
      .then((r) => setWalls(Array.isArray(r) ? r : (r?.walls || [])))
      .catch((e) => setErr(e.message || String(e)));
    req(`/api/teams/${team.team_id}/members`)
      .then((r) => setRoster(r?.members || []))
      .catch(() => {});
  }, [team.team_id]);
  useEffect(() => { load(); }, [load]);

  const memberName = (uid) =>
    prettyName((roster.find((m) => m.user_id === uid) || {}).name || `user ${uid}`);

  const toggleExcluded = (uid) =>
    setExcluded((xs) => xs.includes(uid) ? xs.filter((x) => x !== uid)
                                         : [...xs, uid]);

  const create = () => {
    if (!name.trim() || saving) return;
    setSaving(true); setErr("");
    req(`/api/teams/${team.team_id}/walls`, {
      method: "POST",
      body: JSON.stringify({ name_norm: name.trim().toLowerCase(),
                             excluded_user_ids: excluded,
                             reason: reason.trim() || null }),
    })
      .then(() => { setName(""); setReason(""); setExcluded([]); load(); })
      .catch((e) => setErr(e.message || String(e)))
      .finally(() => setSaving(false));
  };

  const remove = (wid) => {
    req(`/api/teams/${team.team_id}/walls/${wid}`, { method: "DELETE" })
      .then(load)
      .catch((e) => setErr(e.message || String(e)));
  };

  return (
    <div style={{ margin: "0 16px 12px", padding: 12, borderRadius: 12,
                  border: "1px solid rgba(0,0,0,0.08)" }}>
      <p className="bk-name" style={{ marginBottom: 6 }}>Ethical walls</p>
      <p className="bk-hint" style={{ marginTop: 0 }}>
        A walled company disappears from the screened members' team view
        entirely, and their own paths into it are hidden from everyone else.
        Their private book is untouched.
      </p>
      {err && <div className="bk-err">{err}</div>}
      {!walls && <p className="bk-hint"><Loader2 className="bk-spin" size={13} /> Loading…</p>}

      {(walls || []).map((w) => (
        <p key={w.wall_id} className="bk-sub"
           style={{ margin: "6px 0", display: "flex", alignItems: "center", gap: 6 }}>
          <strong>{w.company_name || w.subject_name_norm}</strong>
          {" · screens "}
          {(w.excluded_user_ids || []).length === 0
            ? "everyone"
            : (w.excluded_user_ids || []).map(memberName).join(", ")}
          {w.reason ? ` · ${w.reason}` : ""}
          <button type="button" aria-label="Remove wall"
                  style={{ border: 0, background: "none", cursor: "pointer",
                           marginLeft: "auto", padding: 2 }}
                  onClick={() => remove(w.wall_id)}>
            <X size={14} />
          </button>
        </p>
      ))}
      {Array.isArray(walls) && walls.length === 0 && (
        <p className="bk-hint">No walls yet.</p>
      )}

      <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
        <input className="bk-ask-input" placeholder="Company name to wall…"
               style={{ border: "1px solid rgba(0,0,0,0.12)", borderRadius: 8,
                        padding: "8px 10px" }}
               value={name} onChange={(e) => setName(e.target.value)} />
        <input className="bk-ask-input" placeholder="Reason (kept in the audit trail)"
               style={{ border: "1px solid rgba(0,0,0,0.12)", borderRadius: 8,
                        padding: "8px 10px" }}
               value={reason} onChange={(e) => setReason(e.target.value)} />
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          <span className="bk-hint" style={{ margin: 0 }}>Screen:</span>
          {roster.map((m) => (
            <label key={m.user_id} className="bk-sub"
                   style={{ display: "flex", alignItems: "center", gap: 4,
                            cursor: "pointer", margin: 0 }}>
              <input type="checkbox"
                     checked={excluded.includes(m.user_id)}
                     onChange={() => toggleExcluded(m.user_id)} />
              {prettyName(m.name)}
            </label>
          ))}
          <span className="bk-hint" style={{ margin: 0 }}>
            (none checked = everyone)
          </span>
        </div>
        <button type="button" className="bk-chip"
                style={{ cursor: "pointer", alignSelf: "flex-start",
                         opacity: name.trim() && !saving ? 1 : 0.5 }}
                disabled={!name.trim() || saving}
                onClick={create}>
          {saving ? "Adding…" : "Add wall"}
        </button>
      </div>
    </div>
  );
}

// ── conflict setup : first-run gate for strict-profile teams ─────────────────
// While a team's view_state is "pending" the shared plane is dark. The admin
// either imports the firm's conflict list (one company per line / CSV) and
// confirms the provisional walls, or explicitly skips with a reason (kept in
// the audit trail). Both flip the team live; onLive refetches in place.
function ConflictSetup({ team, onLive }) {
  const [text, setText] = useState("");
  const [mapping, setMapping] = useState(null);  // null = nothing imported yet
  const [counts, setCounts] = useState(null);
  const [busy, setBusy] = useState("");          // "" | "import" | "confirm" | "skip"
  const [err, setErr] = useState("");

  // A previous import survives a reload as provisional walls — pick them up so
  // the confirm button is offered without re-pasting.
  useEffect(() => {
    let cancelled = false;
    req(`/api/teams/${team.team_id}/conflicts`)
      .then((r) => {
        if (cancelled || !r) return;
        const rows = Array.isArray(r)
          ? r : (r.mapping || r.conflicts || r.walls || r.rows || []);
        if (Array.isArray(rows) && rows.length > 0) setMapping(rows);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [team.team_id]);

  const doImport = () => {
    if (!text.trim() || busy) return;
    setBusy("import"); setErr("");
    req(`/api/teams/${team.team_id}/conflicts/import`, {
      method: "POST",
      body: JSON.stringify({ text }),
    })
      .then((r) => {
        setMapping(Array.isArray(r) ? r : (r?.mapping || []));
        setCounts(r && !Array.isArray(r) ? (r.counts || null) : null);
      })
      .catch((e) => setErr(e.message || String(e)))
      .finally(() => setBusy(""));
  };

  const confirm = () => {
    if (busy) return;
    setBusy("confirm"); setErr("");
    req(`/api/teams/${team.team_id}/conflicts/confirm`, {
      method: "POST",
      body: JSON.stringify({ confirmed: true }),
    })
      .then(() => onLive())
      .catch((e) => { setErr(e.message || String(e)); setBusy(""); });
  };

  const skip = () => {
    if (busy) return;
    const reason = window.prompt(
      "Why skip conflict setup for now? A reason is required — it's kept in the audit trail.");
    if (!reason || !reason.trim()) return;
    setBusy("skip"); setErr("");
    req(`/api/teams/${team.team_id}/conflicts/skip`, {
      method: "POST",
      body: JSON.stringify({ reason: reason.trim() }),
    })
      .then(() => onLive())
      .catch((e) => { setErr(e.message || String(e)); setBusy(""); });
  };

  const cell = { padding: "6px 8px", textAlign: "left", verticalAlign: "top" };
  const countsLine = counts
    ? Object.entries(counts)
        .filter(([, v]) => typeof v === "number")
        .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`)
        .join(" · ")
    : "";

  return (
    <div style={{ margin: "0 16px 12px", padding: 12, borderRadius: 12,
                  border: "1px solid rgba(0,0,0,0.08)" }}>
      <p className="bk-name" style={{ marginBottom: 6 }}>Conflict setup</p>
      <p className="bk-hint" style={{ marginTop: 0 }}>
        The team view stays dark until conflicts are handled. Paste your
        conflict list to raise walls before anyone sees shared warmth, or skip
        for now with a reason.
      </p>
      {err && <div className="bk-err">{err}</div>}

      <textarea className="bk-sheet-body" rows={5}
                placeholder="Paste your conflict list (one company per line or CSV)"
                value={text}
                onChange={(e) => setText(e.target.value)}
                style={{ width: "100%", resize: "vertical",
                         border: "1px solid rgba(0,0,0,0.12)", borderRadius: 8,
                         padding: "8px 10px" }} />
      <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
        <button type="button" className="bk-chip"
                style={{ cursor: "pointer",
                         opacity: text.trim() && !busy ? 1 : 0.5 }}
                disabled={!text.trim() || !!busy}
                onClick={doImport}>
          {busy === "import" ? "Importing…" : "Import"}
        </button>
        {mapping && (
          <button type="button" className="bk-chip"
                  style={{ cursor: "pointer", fontWeight: 600,
                           opacity: busy ? 0.5 : 1 }}
                  disabled={!!busy}
                  onClick={confirm}>
            {busy === "confirm" ? "Confirming…" : "Confirm walls + go live"}
          </button>
        )}
        <button type="button" className="bk-link"
                style={{ border: 0, background: "none", cursor: "pointer",
                         padding: 0, fontSize: 13, opacity: busy ? 0.5 : 1 }}
                disabled={!!busy}
                onClick={skip}>
          {busy === "skip" ? "Skipping…" : "Skip for now"}
        </button>
      </div>

      {mapping && (
        <>
          {countsLine && (
            <p className="bk-hint" style={{ marginBottom: 0 }}>{countsLine}</p>
          )}
          <div style={{ overflowX: "auto", marginTop: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr>
                  <th className="bk-hint" style={cell}>Line</th>
                  <th className="bk-hint" style={cell}>Company</th>
                  <th className="bk-hint" style={cell}>State</th>
                  <th className="bk-hint" style={cell}>Matched</th>
                </tr>
              </thead>
              <tbody>
                {mapping.map((m, i) => (
                  <tr key={i} style={{ borderTop: "1px solid rgba(0,0,0,0.06)" }}>
                    <td className="bk-sub" style={cell}>{m.line ?? i + 1}</td>
                    <td className="bk-sub" style={cell}>
                      <strong>{m.name || m.name_norm || "—"}</strong>
                    </td>
                    <td style={cell}>
                      <span className="bk-chip">{m.state || "?"}</span>
                    </td>
                    <td className="bk-sub" style={cell}>
                      {(m.matched_companies || []).map((c) => c.name).join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {mapping.length === 0 && (
            <p className="bk-hint">Nothing parsed from that list.</p>
          )}
        </>
      )}
    </div>
  );
}

// ── audit viewer : the team's compliance trail ───────────────────────────────
// Admin-only, newest first. Every wall change, conflict decision, and team
// view read lands here; the panel is deliberately read-only.
const AUDIT_PAGE = 30;

function auditEventLabel(event) {
  const s = (event || "").replace(/[._]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Event";
}

// Best-effort humanization of the free-form detail object into one muted line.
function auditDetailLine(detail) {
  const d = detail || {};
  const parts = [];
  if (d.count != null)
    parts.push(`${d.count} ${d.count === 1 ? "company" : "companies"} viewed`);
  if (d.reason) parts.push(String(d.reason));
  Object.entries(d).forEach(([k, v]) => {
    if (k === "count" || k === "reason") return;
    if (v == null || typeof v === "object") return;
    parts.push(`${k.replace(/_/g, " ")}: ${v}`);
  });
  return parts.join(" · ");
}

function AuditPanel({ team }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(null);   // null when the API returns a bare list
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);    // short page = end, when total unknown
  const [err, setErr] = useState("");

  const fetchPage = useCallback((offset) => {
    setLoading(true); setErr("");
    req(`/api/teams/${team.team_id}/audit?limit=${AUDIT_PAGE}&offset=${offset}`)
      .then((r) => {
        const page = Array.isArray(r) ? r : (r?.rows || []);
        setRows((cur) => (offset === 0 ? page : [...cur, ...page]));
        if (r && !Array.isArray(r) && typeof r.total === "number") setTotal(r.total);
        if (page.length < AUDIT_PAGE) setDone(true);
        setLoaded(true);
      })
      .catch((e) => setErr(e.message || String(e)))
      .finally(() => setLoading(false));
  }, [team.team_id]);
  useEffect(() => { fetchPage(0); }, [fetchPage]);

  const hasMore = total != null ? rows.length < total : !done;

  return (
    <div style={{ margin: "0 16px 12px", padding: 12, borderRadius: 12,
                  border: "1px solid rgba(0,0,0,0.08)" }}>
      <p className="bk-name" style={{ marginBottom: 6 }}>Audit trail</p>
      {err && <div className="bk-err">{err}</div>}
      {!loaded && !err && (
        <p className="bk-hint"><Loader2 className="bk-spin" size={13} /> Loading audit…</p>
      )}

      {rows.map((r, i) => {
        const detail = auditDetailLine(r.detail);
        return (
          <div key={r.id ?? i} style={{ padding: "6px 0",
                                        borderTop: i > 0 ? "1px solid rgba(0,0,0,0.06)" : 0 }}>
            <p className="bk-sub" style={{ margin: 0 }}>
              <strong>{auditEventLabel(r.event)}</strong>
              {r.company?.name ? ` · ${r.company.name}` : ""}
            </p>
            <p className="bk-meta" style={{ margin: "2px 0 0" }}>
              {[prettyName(r.actor?.name) || (r.actor?.user_id != null ? `user ${r.actor.user_id}` : ""),
                _when(r.at)].filter(Boolean).join(" · ")}
            </p>
            {detail && (
              <p className="bk-meta" style={{ margin: "2px 0 0", opacity: 0.7 }}>{detail}</p>
            )}
          </div>
        );
      })}

      {loaded && rows.length === 0 && !err && (
        <p className="bk-hint">No audit events yet.</p>
      )}
      {loaded && hasMore && (
        <button type="button" className="bk-link"
                style={{ border: 0, background: "none", cursor: "pointer",
                         padding: 0, fontSize: 13, marginTop: 6,
                         opacity: loading ? 0.5 : 1 }}
                disabled={loading}
                onClick={() => fetchPage(rows.length)}>
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}

export default function AccountsTab() {
  const [accounts, setAccounts] = useState(null);   // null = loading
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [openId, setOpenId] = useState(null);

  const [team, setTeam] = useState(null);           // first org, if any
  const [view, setView] = useState("mine");         // "mine" | "team"

  const load = useCallback(() => {
    setErr("");
    req("/api/accounts")
      .then((r) => setAccounts(r?.accounts || []))
      .catch((e) => setErr(e.message || String(e)));
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    // Best-effort: the toggle appears only for org members.
    req("/api/teams/mine")
      .then((r) => {
        const teams = Array.isArray(r) ? r : (r?.teams || []);
        if (teams.length > 0) setTeam(teams[0]);
      })
      .catch(() => {});
  }, []);

  const toggleStar = (a) => {
    const next = !a.starred;
    setAccounts((rows) => rows.map((r) => (r.id === a.id ? { ...r, starred: next } : r)));
    req(`/api/accounts/${a.id}`, { method: "PATCH", body: JSON.stringify({ starred: next }) })
      .catch(() => setAccounts((rows) =>
        rows.map((r) => (r.id === a.id ? { ...r, starred: !next } : r))));
  };

  if (openId != null) {
    return <AccountDetail id={openId} hasTeam={!!team}
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
      {team && (
        <div style={{ display: "flex", gap: 8, padding: "0 16px 8px" }}>
          <button type="button"
                  className={"bk-chip" + (view === "mine" ? " on" : "")}
                  style={{ cursor: "pointer",
                           fontWeight: view === "mine" ? 600 : 400 }}
                  onClick={() => setView("mine")}>
            My accounts
          </button>
          <button type="button"
                  className={"bk-chip" + (view === "team" ? " on" : "")}
                  style={{ cursor: "pointer",
                           fontWeight: view === "team" ? 600 : 400 }}
                  onClick={() => setView("team")}>
            {team.name || "Our team"}
          </button>
        </div>
      )}
      <p className="bk-hint">
        {view === "team"
          ? "Who on your team knows people where — tap a company for whose path is whose"
          : "Companies your relationships roll up into"}
      </p>

      {view === "team" && team && <TeamAccounts team={team} q={q} />}

      {view === "mine" && err && (
        <div className="bk-err">{err} <button className="bk-link" onClick={load}>Retry</button></div>
      )}
      {view === "mine" && !accounts && !err && (
        <div className="bk-loading"><Loader2 className="bk-spin" size={18} /> Loading accounts…</div>
      )}

      {view === "mine" && accounts && (
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
                  {(a.member_preview || []).length > 0 &&
                    ` · ${a.member_preview.map(prettyName).join(", ")}`}
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

// The three per-account sharing levels (docs/accounts-architecture.md §5):
// what — if anything — the team plane may derive from this account.
const SHARING_LEVELS = [
  { value: "private",  label: "Private" },
  { value: "metadata", label: "Team: warmth only" },
  { value: "elevated", label: "Team: shared" },
];

function AccountDetail({ id, onBack, hasTeam }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [objective, setObjective] = useState("");
  const [saveNote, setSaveNote] = useState("");
  const [shareNote, setShareNote] = useState("");

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

  // Sharing level PATCHes optimistically; on failure we put the old value
  // back so the segmented row never lies about what the server holds.
  const setSharing = (level) => {
    if (!d || d.sharing_level === level) return;
    const prev = d.sharing_level;
    setShareNote("");
    setD((cur) => (cur ? { ...cur, sharing_level: level } : cur));
    req(`/api/accounts/${id}`, { method: "PATCH",
                                 body: JSON.stringify({ sharing_level: level }) })
      .catch(() => {
        setD((cur) => (cur ? { ...cur, sharing_level: prev } : cur));
        setShareNote("Couldn't update sharing — try again.");
      });
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

          {hasTeam && (
            <div className="bk-panel">
              <div className="bk-panel-head"><span>Sharing</span></div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {SHARING_LEVELS.map((lv) => (
                  <button type="button" key={lv.value}
                          className={"bk-chip" + (d.sharing_level === lv.value ? " on" : "")}
                          style={{ cursor: "pointer",
                                   fontWeight: d.sharing_level === lv.value ? 600 : 400 }}
                          onClick={() => setSharing(lv.value)}>
                    {lv.label}
                  </button>
                ))}
              </div>
              <p className="bk-hint" style={{ padding: 0 }}>
                Private hides this account from your team entirely. Warmth only
                shares who/how-warm, never content.
              </p>
              {shareNote && <p className="bk-hint" style={{ padding: 0 }}>{shareNote}</p>}
            </div>
          )}

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
