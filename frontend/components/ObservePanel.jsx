// ── Surplus Observe : a live execution log, not a dashboard ────────────────
// One continuous streaming log, read like a deploy log. On mount it opens
// /api/observe/stream/boot and every harness runs for real, one line each.
// Click a contact in the Book to the left and its trace is APPENDED to the
// same log: pipeline stages, the ranking arithmetic factor by factor, the
// ablation deltas, then the jurisdiction rule set walked check by check over
// that contact's real drafted message.
//
// Every line comes from backend/observe/logstream.py and corresponds to work
// that actually executed -- `src` is the real module:function that produced
// it, and every duration is a measured perf_counter delta. Nothing here is
// printed to look busy.
//
// This replaced a card/accordion layout that fetched each section with
// Promise.all and rendered nothing until ALL of them resolved -- so one slow
// endpoint left the whole panel stuck on "loading...". A stream has no such
// coupling: each line paints the moment its work finishes.
import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";

// Terminal palette -- dark, high contrast, deliberately close to a deploy log.
const T = {
  bg: "#0d1117", panel: "#111823", line: "#1f2733",
  dim: "#5b6673", text: "#c9d3df", bright: "#e8eef6",
  ok: "#3fb950", warn: "#d29922", err: "#f85149", step: "#58a6ff",
  accent: "#58a6ff",
};
const MONO = "ui-monospace, SFMono-Regular, Menlo, monospace";
const FONT = "'Inter', system-ui, sans-serif";

const LEVEL_COLOR = { ok: T.ok, warn: T.warn, error: T.err, step: T.step, info: T.text };
const LEVEL_TAG = { ok: "OK", warn: "WARN", error: "FAIL", step: "RUN", info: "" };

function hhmmss(ts) {
  try {
    const d = new Date(ts);
    return d.toTimeString().slice(0, 8) + "." +
      String(d.getMilliseconds()).padStart(3, "0");
  } catch { return ""; }
}

// Short module label: backend.observe.harnesses.ablation:run -> observe.harnesses.ablation:run
function shortSrc(src) {
  return (src || "").replace(/^backend\./, "");
}

function LogLine({ e }) {
  const color = LEVEL_COLOR[e.level] || T.text;
  const tag = LEVEL_TAG[e.level] || "";
  // A line that names a harness, or announces a step, is a section marker --
  // bolded so the eye can find structure without the log turning into cards.
  const isMarker = e.level === "step" || (e.harness && e.kind);
  const indented = /^\s{2}/.test(e.msg || "");

  return (
    <div style={{
      display: "flex", gap: 10, padding: "1px 0", alignItems: "baseline",
      fontFamily: MONO, fontSize: 11.5, lineHeight: 1.55, whiteSpace: "pre-wrap",
      wordBreak: "break-word",
    }}>
      <span style={{ color: T.dim, flexShrink: 0, fontSize: 10.5 }}>{hhmmss(e.ts)}</span>
      <span style={{ color, flexShrink: 0, width: 34, fontSize: 10,
                     fontWeight: 700, textAlign: "right" }}>{tag}</span>
      <span style={{
        color: isMarker ? T.bright : (e.level === "info" ? T.text : color),
        fontWeight: isMarker ? 700 : 400,
        paddingLeft: indented ? 14 : 0, flex: 1,
      }}>
        {e.msg}
      </span>
      {e.src && !indented && (
        <span style={{ color: T.dim, fontSize: 10, flexShrink: 0, opacity: 0.75 }}>
          {shortSrc(e.src)}
        </span>
      )}
    </div>
  );
}

// ── readable summary : a live reduction of the SAME lines the log below
// renders, never a second fetch ──────────────────────────────────────────
//
// Observe used to open on a card per pipeline stage, each fetched
// separately with Promise.all -- exactly the "nothing renders until every
// section resolves" design that timed out at Cloudflare's 524 gateway limit
// (see routes/observe.py's own comment on why that was replaced with this
// stream). Reintroducing per-section readability without reintroducing that
// failure mode means deriving it from the stream already in memory instead
// of fetching anything new.
//
// Real pipeline stage order, from backend/observe/pipeline.py:STAGE_ORDER --
// used only to label + order rows already present in the stream.
const STAGE_LABELS = {
  ingestion: "Ingestion", entity_resolution: "Entity resolution",
  signal_library: "Signal library", targeting: "Targeting",
  relationship: "Relationship", ranking: "Ranking",
  jurisdiction: "Jurisdiction", output: "Output",
};
const STAGE_KEYS = Object.keys(STAGE_LABELS);
const STAGE_SRC_RE = /^backend\.observe\.pipeline:_(\w+)$/;

function extractSummary(lines) {
  // Everything since the most recent "clicked X" marker is this contact's
  // trace; before the first click, the summary reduces the boot sequence.
  let start = 0;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].harness === "click" && lines[i].kind === "marker") { start = i + 1; break; }
  }
  const seg = lines.slice(start);

  if (start === 0) {
    const harnesses = [];
    let accountLine = null;
    for (const e of seg) {
      if (e.harness && e.kind) harnesses.push(e);
      else if (e.src === "backend.models:User") accountLine = e;
    }
    return { mode: "boot", harnesses, accountLine };
  }

  const stages = {};
  const rankingFactors = [];
  let pipelineOverall = null, rankingScore = null, jurisdictionVerdict = null;
  let modal = null, cacheAssess = null, cacheDraft = null;
  let judgeLive = null, judgeDead = null, draft = null;

  for (const e of seg) {
    const src = e.src || "", msg = e.msg || "";
    const stageMatch = STAGE_SRC_RE.exec(src);
    if (stageMatch && STAGE_LABELS[stageMatch[1]]) { stages[stageMatch[1]] = e; continue; }
    if (src === "backend.observe.pipeline:iter_stages" && msg.startsWith("pipeline complete")) {
      pipelineOverall = e; continue;
    }
    if (src.startsWith("backend.demo.ranking_trace:_")) { rankingFactors.push(e); continue; }
    if (src === "backend.demo.ranking_trace:compute_trace" && msg.startsWith("opportunity_score")) {
      rankingScore = e; continue;
    }
    if (src === "backend.solicitation:evaluate" && msg.startsWith("VERDICT")) {
      jurisdictionVerdict = e; continue;
    }
    if (src === "backend.jobs:use_modal") { modal = e; continue; }
    if (src === "backend.agents.relationship.book" && msg.startsWith("assess cache")) {
      cacheAssess = e; continue;
    }
    if (src === "backend.agents.relationship.book" && msg.startsWith("draft cache")) {
      cacheDraft = e; continue;
    }
    if (src === "backend.agents.llm:_llm_json") { judgeLive = e; continue; }
    if (src === "backend.agents.llm:judge_relevance_batch") { judgeDead = e; continue; }
    if ((src.includes("drafting:compose_stream") && msg.startsWith("draft composed")) ||
        (src.includes("updates_engine:autodraft") && msg.includes("reusing stored autodraft")) ||
        (src.includes("book:draft_message_cached") && msg.includes("heuristic drafter produced"))) {
      draft = e; continue;
    }
  }

  return { mode: "contact", stages, pipelineOverall, rankingScore, rankingFactors,
           jurisdictionVerdict, modal, cacheAssess, cacheDraft, judgeLive, judgeDead, draft };
}

function SummaryRow({ label, e }) {
  if (!e) return null;
  return (
    <div style={{ display: "flex", gap: 8, padding: "2px 0", alignItems: "baseline" }}>
      <span style={{ color: T.dim, width: 128, flexShrink: 0, fontSize: 10.5,
                    fontFamily: MONO }}>{label}</span>
      <span style={{ color: LEVEL_COLOR[e.level] || T.text, fontSize: 11.5, fontFamily: MONO,
                    flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {e.msg}
      </span>
    </div>
  );
}

function SummarySection({ title, children }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: 0.7, color: T.accent,
                    textTransform: "uppercase", marginBottom: 3 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function SummaryPanel({ summary, selection }) {
  const boxStyle = { padding: "12px 18px", borderBottom: `1px solid ${T.line}`,
                     maxHeight: "44%", overflowY: "auto", flexShrink: 0, background: T.panel };

  if (summary.mode === "boot") {
    return (
      <div style={boxStyle}>
        <SummarySection title="Account">
          <SummaryRow label="signed in" e={summary.accountLine} />
        </SummarySection>
        <SummarySection title="Harness suite">
          {summary.harnesses.length
            ? summary.harnesses.map((e, i) => <SummaryRow key={i} label={e.harness} e={e} />)
            : <div style={{ color: T.dim, fontSize: 11, fontFamily: MONO }}>
                running harness suite… results appear here as each one finishes.
              </div>}
        </SummarySection>
      </div>
    );
  }

  return (
    <div style={boxStyle}>
      <div style={{ fontSize: 12, fontWeight: 700, color: T.bright, marginBottom: 8,
                    fontFamily: MONO }}>
        {(selection && selection.name) || "this contact"}
      </div>
      <SummarySection title="Pipeline">
        {STAGE_KEYS.map(key => <SummaryRow key={key} label={STAGE_LABELS[key]} e={summary.stages[key]} />)}
        <SummaryRow label="overall" e={summary.pipelineOverall} />
      </SummarySection>
      <SummarySection title="Ranking">
        {summary.rankingFactors.map((e, i) => (
          <SummaryRow key={i} label={(e.src || "").split(":_")[1] || "factor"} e={e} />
        ))}
        <SummaryRow label="opportunity_score" e={summary.rankingScore} />
      </SummarySection>
      <SummarySection title="Jurisdiction">
        <SummaryRow label="verdict" e={summary.jurisdictionVerdict} />
      </SummarySection>
      <SummarySection title="Draft · judge · Modal · cache">
        <SummaryRow label="draft" e={summary.draft} />
        <SummaryRow label="judge (live)" e={summary.judgeLive} />
        <SummaryRow label="judge (dead code)" e={summary.judgeDead} />
        <SummaryRow label="Modal" e={summary.modal} />
        <SummaryRow label="assess cache" e={summary.cacheAssess} />
        <SummaryRow label="draft cache" e={summary.cacheDraft} />
      </SummarySection>
    </div>
  );
}

// Client-side product events (BookApp dispatches `surplus:observe` the
// instant an ask is submitted). These land with ZERO round trip, so typing a
// query paints a line immediately instead of the log sitting silent until
// the server's own narration arrives over the activity stream below.
//
// Both are shown because they are different facts: this is the browser
// saying "I sent it", the backend lines are a worker saying "I ran it". A
// gap between them is itself the useful signal -- it means the request is in
// flight, or that the worker handling it never reported.
function useClientEvents(append) {
  useEffect(() => {
    const onEvt = (e) => {
      const d = e && e.detail;
      if (d && d.msg) append(d);
    };
    window.addEventListener("surplus:observe", onEvt);
    return () => window.removeEventListener("surplus:observe", onEvt);
  }, [append]);
}

// A long-lived tail of what the PRODUCT is doing (ask-bar runs, Draft
// taps), narrated with the real machinery each sets off. Separate from the
// on-demand streams below because it stays open for the whole session
// rather than being replaced by the next boot/contact stream.
function useActivityStream(append) {
  useEffect(() => {
    const es = new EventSource("/api/observe/stream/activity", { withCredentials: true });
    es.addEventListener("log", (ev) => {
      try { append(JSON.parse(ev.data)); } catch { /* partial frame */ }
    });
    return () => es.close();
  }, [append]);
}

// One SSE connection appending into a shared buffer.
function useLogStream(append) {
  const esRef = useRef(null);
  return useCallback((url, { onDone } = {}) => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    const es = new EventSource(url, { withCredentials: true });
    esRef.current = es;
    es.addEventListener("log", (ev) => {
      try { append(JSON.parse(ev.data)); } catch { /* ignore a partial frame */ }
    });
    es.addEventListener("error", (ev) => {
      try {
        const d = JSON.parse(ev.data);
        append({ ts: new Date().toISOString(), level: "error",
                 src: "sse", msg: d.detail || "stream error" });
      } catch { /* transport-level error: EventSource retries on its own */ }
    });
    es.addEventListener("done", () => {
      es.close();
      if (esRef.current === es) esRef.current = null;
      if (onDone) onDone();
    });
    return () => { es.close(); };
  }, [append]);
}

export default function ObservePanel({ selection }) {
  const [lines, setLines] = useState([]);
  const [booting, setBooting] = useState(true);
  const [authError, setAuthError] = useState(false);
  const [follow, setFollow] = useState(true);
  const bottomRef = useRef(null);
  const scrollRef = useRef(null);
  const seq = useRef(0);

  // Lines arrive from three sources with different latencies: client events
  // land instantly, the activity stream polls every ~500ms, and boot/contact
  // streams push as work completes. Appending blindly interleaves them out of
  // order -- a client line stamped :27.323 rendering ABOVE a server line
  // stamped :27.058, which in a log reads as the server having answered
  // before the request was sent.
  //
  // So insert by timestamp, scanning back only a short way: far enough to fix
  // the arrival skew between sources, short enough that this stays O(1) per
  // line and that already-read history never reshuffles under the reader.
  const REORDER_WINDOW = 60;
  const append = useCallback((e) => {
    setLines(prev => {
      const item = { ...e, _k: seq.current++ };
      const t = Date.parse(e.ts);
      let i = prev.length;
      if (!Number.isNaN(t)) {
        const floor = Math.max(0, prev.length - REORDER_WINDOW);
        while (i > floor && Date.parse(prev[i - 1].ts) > t) i--;
      }
      const next = i === prev.length
        ? prev.concat([item])
        : prev.slice(0, i).concat([item], prev.slice(i));
      // Bounded so a long session can't grow without limit.
      return next.length > 2000 ? next.slice(next.length - 2000) : next;
    });
  }, []);

  const summary = useMemo(() => extractSummary(lines), [lines]);

  const open = useLogStream(append);
  useClientEvents(append);
  useActivityStream(append);
  // useLogStream's single EventSource ref is shared by the boot stream and
  // every contact stream, so whichever open() call runs LAST wins. The auth
  // probe below is async (a network round-trip); if a reader clicks a card
  // in the Book before it resolves, the click's open(stream/contact/…) would
  // otherwise be silently clobbered the moment the probe finishes and opens
  // the boot stream -- an active, on-screen contact trace replaced out from
  // under the reader with harness output they didn't ask for. This flag
  // makes the click win: once a selection has happened, the boot stream is
  // never opened, since the reader has already moved past "what ran on
  // load" to "why did THIS happen".
  const hasSelectedRef = useRef(false);

  // Auth probe + boot stream on mount.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/observe/cohorts", { credentials: "same-origin" })
      .then(r => {
        if (cancelled) return;
        if (r.status === 401) { setAuthError(true); setBooting(false); return; }
        if (hasSelectedRef.current) { setBooting(false); return; }
        open("/api/observe/stream/boot", { onDone: () => setBooting(false) });
      })
      .catch(() => { if (!cancelled) setBooting(false); });
    return () => { cancelled = true; };
  }, [open]);

  // Append a contact trace whenever the Book selection changes.
  useEffect(() => {
    if (!selection || !selection.contactId) return;
    hasSelectedRef.current = true;
    append({
      ts: new Date().toISOString(), level: "step", src: "frontend.ObservePanel",
      msg: `── clicked ${selection.name || `contact ${selection.contactId}`} `
           + `(${selection.type} ${selection.id}) ─────────────`,
      harness: "click", kind: "marker",
    });
    open(`/api/observe/stream/contact/${selection.contactId}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection && selection.at]);

  // Follow the tail like a deploy log, unless the reader scrolled up.
  useEffect(() => {
    if (follow && bottomRef.current) {
      bottomRef.current.scrollIntoView({ block: "end" });
    }
  }, [lines, follow]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setFollow(atBottom);
  };

  if (authError) {
    return (
      <div style={{ fontFamily: FONT, background: T.bg, minHeight: "100%",
                    color: T.text, padding: 28 }}>
        <p style={{ fontSize: 14 }}>Sign in to a Surplus account to use Observe.</p>
      </div>
    );
  }

  return (
    <div style={{ fontFamily: FONT, background: T.bg, minHeight: "100%",
                  display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ padding: "14px 18px 10px", borderBottom: `1px solid ${T.line}`,
                    flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 10.5, fontWeight: 800, letterSpacing: 0.7,
                        color: T.accent, textTransform: "uppercase" }}>
            Surplus Observe
          </span>
          <span style={{ fontSize: 10.5, color: T.dim, fontFamily: MONO }}>
            {booting ? "running harness suite…" : `${lines.length} lines`}
          </span>
          {!follow && (
            <button onClick={() => { setFollow(true); }} style={{
              marginLeft: "auto", fontSize: 10.5, fontWeight: 700, color: T.accent,
              background: "transparent", border: `1px solid ${T.line}`, borderRadius: 5,
              padding: "3px 8px", cursor: "pointer", fontFamily: MONO,
            }}>
              ↓ follow
            </button>
          )}
        </div>
        <div style={{ fontSize: 11.5, color: T.dim, marginTop: 4 }}>
          Live execution log. Every harness runs on load; click anyone in the Book to
          append their full decision trace.
        </div>
      </div>

      <SummaryPanel summary={summary} selection={selection} />

      <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: 0.7, color: T.dim,
                    textTransform: "uppercase", padding: "8px 18px 4px", flexShrink: 0 }}>
        Execution log
      </div>
      <div ref={scrollRef} onScroll={onScroll}
           style={{ flex: 1, overflowY: "auto", padding: "0 16px 40px",
                    background: T.bg }}>
        {lines.length === 0 && (
          <div style={{ color: T.dim, fontFamily: MONO, fontSize: 11.5 }}>
            connecting…
          </div>
        )}
        {lines.map(e => <LogLine key={e._k} e={e} />)}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
