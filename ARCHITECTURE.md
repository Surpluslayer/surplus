# Surplus — System Architecture

> The map of what this repo is and how it fits together. Read this first; every
> file below has a one-line purpose so you can open any path and know its job.

## 1. What it is

A FastAPI monolith + a multi-app React (Vite) frontend, served from **one origin**.
Two product surfaces share the codebase:

- **Desktop pipeline** (`www.surpluslayer.com`) — event ROI engine: intake →
  prospecting → outreach → matching → ROI. (`App.jsx`)
- **Phone-first relationship CRM** (`event.surpluslayer.com`) — "your book":
  capture people you meet, auto-detect their updates, draft follow-ups in your
  voice. (`BookApp.jsx`) `/demo` drops into a seeded version of this. Each `/demo`
  visit mints a throwaway `User` with `is_demo=True` (on the real auth/book stack,
  but flagged so it's kept out of real queries/counts); the hourly scheduler
  purges stale demo users (`routes/demo._cleanup_stale_demo_users`, full cascade).

Host header picks the shell: `event.*` → `inperson.html` → `main-inperson.jsx` →
**BookApp**; apex → `index.html` → `main.jsx` → **App**.

- **Marketing landing** (`join.surpluslayer.com`) - the public "Try now"
  page. Ported in-app from the old standalone `roi-engine` FastAPI
  service (which 502'd whenever its Postgres blipped at startup). It is a
  self-contained static `backend/landing/join.html` plus assets served at
  `/landing-assets/*`, with **zero DB dependency** (pure file serve). Host
  routing: any `join.*` host serves the landing instead of the React SPA;
  `event.*` and `www`/apex are unchanged. A host-independent preview lives at
  `/landing` (alias `/join`) for staging verification. The hero "Try now" CTA
  points at `https://event.surpluslayer.com/?signup` (the shared sign-up
  target, below); the secondary email-capture posts to a DB-free
  `/api/join/demo-request` (validate + log, no persistence). See `main.py`
  `_is_landing_host` / `_landing_response`.

- **Sign-up entry (`?signup`)** - the app leads with sign-up, not LinkedIn.
  Every "Sign up now" CTA (BookApp demo banner / draft / tour, CaptureShared
  send-gate, TriageApp, App.jsx sign-in modal) and the landing "Try now" button
  navigate to `/?signup` on their host. Both shells read this param at the app
  root and render `<AuthOptions defaultMode="signup">` ("Create account" with
  email / Google / Microsoft) over any state - signed-out OR demo - so a demo
  visitor can convert without a LinkedIn OAuth bounce. A real signed-in
  (non-demo) user who hits `?signup` falls through to their app. LinkedIn is no
  longer a sign-in door; it stays a CONNECT data-source option after sign-up.

## 1b. The two sides (read this to know which half a file belongs to)

The codebase is two product lines sharing infra. Every backend file belongs to
exactly one of these buckets. (Files are NOT yet physically split into
subpackages — this map is the source of truth for the split.)

### EVENTS side — the desktop event-ROI pipeline (`www`, `App.jsx`)
Intake → prospect → outreach → match → ROI, plus triage & curation.
- routes: `events`, `pipeline`, `matching`, `roi`, `triage`, `curation`, `jobs`
- agents: `prospector`, `scorer`, `outreach`, `matcher`, `matcher_lib`, `sponsor_matcher`, `roi`, `pair_explainer`, `agents/sources/*`
- packages: `backend/triage/`, `backend/curation/`, `backend/matching/`
- frontend: `App.jsx`, `TriageApp.jsx`, `SharedIntake.jsx`, `components/MatchingRadarGraph.jsx`

### RELATIONSHIP side — the phone-first "book" / CRM (`event.*`, `BookApp.jsx`)
Capture people → detect their updates → draft follow-ups in your voice.
- routes: `book`, `relationships`, `inperson`, `followups`
- agents: `book`, `relationships`, `relationship_agent`, `relationship_watch`, `updates_engine`, `updates_scheduler`, `updates_watch`, `drafting`, `reply_agent`, `capture_enrich`, `resolver`, `email_sync`, `send_flow`, `sender`, `followup_scheduler`
- frontend: `BookApp.jsx`, `CaptureShared.jsx`, `main-inperson.jsx`, `components/ContactsButton.jsx`, `components/ContactsPage.jsx`

### SHARED — used by both
- routes: `auth`, `billing`, `demo`, `webhooks`, `admin`
- agents/infra: `llm`, `agent_loop`, `rategate`, `voice`, `exa`, `usage`, `failure_log`, `live_enrich`
- core: `main`, `db`, `models`, `models_monitoring`, `auth`, `schemas`, `config`, `billing_plans`, `pipeline`, `jobs`, `hosts`, `rate_limit`, `jsonx`, `metrics`, `reqlog`, `env_loader`, `demo_seed`
- providers: `base`, `unipile`, `brightdata`
- frontend lib/components: `lib/*`, `UpgradePaywall`, `surplusTheme`, `intakeFormConstants`

`main.py` mounts its routers in these three groups (with section headers) so the
split is visible at the entrypoint.

## 2. Deploy topology

- **Railway** runs the web service (`railway.json` → `Dockerfile`, multi-stage:
  build frontend with Node, serve via uvicorn). Env: `production` (branch `main`,
  `event.surpluslayer.com`) + `staging` (branch `demo`). 2 replicas. Cloudflare in front.
  - **Deploy healthcheck posture**: Railway probes `/api/health` with a 600s
    `healthcheckTimeout` window. The in-process scheduler threads sleep an
    initial delay before their first claim (default 420s,
    `UPDATES_SCHEDULER_INITIAL_DELAY_SECONDS`) so a fresh container is healthy
    before it spends CPU on sweeps: a heavy first gathering pass during boot
    starved `/api/health` on the single worker and failed deploy 247f9eb2
    (2026-07-01). Steady-state cadence is unaffected (claims are shared, an
    already-running replica keeps ticking).
- **Modal** (`modal_jobs.py`, app `surplus-jobs`) runs off-box batch + scheduled
  jobs when `USE_MODAL=1` (triage scoring, prospecting, CRM refresh, the
  on-connect WhatsApp first sync, the hourly updates sweep). Secrets:
  `surplus-jobs` (DB/Anthropic/etc) + `surplus-brightdata`.
- **Postgres** (Railway) in prod; SQLite (`backend/data/surplus.db`) for local dev.
  Schema migrations are inline idempotent `_migrate_*()` functions in `db.py`
  (no Alembic).
- Prod DB from a laptop: use the Postgres service's `DATABASE_PUBLIC_URL`
  (`zephyr.proxy.rlwy.net`), not the internal `DATABASE_URL`.

## 3. Request lifecycle

`main.py` (FastAPI app + lifespan) mounts the routers, CORS, request-log
middleware, and serves the SPA. Auth is **session-cookie** based: LinkedIn via
Unipile hosted-auth → `User` row → `current_user` dependency. No passwords.
`lifespan` runs `init_db()` (migrations) and starts the in-process scheduler
threads (updates/gathering sweeps + the punctual follow-up dispatcher, §6c).

## 4. Subsystems (backend/)

### Core (`backend/*.py`)
- `main.py` — app, lifespan, middleware, SPA routing, health/diagnostics.
- `db.py` — engine, `SessionLocal` (autoflush=False), `get_db()`, inline migrations.
- `models.py` — ORM schema (~25 tables: Event, Prospect, Contact, RelationshipInteraction, Conversion, MatchEdge, User, Session, Applicant, Job, …).
- `models_monitoring.py` — MonitoredPerson / HostPersonLink (continuous-enrichment dedup).
- `auth.py` — sessions, cookies, `current_user`, send kill-switches.
- `schemas.py` — Pydantic request/response shapes.
- `config.py` — policy tables (funnel/follow-up/format/goal levers).
- `billing_plans.py` — plan tiers + metered-usage limits.
- `pipeline.py` — stage 02–03 orchestrator (prospect + outreach).
- `jobs.py` — job dispatch: local BackgroundTask vs Modal (`use_modal()`).
- `hosts.py` — in-person host detection. `rate_limit.py` — per-IP limiter.
- `jsonx.py` — robust JSON extraction from LLM output. `metrics.py` / `reqlog.py` — request/LLM stats + logging. `env_loader.py` — load .env first.
- `demo_seed.py` — demo workspace bootstrap. `seed.py` — dev-only CLI (`python -m backend.seed`), not imported by the app.

### Routes (`backend/routes/`) — all mounted in `main.py`
- `auth.py` — LinkedIn/email sign-in (Unipile), session, `/api/me`, onboarding, **auto-import on connect** (background worker seeds the Book from genuine DM conversations AND auto-syncs the host's voice from their own sent messages via `live_enrich.sync_host_voice_on_connect` — same ban-safe own-account read, idempotent). The WhatsApp connect webhook dispatches its first conversation sync DURABLY off the request lifecycle via `jobs.dispatch_whatsapp_first_sync` (Modal `run_whatsapp_first_sync` when `USE_MODAL`, else a daemon thread that owns its own DB session): minutes of Unipile I/O can't run in the webhook thread or it gets killed mid-sync. `whatsapp_sync` fetches each chat's attendees+messages concurrently (bounded `ThreadPoolExecutor`, read-only HTTP) then ingests single-threaded; idempotent by message id.
- `book.py` — the BookApp surface: `/api/book/today` feed, `/draft`(+stream), `/ask`(+stream), relationship detail, `run-updates` sweep, `_updates-status` diagnostics, `_draft-preview` (admin: composes drafts across a user's top contacts + the "natural move" reasoning, to inspect messaging quality — read-only, bounded).
- `relationships.py` — contact spine read API, star/VIP, email threads, **import-conversations**, CRM refresh, updates feed.
- `demo.py` — token-gated demo entry + public walkthrough.
- `events.py` `pipeline.py` `matching.py` `roi.py` — the desktop event pipeline (intake → prospect/outreach → match → ROI).
- `triage.py` `curation.py` — inbound applicant triage + event curation surfaces.
- `inperson.py` - phone capture (QR/paste/manual). **Scan fast-path**: `POST
  /api/inperson/scan` does only the DB upsert and returns immediately with
  `draft_status="pending"`; the slow half (Unipile resolve + enrichment + draft
  compose, `finish_scan_capture`) runs detached on its own DB session
  (`jobs.run_detached`) and the UI polls `GET /scan/{id}/draft` until
  `ready`/`failed`. `jobs.py` - async job dispatch+poll.
- `followups.py` — scheduled follow-up queue (Gmail-style). `billing.py` — Stripe. `admin.py` — token-gated ops. `webhooks.py` — Unipile / Bright Data / Stripe ingestion.

### Agents / logic (`backend/agents/`)
LLM + business logic. Infra: `llm.py` (Anthropic client + models), `agent_loop.py`
(multi-turn tool loop), `rategate.py` (concurrency gate), `voice.py` (host voice
extraction/matching), `exa.py` (Exa search), `jsonx` use.

**The relationship / "what's new" system (current focus):**
- `relationships.py` — event-native **read model** (timeline, contact_summary, list_contacts) + `import_conversation_contacts()`. *(distinct from routes/relationships.py)*
- `updates_engine.py` — **the updates orchestrator**: `run_sweep` (Bright Data primary → Exa fallback), `due_contacts` (vip=daily/others=weekly tiering), `apply_profile`/`apply_posts` (diff + baseline-first), `autodraft` (drafts only `_DRAFTWORTHY_KINDS`).
- `updates_scheduler.py` — in-process daemon that claims+runs the sweep hourly (shared `scheduler_claims` row dedups with Modal).
- `updates_watch.py` — Exa fallback search. `relationship_watch.py` — Unipile CRM poller; `_emit()` writes every `activity_update` **and fires autodraft** (single choke point).
- `drafting.py` — the one voice-matched follow-up composer (`compose_followup`/`compose_batch`/stream), used by autodraft, book, and the agent. A draft runs a **4-stage pipeline** (full design in `docs/draft-pipeline.md`) so the per-person honing is principled, not an accreting pile of prompt clauses:
  - **① GATHER** (`build_context` → `_relationship_facts`, all DB reads on the request thread): the host's **packaged voice** (`voice.build_voice_context` — distilled `<host_voice_profile>` + ground-truth `<style_examples>`, channel-scoped), **person facts** (name/role/company), the **real prior thread**, **relationship grounding** (met where/when, the host's own noted next step, stage, relationship types), **their most recent detected update** + the **real content behind it** (`latest_update_detail` = actual post text / role detail, so a draft says "your iHeartRadio feature on The Hospitality Reset", not "saw your post"), the contact's **register** (`voice.detect_register`), and low-confidence **About** (`about`, graceful read — no-op until enrichment populates it).
  - **② RESOLVE** (`_resolve_voice`, `_natural_action` / `Intent`): collapse the competing voice signals into ONE instruction by precedence — **FORMAL register > thread dynamic > host voice profile** (formal is a hard no-emoji constraint that must outrank the casual host voice even mid-thread; the thread mirror is for non-formal threads). The message's GOAL comes from an optional **`Intent`** (hybrid: a taxonomy `kind` from `INTENT_KINDS` + a free-form `objective` + optional `must`/`avoid`) passed by the caller; when none is passed, the goal is derived from `_natural_action` (deliver-on-promise / react-to-update / reply-when-they-spoke-last / re-engage-stale) exactly as before. This is the seam that lets the SAME engine write any message (congratulate / intro / ask / thank / schedule / ...), not just a follow-up — the relationship agent will eventually decide an `Intent` and hand it here instead of drafting inline (see `docs/draft-pipeline.md`).
  - **③ SELECT** (`_select_grounding`): order facts strongest-first and gate by confidence — **verified** facts (their update, your open loop, where you met) may be asserted; **low-confidence color** (what they work on) is offered as optional, so anti-fabrication is structural rather than a prompt plea.
  - **④ RENDER** (`_user_prompt`): assemble the user message from the resolved situation; the system prompt carries the resolved voice. Brevity (2-3 sentences) + use-only-stated-facts are enforced here.

  The host's free-form **ask-bar instruction** threads through as a shared `directive` (`compose_from_context`/`compose_batch`/`stream_from_context`): `/ask`+`/ask/stream` pass the typed query so one intent ("mention the webinar Thursday") lands in every draft, while the per-person `reason` + facts keep each message differentiated rather than a pasted line.
- `messaging_eval.py` — repeatable quality eval for the composer (messaging is the crux). A fixed scenario set (voiced/no-voice, recent update, open loop, live thread, stale, formal, cold) → real drafts → deterministic gates (no em dash / concise / not-generic) + an LLM judge (voice_match, specificity, correct_intent, natural, 1-5). `python -m backend.agents.messaging_eval [--runs N] [--dump out.json]` prints a per-case scorecard; `--pairwise base.json new.json` runs a position-randomized head-to-head judge (lower-variance than the absolute 1-5 means, which are ceiling-limited). Run before/after any prompt or context change to catch regressions — dump both, then pairwise. Baseline ~voice 4.2 / spec 3.7 / intent 4.5 / natural 4.6, gates clean; the 4-stage pipeline holds this at parity (48% pairwise vs the pre-pipeline composer) and turns formal-register adaptation from the known weak spot into a win (4-1).
- `relationship_agent.py` — propose-only multi-turn CRM agent (the /ask bar). When a call is the move, a `draft_message` also carries a meeting `booking_payload` (scheduling link / proposed time woven into the body); the booking fires only when the draft is SENT (see §6b).
- `book.py` — BookApp "today" engine: health scoring + update detection + `build_today` feed (drafts surfaced first).

**The account layer (company-wide relationship graph — design: `docs/accounts-architecture.md`):**
- Thesis: the company account is a LENS over individual graphs, never a bucket. Company rows are GLOBAL + public-data-only + pipeline-owned (user corrections live in per-user `CompanyOverlay`); everything relationship-flavored stays per-user and team views are assembled at query time through gates (wall -> compliance profile -> owner sharing level), so walls are instant/provable and leaving a team removes edges with nothing to claw back.
- Models (end of `models.py`): `Company`/`CompanyIdentity` (global, mirrors ContactIdentity: domain + linkedin_company strong keys, name_norm weak/never-automerge), `CompanyOverlay`, `AccountMembership` (per-user TIME-BOUNDED person<->company edge; job change = close+reopen), `Account` (per-owner view: tier/objective/sharing_level + cached rollups), `Team`/`TeamMembership` (compliance_profile: collaborative default / strict = Level-1 ceiling + pending view interlock; share_signals kill switch), `Wall` (ethical wall: bidirectional invisibility incl. counts, beats every level, query-layer enforcement).
- `company_resolve.py` — person->company resolution: strong keys (non-freemail domain, linkedin company id) auto-link at 1.0; company-name / headline extraction ("X at Acme") deterministic-first, LLM disambiguation below threshold -> `pending_review`. `backfill()` with dry-run report; `scripts/backfill_accounts.py`.
- `accounts_read.py` + `routes/accounts.py` — the owner's account read model (members warmest-first via `score_health`, unioned timeline, coverage/single-threaded, rollup recompute). `routes/teams.py` + `team_view.py` — the team plane: Level-1 metadata-only aggregates ({member, contact, warmth band, recency band} — never content), gates enforced pre-aggregation.

**Outreach/pipeline:** `prospector.py` `scorer.py` `outreach.py` `matcher.py`(+`matcher_lib.py`) `sponsor_matcher.py` `roi.py` `pair_explainer.py`.
**Messaging:** `reply_agent.py` (inbound DM classify, propose-only) `sender.py` `send_flow.py` `followup_scheduler.py` `email_sync.py`.
**Enrichment:** `capture_enrich.py` `live_enrich.py` `resolver.py`.
**Utils:** `failure_log.py` `usage.py`.

### Providers (`backend/providers/`)
- `base.py` — `LinkedInProvider` contract + payload/result types + dash hygiene.
- `unipile.py` — Unipile (sends, profile/posts reads, chats, relations, **list_active_conversation_contacts**).
- `brightdata.py` — Bright Data scraper client (async profile/posts trigger → webhook).

### Other backend dirs
- `triage/` — applicant intake pipeline (CSV → ICP → enrich → score → review).
- `curation/` — event-curation (capture, enrich, draft, score, attribution).
- `matching/` — symbiotic matching (ingest, rubric, GitHub enrich, matrix, explain).
- `data/` — `prospect_pool.json` (mock pool), `surplus.db` (local SQLite).

## 5. Frontend (frontend/)

- Entries: `main.jsx` → `App.jsx` (desktop); `main-inperson.jsx` → `BookApp.jsx` (phone).
- Apps: `App.jsx` (5-stage pipeline), `BookApp.jsx` (relationship CRM), `TriageApp.jsx` (inbound), `SharedIntake.jsx` (unified intake), `CaptureShared.jsx` (capture/in-person).
- Shared: `lib/api.js` (all endpoints), `lib/labels.js` `lib/notify.js` `lib/analytics.js` `lib/resilience.jsx`; components `UpgradePaywall` `ContactsButton` `ContactsPage` `MatchingRadarGraph`; `surplusTheme.js` / `intakeFormConstants.js`.
- Build: Vite multi-page (`vite.config.js`); BookApp kept in its own chunk for health-fingerprint tracking.

## 6. The updates → draft → Book flow (end to end)

1. **Scheduler** (Modal hourly primary, in-process fallback; claim-deduped) calls `run_sweep`.
2. `due_contacts` picks who's due (⭐ vip daily / others weekly, via `watched_at`).
3. Bright Data scrapes each contact's public profile/posts on its own infra → delivers to `/webhooks/brightdata`. Posts use `only_authored_posts=true` (their own posts only, not the activity feed) — keeps the signal clean and slashes credit burn (a non-poster = 0 records).
4. `apply_profile`/`apply_posts` diff vs baseline (first scrape = silent baseline) → `_emit` an `activity_update`.
5. `_emit` auto-drafts a follow-up **for important kinds only** (`job_change`, milestone `new_post`) in the host's voice.
6. `/api/book/today` surfaces draft-bearing updates **first**, with the ready message inline.

## 6b. Meeting booking (a side effect of SENDING a draft)

When a CALL is the natural next step, surplus can book the meeting itself. Booking is **coupled to the draft+send flow**, not a standalone agent action: the agent's draft carries the scheduling offer in its text **and** a structured booking payload, and the actual calendar event fires **when that draft is sent**.

- **Availability** (`integrations/booking.find_open_slot`): reads the host's connected calendar (Google or Outlook, via the same `fetch_calendar_events` the read-sync uses) and returns the earliest open, business-hours, timezone-aware slot over the next ~5 business days. Never double-books. Host tz defaults to `SURPLUS_BOOKING_TZ` (no per-user tz column yet); business hours / lead time are env-tunable (`SURPLUS_BOOKING_START_HOUR` / `_END_HOUR` / `_MIN_LEAD_HOURS`).
- **Draft-time decision** (`booking.propose_meeting_slot`): Calendly connected -> put the self-serve link in the draft (the link IS the booking); else propose a concrete open slot in the draft text. The relationship agent (`pipeline/agent/run.py`) detects a meeting cue on a `draft_message`, precomputes the slot **on the main thread** (the fan-out can't touch the DB session), appends the link/time to the body, and attaches the payload to the staged `Proposal` (surfaced as `booking_payload`).
- **Booking action** (`booking.agent_book_meeting`): picks/uses a slot, invites the **contact** at their email (`Contact.email` else a strong `ContactIdentity` of kind `email`), attaches a **Zoom** link when Zoom is connected (else native Meet/Teams), and records a `meeting_booked` `RelationshipInteraction`. **Idempotent** (a live future booking for that contact is returned, never duplicated) and **email-required** (no email -> raises, so the message still sends with just the text/link, no broken attendee-less event).
- **Send fires it** (`pipeline/send/sender.fire_booking_on_send`): every send path (`/api/relationships/contacts/{id}/schedule` send-now, `/api/followups/{id}/send-now`, and the `run-followups` cron) calls this **after** a clean dispatch. A `propose_time` payload creates the event+invite; a `calendly` payload is a no-op (the link in the body is the booking). A booking miss never fails the message that already went out. The payload rides on `ScheduledFollowup.booking_payload` (new nullable column) for scheduled/cron sends.
- **The gate** (the general-send master `SURPLUS_AUTOMATED_SENDS`, OFF by default; see §6c): **manual** (default) -> the agent drafts the message with the link/time and stages it; nothing books until the HOST approves/sends, at which point send fires the booking. **automatic** (master on) -> the dispatcher auto-sends and auto-books, no approval. No surprise invites for anyone while the flag is off.

## 6c. Send automation (who may send with no human, and how it dispatches)

Three kinds of automated send, two env gates (both OFF by default in code;
per-channel allowlist `SURPLUS_AUTOMATED_SEND_CHANNELS` applies to both):

1. **Post-accept first follow-up** - BUILT-IN product behavior: when an invite
   is accepted, `webhooks._trigger_auto_dm` sends the first DM. Pre-authorized
   by the host's own action (they sent the invite), so it has its own master:
   `SURPLUS_AUTO_FOLLOWUPS` (`sender.follow_up_send_enabled`). A clean send
   auto-stages the later nudge (`followup_scheduler.stage_followup`).
2. **The later nudge** ("checking in" after no reply) - agent autonomy, NOT a
   built-in. Gated by the general-send master `SURPLUS_AUTOMATED_SENDS`
   (`sender.automated_send_enabled`), shared with:
3. **AI auto-reply** to an inbound DM - same general-send master.

Manual UI sends (send-now, approve-a-draft) never pass through either gate.
The per-user `users.auto_followups_enabled` column is LEGACY: it gates neither
staging nor dispatch, its settings routes/UI toggle are gone, and only a few
relationships.py approve/schedule paths still read it (False for new users).

**Dispatch topology**: due `ScheduledFollowup` rows are sent by the in-process
`followup-dispatch` daemon thread (`updates_scheduler`), which ticks every
~60s, claim-guarded via `scheduler_claims`, and calls
`admin.dispatch_due_followups` directly: punctual sends, no external
dependency. The GitHub Actions `run-followups` cron (hits `POST
/api/admin/run-followups`) is redundancy only. Idempotent either way: each row
flips to sent/cancelled/failed the moment it's processed. Gate off -> due rows
HOLD (stay `scheduled` for a manual send-now); a reply cancels; rows overdue
past ~7 days expire as `stale`.

## 6d. Gathering (conversation context the drafter reads)

The per-contact message context is kept fresh by three entry points into the
same idempotent syncs (`linkedin_chat_sync` + `email_sync`, both bounded and
watermarked: LinkedIn by `users.linkedin_chat_synced_at`, dedup by Unipile
message id):

1. **On connect** (`auth._autoimport_conversations`): two durable background
   passes the moment a LinkedIn seat connects: the conversation seed
   (contacts + host voice from the most active chats) and the FULL LinkedIn
   chat sync (message bodies into each contact's timeline). Same
   magic-moment pattern as the WhatsApp first sync: connect -> the book fills
   itself.
2. **Gathering sweep** (`updates_scheduler.run_gathering_sweep`): every 6h
   (`GATHERING_SWEEP_GAP_SECONDS`, claim-guarded, capped at
   `GATHERING_SWEEP_USER_LIMIT` users/pass), runs the INCREMENTAL LinkedIn DM
   sync + email correspondents re-sync for every user with an active seat.
3. **Admin backfill** (`POST /api/admin/sync-linkedin-chats`): on-demand
   dispatch for one user or all active seats; `incremental=false` forces a
   full re-scan (write-idempotent). `POST /api/admin/backfill-contact-links`
   links legacy contact-less prospects so their conversations become visible
   to the relationship layer.

## 7. Conventions

- Commit/push only when asked; prod deploys on `main`.
- Sends are gated by kill-switches + billing; never auto-send without the user.
- LinkedIn reads go through the user's **own** Unipile account or Bright Data's infra — never the host account (ban-safe).
