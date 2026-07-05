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

## One-glance scorecard

| Item | | Status |
|------|--|--------|
| TLS 1.3 public endpoints `[gate]` | 1 | 🏗️ verify in Cloudflare |
| HSTS | 2 | 🔴 missing (trivial fix) |
| Internal service TLS | 3 | 🟡 Postgres hop needs `sslmode` |
| TLS to subprocessors | 4 | ✅ done (HTTPS everywhere) |
| KMS on DB/storage/disk `[gate]` | 5 | 🏗️ verify in Railway |
| Encrypted backups `[gate]` | 6 | 🏗️ verify in Railway |
| App-level field encryption `[sell]` | 7 | 🔴 missing (no crypto dep) |
| Keys in KMS not config | 8 | 🔴 missing (no keys yet) |
| Per-tenant DEK `[sell]` | 9 | 🔴 missing + design decision |
| No E2E marketing claim | 10 | ✅ already clean |
| Trust page documents boundary | 11 | 🔴 missing |
| *(bonus)* Plaintext OAuth tokens | — | 🔴 real `[gate]` risk — encrypt first |

**Ship order:** 11 & 10 (docs) → 2 (HSTS) → OAuth tokens → 7/8/9 (engine) →
capture 1/5/6 evidence.
