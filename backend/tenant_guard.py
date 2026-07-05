"""tenant_guard.py : structural cross-tenant isolation for LLM context.

The checklist `[gate]`: "one firm's data can never enter another's prompt."
Context is already scoped by `user_id` at every query, but that's by
convention. This makes it structural: the drafting/agent context builders call
`assert_owned_by(user_id, contact)` before assembling anything, so a bug (or a
crafted id) that loaded a row belonging to a different tenant fails loudly
instead of silently leaking that row into a prompt bound for the model.

"Tenant" == `User.id` today (per-user isolation; no Org entity yet), matching
the per-tenant DEK model in `backend/crypto.py`.
"""
from __future__ import annotations

import logging

log = logging.getLogger("surplus.tenant_guard")


class TenantIsolationError(PermissionError):
    """A record belonging to one tenant was about to be used in another
    tenant's context. Raised, never swallowed — this is a safety invariant."""


def assert_owned_by(user_id, obj, *, kind: str = "record") -> None:
    """Raise TenantIsolationError if `obj` is owned by a different user_id.

    No-op when either side is unknown (obj has no `user_id`, or user_id is
    None): we only fire on a concrete mismatch, so this never breaks rows that
    predate ownership or contexts without a caller identity."""
    owner = getattr(obj, "user_id", None)
    if owner is not None and user_id is not None and owner != user_id:
        log.error("tenant isolation violation: %s owner=%s requested-by=%s",
                  kind, owner, user_id)
        raise TenantIsolationError(
            f"{kind} belongs to tenant {owner}, not {user_id}; refusing to "
            f"assemble it into another tenant's LLM context")
