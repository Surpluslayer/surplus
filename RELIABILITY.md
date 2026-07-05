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
| Config validator crash-safety | `import os` moved to module top in main.py (the validator relied on os leaking into globals) | 7b82d09 |
| C1: double-send race | follow-up dispatch now claims each row atomically (`scheduled` -> `sending`, committed) BEFORE the network send; `_due_followups` uses `with_for_update(skip_locked=True)` on Postgres so replicas claim disjoint rows; `send_followup_now` 409s if the row is no longer scheduled. Cron/replica/double-tap can no longer send the same DM twice | 8ccebd5 |
| FK-cascade family | `ondelete="CASCADE"` on the User/Contact child FKs (ContactIdentity, ContactFact, OutgoingMessage, Job, ConnectedAccount, EmailAccount); SQLite FK pragma enabled; Postgres `_migrate_fk_cascade` rewrites existing constraints (idempotent, atomic in one txn, fail-soft). merge_users / demo cleanup / cleanup-email-contacts delete paths no longer 500 on ForeignKeyViolation | 16bcf90 |
| Quick wins | M3 rate-limiter empty-bucket eviction; H5 /api/relationships selectinload + limit; M2 guarded json.loads; M1 Exa 429 backoff; M6 blank-name IndexError; M8 frontend 30s fetch timeout | 4d20d44, 7b82d09 |
| Boot resilience | restartPolicyMaxRetries 3 -> 10; init_db() retries transient DB errors 5x w/ backoff so a Postgres-proxy blip at boot self-heals instead of crash-looping to hard-down | 3f6da39 |
| CPU leading indicator | /api/health?deep=1 cpu block (windowed + since-boot-avg, never null), warns >=60% sustained; deep-path only so no added load on the shallow healthcheck | bdbdea7, a0bcb9b |
| 524: webhook detach | live-provider auto-DM + AI-reply now detached (jobs.run_detached) so the Unipile webhook acks 200 immediately instead of holding open for LLM+send (retry-storm risk); retry-safe via the pre-committed OutreachLog | 9f200d9 |
| 524: enrich-all detach | curation /enrich-all (one Claude call per attendee) detached, returns 202; UI polls | 3cf796d |
| Pool: Exa sweep | updates_engine sweep commits per contact so the pooled connection is freed across the network-bound gap (was held across up-to-200-contact loop -> starvation) | d7a6db2 |
| Unbounded queries | triage CSV export selectinload + 5000-row cap (was 2N+1); curation list_attendees limit clamp 2000 | 7f2a5e9 |
| Provider soft-fail | resolver.resolve_by_url returns {confidence: failed} on a degraded provider rather than relying on each caller to guard | 15d1457 |

### How to use the new observability
- `GET /api/health` (public): shallow. `uptime_seconds` resetting to near-zero across polls = crash loop.
- `GET /api/health?deep=1` with header `X-Admin-Token: <ADMIN_TOKEN>`: pool/memory/db_ping + a `warnings` list. Non-empty warnings = fix before it becomes an outage.
- The uptime GitHub Action emails on real downtime OR any tripped leading indicator. It is a BACKSTOP (GitHub crons drift). PRIMARY should be UptimeRobot/BetterStack (free) with phone push -> add a monitor for https://event.surpluslayer.com/api/health.
- Slow admin endpoints (dedup, cleanup) exceed Cloudflare's 100s cap (error 524): call them via https://surplus-production.up.railway.app directly.

## Still open (backlog, from the verified audit)

Ranked; each has a known fix. Pick up any item independently.

- **M7** frontend App.jsx:663 reads runResult.event.threshold unguarded (Tech Week/matching surface, not the core book). Optional-chain it.
- **check_connections (524 + pool-hold)** pipeline.py:481 makes one Unipile call per prospect synchronously in the request path, holding the session across all of them. Detaching changes its inline-results contract -> needs a UI poll change. Flagged, not yet done.
- **Legacy sync /prospect, /outreach, /run** (pipeline.py) heavy LLM+provider fan-out inline. jobs.py already has /async replacements; these are superseded but still return inline results tests depend on, with deliberate confirm_live_batch guards. Needs a deprecate-vs-document decision.

(Fixed 2026-07-03 and moved to the table above: H4 Exa-sweep connection hold, H6 resolver soft-fail, M4 enrich-all detach, M5 list/export bounds, plus the webhook detach.)

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

## Scaling runbook (workers / replicas / the 100-connection ceiling)

Set 2026-07-05: `WEB_CONCURRENCY=2`, `DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=5` on prod
AND staging (validated on staging first). Two workers per replica now, so one
wedged worker no longer drops all traffic on its container.

**The hard constraint is Postgres `max_connections=100.`** The budget is:

    replicas  x  workers/replica  x  (DB_POOL_SIZE + DB_MAX_OVERFLOW)  <=  ~90

Leave headroom (~10) for the Postgres service itself, monitoring, and rolling
deploys (old + new containers overlap briefly). Current: `2 x 2 x (5+5) = 40`
max, same ceiling as the old `2 x 1 x (10+10)`. Live usage sits ~18/100.

To scale UP:
- **More workers**: raise `WEB_CONCURRENCY`, and DROP `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`
  so the product stays under ~90. E.g. 3 workers -> pool 3 / overflow 3
  (`2 x 3 x 6 = 36`).
- **More replicas** (`multiRegionConfig` numReplicas): same math -- more replicas
  multiply the connection draw, so shrink the per-worker pool in step.
- Past ~90 total you need a bigger Postgres (raise `max_connections`) or a
  connection pooler (PgBouncer) in front. Do NOT just bump pools blindly.

Verify after any change:
- `GET /api/health?deep=1` -> `db_pool.size` reflects the new `DB_POOL_SIZE`.
- `SELECT count(*) FROM pg_stat_activity` on the Postgres public URL stays < 90.
- Boot healthy (uptime resets, `warnings: []`).
