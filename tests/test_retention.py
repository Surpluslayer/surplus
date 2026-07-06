"""Tests for backend.retention : offboarding export/delete + the TTL purge."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend import models, retention
from backend.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_con, _rec):   # mirror prod: enforce ON DELETE CASCADE
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


def _seed_user(db, uid_email="u@x.com"):
    u = models.User(email=uid_email, name="Test User", password_hash="SECRET_HASH")
    db.add(u); db.flush()
    db.add(models.Contact(user_id=u.id, primary_identity_key=f"k{u.id}",
                          name="Maya", email="maya@acme.co"))
    db.add(models.ConnectedAccount(user_id=u.id, provider="google",
                                   account_email="u@x.com",
                                   access_token="SECRET_TOKEN",
                                   refresh_token="SECRET_REFRESH"))
    db.add(models.TenantKey(tenant_id=u.id, wrapped_dek="WRAPPED_SECRET"))
    db.add(models.OutgoingMessage(user_id=u.id, channel="email", body="hi"))
    db.commit()
    return u


def test_export_excludes_secrets_and_includes_owned_data(db):
    u = _seed_user(db)
    out = retention.export_user_data(db, u.id)
    blob = json.dumps(out)
    # secrets never leave via export
    for secret in ("SECRET_HASH", "SECRET_TOKEN", "SECRET_REFRESH", "WRAPPED_SECRET"):
        assert secret not in blob
    # but the user's real data is present
    assert out["user"]["email"] == "u@x.com"
    assert any(c["name"] == "Maya" for c in out["contacts"])
    assert "password_hash" not in out["user"]


def test_delete_removes_all_and_writes_audit(db):
    u = _seed_user(db)
    uid = u.id
    res = retention.delete_user_data(db, uid, actor="self")
    assert res["status"] == "deleted"
    # every owned row is gone
    assert db.get(models.User, uid) is None
    assert db.query(models.Contact).filter_by(user_id=uid).count() == 0
    assert db.query(models.ConnectedAccount).filter_by(user_id=uid).count() == 0
    assert db.query(models.OutgoingMessage).filter_by(user_id=uid).count() == 0
    assert db.query(models.TenantKey).filter_by(tenant_id=uid).count() == 0
    # metadata-only audit row survives, with counts, no content
    audit = db.query(models.DeletionAudit).filter_by(subject_user_id=uid).one()
    assert audit.actor == "self"
    counts = json.loads(audit.counts_json)
    assert counts["contacts"] == 1
    assert "SECRET" not in audit.counts_json


def test_delete_missing_user_is_noop(db):
    assert retention.delete_user_data(db, 99999)["status"] == "not_found"


def test_purge_off_by_default(db, monkeypatch):
    monkeypatch.delenv("SURPLUS_RETENTION_ENABLED", raising=False)
    assert retention.run_purge_sweep(db)["enabled"] is False


def test_purge_dry_run_then_live(db, monkeypatch):
    monkeypatch.setenv("SURPLUS_RETENTION_ENABLED", "1")
    u = _seed_user(db)
    old = datetime.now(timezone.utc) - timedelta(days=400)
    db.add(models.Session(session_token="t1", user_id=u.id, expires_at=old))
    db.add(models.Job(id="j1", user_id=u.id, kind="x", status="done", created_at=old))
    db.commit()

    dry = retention.run_purge_sweep(db, dry_run=True)
    assert dry["enabled"] and dry["dry_run"]
    assert dry["sessions"] == 1 and dry["jobs"] == 1
    # dry run deleted nothing
    assert db.query(models.Session).filter_by(session_token="t1").count() == 1

    live = retention.run_purge_sweep(db, dry_run=False)
    assert live["sessions"] == 1 and live["jobs"] == 1
    assert db.query(models.Session).filter_by(session_token="t1").count() == 0
    assert db.query(models.Job).filter_by(kind="x").count() == 0
