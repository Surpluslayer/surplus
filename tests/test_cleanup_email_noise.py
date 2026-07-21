"""admin /cleanup-email-noise: retroactively remove the one-way email-sync
contacts the old gate created, demoting them to pending markers (never touching
two-way relationships or contacts with any other signal)."""
import json
import os

from fastapi.testclient import TestClient

from backend import db as _db
from backend import models
from backend.main import app


def _setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "testtok")
    _db.reset_db()
    s = _db.SessionLocal()
    u = models.User(email="host@gmail.com", name="Host")
    s.add(u); s.commit(); s.refresh(u)
    return s, u


def _email_contact(s, u, *, email, name, n_in, n_out, last_out="2026-06-01T10:00:00+00:00"):
    from backend.agents.relationship.enrichment_cache import identity_keys
    key = identity_keys(email=email, linkedin_url="")[0]
    c = models.Contact(user_id=u.id, primary_identity_key=key, name=name, email=email)
    s.add(c); s.commit(); s.refresh(c)
    s.add(models.RelationshipInteraction(
        actor_user_id=u.id, contact_id=c.id, source_type="email_sync",
        interaction_type="email_thread", title="Email correspondence",
        meta_json=json.dumps({"n_in": n_in, "n_out": n_out, "last_out": last_out})))
    s.commit()
    return c


def _hdr():
    return {"X-Admin-Token": "testtok"}


def test_dry_run_lists_one_way_keeps_two_way(monkeypatch):
    s, u = _setup(monkeypatch)
    _email_contact(s, u, email="leo@acme.com", name="Leo Park", n_in=0, n_out=1)   # noise
    _email_contact(s, u, email="mia@acme.com", name="Mia Wu", n_in=2, n_out=3)     # real
    c = TestClient(app)
    r = c.post("/admin/cleanup-email-noise",
               json={"user_id": u.id, "dry_run": True}, headers=_hdr()).json()
    assert r["would_remove"] == 1
    assert any("leo@acme.com" in x for x in r["sample"])
    assert r["kept_by"].get("two_way") == 1
    # dry run touches nothing
    assert s.query(models.Contact).count() == 2
    assert s.query(models.EmailPendingOutreach).count() == 0


def test_apply_removes_and_demotes_to_pending(monkeypatch):
    s, u = _setup(monkeypatch)
    _email_contact(s, u, email="leo@acme.com", name="Leo Park", n_in=0, n_out=1)
    c = TestClient(app)
    r = c.post("/admin/cleanup-email-noise",
               json={"user_id": u.id, "dry_run": False}, headers=_hdr()).json()
    assert r["removed"] == 1 and r["demoted_to_pending"] == 1
    assert s.query(models.Contact).count() == 0            # contact gone
    p = s.query(models.EmailPendingOutreach).one()          # outreach preserved
    assert p.address == "leo@acme.com"


def test_other_signals_are_kept(monkeypatch):
    s, u = _setup(monkeypatch)
    # one-way email BUT has a linkedin identity -> keep
    c = _email_contact(s, u, email="vip@acme.com", name="VIP", n_in=0, n_out=1)
    s.add(models.ContactIdentity(user_id=u.id, contact_id=c.id, kind="linkedin",
                                 value="vip-slug", source="linkedin_profile"))
    s.commit()
    cli = TestClient(app)
    r = cli.post("/admin/cleanup-email-noise",
                 json={"user_id": u.id, "dry_run": False}, headers=_hdr()).json()
    assert r["removed"] == 0
    assert r["kept_by"].get("linkedin") == 1
    assert s.query(models.Contact).count() == 1


def test_requires_admin_token(monkeypatch):
    _setup(monkeypatch)
    c = TestClient(app)
    assert c.post("/admin/cleanup-email-noise", json={"dry_run": True}).status_code == 404
