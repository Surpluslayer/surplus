"""RLS request scoping: set_rls_user / reset_rls_user.

The DB-level RLS is Postgres-only and validated separately on staging. Here we
pin the APP-side gating: it is a no-op unless SURPLUS_RLS_ENABLED AND Postgres,
and when active it issues a parameterized set_config for the right uid (so a
value can't be injected), and reset clears it.
"""
from types import SimpleNamespace

from backend import db as db_module


def _capture_db():
    calls = []

    def _exec(stmt, params=None):
        calls.append((str(stmt), params))
    return SimpleNamespace(execute=_exec), calls


def test_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("SURPLUS_RLS_ENABLED", raising=False)
    fake, calls = _capture_db()
    db_module.set_rls_user(fake, 42)
    assert calls == []


def test_noop_on_non_postgres_even_when_enabled(monkeypatch):
    monkeypatch.setenv("SURPLUS_RLS_ENABLED", "1")
    # ENGINE is sqlite in tests -> dialect name is not postgresql -> no-op.
    assert db_module.ENGINE.dialect.name != "postgresql"
    fake, calls = _capture_db()
    db_module.set_rls_user(fake, 42)
    assert calls == []


def test_sets_config_with_uid_when_enabled_on_postgres(monkeypatch):
    monkeypatch.setenv("SURPLUS_RLS_ENABLED", "1")
    monkeypatch.setattr(db_module.ENGINE.dialect, "name", "postgresql")
    fake, calls = _capture_db()
    db_module.set_rls_user(fake, 42)
    assert len(calls) == 1
    sql, params = calls[0]
    assert "set_config('app.user_id'" in sql
    assert params == {"uid": "42"}          # parameterized + coerced to str


def test_reset_clears_scope(monkeypatch):
    monkeypatch.setenv("SURPLUS_RLS_ENABLED", "1")
    monkeypatch.setattr(db_module.ENGINE.dialect, "name", "postgresql")
    fake, calls = _capture_db()
    db_module.reset_rls_user(fake)
    assert len(calls) == 1
    assert "set_config('app.user_id', ''" in calls[0][0]


def test_reset_swallows_broken_transaction(monkeypatch):
    monkeypatch.setenv("SURPLUS_RLS_ENABLED", "1")
    monkeypatch.setattr(db_module.ENGINE.dialect, "name", "postgresql")

    def _boom(*a, **k):
        raise RuntimeError("txn aborted")
    fake = SimpleNamespace(execute=_boom)
    db_module.reset_rls_user(fake)  # must NOT raise (session close must proceed)
