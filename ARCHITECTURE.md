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
- **Modal** (`modal_jobs.py`, app `surplus-jobs`) runs off-box batch + scheduled
  jobs when `USE_MODAL=1` (triage scoring, prospecting, CRM refresh, the hourly
  updates sweep). Secrets: `surplus-jobs` (DB/Anthropic/etc) + `surplus-brightdata`.
- **Postgres** (Railway) in prod; SQLite (`backend/data/surplus.db`) for local dev.
  Schema migrations are inline idempotent `_migrate_*()` functions in `db.py`
  (no Alembic).
- Prod DB from a laptop: use the Postgres service's `DATABASE_PUBLIC_URL`
  (`zephyr.proxy.rlwy.net`), not the internal `DATABASE_URL`.

## 3. Request lifecycle

`main.py` (FastAPI app + lifespan) mounts 17 routers, CORS, request-log
middleware, and serves the SPA. Auth is **session-cookie** based: LinkedIn via
Unipile hosted-auth → `User` row → `current_user` dependency. No passwords.
`lifespan` runs `init_db()` (migrations) and starts the in-process updates
scheduler thread.

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
- `auth.py` — LinkedIn/email sign-in (Unipile), session, `/api/me`, onboarding, **auto-import on connect** (background worker seeds the Book from genuine DM conversations AND auto-syncs the host's voice from their own sent messages via `live_enrich.sync_host_voice_on_connect` — same ban-safe own-account read, idempotent).
- `book.py` — the BookApp surface: `/api/book/today` feed, `/draft`(+stream), `/ask`(+stream), relationship detail, `run-updates` sweep, `_updates-status` diagnostics.
- `relationships.py` — contact spine read API, star/VIP, email threads, **import-conversations**, CRM refresh, updates feed.
- `demo.py` — token-gated demo entry + public walkthrough.
- `events.py` `pipeline.py` `matching.py` `roi.py` — the desktop event pipeline (intake → prospect/outreach → match → ROI).
- `triage.py` `curation.py` — inbound applicant triage + event curation surfaces.
- `inperson.py` — phone capture (QR/paste/manual). `jobs.py` — async job dispatch+poll.
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
- `drafting.py` — the one voice-matched follow-up composer (`compose_followup`/`compose_batch`/stream), used by autodraft, book, and the agent. Each draft is built from four layers so it hones in on the exact person instead of generalizing: (1) the host's **packaged voice** via `voice.build_voice_context` — the distilled `<host_voice_profile>` rules + ground-truth `<style_examples>`, channel-scoped; (2) **person facts** (name/role/company) + the real prior thread, led with in the prompt; (3) **relationship grounding** (`_relationship_facts` from `contact_summary`): where/when they met, the host's own noted next step (open loop), and stage — so a draft is specific even with no message thread (the common case); (4) the **contact's register** (`voice.detect_register`) so the draft meets their formality while keeping the host's voice. Prompts enforce brevity (2-3 sentences), specificity, and use-only-stated-facts (no fabricated meetings/updates). The host's free-form **ask-bar instruction** is threaded through as a shared `directive` (`compose_from_context`/`compose_batch`/`stream_from_context`): `/ask`+`/ask/stream` pass the typed query so one intent ("mention the webinar Thursday") lands in every draft, while the per-person `reason` + facts keep each message differentiated rather than a pasted line.
- `relationship_agent.py` — propose-only multi-turn CRM agent (the /ask bar).
- `book.py` — BookApp "today" engine: health scoring + update detection + `build_today` feed (drafts surfaced first).

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

## 7. Conventions

- Commit/push only when asked; prod deploys on `main`.
- Sends are gated by kill-switches + billing; never auto-send without the user.
- LinkedIn reads go through the user's **own** Unipile account or Bright Data's infra — never the host account (ban-safe).
