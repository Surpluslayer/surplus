"""Tests for cross-tenant isolation: the guard helper + its wiring into the
relationship context builder ([gate] one firm's data can't enter another's
prompt)."""
from __future__ import annotations

import pytest

from backend import tenant_guard
from backend.tenant_guard import TenantIsolationError


class _Row:
    def __init__(self, user_id=None):
        self.user_id = user_id


def test_same_tenant_ok():
    tenant_guard.assert_owned_by(1, _Row(user_id=1), kind="contact")  # no raise


def test_cross_tenant_raises():
    with pytest.raises(TenantIsolationError):
        tenant_guard.assert_owned_by(1, _Row(user_id=2), kind="contact")


def test_unknown_ownership_is_noop():
    tenant_guard.assert_owned_by(1, _Row(user_id=None))   # legacy row, no owner
    tenant_guard.assert_owned_by(None, _Row(user_id=2))   # no caller identity
    tenant_guard.assert_owned_by(1, object())             # no user_id attr


def test_gather_refuses_cross_tenant_before_any_db_access():
    """The guard is the first line of gather_contact_context, so a mismatched
    contact is rejected before it can touch the DB (db=None proves no read)."""
    from backend.agents.relationship.pipeline.context import gather

    class _Contact:
        user_id = 2
        name = "Someone Else"

    with pytest.raises(TenantIsolationError):
        gather.gather_contact_context(None, 1, _Contact())
