# Reliability log

A running record of what breaks, what was fixed, and what is still open. Kept
so a future session (or a human) can pick up mid-stream without re-deriving it.

## The root cause of most reliability issues

Almost every failure is the same shape: an assumption that held on the happy
path at small scale, with no bound / timeout / guard / constraint / alarm for
when it did not. The fix is not heroics on each bug, it is finishing the
guardrail layer. On top of that sits a fragile foundation we can only mitigate:

1. **Dependency-heavy.** Unipile, Anthropic, Exa, Google, Stripe, Resend,
   Brightdata, Modal, Railway, Cloudflare. Every one is a failure point we do
   not own. Much of the "unreliability" is the platform/providers, not our code.
2. **Single-process, single-worker** (`WEB_CONCURRENCY=1`), synchronous handlers
   that make blocking LLM/network calls. One slow thing blocks everything.
3. **State that silently drifts.** ~40 env vars + dashboard config, hand-rolled
   migrations on boot, invariants in app code rather than the database.

## Fixed (shipped to main, 2026-07-03)

| Area | What | Commit |
|---|---|---|
| DB pool exhaustion | pool 8 -> 20 per replica (env DB_POOL_SIZE=10 / DB_MAX_OVERFLOW=10) | env |
| Graceful degradation | pool TimeoutError -> retriable 503 (frontend auto-retries); pool gauge in deep health | 8ee7b39 |
| Session hygiene | sync jobs release the DB connection before network I/O + write in short batches; LOCAL_JOBS_MAX_CONCURRENT cap | b956e1e, 5e7fc6b |
| Deploy: slow boot | schema-revision sentinel (skip ~50 migration checks when schema is current: 0.45s -> 0.001s); Unipile backfill made fire-and-forget | 2edc44f |
| Deploy: PORT vanished | PORT=8080 re-set AND startCommand fallback pinned to ${PORT:-8080} so a missing var cannot strand a container | 5c6cfbb |
| Deploy: healthcheck | gate restored at healthcheckTimeout 600 after PORT fix; scheduler first tick waits out the healthcheck window | 5c6cfbb, 2d4bd90 |
| Detection | external uptime monitor (.github/workflows/uptime.yml, every 5 min, both domains) + uptime_seconds crash-loop signal on /api/health | 88ef1ba, 9228134 |
| Prediction | leading-indicator warnings on /api/health?deep=1 (pool used_pct >=80, memory used_pct >=85, db_ping_ms >=1500, missing config); monitor fails on any tripped indicator; deep gated behind admin token | 5693f1f, 583144c |
| Config drift | boot-time validator: fatal on <32-byte SURPLUS_OAUTH_STATE_SECRET in prod, loud CONFIG WARNING on missing integration keys | 7e7f9dc |

### How to use the new observability
- `GET /api/health` (public): shallow. `uptime_seconds` resetting to near-zero across polls = crash loop.
- `GET /api/health?deep=1` with header `X-Admin-Token: <ADMIN_TOKEN>`: pool/memory/db_ping + a `warnings` list. Non-empty warnings = fix before it becomes an outage.
- The uptime GitHub Action emails on real downtime OR any tripped leading indicator. It is a BACKSTOP (GitHub crons drift). PRIMARY should be UptimeRobot/BetterStack (free) with phone push -> add a monitor for https://event.surpluslayer.com/api/health.
- Slow admin endpoints (dedup, cleanup) exceed Cloudflare's 100s cap (error 524): call them via https://surplus-production.up.railway.app directly.

## In flight (2026-07-03, agents committing locally for review)

- **C1 (Critical): double-send race.** dispatch_due_followups + send_followup_now read status=="scheduled", send, then flip to "sent". 2 replicas / cron overlap / double-tap can send the same LinkedIn DM twice. Fix: claim rows with_for_update(skip_locked=True) + flip to "sending" BEFORE the send.
- **FK-cascade family (High).** Three delete paths throw ForeignKeyViolation because child tables lack ON DELETE CASCADE: merge_users (admin.py:722), demo cleanup (demo.py:283), cleanup-email-contacts missing ContactFact (admin.py:931). Fix: ondelete="CASCADE" on User/Contact child FKs in models.py + a migration.
- **Quick wins.** M3 rate-limiter empty-bucket eviction (memory leak); H5 /api/relationships N+1 + unbounded (selectinload + limit, 524 risk); M2 raw json.loads on job-done branch (jobs.py:46, relationships.py:437); M1 Exa 429 no backoff; M6 name.split()[0] IndexError on blank name; M8 fetch timeout in lib/api.js.

## Still open (backlog, from the verified audit)

Ranked; each has a known fix. Pick up any item independently.

- **H4** updates sweep (updates_engine.py:367) holds one DB connection across a 200-contact Exa loop. Under LOCAL_JOBS_MAX_CONCURRENT=4 that is up to 4 pinned connections during slow-Exa. Fix: fetch Exa with the session closed, reopen to write; or commit per contact.
- **H6** resolver.py:116 `provider.resolve_linkedin_user` is unguarded in a request path -> 500 when Unipile degrades. Wrap, return {confidence: "failed"}.
- **M4** curation.py:361 /enrich-all: synchronous Claude call per attendee in a request handler -> 524 on any real event. Detach like the other sweeps.
- **M5** list_attendees (curation.py:322) + triage CSV export (triage.py:605) N+1, no limit. selectinload + limit.
- **M7** frontend App.jsx:663 reads runResult.event.threshold unguarded (Tech Week/matching surface, not the core book). Optional-chain it.
- **LOW** auto-DM on invite_accepted: a crash after send but before commit could let a webhook retry re-fire. Mostly covered by OutreachLog dedup; worth an explicit idempotency marker.

## Inherent (mitigate, do not eliminate at this stage)

- **Data isolation is tier-1** (per-query user_id scoping, audit-confirmed no IDOR holes) but NOT enforced by the DB. Postgres RLS on the ~6 sensitive tables is the ~1-day structural upgrade before onboarding paying customers.
- **Provider dependency** (esp. Unipile). Provider-abstraction seam is designed but not built (a MessagingProvider interface with CSV/Gmail fallbacks).
- **Single worker.** WEB_CONCURRENCY=1; raising it needs RAM headroom (heavy imports). Async handlers + a job queue would isolate slow work.

## The recurring platform gotchas (Railway/Cloudflare)

- On "service unavailable" healthcheck failures, check BOUND PORT vs DOMAIN TARGET PORT first (uvicorn log line + `railway domain`).
- Railway's Postgres TCP proxy (crossover.proxy.rlwy.net) intermittently drops rapid successive connections. Single connections fine.
- Railway's Diagnosis bot auto-merges PRs to main (the "Run automatically" toggle). It treats symptoms (raised healthcheckTimeout). Consider turning it to propose-only.
- Cloudflare caps responses at ~100s (error 524). Long endpoints must be backgrounded or called via the .up.railway.app domain.
- `railway service scale REGION=N` needs exact slugs from `railway status --json` (eu=europe-west4-drams3a, us=us-west2).
