# Security & Data Handling

This is the developer-facing companion to the public Trust page (served at
`/trust`). It states the real data-protection boundary — precisely, including
where protections stop.

## Reporting a vulnerability

Email **support@surpluslayer.com**. Please do not open a public issue for
security reports. We aim to acknowledge within a few business days.

## Encryption in transit

- Public endpoints are served over **TLS 1.3 (fallback 1.2)** with **HSTS**
  (`Strict-Transport-Security`, set in `backend/main.py`).
- The app↔Postgres hop uses `sslmode=require` (`backend/db.py`, override via
  `DB_SSLMODE`). Outbound calls to every subprocessor are HTTPS.

## Encryption at rest

- Databases, disks, and backups are encrypted at rest by the infrastructure
  provider (Railway).
- **Application-level field encryption** (`backend/crypto.py`): sensitive fields
  — today the OAuth access/refresh tokens on `ConnectedAccount` — are
  AES-256-GCM encrypted before they are persisted, using a **per-tenant
  data-encryption key (DEK)** wrapped under a key-encryption key (KEK).
  Per-tenant DEKs give cryptographic isolation between firms.
  - The KEK is loaded via `crypto._load_kek` (env `SURPLUS_ENCRYPTION_KEK`
    today; that function is the single seam for a KMS/HSM). **Field encryption
    is active only when the KEK is provisioned** — until then values are stored
    as-is and reads pass through unchanged.

## Tenant isolation

Isolation is per user/firm. `backend/tenant_guard.assert_owned_by` is enforced
at the LLM context builder (`gather_contact_context`), so one firm's records
cannot be assembled into another firm's request. There is no shared vector
store; context is built per-request from the caller's own rows.

## AI processing — the honest boundary

surplus is **not** end-to-end encrypted for AI-processed content. To draft
messages and answer questions, the server decrypts the relevant content and
sends it (over TLS) to the AI provider; it is transiently in plaintext in
memory during processing. We do not market otherwise.

- **PII minimization** (`backend/redaction.py`): email / phone / SSN /
  Luhn-valid card patterns are stripped from content before it reaches the
  model — only what the task needs is sent.
- The AI provider (Anthropic) is used under a commercial agreement; content is
  not used to train models. *(Confirm the commercial tier + DPA/ZDR at the
  account/contract level — not enforceable from code.)*

## Data retention & deletion

- **Export**: `GET /api/me/export` returns a secret-free dump of a user's data.
- **Delete**: `DELETE /api/me?confirm=true` (self) and `POST /admin/delete-user`
  (support) fully delete a user and their data, revoke connected subprocessor
  access, and write a **metadata-only** `DeletionAudit` row (no content).
- **TTL purge** (`backend/retention.run_purge_sweep`, `POST /admin/run-retention-purge`):
  off by default; when enabled, purges only ephemeral rows (expired sessions,
  old finished jobs). Content is retained while the account is active and
  removed at offboarding — never time-purged on a guessed schedule.

## Subprocessors

Anthropic (AI), Unipile (LinkedIn/WhatsApp/email), Bright Data (profile
enrichment), Exa (search), Resend (email), Stripe (billing), Google & Microsoft
(sign-in + mail/calendar when connected), Zoom (meeting links), Railway
(hosting + database), Cloudflare (edge/TLS), Modal (batch jobs).

## Known follow-ups (not yet done)

- Move the KEK into a managed KMS/HSM.
- Capture Cloudflare (min TLS, Full-strict origin) and Railway (backup
  encryption + retention window) evidence.
- Sign DPAs (+ BAA if PHI is ever in scope); enable ZDR if eligible; set
  regional endpoints for EU/UK data residency.
- Extend field encryption beyond OAuth tokens to note/message bodies, and
  redaction to the prospecting/triage/curation prompt builders.
