# Surplus — Disaster Recovery & Business-Continuity Plan

> **What this is.** The written DR/BCP the security checklist (Phase 4:
> access, monitoring, resilience) asks for — plus the **backup-restore test
> procedure** that turns "we have backups" into "we have *tested* backups".
> Scoped to what surplus actually runs on: **Railway** (app host + managed
> Postgres), **Cloudflare** (edge/DNS), **Modal** (detached batch jobs), and the
> subprocessors enumerated in `docs/SECURITY_CHECKLIST_ASSESSMENT.md`.
>
> Companion docs: `RELIABILITY.md` (the incident log + scaling runbook) and
> `ARCHITECTURE.md` (the topology). This doc is the *recovery* plane; those are
> the *steady-state* plane.

## Objectives (targets, not guarantees)

| Metric | Target | Rationale |
|---|---|---|
| **RPO** (max data loss) | **≤ 24h**, goal ≤ 1h | Railway Postgres does daily automated backups; point-in-time / more frequent snapshots tighten this. Confirm the actual cadence in the Railway console and record it below. |
| **RTO** (time to restore service) | **≤ 4h** for a full DB restore; **≤ 30m** for an app-only redeploy | The app is stateless (all state is in Postgres); a redeploy is minutes. A DB restore dominates RTO. |
| **Data durability** | Managed by Railway (replicated volumes + backups) | Not customer-managed; see the "Infra to verify" gaps. |

These are **operating targets**. They become commitments only once the restore
drill below has been run and timed at least once (see "Backup test procedure").

## What must survive a disaster

Ranked by recovery priority. Only the first is irreplaceable.

1. **Postgres** — the single source of truth: users, contacts, messages,
   OAuth-token ciphertext, `tenant_keys` (wrapped DEKs), audit logs. Losing this
   is losing the product. Everything else is rebuildable.
2. **Secrets/env** — ~40 env vars incl. `ADMIN_TOKEN`, `SURPLUS_ENCRYPTION_KEK`,
   `SURPLUS_OAUTH_STATE_SECRET`, provider API keys. **Losing the KEK makes every
   encrypted field (OAuth tokens) unrecoverable even with an intact DB** — the
   DEKs are wrapped under it. The KEK must be backed up **independently of the
   database**, in a separate secret store, or a DB restore yields undecryptable
   ciphertext.
3. **Code** — this Git repo (GitHub). Redeployable from any commit.
4. **Cloudflare DNS/edge config** — export the zone; small and static.

> **The KEK is the crown-jewel of recovery.** DB backup + KEK backup are *both*
> required and must live in *different* trust domains. Back the KEK up the day it
> is provisioned; store a copy in an offline/second KMS. See item 8 in the
> security assessment.

## Failure scenarios → response

| Scenario | Blast radius | Response |
|---|---|---|
| **App crash-loop / bad deploy** | Service down, data intact | Railway rollback to the last healthy deploy (`restartPolicyMaxRetries=10` self-heals transient boots). Confirm via `GET /api/health` `uptime_seconds`. RRTO ~minutes. |
| **Postgres data loss / corruption** | Total | Restore latest backup into a new Postgres instance → repoint `DATABASE_URL` → redeploy. Then re-provision the **KEK** so encrypted fields decrypt. Run the restore drill (below). |
| **KEK lost** | Encrypted fields (OAuth tokens) unrecoverable | Non-recoverable by design if no independent KEK backup exists. Users re-connect OAuth (re-auth mints fresh tokens). This is *why* the KEK gets an independent backup. |
| **Region outage (Railway)** | Service down | Railway multi-region config (`multiRegionConfig`) — fail traffic to the healthy region; the DB is the constraint (single primary). Document the region slugs (`RELIABILITY.md`). |
| **Cloudflare / DNS outage** | Edge down, origin up | The `*.up.railway.app` origin domain stays reachable — the uptime monitor already probes both. Communicate the direct URL; restore DNS from the zone export. |
| **Subprocessor outage** (Unipile/Anthropic/etc.) | Feature-degraded, not down | Kill-switches (`SURPLUS_KILL_OUTREACH`) + provider soft-fail keep the core app up; wait out the provider. Tracked in `RELIABILITY.md`. |
| **Secret/credential compromise** | Security incident | Rotate the affected secret (`ADMIN_TOKEN`, provider key, KEK), revoke sessions, review `GET /admin/audit-log` for the `denied`/anomalous access trail. |

## Restore runbook (Postgres → running app)

1. **Provision** a fresh Postgres (Railway) from the most recent backup snapshot.
2. **Point** the app at it: set `DATABASE_URL` (keep `DB_SSLMODE=require`).
3. **Provision the KEK**: set `SURPLUS_ENCRYPTION_KEK` to the *same* KEK the data
   was encrypted under (from the independent KEK backup). A different KEK cannot
   unwrap the DEKs.
4. **Redeploy** the app. `init_db()` runs the idempotent boot migrations and
   stamps the schema revision; it retries transient DB blips with backoff.
5. **Verify** (the drill's pass/fail gate):
   - `GET /api/health` → healthy, `uptime_seconds` climbing.
   - `GET /api/health?deep=1` (with `X-Admin-Token`) → `warnings: []`,
     `db_pool` reflects config, `db_ping_ms` sane.
   - A signed-in user can read their contacts; a connected mailbox can send
     (proves OAuth-token ciphertext decrypts → KEK is correct).
   - `GET /admin/audit-log` returns rows (audit table intact).
6. **Repoint DNS** if the origin changed; confirm both domains answer 2xx.

## Backup test procedure (makes backups "tested")

An untested backup is a hope, not a control. **Quarterly**, run a non-production
restore drill:

1. Restore the latest production Postgres backup into a **throwaway** Railway
   Postgres (never the live one).
2. Boot a **staging** app pointed at the restored DB + a **staging KEK copy**.
3. Run the step-5 verification checks above against staging.
4. **Record**: backup timestamp, restore wall-clock time (→ validates RTO),
   newest row's timestamp vs. now (→ validates RPO), and any failure.
5. Tear down the throwaway DB + staging app.

Log each drill in the table below. A drill that can't decrypt OAuth tokens is a
**failed** drill — it means the KEK backup is wrong or missing, which is exactly
the failure this procedure exists to catch *before* a real incident.

### Drill log

| Date | Backup age at restore | RTO (actual) | RPO (actual) | OAuth decrypt OK? | Notes |
|------|-----------------------|--------------|--------------|-------------------|-------|
| _pending first drill_ | | | | | Run once `SURPLUS_ENCRYPTION_KEK` is provisioned in prod. |

## Roles & communication

- **Incident owner**: the on-call operator (single-operator project today; name
  the human in the ops handbook as the team grows).
- **Detection**: `.github/workflows/uptime.yml` (external, every 5 min, both
  domains) + leading-indicator warnings on deep health. PRIMARY should be an
  external monitor (UptimeRobot/BetterStack) with phone push — see
  `RELIABILITY.md`.
- **Status communication**: notify affected users if data-affecting or if an
  RPO-window of data is lost; for a security incident follow breach-notification
  obligations (tie to the DPA/subprocessor list in the security assessment).

## Infra to verify (not assertable from this repo)

These are **console-verification** items — Railway/Cloudflare settings this repo
can't confirm. Capture written evidence (screenshot/support confirmation):

- [ ] **Railway Postgres backup cadence + retention window** (drives the real
      RPO). Record the number here once confirmed.
- [ ] **Backups are encrypted at rest** (checklist item 6) — Railway-managed;
      get it in writing.
- [ ] **KEK is backed up independently** of the DB, in a separate secret
      store/KMS, and that backup is itself tested by the drill above.
- [ ] **Point-in-time recovery** available? If so, RPO tightens toward the ≤ 1h
      goal — document the achievable window.
- [ ] **Cloudflare zone export** stored somewhere restorable.
- [ ] The quarterly restore drill is **scheduled** (calendar/owner assigned),
      not just documented.
