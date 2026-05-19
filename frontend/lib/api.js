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
    throw err;
  }
  // some endpoints return empty body
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

export const api = {
  // 01 intake
  createEvent: (body) =>
    request("/events", { method: "POST", body: JSON.stringify(body) }),
  getEvent: (id) => request(`/events/${id}`),

  // 02 prospecting
  runProspect: (id) => request(`/events/${id}/prospect`, { method: "POST" }),
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
  runMatch: (id) => request(`/events/${id}/match`, { method: "POST" }),
  getMatches: (id) => request(`/events/${id}/matches`),
  // manual RSVP override : for demo / Railway testing without the webhook
  markRsvp: (id, body) =>
    request(`/events/${id}/rsvp`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // on-demand LLM justification for a single pair
  explainPair: (eid, a_id, b_id) =>
    request(`/events/${eid}/pairs/explain`, {
      method: "POST",
      body: JSON.stringify({ a_id, b_id }),
    }),

  // 05 ROI
  getRoi: (id) => request(`/events/${id}/roi`),

  // 06 triage : Applicant Triage flow (Luma CSV -> scored applicants)
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

  // meta
  health: () => request("/api/health"),

  // auth : Sign in with LinkedIn (via Unipile hosted-auth)
  // me() returns the current user, or throws 401 (caller treats as signed-out)
  me: () => request("/api/auth/me"),
  // returns { url } : frontend sets window.location = url to begin the flow
  startLinkedinAuth: () => request("/api/auth/linkedin/start", { method: "POST" }),
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
