# Demo-day deploy prep (Fly.io failover)

Single doc covering the four prep items so you don't tab-hunt at 2am. Read
top-to-bottom on Windows, copy-paste the commands.

Order to actually run things:
- **Tonight**: § 2 (gotchas — read first) → § 1 (set secrets + deploy)
- **Tomorrow**: § 4 (Railway → Neon migration) → § 3 (Cloudflare LB)
- **Demo day**: § 4 (pre-warm + drill)

---

## 1 · `flyctl secrets set` — one command, fill the blanks

Run **after** `flyctl launch --no-deploy` succeeds and **before** `flyctl
deploy`. Paste this entire line into PowerShell, replace each
`PASTE_HERE_*` with the real value, hit Enter once.

```powershell
flyctl secrets set `
  DATABASE_URL="PASTE_HERE_NEON_URL" `
  PROVIDER="unipile" `
  UNIPILE_DSN="PASTE_HERE_UNIPILE_DSN" `
  UNIPILE_API_KEY="PASTE_HERE_UNIPILE_API_KEY" `
  UNIPILE_ACCOUNT_ID="PASTE_HERE_UNIPILE_ACCOUNT_ID" `
  UNIPILE_WEBHOOK_SECRET="PASTE_HERE_UNIPILE_WEBHOOK_SECRET" `
  UNIPILE_DRY_RUN="false" `
  UNIPILE_REQUIRE_SIGNATURE="true" `
  ANTHROPIC_API_KEY="PASTE_HERE_ANTHROPIC_KEY" `
  EXA_API_KEY="PASTE_HERE_EXA_KEY" `
  DEMO_ACCESS_TOKEN="PASTE_HERE_DEMO_TOKEN" `
  ADMIN_TOKEN="PASTE_HERE_ADMIN_TOKEN" `
  SURPLUS_BASE_URL="https://www.surpluslayer.com" `
  GITHUB_TOKEN="PASTE_HERE_GITHUB_TOKEN_OR_OMIT_THIS_LINE"
```

(The backtick `` ` `` is PowerShell's line continuation — leave it in.
If a value contains a literal `"`, double it to `""` inside the quotes.)

### Per-secret notes (skim this once before pasting)

| Secret | Required? | Notes |
|---|---|---|
| `DATABASE_URL` | **YES** | Neon URL with `?sslmode=require` at the end. Your `db.py` handles `postgres://` → `postgresql://` if needed. |
| `PROVIDER` | YES | `unipile` |
| `UNIPILE_DSN` | YES | Per-tenant, looks like `https://apiX.unipile.com:13443` |
| `UNIPILE_API_KEY` | YES | Bearer token from Unipile dashboard |
| `UNIPILE_ACCOUNT_ID` | YES | LinkedIn account that sends outreach |
| `UNIPILE_WEBHOOK_SECRET` | YES | HMAC-SHA256 for webhook signature check |
| `UNIPILE_DRY_RUN` | YES | **`false` for prod**, otherwise no real LinkedIn invites go out (defaults to `true`!) |
| `UNIPILE_REQUIRE_SIGNATURE` | YES | Keep `true` — webhook security |
| `ANTHROPIC_API_KEY` | YES | Used for prospect discovery, scoring rationale, outreach copy, pair explanations, attribution |
| `EXA_API_KEY` | YES | Primary candidate discovery. Without it Anthropic web_search is the fallback (much slower — 60-90s per source) |
| `DEMO_ACCESS_TOKEN` | YES if you use `/api/demo/enter` | The demo URL gate |
| `ADMIN_TOKEN` | YES if you'll hit `/admin/run-followups` or pending-replies | Constant-time-compared against `X-Admin-Token` header |
| `SURPLUS_BASE_URL` | YES | **Keep `https://www.surpluslayer.com`** even on Fly so Unipile webhook/redirect URLs stay correct. Don't set it to `surplus-prod.fly.dev`. |
| `GITHUB_TOKEN` | optional | Only if GitHub source adapter is enabled. Omit the line if you don't have one — line, comma and all. |

### Optional tuning secrets (defaults are fine; only set if you need to override)

```powershell
# Per-deploy tuning : add these only if defaults bite during the demo.
flyctl secrets set `
  PROSPECTING_MAX_PER_SOURCE="5" `
  PROSPECTING_CACHE_TTL="3600" `
  PROSPECTING_ADAPTER_TIMEOUT="120" `
  PROSPECTING_JUDGE_TIMEOUT="15" `
  ENRICH_CONCURRENCY="8" `
  OUTREACH_COMPOSE_DISABLE="0" `
  OPERATOR_VOICE_EXAMPLES=""
```

### Verify before deploy

```powershell
flyctl secrets list
```

Should show every secret name above (values hidden). Count rows. If you're
missing one, set it before deploying — boot failures from missing env vars
look mysterious in `flyctl logs`.

---

## 2 · Dockerfile + app gotchas to know up front

I read `Dockerfile`, `backend/main.py`, and `backend/db.py`. Five things
that could bite the first deploy:

### 2a · Build timeout on multi-stage Dockerfile
Your Dockerfile has a Node build stage (`npm ci` + `npm run build`) before
the Python stage. Fly's default remote-builder timeout is 10 minutes. If
the build hits it, add to `fly.toml`:
```toml
[build]
  dockerfile = "Dockerfile"
  build-timeout = "20m"
```
**Status**: not added by default; only do it if the first `flyctl deploy`
times out during build.

### 2b · Healthcheck path
`fly.toml` is wired to `GET /api/health` which is defined at
`backend/main.py:62`. It returns 200 quickly (no DB call, no LLM call).
Safe. No change needed.

### 2c · `$PORT` handling
Your Dockerfile's `CMD` is:
```
sh -c "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"
```
Fly injects `PORT=8080` by default on machines, but **your Dockerfile
hardcodes `ENV PORT=8000`** at line 34. That env wins unless overridden.
`fly.toml` sets `internal_port = 8000` to match, so this Just Works —
but **don't change `ENV PORT` in the Dockerfile** without also updating
`fly.toml`'s `internal_port`.

### 2d · SQLite path is a trap on Fly (same as Railway)
`backend/db.py:28` falls back to `backend/data/surplus.db` when
`DATABASE_URL` is unset. Fly machines have **ephemeral disks just like
Railway** — without a volume mount, every restart wipes data.

✅ Mitigated as long as `DATABASE_URL` (Neon) is set in secrets. The
`fly.toml` `[mounts]` block is commented out by default. If you ever
accidentally deploy without `DATABASE_URL`, the app boots but data
disappears on the next deploy — silent foot-gun.

### 2e · CORS is `*`
`backend/main.py:38` allows all origins. Fine for the demo, but if you
front Fly with Cloudflare and the LinkedIn OAuth callback flow doesn't
work, this is NOT the suspect — `SURPLUS_BASE_URL` is.

### 2f · `UNIPILE_DRY_RUN` defaults to `true`
At `backend/providers/__init__.py:63`, the env-var read defaults to
`True` when unset. If you forget to set `UNIPILE_DRY_RUN=false` in Fly
secrets, **no real LinkedIn invites go out** and "Reach out" succeeds
silently with a fake message id. Confirm with:
```powershell
flyctl secrets list | findstr UNIPILE_DRY_RUN
```

---

## 2.5 · Staging (Railway) — make a deploy actually ship

The staging demo is `https://surplus-staging.up.railway.app`. Two failure modes
have bitten here; both look like "my merge didn't change anything."

### How staging should deploy
- **Preferred: GitHub auto-deploy from `demo`.** Railway → staging service →
  Settings → Source → connect repo `jjin18/surplus`, branch **`demo`**, enable
  **Automatic Deploys**. A GitHub-triggered build clones the branch fresh (so
  source is never stale) and auto-injects `RAILWAY_GIT_COMMIT_SHA`, so
  `/api/health` `git_sha` shows the real commit.
- **If you instead `railway up` (CLI):** it uploads your **local** directory, so
  deploy from a clean checkout or you'll ship stale code:
  ```bash
  git fetch origin && git checkout demo && git reset --hard origin/demo
  railway up
  ```

### Verify the deploy actually rebuilt — don't trust "Deployed"
Hit `https://surplus-staging.up.railway.app/api/health` and check **two** fields:
- **`build_time`** — baked into the image by the Dockerfile. It moves on every
  real rebuild. **If it didn't change after your deploy, your code did NOT ship**
  (stale source, or a full Docker cache hit — watch for `COPY frontend/ ./ cached`
  + `RUN npm run build cached` in the build log).
- **`git_sha`** — the live commit (real on GitHub deploys; `unknown` on a bare
  `railway up` unless you pass `--build-arg GIT_SHA=$(git rev-parse --short HEAD)`).

A cache-bust knob in the Dockerfile ties the frontend build to `GIT_SHA`, so
passing a new sha guarantees a fresh bundle even on a coarse remote cache.

### The book/advisor redesign surface
The redesign lives on the **`/book`** surface only. To make the plain demo link
land there, set **`DEMO_DEFAULT_SURFACE=book`** in the staging service's
Variables (see `routes/demo.py`). Without it, `/api/demo/enter` defaults to the
desktop pipeline at `/`. The redesign is reachable regardless at
`…/api/demo/enter?key=<DEMO_ACCESS_TOKEN>&surface=book`.

---

## 3 · Cloudflare Load Balancer config

Set this up **after** Fly is deployed and Neon is the shared DB so both
origins read/write the same data.

### Origins
- `surplus-prod.fly.dev` — weight 1, enabled
- `surplus-prod-<your-railway-slug>.up.railway.app` — weight 1, enabled

(After verifying both serve identical traffic, you can shift weights to
favor Fly: e.g. 9 / 1.)

### Health check (matches `/api/health` exactly)
```
Type:               HTTPS
Method:             GET
Path:               /api/health
Port:               443
Interval:           30 seconds
Retries:            2
Timeout:            5 seconds
Expected codes:     200
Expected response:  body contains: "surplus-roi-engine"
Follow redirects:   No
Host header:        www.surpluslayer.com
```

The `body contains` check is the important bit — it catches a Cloudflare
edge that returns a 200 HTML error page when the origin is unreachable.
`/api/health` returns:
```json
{"service":"surplus-roi-engine","version":"0.1.0",...}
```
so `surplus-roi-engine` is a stable, distinctive token.

### Pool
- Name: `surplus-pool`
- Origin Steering: **Random** (or "Off") for active-active. Switch to
  "Failover" with Fly first if you only want Railway as warm-standby.
- Endpoint steering: leave default
- Notification email: set to yours

### Load balancer
- Hostname: `www.surpluslayer.com` (or apex, whichever Unipile webhook
  points at)
- Default pool: `surplus-pool`
- Fallback pool: `surplus-pool`
- Session affinity: **None** for the demo (you don't need sticky sessions;
  Postgres holds session cookies)
- Proxy status: **Proxied** (orange cloud)

### Estimated cost
~$5/mo for the LB itself plus health-check usage (negligible).

---

## 4 · Migration + failover drill commands

### 4a · Railway → Neon (run once, when Railway is back)
From your **Windows laptop**, with `psql` and `pg_dump` installed
(comes with PostgreSQL: <https://www.postgresql.org/download/windows/>):

```powershell
# Get the Railway URL from the Railway dashboard once it's back up.
$RAILWAY_URL = "postgresql://USER:PASS@HOST:PORT/DBNAME"
$NEON_URL    = "postgresql://USER:PASS@HOST/DBNAME?sslmode=require"

# Dump. -Fc = custom format (smaller, parallel restore possible).
# --no-owner / --no-acl strip Railway-specific role grants Neon won't accept.
pg_dump --no-owner --no-acl -Fc $RAILWAY_URL -f railway_backup.dump

# Restore. Neon's initial db is empty; --clean drops anything first
# (safe because there's nothing to drop on a fresh Neon DB).
pg_restore --no-owner --no-acl --clean --if-exists -d $NEON_URL railway_backup.dump

# Sanity check : row counts in both DBs should match.
psql $RAILWAY_URL -c "SELECT 'events' AS t, count(*) FROM events
                       UNION ALL SELECT 'prospects', count(*) FROM prospects
                       UNION ALL SELECT 'users', count(*) FROM users;"
psql $NEON_URL    -c "SELECT 'events' AS t, count(*) FROM events
                       UNION ALL SELECT 'prospects', count(*) FROM prospects
                       UNION ALL SELECT 'users', count(*) FROM users;"
```

Then update **Railway** to point at Neon too:
- Railway dashboard → service → Variables → `DATABASE_URL` = the Neon URL
- Redeploy Railway
- Both origins now hit the same DB — failover is real.

### 4b · Failover drill (run T-24h, before demo)
```powershell
# 1. Confirm Fly is serving real traffic.
curl https://surplus-prod.fly.dev/api/health
# Should print: {"service":"surplus-roi-engine",...}

# 2. Kill Fly. Railway should pick up via Cloudflare LB.
flyctl scale count 0 --app surplus-prod
# Wait 60s for Cloudflare health check to mark Fly DOWN.
curl https://www.surpluslayer.com/api/health
# Should still return 200, served by Railway. Check response headers
# for cf-ray and origin clues if you want proof.

# 3. Bring Fly back.
flyctl scale count 1 --app surplus-prod
# Wait 60s for Cloudflare to re-add Fly to the pool.

# 4. Reverse: pause Railway service in dashboard.
# Cloudflare should now route only to Fly.
curl https://www.surpluslayer.com/api/health
# Still 200.

# 5. Unpause Railway. Pool is back to active/active.
```

If any step returns non-200 from the custom domain, the LB health check
isn't matching what you set up — re-check § 3.

### 4c · Demo-day morning (T-2h, run once)
```powershell
# Pre-warm both origins so first request isn't a cold start.
curl https://surplus-prod.fly.dev/api/health
curl https://surplus-prod-<railway-slug>.up.railway.app/api/health

# Cold backup : in case Neon has a bad hour during the demo, you have
# a local file to restore from.
pg_dump --no-owner --no-acl -Fc $NEON_URL -f neon_cold_backup_$(Get-Date -Format yyyyMMdd).dump

# Tail both logs in two PowerShell windows — keep them visible during demo.
flyctl logs --app surplus-prod
# (second window) : Railway dashboard → service → Logs
```

### 4d · If something breaks mid-demo
Order of escalation, fastest to slowest:
1. **Refresh the demo page**. Cloudflare may have already moved you to the
   healthy origin.
2. **Cloudflare dashboard → Traffic → Load Balancing → Pools** : check
   origin health. Manually disable the bad one if Cloudflare hasn't yet.
3. **DNS direct-flip**: change `www.surpluslayer.com` A/CNAME to point
   straight at `surplus-prod.fly.dev` or the Railway URL. Skips
   Cloudflare entirely. TTL is the bottleneck — set TTL on the LB record
   to 60s **now** (not during the demo) so this is fast.
4. **Worst case**: pull up `surplus-prod.fly.dev` directly and present
   from there. No DNS, no LB, no Cloudflare. Ugly URL but it works.

---

## Pre-demo checklist (last sanity pass)

- [ ] `flyctl secrets list` shows every required key from § 1
- [ ] `flyctl status` shows ≥1 machine in `started` state
- [ ] `curl https://surplus-prod.fly.dev/api/health` returns 200
- [ ] Cloudflare LB pool: both origins green
- [ ] Failover drill in § 4b passed both directions
- [ ] `UNIPILE_DRY_RUN=false` confirmed on both Railway AND Fly
- [ ] Unipile webhook URL points at `https://www.surpluslayer.com/...`
      not at either origin directly
- [ ] DNS TTL on LB record ≤ 60s
- [ ] Cold pg_dump from Neon on your laptop
- [ ] Phone unlocked, `status.railway.com` and `status.flyio.net` bookmarked

Good luck.
