"""crypto.py : application-level field encryption (envelope, per-tenant DEK).

The one place that turns a plaintext secret into ciphertext-at-rest and back.
Used today for the OAuth access/refresh tokens on ConnectedAccount (the
highest-value secrets in the DB); the same `encrypt_for` / `decrypt_for` pair
is the seam any other crown-jewel field (notes, message bodies) can adopt.

Design (envelope encryption):
  - A single **key-encryption key (KEK)** comes from the environment
    (`SURPLUS_ENCRYPTION_KEK`) — the v1 stand-in for a KMS/HSM. `_load_kek` is
    the ONLY place that materializes it, so swapping in AWS KMS / GCP KMS later
    is a one-function change and the KEK never has to live in app config again.
  - Each **tenant** gets its own random 32-byte **data-encryption key (DEK)**,
    stored wrapped (encrypted under the KEK) in the `tenant_keys` table. A DB
    leak without the KEK yields only wrapped DEKs + ciphertext. Per-tenant DEKs
    give cryptographic isolation: one tenant's key can't decrypt another's data.
    "Tenant" == `User.id` in v1 (there is no Org/Firm entity yet); the column is
    named `tenant_id` precisely so it can later point at an Org without a
    re-encrypt.
  - Field values are AES-256-GCM sealed under the tenant DEK and stored as
    `enc:v1:<base64(nonce|ciphertext|tag)>`.

Back-compat + rollout are deliberate:
  - **No KEK set** -> `encrypt_for` is a pass-through (stores plaintext) and
    `decrypt_for` returns the value unchanged. The app behaves exactly as it did
    before this module existed, so shipping the code is zero-risk; encryption
    turns on the moment the env var is set.
  - **decrypt is format-sniffing**: a value without the `enc:v1:` prefix is
    treated as legacy plaintext and returned as-is. So a DB with a mix of
    encrypted (new writes) and plaintext (pre-KEK) rows reads correctly during
    the migration window. `backend/db._migrate_encrypt_connected_account_tokens`
    backfills the plaintext rows once the KEK is present.

HONESTY: this is encryption at rest, not end-to-end. The server holds the KEK
and necessarily sees plaintext in memory to use the token / call the model.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from typing import Optional

log = logging.getLogger("surplus.crypto")

_ENC_PREFIX = "enc:v1:"
_NONCE_LEN = 12  # AES-GCM standard nonce

# Per-process cache of unwrapped DEKs: tenant_id -> raw 32-byte key. The KEK is
# constant for a process and DEKs don't rotate mid-process, so this is safe and
# saves a DB round-trip + an unwrap per decrypt.
_dek_cache: dict[int, bytes] = {}
_dek_lock = threading.Lock()


def _load_kek() -> Optional[bytes]:
    """The key-encryption key, or None when encryption is disabled.

    v1 source is the `SURPLUS_ENCRYPTION_KEK` env var. Accepts a base64- or
    hex-encoded 32-byte key (e.g. `openssl rand -base64 32`); any other
    >=32-char value is hashed to 32 bytes so a plain random passphrase also
    works. Shorter/empty -> None (encryption off).

    THE SEAM: to move the KEK into a KMS/HSM (checklist item "keys in KMS, not
    app config"), replace this function body with a KMS fetch. Nothing else in
    the codebase materializes key material.
    """
    raw = (os.environ.get("SURPLUS_ENCRYPTION_KEK") or "").strip()
    if len(raw) < 32:
        return None
    # Exact 32 bytes via base64 or hex -> use directly (proper key material).
    for decode in (_try_b64, _try_hex):
        k = decode(raw)
        if k is not None and len(k) == 32:
            return k
    # Otherwise treat the value as a passphrase: SHA-256 -> 32 bytes.
    return hashlib.sha256(raw.encode()).digest()


def _try_b64(s: str) -> Optional[bytes]:
    try:
        return base64.b64decode(s, validate=True)
    except Exception:  # noqa: BLE001
        try:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:  # noqa: BLE001
            return None


def _try_hex(s: str) -> Optional[bytes]:
    try:
        return bytes.fromhex(s)
    except Exception:  # noqa: BLE001
        return None


def encryption_enabled() -> bool:
    """True iff a KEK is configured (so new writes will be encrypted)."""
    return _load_kek() is not None


def _aesgcm(key: bytes):
    # Imported lazily so importing this module never hard-requires cryptography
    # (e.g. tooling that imports models but never encrypts).
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _get_or_create_dek(tenant_id: int, db) -> bytes:
    """Return the unwrapped DEK for a tenant, minting + persisting one on first
    use. Requires a configured KEK (callers gate on encryption_enabled())."""
    cached = _dek_cache.get(tenant_id)
    if cached is not None:
        return cached

    kek = _load_kek()
    if kek is None:  # pragma: no cover - callers gate on encryption_enabled()
        raise RuntimeError("no KEK configured; cannot derive a DEK")

    from . import models  # lazy: avoid import cycle (models -> db)

    with _dek_lock:
        cached = _dek_cache.get(tenant_id)
        if cached is not None:
            return cached

        row = (db.query(models.TenantKey)
               .filter_by(tenant_id=tenant_id).one_or_none())
        if row is not None:
            dek = _unwrap_dek(kek, tenant_id, row.wrapped_dek)
            _dek_cache[tenant_id] = dek
            return dek

        # Mint a fresh DEK, wrap it under the KEK, persist it.
        dek = os.urandom(32)
        wrapped = _wrap_dek(kek, tenant_id, dek)
        db.add(models.TenantKey(tenant_id=tenant_id, wrapped_dek=wrapped))
        try:
            db.flush()
        except Exception:  # noqa: BLE001 - lost a race to create the row
            db.rollback()
            row = (db.query(models.TenantKey)
                   .filter_by(tenant_id=tenant_id).one())
            dek = _unwrap_dek(kek, tenant_id, row.wrapped_dek)
        _dek_cache[tenant_id] = dek
        return dek


def _wrap_dek(kek: bytes, tenant_id: int, dek: bytes) -> str:
    nonce = os.urandom(_NONCE_LEN)
    ct = _aesgcm(kek).encrypt(nonce, dek, str(tenant_id).encode())
    return base64.b64encode(nonce + ct).decode()


def _unwrap_dek(kek: bytes, tenant_id: int, wrapped: str) -> bytes:
    blob = base64.b64decode(wrapped)
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return _aesgcm(kek).decrypt(nonce, ct, str(tenant_id).encode())


def encrypt_for(tenant_id: int, plaintext: Optional[str], db) -> Optional[str]:
    """Encrypt a value for a tenant, returning `enc:v1:...`. Pass-through when
    encryption is disabled (no KEK) or the value is empty/None, so callers can
    wrap every write unconditionally and behavior is unchanged until a KEK is
    set."""
    if not plaintext or not encryption_enabled():
        return plaintext
    if plaintext.startswith(_ENC_PREFIX):  # already encrypted (idempotent)
        return plaintext
    dek = _get_or_create_dek(tenant_id, db)
    nonce = os.urandom(_NONCE_LEN)
    ct = _aesgcm(dek).encrypt(nonce, plaintext.encode(), None)
    return _ENC_PREFIX + base64.b64encode(nonce + ct).decode()


def decrypt_for(tenant_id: int, stored: Optional[str], db) -> Optional[str]:
    """Inverse of `encrypt_for`. A value without the `enc:v1:` prefix is legacy
    plaintext and returned unchanged (rollout back-compat)."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    dek = _get_or_create_dek(tenant_id, db)
    blob = base64.b64decode(stored[len(_ENC_PREFIX):])
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return _aesgcm(dek).decrypt(nonce, ct, None).decode()
