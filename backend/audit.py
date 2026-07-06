"""audit.py : access-audit trail — who did what, when, from where, allowed or
denied (Phase 4: access, monitoring, resilience).

Design mirrors `DeletionAudit`: **METADATA ONLY**. We record the actor (a role
label, never a token), the action, an optional opaque target, the outcome, and a
best-effort source IP — never request bodies, secrets, or crown-jewel content,
so the audit trail can't itself become a copy of the data it exists to protect.

The single writer is `record()`. The privileged admin surface (`routes/admin.py`)
calls it via the `require_admin` dependencies so every privileged access — both
ALLOWED and DENIED — leaves a row. Denied rows are the monitoring signal: a
burst of them is someone probing the admin surface, visible at a glance through
`GET /admin/audit-log`.

Also here (colocated because it's the same access-control concern): the optional
admin **IP allowlist**, a network-level second factor for the admin surface
(checklist item: "enforced MFA on internal/admin access" — a shared machine
token can't do TOTP, but it can be pinned to known egress IPs and separated by
privilege). It is OFF unless `ADMIN_IP_ALLOWLIST` is set, so behaviour is
unchanged until an operator opts in.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import Optional

log = logging.getLogger("surplus.audit")

_DETAIL_MAX = 300


def client_ip(request) -> str:
    """Best-effort real client IP behind Cloudflare / Railway.

    Prefers `CF-Connecting-IP`, then the first hop of `X-Forwarded-For`, then the
    socket peer. These headers are spoofable by a direct-to-origin caller, so
    they are used ONLY for the audit trail and the optional allowlist
    (defence-in-depth), never as the primary authz gate — the token is that.
    """
    if request is None:
        return ""
    try:
        headers = request.headers
    except Exception:  # noqa: BLE001 -- non-request object
        return ""
    cf = headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", "") or "")[:64]


def record(db, *, actor: str, action: str, target: str = "",
           outcome: str = "allowed", source_ip: str = "",
           detail: str = "") -> None:
    """Write one metadata-only audit row.

    Best-effort: an audit-write failure must never turn into a 500 on the
    operation it records, so everything is swallowed. No-ops when `db` isn't a
    usable Session (e.g. the dependency was called directly, not via FastAPI).
    """
    if db is None or not hasattr(db, "add"):
        return
    from . import models
    try:
        db.add(models.AuditLog(
            actor=(actor or "")[:64],
            action=(action or "")[:160],
            target=(target or "")[:160],
            outcome=(outcome or "")[:16],
            source_ip=(source_ip or "")[:64],
            detail=(detail or "")[:_DETAIL_MAX],
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("audit record failed: %s: %s", type(exc).__name__, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


# ─── Admin IP allowlist (optional network second factor) ────────────────

def _allowlist() -> Optional[list]:
    """Parse `ADMIN_IP_ALLOWLIST` (comma-separated IPs / CIDRs) into networks,
    or None when unset. Unparseable entries are dropped with a warning; a list
    that parses to nothing returns None (treated as "no allowlist configured")."""
    raw = (os.environ.get("ADMIN_IP_ALLOWLIST") or "").strip()
    if not raw:
        return None
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("ignoring bad ADMIN_IP_ALLOWLIST entry %r", part)
    return nets or None


def ip_allowed(source_ip: str) -> bool:
    """True when no allowlist is configured (open, the default) or `source_ip`
    falls within it. A configured allowlist with an unparseable / empty source
    IP fails CLOSED — if the operator pinned the admin surface to known IPs, an
    unidentifiable caller is denied."""
    nets = _allowlist()
    if nets is None:
        return True
    try:
        addr = ipaddress.ip_address((source_ip or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in nets)
