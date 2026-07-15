"""The per-user book cache (routes/book._load_book) must reuse a build for
repeated reads but rebuild the instant the fingerprint moves (a capture adds a
contact, an update lands an interaction) -- so a large contact-first book is
fast without ever serving a stale book."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import book as bk


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, autoflush=False, autocommit=False)()
    bk._BOOK_CACHE.clear()
    try:
        yield s
    finally:
        s.close()
        bk._BOOK_CACHE.clear()


def _user(db):
    u = models.User(name="Op", email="op@x.com", unipile_account_id="a")
    db.add(u); db.commit()
    return u


def test_cache_reuses_build_then_rebuilds_on_change(db, monkeypatch):
    u = _user(db)
    calls = {"n": 0}

    def _fake_spine(_db, _user):
        calls["n"] += 1
        return [{"id": 1, "name": "A"}]

    monkeypatch.setattr(bk, "_book_from_spine", _fake_spine)
    monkeypatch.setattr(bk, "_demo_book", lambda: [])

    bk._load_book(db, u)
    bk._load_book(db, u)
    assert calls["n"] == 1  # second read served from cache (fingerprint stable)

    # A new contact (a capture) moves the fingerprint -> next read rebuilds.
    db.add(models.Contact(user_id=u.id, primary_identity_key="li:new", name="New"))
    db.commit()
    bk._load_book(db, u)
    assert calls["n"] == 2


def test_fingerprint_moves_when_contact_added(db):
    u = _user(db)
    fp0 = bk._book_fingerprint(db, u.id)
    db.add(models.Contact(user_id=u.id, primary_identity_key="li:x", name="X"))
    db.commit()
    assert bk._book_fingerprint(db, u.id) != fp0
