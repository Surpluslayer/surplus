// Thin fetch wrappers around the surplus backend.
//
// In production the React app is served by FastAPI at the same origin, so
// relative URLs ("/events", "/webhooks/...") just work. In dev, Vite proxies
// these same paths to localhost:8000 : same code, no env switching.
//
// Every call throws on non-2xx so the caller can use try/catch + render
// errors normally.

async function request(path, opts = {}) {
  const res = await fetch(path, {
    // include cookies on every call : the surplus_session cookie carries
    // the signed-in user. "same-origin" works in prod (FastAPI serves the
    // SPA + API at one origin) and in dev (Vite proxies /api → :8000).
    credentials: "same-origin",
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const err = new Error(`${res.status} ${res.statusText} : ${text.slice(0, 240)}`);
    // Surface the status so callers can branch on 404 (event wiped by a
    // backend redeploy) vs 409 (precondition not met) vs 5xx (server error)
    // without parsing the message string.
    err.status = res.status;
    // 402 paywall responses ship a structured body { code, message } the
    // SPA branches on (payment_required vs linkedin_send_locked). Try
    // parsing JSON and attach to the error; non-JSON bodies leave .body null.
    try { err.body = JSON.parse(text); } catch { err.body = null; }
    throw err;
  }
  // some endpoints return empty body
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

// Start an async job and poll it to completion, resolving to the SAME shape the
// old synchronous route returned (PipelineResult / MatchResult). This keeps the
// rest of the app (App.jsx) unchanged: `await api.runProspect(id)` still yields
// a PipelineResult — it just no longer blocks an HTTP worker server-side.
//
// startPath POSTs and returns { job_id, status, ... }; we then poll
// GET /events/{id}/jobs/{job_id} every `intervalMs` until status is done/error.
async function runJob(eventId, startPath, { intervalMs = 2000, timeoutMs = 20 * 60 * 1000 } = {}) {
  const started = await request(startPath, { method: "POST" });
  const jobId = started.job_id;
  if (!jobId) {
    // Defensive: a backend without the async route would 404 above, but if it
    // ever returns a body without a job_id, surface it rather than poll forever.
    throw new Error("async job did not return a job_id");
  }
  const deadline = Date.now() + timeoutMs;
  // small helper so we don't import a sleep util
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  while (Date.now() < deadline) {
    await wait(intervalMs);
    const job = await request(`/events/${eventId}/jobs/${jobId}`);
    if (job.status === "done") return job.result;
    if (job.status === "error") {
      const err = new Error(job.error || "job failed");
      // mirror the synchronous route's 409 contract: not-ready match errors
      // carried operator-facing detail; callers branch on the message text.
      err.jobError = true;
      throw err;
    }
    // queued / running -> keep polling
  }
  throw new Error("job timed out");
}

export const api = {
  // 01 intake
  createEvent: (body) =>
    request("/events", { method: "POST", body: JSON.stringify(body) }),
  getEvent: (id) => request(`/events/${id}`),
  // Describe an event in plain English -> normalized intake profile snapped to
  // the form's chip vocabulary. Mode-less : nothing is persisted, the caller
  // merges the returned fields onto its profile state for the operator to edit.
  intakeFromText: (description) =>
    request("/events/intake/from-text", {
      method: "POST",
      body: JSON.stringify({ description }),
    }),
  // Multi-turn intake interview. The client owns the transcript and replays it
  // each turn ([{role, content}, ...], assistant turns carry the raw JSON the
  // model returned). Returns either a clarifying { question } (complete=false)
  // or a finalized { profile, triage_config, captured, summary } that fills the
  // form just like intakeFromText. Stateless : nothing is persisted server-side.
  intakeTurn: (messages) =>
    request("/events/intake/turn", {
      method: "POST",
      body: JSON.stringify({ messages }),
    }),

  // 02 prospecting
  // Async under the hood: starts a job + polls to completion, resolving to the
  // same PipelineResult the old synchronous /prospect route returned. Pass
  // {fresh:true} to bust the ICP cache.
  runProspect: (id, { fresh = false } = {}) =>
    runJob(id, `/events/${id}/prospect/async${fresh ? "?fresh=true" : ""}`),
  getProspects: (id) => request(`/events/${id}/prospects`),

  // 03 outreach (provider-backed; DRY_RUN by default)
  previewOutreach: (id) => request(`/events/${id}/outreach/preview`),
  runOutreach: (id) => request(`/events/${id}/outreach`, { method: "POST" }),
  getOutreachLog: (id) => request(`/events/${id}/outreach/log`),

  // per-prospect, one-at-a-time. Safer than the batch /outreach for live.
  // Pass {note, message} to override the agent-composed text before sending.
  // Smart-routes server-side: cold prospects get a connection request, warm
  // (already-connected) prospects get a direct DM. The response includes
  // connection_status + path_taken so the caller can re-render the button
  // label after the action.
  sendInvite: (eid, pid, override = {}) =>
    request(`/events/${eid}/prospects/${pid}/invite`, {
      method: "POST",
      body: JSON.stringify(override),
    }),
  sendDirectMessage: (eid, pid, override = {}) =>
    request(`/events/${eid}/prospects/${pid}/dm`, {
      method: "POST",
      body: JSON.stringify(override),
    }),
  // Bulk-refresh connection_status for every "unknown" prospect on this
  // event. Called once when the auto-outreach screen loads so button labels
  // render correctly. Non-blocking, cheap (skips already-classified rows).
  checkConnections: (eid) =>
    request(`/events/${eid}/check-connections`, { method: "POST" }),

  // convenience : full pipeline in one call (BLOCKED in live without confirm)
  runPipeline: (id) => request(`/events/${id}/run`, { method: "POST" }),

  // 04 matching
  // Async under the hood: starts a job + polls to completion, resolving to the
  // same MatchResult the old synchronous /match route returned.
  runMatch: (id) => runJob(id, `/events/${id}/match/async`),
  getMatches: (id) => request(`/events/${id}/matches`),
  // manual RSVP override : for demo / Railway testing without the webhook
  markRsvp: (id, body) =>
    request(`/events/${id}/rsvp`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Sponsor CRUD : sponsors are added inline on the Matching screen, not
  // at intake. POST creates one, PATCH edits, DELETE removes. After any
  // mutation, the frontend re-calls runMatch to refresh SponsorMatch rows.
  listSponsors: (eid) => request(`/events/${eid}/sponsors`),
  createSponsor: (eid, body) =>
    request(`/events/${eid}/sponsors`, {
      method: "POST", body: JSON.stringify(body),
    }),
  updateSponsor: (eid, sid, body) =>
    request(`/events/${eid}/sponsors/${sid}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  deleteSponsor: (eid, sid) =>
    request(`/events/${eid}/sponsors/${sid}`, { method: "DELETE" }),

  // on-demand LLM justification for a single pair.
  // `kind` defaults to "prospect" on both sides; pass "sponsor" for a
  // sponsor↔attendee explanation (uses the SAME endpoint + popover).
  explainPair: (eid, a_id, b_id, { a_kind = "prospect", b_kind = "prospect" } = {}) =>
    request(`/events/${eid}/pairs/explain`, {
      method: "POST",
      body: JSON.stringify({ a_id, b_id, a_kind, b_kind }),
    }),

  // 05 ROI
  getRoi: (id) => request(`/events/${id}/roi`),

  // 06 triage : Applicant Triage flow (Luma CSV -> scored applicants)
  // Paste a public Luma event URL, server scrapes the page (JSON-LD + OG)
  // and returns parsed metadata so the Configure form can auto-fill.
  previewLumaEvent: (url) =>
    request(`/events/triage/luma-preview`, {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  getTriageConfig: (id) => request(`/events/${id}/triage/config`),
  setTriageConfig: (id, body) =>
    request(`/events/${id}/triage/config`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  uploadTriageCsv: async (id, file) => {
    // multipart/form-data : can't use the JSON-default request() helper.
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`/events/${id}/triage/upload`, {
      method: "POST", credentials: "same-origin", body: form,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      const err = new Error(`${res.status} ${res.statusText} — ${text.slice(0, 240)}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  },
  listTriageApplicants: (id) => request(`/events/${id}/triage/applicants`),
  getTriageProgress: (id) => request(`/events/${id}/triage/evaluations`),
  // PR E : operator accept/maybe/reject decision per applicant
  setTriageDecision: (eid, aid, body) =>
    request(`/events/${eid}/triage/applicants/${aid}/decision`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // PR E : download a CSV of all applicants + AI scores + operator decisions
  triageExportUrl: (id) => `/events/${id}/triage/export.csv`,

  // ── in-person scan-to-connect (phone-first surface) ──────────────────
  // Create-or-fetch the user's in_person Event by label. Returns { event_id }.
  inpersonCreateEvent: (label, city = "") =>
    request("/api/inperson/events", {
      method: "POST", body: JSON.stringify({ label, city }),
    }),
  // Resolve-only : never creates a Prospect, never sends.
  //   { method:"url", linkedin_url }            -> single high-confidence hit
  //   { method:"text", name, title?, company? } -> ranked candidate list
  inpersonResolve: (body) =>
    request("/api/inperson/resolve", {
      method: "POST", body: JSON.stringify(body),
    }),
  // Capture a now-known linkedin_url as a pending Prospect + return a draft.
  // Used by QR/paste (straight through) and by a CONFIRMED text candidate.
  inpersonScan: (body) =>
    request("/api/inperson/scan", {
      method: "POST", body: JSON.stringify(body),
    }),
  // CRM list of every capture on this in_person event.
  inpersonCaptures: (eventId) =>
    request(`/api/inperson/events/${eventId}/captures`),
  // Operator-only roll-up of ALL in-person captures across every event
  // (guests included). 403 for non-operator, 404 off the in-person host.
  inpersonActivity: () => request("/api/inperson/activity"),
  // Fire the connect-request / DM for one capture through the shared send
  // helper. Pass { note?, message? } to override the composed draft.
  inpersonSend: (prospectId, override = {}) =>
    request(`/api/inperson/captures/${prospectId}/send`, {
      method: "POST", body: JSON.stringify(override),
    }),

  // ── relationship CRM : the durable "who I've met" spine across events ──
  // Contact-centric read model (one row per durable person, rolled up over
  // every event you've shared with them). Owner-scoped server-side.
  listContacts: () => request("/api/relationships/contacts"),
  getContact: (contactId) => request(`/api/relationships/contacts/${contactId}`),
  // Propose-only relationship agent : loops over the caller's contact spine
  // and returns staged next-step / draft-message suggestions (no sends, no
  // writes). Owner-scoped server-side.
  runRelationshipAgent: () =>
    request("/api/relationships/agent/run", { method: "POST" }),
  // Follow-up chat : send the host's ask to the same propose-only agent and get
  // back { summary, proposals[], auto_send_enabled }. No sends here.
  relationshipChat: (message) =>
    request("/api/relationships/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  // Streaming twin of relationshipChat: opens the SSE endpoint and invokes the
  // callbacks as frames arrive so the UI can reveal each drafted person the
  // moment the agent stages it. Callbacks: onMeta({auto_send_enabled}),
  // onProposal(proposal), onDone({summary, auto_send_enabled}), onError({message}).
  // Resolves when the stream closes. Nothing is sent — proposals are staged only.
  relationshipChatStream: async (message, { onMeta, onProposal, onDone, onError } = {}) => {
    // Stall watchdog. The server emits a keepalive comment every ~10s even
    // while the agent is mid-think, so a healthy stream is never silent for
    // long. If NOTHING arrives for STALL_MS (4+ missed heartbeats) the
    // connection is black-holed (proxy died, wifi dropped without a reset) —
    // abort so the caller gets a clean error instead of an infinite spinner.
    const STALL_MS = 45000;
    const controller = new AbortController();
    let stalled = false;
    let stallTimer = null;
    const armWatchdog = () => {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(() => { stalled = true; controller.abort(); }, STALL_MS);
    };
    const stallError = () =>
      new Error("the connection went quiet and was closed — try asking again");

    armWatchdog();
    try {
      let res;
      try {
        res = await fetch("/api/relationships/chat/stream", {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ message }),
          signal: controller.signal,
        });
      } catch (e) {
        throw stalled ? stallError() : e;
      }
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => "");
        const err = new Error(`${res.status} ${res.statusText} : ${text.slice(0, 240)}`);
        // Mirror request(): surface status + parsed body so the caller can
        // branch on the 402 relationship-quota paywall (LIMIT_REACHED /
        // CONTACT_LIMIT_REACHED) instead of just showing a raw error string.
        err.status = res.status;
        try { err.body = JSON.parse(text); } catch { err.body = null; }
        throw err;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      // SSE frames are separated by a blank line; each frame has an `event:` and
      // a `data:` line. Buffer across chunks since a frame can split mid-read.
      const dispatch = (frame) => {
        let ev = "message", data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) return;
        let payload;
        try { payload = JSON.parse(data); } catch { return; }
        if (ev === "meta") onMeta?.(payload);
        else if (ev === "proposal") onProposal?.(payload);
        else if (ev === "done") onDone?.(payload);
        else if (ev === "error") onError?.(payload);
      };
      for (;;) {
        let chunk;
        try {
          chunk = await reader.read();
        } catch (e) {
          // A mid-stream network failure rejects read(); surface it as a
          // clean error (the keepalive watchdog maps to its own message).
          throw stalled ? stallError() : e;
        }
        armWatchdog(); // any bytes (frames OR keepalives) prove liveness
        const { value, done } = chunk;
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) !== -1) {
          dispatch(buf.slice(0, i));
          buf = buf.slice(i + 2);
        }
      }
      if (buf.trim()) dispatch(buf);
    } finally {
      clearTimeout(stallTimer);
    }
  },
  // Approve one drafted follow-up for a contact. Honors the host's auto-send
  // toggle server-side: returns { status: "sent" | "drafted", ... }.
  sendContactFollowup: (contactId, message) =>
    request(`/api/relationships/contacts/${contactId}/followup`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  // Schedule a chat-drafted follow-up (Gmail-style). `sendAt` is an ISO string
  // for a future fire time, or null to send now. Returns { status: "sent" |
  // "scheduled", send_at?, auto_send_enabled?, ... }. The auto-send toggle still
  // gates whether a SCHEDULED row auto-fires; send-now always sends.
  scheduleContactFollowup: (contactId, message, sendAt = null) =>
    request(`/api/relationships/contacts/${contactId}/schedule`, {
      method: "POST",
      body: JSON.stringify({ message, send_at: sendAt }),
    }),

  // ── scheduled follow-ups : per-user auto-message preference ──
  // Whether a follow-up is auto-staged when a first DM goes out. Off by
  // default; the host opts in. Returns { auto_followups_enabled }.
  getFollowupSettings: () => request("/api/followups/settings"),
  setFollowupSettings: (enabled) =>
    request("/api/followups/settings", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),

  // meta
  health: () => request("/api/health"),

  // auth : Sign in with LinkedIn (via Unipile hosted-auth)
  // me() returns the current user, or throws 401 (caller treats as signed-out)
  me: () => request("/api/auth/me"),
  // returns { url } : frontend sets window.location = url to begin the flow
  startLinkedinAuth: () => request("/api/auth/linkedin/start", { method: "POST" }),
  // Connect the signed-in user's mailbox (Gmail/Outlook) as a second Unipile
  // seat. Returns { url } — redirect the browser there; the hosted page does
  // the OAuth and bounces back with the Integrations tile flipped.
  startEmailAuth: () => request("/api/auth/email/start", { method: "POST" }),
  // First-time-user onboarding tour : persist progress so the coachmark flow
  // survives a refresh / device switch. Pass { step } to advance, { status }
  // to finish ("done") / dismiss ("skipped"), or { status:"active", step:0 }
  // to replay from settings. Returns the new { onboarding_status, onboarding_step }.
  setOnboarding: (patch) =>
    request("/api/auth/onboarding", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  // in-person guest : mint a LinkedIn-less anonymous session so the capture
  // flow works on event.surpluslayer.com without signing in (real sends stay
  // blocked until LinkedIn is connected). 403s on non-in-person hosts.
  inpersonGuest: () => request("/api/auth/inperson/guest", { method: "POST" }),
  // billing : start a Stripe Checkout Session and return { url } to redirect to.
  startCheckout: () => request("/api/billing/checkout-session", { method: "POST" }),
  // Triage-only signup : no LinkedIn / Unipile required. Creates a User
  // row + session for someone who only wants to review applicants.
  // Outbound features grey out / show "Connect LinkedIn" until they
  // optionally connect later.
  triageSignup: ({ name, email }) =>
    request("/api/auth/triage/signup", {
      method: "POST",
      body: JSON.stringify({ name, email }),
    }),
  // Zero-friction triage entry : creates an anonymous User row + session
  // so 'Triage mode' button can route straight into the flow with no form.
  triageQuickStart: () => request("/api/auth/triage/quick-start", { method: "POST" }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
};
