"""
Tests for MULTIPLE connected mailboxes per user (Gmail + Outlook + work).

Covers:
  - the backfill migration lifts a legacy User.* single mailbox into a
    primary EmailAccount row;
  - upsert_email_account creates a second row + keeps exactly one primary,
    and mirrors the User.* fields to the primary;
  - cross-user re-connect moves the mailbox (releasing the prior owner);
  - /me exposes the email_accounts array (primary first);
  - sync_email_contacts iterates ALL active mailboxes (own-address per
    account), with the injected/legacy fallback still working.

Pattern mirrors test_email_connect.py / test_email_sync.py : in-memory
SQLite + direct route-function calls (no FastAPI app import -- the py3.9
`str | None` eval trap).
"""
from __future__ import annotations
import asyncio
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import auth as auth_route
from backend.agents.relationship import email_sync as es


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Host"),
                    email=kw.get("email", "host@x.com"),
                    unipile_account_id=kw.get("acct", "li_acct_1"))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── helper : upsert + primary + mirror ───────────────────────────────────────

def test_upsert_creates_first_account_as_primary_and_mirrors(db):
    u = _user(db)
    a = models.upsert_email_account(
        db, user=u, unipile_account_id="mail_1",
        address="me@gmail.com", provider="google")
    db.commit()
    assert a.is_primary is True
    assert a.status == "active"
    # User.* mirror reflects the primary.
    assert u.unipile_email_account_id == "mail_1"
    assert u.email_account_address == "me@gmail.com"
    assert u.email_status == "active"


def test_upsert_second_account_keeps_single_primary(db):
    u = _user(db)
    models.upsert_email_account(db, user=u, unipile_account_id="mail_1",
                                address="me@gmail.com", provider="google")
    second = models.upsert_email_account(
        db, user=u, unipile_account_id="mail_2",
        address="me@outlook.com", provider="outlook")
    db.commit()
    accts = models.list_email_accounts(db, u)
    assert len(accts) == 2
    primaries = [x for x in accts if x.is_primary]
    assert len(primaries) == 1
    assert primaries[0].unipile_account_id == "mail_1"   # first one stays primary
    assert second.is_primary is False
    # Mirror still points at the primary (unchanged by the second connect).
    assert u.unipile_email_account_id == "mail_1"
    assert u.email_account_address == "me@gmail.com"


def test_upsert_cross_user_reassign_moves_account(db):
    u1 = _user(db, acct="li_1", email="u1@x.com")
    u2 = _user(db, acct="li_2", email="u2@x.com")
    models.upsert_email_account(db, user=u1, unipile_account_id="mail_x",
                                address="shared@gmail.com")
    db.commit()
    assert u1.email_status == "active"
    # u2 re-connects the same mailbox : it moves over, u1 loses it.
    models.upsert_email_account(db, user=u2, unipile_account_id="mail_x",
                                address="shared@gmail.com")
    db.commit()
    assert models.list_email_accounts(db, u1) == []
    assert u1.email_status == "disconnected"
    assert u1.unipile_email_account_id is None
    u2_accts = models.list_email_accounts(db, u2)
    assert len(u2_accts) == 1
    assert u2_accts[0].is_primary is True
    assert u2.unipile_email_account_id == "mail_x"


# ── backfill migration ───────────────────────────────────────────────────────

def test_backfill_creates_primary_from_legacy_user_field():
    # Standalone engine so we can point the migration module's ENGINE at it.
    from backend import db as db_mod
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    # Seed a user with the legacy single-mailbox fields set, no EmailAccount.
    u = models.User(name="Legacy", email="legacy@x.com",
                    unipile_account_id="li_legacy",
                    unipile_email_account_id="legacy_mail",
                    email_account_address="legacy@gmail.com",
                    email_status="active")
    session.add(u); session.commit()
    # Drop the EmailAccount table so the migration recreates + backfills it.
    session.execute(text("DROP TABLE email_accounts"))
    session.commit()

    orig = db_mod.ENGINE
    try:
        db_mod.ENGINE = engine
        db_mod._migrate_email_accounts()
    finally:
        db_mod.ENGINE = orig

    rows = session.execute(text(
        "SELECT user_id, unipile_account_id, address, status, is_primary "
        "FROM email_accounts")).fetchall()
    assert len(rows) == 1
    user_id, acct, addr, status, prim = rows[0]
    assert user_id == u.id
    assert acct == "legacy_mail"
    assert addr == "legacy@gmail.com"
    assert status == "active"
    assert bool(prim) is True
    session.close()


# ── /me array ────────────────────────────────────────────────────────────────

def test_me_returns_email_accounts_array(db):
    u = _user(db)
    models.upsert_email_account(db, user=u, unipile_account_id="mail_1",
                                address="me@gmail.com", provider="google")
    models.upsert_email_account(db, user=u, unipile_account_id="mail_2",
                                address="me@outlook.com", provider="outlook")
    db.commit()

    resp = auth_route.me(db=db, user=u)
    body = json.loads(resp.body)
    accts = body["email_accounts"]
    assert len(accts) == 2
    # primary first
    assert accts[0]["is_primary"] is True
    assert accts[0]["address"] == "me@gmail.com"
    addrs = {a["address"] for a in accts}
    assert addrs == {"me@gmail.com", "me@outlook.com"}
    # Legacy compatibility keys still present + reflect the primary.
    assert body["email_account_address"] == "me@gmail.com"
    assert body["unipile_email_account_id"] == "mail_1"
    assert body["email_status"] == "active"


# ── sync iterates multiple mailboxes ─────────────────────────────────────────

def _mail(frm, to, *, date, role="inbox", pid="m1"):
    return {"provider_id": pid, "thread_id": "t1", "subject": "hey",
            "date": date, "role": role,
            "from_attendee": {"identifier": frm[0], "display_name": frm[1]},
            "to_attendees": [{"identifier": a, "display_name": n}
                             for a, n in to]}


def test_sync_iterates_all_active_mailboxes(db, monkeypatch):
    u = _user(db)
    models.upsert_email_account(db, user=u, unipile_account_id="mail_g",
                                address="me@gmail.com", provider="google")
    models.upsert_email_account(db, user=u, unipile_account_id="mail_o",
                                address="me@outlook.com", provider="outlook")
    db.commit()

    # Each mailbox surfaces a DIFFERENT two-way counterpart (the gate mints a
    # contact only on genuine reciprocity: the user wrote to them AND heard
    # back). The default fetcher is keyed off account_id so we route per mailbox.
    pages = {
        "mail_g": [_mail(("me@gmail.com", "Me"),
                         [("alice@lo91r.com", "Alice")],
                         date="2026-06-08T10:00:00Z", role="sent", pid="g1"),
                   _mail(("alice@lo91r.com", "Alice"),
                         [("me@gmail.com", "Me")],
                         date="2026-06-09T10:00:00Z", pid="g2")],
        "mail_o": [_mail(("me@outlook.com", "Me"),
                         [("bob@lo91r.com", "Bob")],
                         date="2026-06-08T10:00:00Z", role="sent", pid="o1"),
                   _mail(("bob@lo91r.com", "Bob"),
                         [("me@outlook.com", "Me")],
                         date="2026-06-09T10:00:00Z", pid="o2")],
    }

    def fake_fetch(dsn, api_key, account_id, cursor):
        return {"items": pages.get(account_id, []), "cursor": None}

    monkeypatch.setattr(es, "_default_fetch_page", fake_fetch)

    stats = es.sync_email_contacts(db, u, dsn="d", api_key="k")
    assert stats["error"] is None
    assert stats["contacts_created"] == 2          # Alice + Bob
    emails = {c.email for c in db.query(models.Contact)
              .filter_by(user_id=u.id).all()}
    assert emails == {"alice@lo91r.com", "bob@lo91r.com"}
    # last_synced_at stamped per mailbox.
    for a in models.list_email_accounts(db, u):
        assert a.last_synced_at is not None


def test_sync_legacy_fallback_when_no_email_accounts(db):
    # User with only the legacy single field set (no EmailAccount rows).
    u = models.User(name="Legacy", email="legacy@x.com",
                    unipile_account_id="li_legacy",
                    unipile_email_account_id="legacy_mail",
                    email_account_address="me@gmail.com",
                    email_status="active")
    db.add(u); db.commit(); db.refresh(u)
    assert models.list_email_accounts(db, u) == []

    # Two-way thread : the gate mints a contact only on reciprocity (you wrote
    # to them AND they replied), so include both directions.
    mails = [_mail(("me@gmail.com", "Me"), [("carol@lo91r.com", "Carol")],
                   date="2026-06-08T10:00:00Z", role="sent", pid="c1"),
             _mail(("carol@lo91r.com", "Carol"), [("me@gmail.com", "Me")],
                   date="2026-06-09T10:00:00Z", pid="c2")]
    fetch = lambda cursor: {"items": mails, "cursor": None}  # noqa: E731
    stats = es.sync_email_contacts(db, u, dsn="d", api_key="k",
                                   fetch_page=fetch)
    assert stats["error"] is None
    assert stats["contacts_created"] == 1
