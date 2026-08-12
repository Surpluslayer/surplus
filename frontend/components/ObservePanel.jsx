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
import React, { useState, useEffect, useRef, useCallback } from "react";

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
  // The log names the fix for a missing evaluation dataset -- but the fix is a
  // POST, which nobody can issue from an address bar. When that state is
  // detected the panel offers the button that does it.
  const [needsDataset, setNeedsDataset] = useState(false);
  const [seeding, setSeeding] = useState(false);
  // The log tells you the evaluation dataset is missing and names the fix --
  // but the fix is a POST, which nobody can issue from an address bar. So
  // when that state is detected the panel offers the button that does it.
  const [needsDataset, setNeedsDataset] = useState(false);
  const [seeding, setSeeding] = useState(false);
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
      if ((e.msg || "").includes("no usable evaluation cohort")) setNeedsDataset(true);
      const t = Date.parse(e.ts);
      let i = prev.length;
      if (!Number.isNaN(t)) {
        const floor = Math.max(0, prev.length - REORDER_WINDOW);
        while (i > floor && Date.parse(prev[i - 1].ts) > t) i--;
      }
      if ((e.msg || "").includes("no usable evaluation cohort")) setNeedsDataset(true);
      const next = i === prev.length
        ? prev.concat([item])
        : prev.slice(0, i).concat([item], prev.slice(i));
      // Bounded so a long session can't grow without limit.
      return next.length > 2000 ? next.slice(next.length - 2000) : next;
    });
  }, []);


  const open = useLogStream(append);

  const seedDataset = useCallback(() => {
    if (seeding) return;
    setSeeding(true);
    append({ ts: new Date().toISOString(), level: "step", src: "frontend.ObservePanel",
             msg: "── building an evaluation dataset → POST /api/observe/evaluation-dataset ──" });
    fetch("/api/observe/evaluation-dataset?lawyers=25&days=30",
          { method: "POST", credentials: "same-origin" })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) throw new Error(d && d.detail ? d.detail : "request failed");
        append({ ts: new Date().toISOString(), level: "ok",
                 src: "backend.routes.observe:create_evaluation_dataset",
                 msg: `  cohort_id=${d.cohort_id} · ${d.lawyers_resolvable} lawyers `
                      + `resolvable · provenance=${d.provenance}` });
        setNeedsDataset(false);
        open("/api/observe/stream/boot", { onDone: () => setBooting(false) });
      })
      .catch(e => append({ ts: new Date().toISOString(), level: "error",
                           src: "frontend.ObservePanel",
                           msg: `  could not build a dataset: ${e.message || e}` }))
      .finally(() => setSeeding(false));
  }, [append, open, seeding]);

  const seedDataset = useCallback(() => {
    if (seeding) return;
    setSeeding(true);
    append({ ts: new Date().toISOString(), level: "step",
             src: "frontend.ObservePanel",
             msg: "── building an evaluation dataset → POST /api/observe/evaluation-dataset ──" });
    fetch("/api/observe/evaluation-dataset?lawyers=25&days=30",
          { method: "POST", credentials: "same-origin" })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) throw new Error(d && d.detail ? d.detail : "request failed");
        append({ ts: new Date().toISOString(), level: "ok",
                 src: "backend.routes.observe:create_evaluation_dataset",
                 msg: `  cohort_id=${d.cohort_id} · ${d.lawyers_resolvable} lawyers `
                      + `resolvable · provenance=${d.provenance}` });
        setNeedsDataset(false);
        open("/api/observe/stream/boot", { onDone: () => setBooting(false) });
      })
      .catch(e => append({ ts: new Date().toISOString(), level: "error",
                           src: "frontend.ObservePanel",
                           msg: `  could not build a dataset: ${e.message || e}` }))
      .finally(() => setSeeding(false));
  }, [append, open, seeding]);
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
          {needsDataset && (
            <button onClick={seedDataset} disabled={seeding} style={{
              marginLeft: "auto", fontSize: 10.5, fontWeight: 700,
              color: seeding ? T.dim : T.bg, background: seeding ? "transparent" : T.warn,
              border: `1px solid ${T.warn}`, borderRadius: 5, padding: "3px 8px",
              cursor: seeding ? "default" : "pointer", fontFamily: MONO,
            }}>
              {seeding ? "building…" : "build evaluation dataset"}
            </button>
          )}
          {needsDataset && (
            <button onClick={seedDataset} disabled={seeding} style={{
              marginLeft: "auto", fontSize: 10.5, fontWeight: 700,
              color: seeding ? T.dim : T.bg, background: seeding ? "transparent" : T.warn,
              border: `1px solid ${T.warn}`, borderRadius: 5, padding: "3px 8px",
              cursor: seeding ? "default" : "pointer", fontFamily: MONO,
            }}>
              {seeding ? "building…" : "build evaluation dataset"}
            </button>
          )}
          {!follow && (
            <button onClick={() => { setFollow(true); }} style={{
              marginLeft: needsDataset ? 8 : "auto", fontSize: 10.5, fontWeight: 700, color: T.accent,
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

      {/* No summary panel above the log. It rendered a REDUCTION OF THE SAME
          LINES, so every fact appeared twice on one screen and a reader had
          to work out which half was authoritative -- the log below is, since
          it is the thing streamed from the backend. One view, read top to
          bottom, is the whole point of a deploy-log shape. */}
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
