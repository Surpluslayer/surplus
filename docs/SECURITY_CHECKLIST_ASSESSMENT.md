# Surplus — Security & Data-Handling Checklist: Gap Assessment

> **What this is.** A line-by-line reconciliation of the Encryption checklist
> against the *actual* state of this repository as of 2026-07-05. For each item:
> its real status, the evidence (file:line where verifiable), the gap, and a
> concrete implementation plan. No code was changed to produce this document —
> it is the plan, not the work.
>
> **Scope note.** This is an *event / relationship CRM* (see `ARCHITECTURE.md`),
> not a legal matter-management tool. The checklist's "matter content" maps here
> to the crown-jewel fields that actually exist: draft/outbound **message
> bodies**, **private notes**, **relationship interaction content**, imported
> **chat/DM bodies**, and — most sensitively — stored **OAuth tokens**.

## Status legend

| Mark | Meaning |
|------|---------|
| ✅ **DONE** | Implemented and verifiable in the repo. |
| 🟡 **PARTIAL** | Some of it is in place; a specific gap remains. |
| 🔴 **MISSING** | Required by the checklist; not present in code. |
| 🏗️ **INFRA** | Lives in cloud config (Railway / Cloudflare / Postgres / Modal), **not** this repo. Cannot be confirmed from source — needs a console check + captured evidence. |
| ⚪ **GUARDRAIL** | A policy/marketing constraint, not code. |

---

## Update — implemented (2026-07-05, follow-up PR)

The assessment below is the original baseline. Since then the **code-fixable
Phase 1 items** have been implemented:

- ✅ **Application-level field encryption engine** — `backend/crypto.py`:
  AES-256-GCM envelope encryption with a **per-tenant DEK** wrapped under a KEK.
  Adds the `TenantKey` table (`tenant_id` == `User.id` in v1, named so it can
  later point at an Org). `cryptography` is now a dependency.
- ✅ **OAuth tokens encrypted at rest** (the `[gate]` risk) — wired at the sole
  seam `integrations/oauth.py` (`save_tokens` / `get_valid_access_token`).
  `_migrate_encrypt_connected_account_tokens` backfills existing plaintext rows.
- ✅ **HSTS** (+ `X-Content-Type-Options: nosniff`) in `main.py`, HTTPS-gated.
- ✅ **Postgres TLS** — `sslmode` (default `require`) on the engine, via `DB_SSLMODE`.
- 🟡 **Keys in KMS** — KEK loads from `SURPLUS_ENCRYPTION_KEK` (env) today, with
  `crypto._load_kek` as the single seam to swap in a real KMS/HSM.

**Rollout is zero-risk:** with no `SURPLUS_ENCRYPTION_KEK` set, encryption is a
pass-through (behavior unchanged); decryption sniffs the `enc:v1:` prefix so
legacy plaintext keeps reading during the migration window. Encryption turns on
the moment the KEK env var is provisioned. Per-tenant vs. per-Org keying is
still the one open **product decision** (item 9) — v1 ships per-`User`.

---

## Executive summary

**The honest headline is good news, then two real gaps.**

1. **Honesty guardrail — already clean.** There are **zero** end-to-end /
   "zero-knowledge" / "bank-grade" encryption claims anywhere in the frontend,
   landing page, store listings, or docs (`grep` for `end.to.end|e2e|zero.knowledge|bank.grade`
   returns only unrelated prose like "runs end to end"). Nothing to retract.
   The remaining work is *additive*: publish a Trust page that states the real
   boundary. This is the cheapest item on the list and should ship first.

2. **Biggest at-rest gap — plaintext OAuth tokens.** `ConnectedAccount`
   stores Google/Outlook `access_token` and `refresh_token` **in plaintext**,
   with an in-code TODO acknowledging it (`backend/models.py:1356-1358`). These
   are the highest-value secrets in the DB (live mailbox + calendar access). The
   `cryptography` package isn't even a dependency yet (`requirements.txt`).
   This is the item most worth doing before real firm data lands.

3. **Tenancy reality changes the "per-tenant DEK" item.** There is **no
   Org / Workspace / Firm / Tenant table** — isolation is **per `User`**
   (`user_id` FKs everywhere; the "multi-tenant" comment at `models.py:30`
   refers to per-user ownership, and "workspace" refers to the *Unipile*
   workspace, not a Surplus tenant). "Per-tenant encryption keys" therefore
   means **per-User keys today**, or requires introducing an Org concept first
   if the goal is one key per *firm* with multiple seats. That is a product
   decision, flagged below.

4. **Transit & disk-level at-rest are mostly a console-verification exercise.**
   Cloudflare (edge) and Railway/Postgres (managed) provide TLS and volume
   encryption by default, but **none of it is asserted or enforced from this
   repo**. The gaps that *are* code: no HSTS header, no app-level HTTPS
   redirect, and no explicit Postgres `sslmode` pin.

**Suggested order:** Trust page (⚪, hours) → HSTS (🔴, trivial) → encrypt
OAuth tokens (🔴, the real [sell]/[gate] risk) → per-User field encryption for
message/note bodies (🔴, [sell]) → console evidence capture for the 🏗️ items.

---

## Phase 1 — Encryption · In transit

| # | Item | Status | Evidence / Gap |
|---|------|--------|----------------|
| 1 | **[gate]** Enforce TLS 1.3 (fallback 1.2) on all public endpoints | 🏗️ INFRA | Terminated at **Cloudflare**, which sits in front of Railway (`ARCHITECTURE.md:79`, `main.py:209-215`). TLS version is a Cloudflare SSL/TLS setting, not in this repo. **Gap:** set Cloudflare min TLS to 1.2 and enable TLS 1.3 in the dashboard; capture a screenshot / `nmap --script ssl-enum-ciphers` output as evidence. The origin (uvicorn on `0.0.0.0:$PORT`, `railway.json:8`) serves plain HTTP behind the proxy — fine *only* because Railway's edge and CF-to-origin are the encrypted hops (verify CF origin mode is "Full (strict)"). |
| 2 | **Enable HSTS** | 🔴 MISSING | No `Strict-Transport-Security` header anywhere (`grep` for `strict-transport\|hsts` in `backend/` finds nothing but an auth redirect comment). The response middleware at `main.py:229-238` only stamps `Cache-Control`. **Plan:** add `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload` in that same `no_store_for_api` middleware (or a sibling), guarded to HTTPS requests. ~5 lines. Consider `preload` only after confirming every subdomain (`event.`, `www.`, `join.`, apex) is HTTPS-only. |
| 3 | **TLS on all internal service-to-service traffic** | 🟡 PARTIAL / 🏗️ | Two internal hops exist: (a) **CF → Railway origin** (public, TLS if CF mode is Full-strict — see #1); (b) **app → Postgres**. The Postgres hop uses Railway's **private network** via internal `DATABASE_URL` (`ARCHITECTURE.md:95-96`), which is *not* TLS by default. `create_engine` sets no `sslmode` (`db.py:46-53`). **Gap:** pin `sslmode=require` (or `verify-full` with the CA) in the engine `connect_args` for the Postgres branch, and confirm Modal↔Postgres (`modal_jobs.py`) uses the same. Also the internal Unipile relay (`internal_relay.router`, `relay_client.py`) — confirm it's HTTPS. |
| 4 | **TLS to the LLM provider and every subprocessor** (confirm) | ✅ DONE (verify pins) | All outbound subprocessor calls are HTTPS: Anthropic via SDK default endpoint (`agents/llm.py:101`, `anthropic.Anthropic(...)` → `https://api.anthropic.com`), Exa `https://api.exa.ai/...`, Bright Data `https://api.brightdata.com/...` (grep of `providers/` + `agents/`). Stripe/Resend/Google/Microsoft SDKs are HTTPS by default. **Gap (minor):** none are certificate-pinned and none explicitly disable `verify` (good — no `verify=False` anywhere). Document the full subprocessor list on the Trust page (see Honesty section). |

**Subprocessors that see plaintext in transit** (for the Trust page):
Anthropic (LLM), Unipile (LinkedIn/WhatsApp/email I/O), Bright Data (profile
scraping), Exa (search), Resend (email send), Stripe (billing), Google &
Microsoft (OAuth + mail/calendar), Zoom (meeting links), Modal (batch jobs),
Cloudflare (edge), Railway (host + Postgres). Source: subprocessor reference
counts across `backend/`.

---

## Phase 1 — Encryption · At rest

| # | Item | Status | Evidence / Gap |
|---|------|--------|----------------|
| 5 | **[gate]** KMS-backed encryption on databases, object storage, disks | 🏗️ INFRA | Postgres + any volumes are Railway-managed; Railway encrypts volumes at rest by default, but this is **not** customer-managed KMS and is not evidenced in-repo. There is **no object storage** in use (SPA served from the container filesystem; SQLite only in local dev, `db.py:55-57`). **Gap:** confirm Railway's at-rest encryption posture in writing (support/docs), decide whether Railway's default satisfies "KMS-backed" for your buyers or whether you need a provider that exposes CMEK. Capture evidence. |
| 6 | **[gate]** Backups encrypted too | 🏗️ INFRA | Railway Postgres backups are managed; encryption is Railway's, not configured here. **Gap:** confirm backup encryption + retention with Railway; document. No backup tooling exists in-repo to change. |
| 7 | **[sell]** Application-level (field) encryption for crown-jewel fields — encrypt in-app before persisting | 🔴 MISSING | **Nothing is field-encrypted.** `cryptography` is not a dependency (`requirements.txt` — only `bcrypt` for passwords). Crown-jewel plaintext columns today: `ScheduledFollowup.body`/`draft_message` (`models.py:1332`, `210`), `Contact.private_note`/`note` (`models.py:174-175`), `OutgoingMessage.body` (`models.py:1252`), `RelationshipInteraction` note/body content (`models.py:1188+`), imported DM/chat bodies, `ReviewItem.reviewer_notes` (`models.py:454`). **Plan below.** |
| 8 | **Keys stored in KMS/HSM, not app config or code** | 🔴 MISSING | No keys of any kind exist yet (no field encryption). When added, the envelope-encryption design (below) should fetch the **key-encryption key (KEK)** from a KMS at boot, never store it in `.env`/`config.py`. Today the closest analog — OAuth tokens — are stored plaintext with *no* wrapping (`models.py:1356`). |
| 9 | **[sell]** Per-tenant data-encryption keys for cryptographic isolation | 🔴 MISSING / ⚠️ DESIGN | No key material exists, **and** "tenant" is undefined at the schema level: isolation is per-`User` (no Org/Firm table — confirmed, 0 matches for `class Org/Workspace/Tenant/Firm`). **Decision required:** per-`User` DEK now (simple, matches current model) **vs.** introduce an `Org`/`Firm` entity so multi-seat firms share one key (matches the "legal firm" buyer). Plan covers both. |

### Bonus finding not on the checklist — but it's the real [gate] risk

**Plaintext OAuth tokens.** `ConnectedAccount.access_token` /
`refresh_token` are stored as plaintext `Text` (`models.py:1372-1373`) with an
explicit in-code hardening TODO (`models.py:1356-1358`: *"stored in PLAINTEXT …
BEFORE any production use, encrypt both at rest (Fernet keyed off an env
secret)"*). These grant live Gmail + Calendar (and later Zoom) access — a DB
leak is a mailbox takeover, strictly worse than leaking notes. **This should be
the first field encrypted** and is really a `[gate]` item even though the
checklist filed field-encryption under `[sell]`.

**Already handled correctly at rest:** user passwords are **bcrypt**-hashed
(`models.py:578-582`, `bcrypt>=4.1` in `requirements.txt`) — not reversible
encryption, which is the right call for credentials.

---

## Honesty guardrail

| # | Item | Status | Evidence / Gap |
|---|------|--------|----------------|
| 10 | Do **NOT** market end-to-end encryption for AI-processed content | ✅ DONE | No E2E/zero-knowledge/bank-grade claim exists in `frontend/`, `backend/landing/`, `store/`, or `docs/` (grep clean). The server necessarily sees plaintext to call Anthropic (`agents/llm.py`) — consistent with making no E2E claim. **Keep it that way:** add a one-line note to the Trust page draft so a future marketing edit doesn't reintroduce the claim. |
| 11 | Document the real boundary on a Trust page (encrypted in transit + at rest + per-tenant; plaintext only transient during processing) | 🔴 MISSING | No Trust/Security page exists (no `SECURITY.md`, no trust route; `store/privacy-policy.md` covers only the Chrome extension). **Plan:** publish a Trust page stating: TLS in transit (CF, TLS 1.2+/1.3); encryption at rest (Railway-managed + app-level field encryption once #7 ships); per-User (or per-Org) key isolation once #9 ships; **explicitly** that AI-processed content is decrypted transiently server-side to call the model and is **not** end-to-end encrypted; and the subprocessor list above. Serve it at `/trust` (static, DB-free — mirror the `/landing` pattern in `main.py:459-467`) and/or add a repo `SECURITY.md`. |

---

## Implementation plan for the code items

### A. HSTS (item 2) — ~½ day, trivial
Add to the existing HTTP middleware (`main.py:229-238`):
```
if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
    response.headers["Strict-Transport-Security"] = \
        "max-age=63072000; includeSubDomains"   # add '; preload' after subdomain audit
```
Also consider `X-Content-Type-Options: nosniff` and a `Content-Security-Policy`
while touching this middleware (not on the checklist, but adjacent hardening).

### B. Postgres TLS (item 3) — ~½ day
In `db.py:46-53`, add `connect_args={"sslmode": "require"}` for the Postgres
branch (leave SQLite untouched). Verify Modal (`modal_jobs.py`) connects with
the same. Prefer `verify-full` + Railway CA if attainable.

### C. Envelope encryption engine (items 7, 8, 9, + OAuth tokens) — the big one, ~1–2 weeks
Design (KMS-agnostic, so the KEK never lives in app config):

1. **Add `cryptography`** to `requirements.txt`.
2. **KEK in KMS.** At boot, fetch a key-encryption key from a KMS
   (AWS KMS / GCP KMS / Railway secret-as-KEK for v1). Never persist it.
3. **Per-tenant DEK.** New `tenant_keys` table: `user_id` (or `org_id`, see
   decision) → `wrapped_dek` (DEK encrypted under the KEK). Generate a random
   DEK per tenant on first write; unwrap lazily and cache in-process.
4. **Field type.** A SQLAlchemy `TypeDecorator` (`EncryptedText`) that
   AES-256-GCM-encrypts on write and decrypts on read, keyed by the row's
   tenant DEK. Store `nonce || ciphertext || tag`, versioned with a key-id
   prefix for rotation.
5. **Migration.** Inline `_migrate_encrypt_*()` in `db.py` (matches the
   existing no-Alembic pattern, `ARCHITECTURE.md:92-94`): backfill-encrypt
   existing plaintext rows in batches; make it idempotent (skip already-tagged
   values).
6. **Roll out in risk order:** `ConnectedAccount` tokens **first** → then
   `ScheduledFollowup`/`OutgoingMessage` bodies + `Contact.private_note` →
   then interaction/DM content.
7. **Search caveat:** encrypted columns can't be `LIKE`-searched or indexed by
   content. Audit query paths (contact search, dedup on message id) before
   encrypting a column that's filtered on. `Unipile message id` dedup keys and
   email hashes stay plaintext (they're already hashes / opaque ids).

**Decision gate for the team (item 9):** per-`User` DEK (ships now, no schema
change beyond `tenant_keys`) vs. introduce `Org`/`Firm` (needed if one law firm
= many seats sharing data + one key). Recommend per-`User` for v1 to unblock
`[sell]`, with the DEK keyed by a `tenant_id` column that can later point at an
Org without re-encrypting.

### D. Trust page (items 10, 11) — ~½ day
Static, DB-free page at `/trust` mirroring the landing-page serve pattern
(`main.py:459-467`), plus a root `SECURITY.md`. State the real boundary and the
subprocessor list verbatim from this doc. Add a comment so marketing copy can't
silently reintroduce an E2E claim.

### Infra evidence to capture (items 1, 5, 6) — not code
- Cloudflare: min TLS 1.2, TLS 1.3 on, origin mode **Full (strict)**, HSTS at
  edge too. Screenshot.
- Railway: Postgres + volume at-rest encryption statement; backup encryption +
  retention. Written confirmation.
- Decide whether Railway-default at-rest satisfies "KMS-backed" for buyers or
  whether a CMEK-capable provider is needed for the `[gate]`.

---

## Phase 3 — Data retention (assessment + what shipped)

The DB already hard-deletes via FK CASCADE (the demo-user purge uses it). This
phase adds offboarding, an audit trail, and a purge engine. *(Code items
implemented in the retention PR; the rest need a retention schedule / infra.)*

| Item | Status | Notes |
|------|--------|-------|
| `[sell]` Category-level TTLs / scheduled purge jobs | 🟡 ENGINE SHIPPED, config-gated | `backend/retention.run_purge_sweep` + `POST /admin/run-retention-purge`. **OFF by default**, dry-run reports what it would purge. Only touches **ephemeral** rows (expired/revoked sessions, old finished jobs) — never contact/message content. Set `SURPLUS_RETENTION_*` periods + `SURPLUS_RETENTION_ENABLED=1` to activate. **Decision needed:** the content-retention schedule (if content should ever be time-purged vs. kept-while-active). |
| `[sell]` Soft-delete → hard-delete pipeline | 🔴 NOT DONE | The app has no soft-delete (no `deleted_at` columns); deletes are immediate hard-deletes. A grace-window soft-delete is a separate, larger change. Offboarding delete below is a direct hard-delete. |
| Explicit backup retention window | 🏗️ INFRA | Railway-managed; set + document the window (e.g. 30d) and the deletion-lag-into-backups. Not in-repo. |
| `[sell]` Offboarding: full export + full delete | ✅ IMPLEMENTED | `GET /api/me/export` (secret-free JSON dump) + `DELETE /api/me?confirm=true` (full self-delete). `backend/retention.export_user_data` / `delete_user_data`. |
| Deletion confirmation available to customers | ✅ IMPLEMENTED | Both delete paths return per-category counts as the confirmation; `POST /admin/delete-user` lets support run it on request. |
| Deletion audit log (metadata only) | ✅ IMPLEMENTED | `DeletionAudit` table — who/when/counts, **no content** (`subject_user_id` is deliberately not a FK so the audit outlives the deleted row). |
| Deletion propagates to subprocessors | 🟡 PARTIAL | `delete_user_data` best-effort revokes the user's Unipile account(s). LLM side is covered by ZDR (contractual); OAuth-token revocation beyond deleting the row is a follow-up. |

---

## One-glance scorecard

| Item | | Status |
|------|--|--------|
| TLS 1.3 public endpoints `[gate]` | 1 | 🏗️ verify in Cloudflare |
| HSTS | 2 | ✅ implemented (`main.py`, HTTPS-gated) |
| Internal service TLS | 3 | ✅ Postgres `sslmode=require` (`db.py`) |
| TLS to subprocessors | 4 | ✅ done (HTTPS everywhere) |
| KMS on DB/storage/disk `[gate]` | 5 | 🏗️ verify in Railway |
| Encrypted backups `[gate]` | 6 | 🏗️ verify in Railway |
| App-level field encryption `[sell]` | 7 | ✅ engine shipped (`crypto.py`), applied to OAuth tokens |
| Keys in KMS not config | 8 | 🟡 KEK via env; `_load_kek` is the KMS seam |
| Per-tenant DEK `[sell]` | 9 | ✅ per-`User` DEK (Org path documented) |
| No E2E marketing claim | 10 | ✅ already clean |
| Trust page documents boundary | 11 | 🔴 missing (not in this PR) |
| *(bonus)* Plaintext OAuth tokens | — | ✅ encrypted at rest (KEK-gated) |

**Remaining:** provision `SURPLUS_ENCRYPTION_KEK` (turns encryption on) → move
KEK into a real KMS (item 8) → publish the Trust page (item 11) → capture the
Cloudflare/Railway console evidence (items 1/5/6) → extend field encryption to
notes/message bodies using the same `crypto.encrypt_for` seam.

---

## Phase 2 — LLM request pipeline (assessment)

This phase is **mostly commercial/contractual** (provider tier, DPA, BAA, ZDR),
which lives in vendor agreements and account settings — not this repo. The
code-relevant items are called out below. *(Assessment only; not implemented in
this PR.)*

### Provider selection & contracts

| Item | Status | Notes |
|------|--------|-------|
| `[gate]` API/business tier, never consumer | 🏗️ VENDOR | The app calls Anthropic via the API SDK with `ANTHROPIC_API_KEY` (`agents/llm.py`) — an API account, not a consumer plan. Confirm the account is a commercial/Console org, not a personal Claude.ai subscription. |
| `[gate]` Commercial terms exclude training | 🏗️ VENDOR | Anthropic's commercial API terms already exclude training on API inputs/outputs by default — confirm in writing for the specific account. |
| `[sell]` Zero Data Retention | 🏗️ VENDOR | Request ZDR from Anthropic if eligible; no code change beyond not persisting content (see logging below). |
| `[sell]` Sign a DPA | 🏗️ LEGAL | Execute Anthropic's DPA; repeat for every subprocessor in the list above. |
| BAA if PHI could flow | 🏗️ LEGAL | Only if targeting med-mal/PI firms; otherwise document that PHI is out of scope. |
| Disclose provider on subprocessor list | 🔴 DOC | Ties to Trust-page item 11 — publish the subprocessor list already enumerated above. |
| Data residency (EU/UK) | 🏗️ VENDOR | Regional endpoints (Bedrock/Vertex) if EU/UK firms are in scope; not currently configured. |

### App-side controls (the code items)

| Item | Status | Evidence / Gap |
|------|--------|----------------|
| `[sell]` Redaction / PII-minimization before the call | ✅ IMPLEMENTED | `backend/redaction.py` — `scrub_pii` / `scrub_obj` strip email / phone / SSN / Luhn-valid card identifiers from the LLM-bound context (`as_composer_context` / `as_agent_context`) and the inbound-reply classifier, keeping names/roles/topics/URLs the composer needs. ON by default (`SURPLUS_LLM_REDACTION=0` disables). Conservative patterns; run `messaging_eval` when tuning. *Follow-up: extend to the prospecting/triage/curation prompt builders, which still send functional identifiers.* |
| `[gate]` Tenant isolation in context/RAG assembly | ✅ IMPLEMENTED | `backend/tenant_guard.assert_owned_by` is called at the top of `gather_contact_context`, so a contact owned by another `user_id` raises `TenantIsolationError` before any prompt is assembled — isolation is now structural, not by-convention. No shared vector store exists (context is per-request from the caller's own rows). Tested incl. a cross-tenant refusal. *Follow-up: add the same guard to any future non-`gather` context path.* |
| Logging policy: don't log prompt/response content | 🟡 VERIFY | Request logging is one line per request (status + duration, `reqlog.py`) and does **not** log bodies. Confirm `metrics.py`/`failure_log.py` and any LLM debug paths never persist prompt/response text; if any do, encrypt + short-TTL them. |
| Human-in-the-loop before sensitive actions | ✅ MOSTLY | Sends are gated by kill-switches + billing and default to manual approval (`SURPLUS_AUTOMATED_SENDS` OFF by default; ARCHITECTURE.md §6b/§6c). Model output does not auto-fire sends/bookings unless the operator opts into automation. |

**Phase 2 ship order:** disclose subprocessors + DPA/ZDR (contractual) →
redaction layer (`[sell]`, code) → tenant-isolation audit + test (`[gate]`) →
confirm/lock logging policy.

---

## Phase 4 — Access, monitoring, resilience (assessment + what shipped)

This phase splits, like the others, into **code-fixable** items (shipped here,
with tests) and **infra/process** items (documented, needing a console action or
a calendar commitment). The centerpiece is the access-audit trail; the admin
surface also gains RBAC (least privilege) and an optional network second factor.

| # | Item | Status | Evidence / Gap |
|---|------|--------|----------------|
| 1 | **[gate]** Enforced MFA on all internal & admin access | 🟡 CODE HARDENING + 🏗️ INFRA | True TOTP/WebAuthn MFA on the **human consoles** (Railway, Cloudflare, GitHub, Google Workspace) is an account setting — **enforce it there** and capture evidence. The app's own admin surface is a machine **shared token** (`X-Admin-Token`), which can't do TOTP; the defensible in-repo hardening shipped: an optional **IP allowlist** (`ADMIN_IP_ALLOWLIST`, a network second factor, fail-closed when set — `backend/audit.ip_allowed`), the **least-privilege token split** (item 2), and **denied-access auditing** (item 3) so a probe of the admin surface is visible. **Gap:** per-human admin identities (vs. one shared token) is a larger change — documented, not built. |
| 2 | **[sell]** Role-based access control, least privilege | ✅ ENGINE SHIPPED | Two dimensions. **App data plane:** already least-privilege per-`User` — every event-scoped route goes through `auth.get_owned_event` and LLM context through `tenant_guard.assert_owned_by` (Phase 2), so no user reads another's rows. **Admin plane (new):** a role split in `routes/admin._admin_role` — `ADMIN_TOKEN` → `admin` (full), optional `ADMIN_READONLY_TOKEN` → `readonly` (read-only endpoints only). Mutating endpoints require the full role; a monitoring/dashboard consumer carries a token that **mechanically cannot** delete a user or purge data. **Gap:** still token-roles, not per-identity RBAC (see item 1). |
| 3 | Audit logging of who accessed what & when | ✅ IMPLEMENTED | New `AuditLog` table + `backend/audit.py` (`record` / `client_ip`). Wired into the admin gate (`_check_admin`) so **every** privileged access — allowed **and denied** — writes a metadata-only row: actor (role label, never a token), action (`METHOD /path`), outcome, source IP, reason. Readable at `GET /admin/audit-log` (filter `?outcome=denied` for the probe signal), reachable by the least-privilege read token. Metadata-only by construction (mirrors `DeletionAudit`) — no bodies/secrets. Tested (`tests/test_audit.py`). **Gap:** currently covers the admin/privileged surface; extending to per-user data-access events is a follow-up. |
| 4 | Secure SDLC: code review + dependency/vulnerability scanning | ✅ IMPLEMENTED | **Code review + tests:** `.github/workflows/ci.yml` already gates the full pytest suite on every PR to `main`. **Dependency scanning (new):** `.github/dependabot.yml` (pip + npm + github-actions, weekly + security advisories). **Vulnerability scanning (new):** `.github/workflows/security-scan.yml` — `pip-audit` (backend), `npm audit` (frontend), and **CodeQL** static analysis (python + JS/TS) into the Security tab, on push/PR + weekly. **Gap:** enabling **branch protection / required review** is a GitHub repo setting (infra) — turn it on so CI + review are *required*, not just present. |
| 5 | Schedule an independent penetration test (annual) | ⚪ PROCESS / 🔴 TO SCHEDULE | Not a code item. **Plan:** engage a reputable firm for a **first** external pen test **before onboarding paying firm data**, then annually; scope = the public app + the admin surface + the OAuth/token seams. Feed findings back as tracked issues. Assign an owner + a date. |
| 6 | Tested backups + written DR / BCP plan | 🟡 DOC SHIPPED, drill pending | **Written DR/BCP shipped:** `docs/DISASTER_RECOVERY.md` — RTO/RPO targets, failure-scenario→response table, a Postgres→app **restore runbook**, and a **quarterly backup-test procedure** with a drill log. It flags the crown-jewel recovery risk: **the KEK must be backed up independently of the DB**, else a restored DB yields undecryptable OAuth-token ciphertext. **Gap (infra/process):** confirm Railway's backup cadence + at-rest encryption, back the KEK up independently, and **run the first restore drill** (the doc's pass/fail gate) — targets become commitments only once timed. |

### Phase 4 scorecard

| Item | | Status |
|------|--|--------|
| MFA on internal/admin access `[gate]` | 1 | 🟡 IP-allowlist + token-split + denied-audit shipped; human-console MFA is infra |
| RBAC, least privilege `[sell]` | 2 | ✅ per-`User` data plane + admin read/write token split (`routes/admin`) |
| Audit logging (who/what/when) | 3 | ✅ `AuditLog` + `backend/audit.py`, admin gate + `GET /admin/audit-log` |
| Secure SDLC (review + dep/vuln scan) | 4 | ✅ CI tests + dependabot + pip-audit/npm-audit/CodeQL |
| Independent pen test (annual) | 5 | 🔴 schedule (process; before paying data) |
| Tested backups + DR/BCP | 6 | 🟡 DR/BCP doc + drill procedure shipped; run the first drill (infra) |

**Remaining:** enforce console MFA + turn on branch protection (infra settings) →
back up the KEK independently and **run the first restore drill** → schedule the
annual pen test → (follow-up) extend the audit trail to per-user data-access
events and move to per-identity admin RBAC.

**New env (all optional, behaviour unchanged until set):**
`ADMIN_READONLY_TOKEN` (least-privilege read-only admin token),
`ADMIN_IP_ALLOWLIST` (comma-separated IPs/CIDRs pinning the admin surface).
