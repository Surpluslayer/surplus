"""
Tests for POST /api/auth/triage/signup : the skip-LinkedIn path for users
who only want Applicant Triage (no outbound, no Unipile connection).

Verifies:
  - new User row gets unipile_account_id=NULL
  - existing email returns the existing User (no duplicate row)
  - bad email / empty name → 400
  - session cookie is set on the RETURNED response (not a dep-injected one;
    FastAPI gotcha where dep-Response cookies are dropped if you return a
    different Response instance)
  - existing User rows with LinkedIn aren't affected

Patterns mirrors test_followups.py : exercise the route function directly
to avoid the FastAPI app import (Python 3.9 / str | None evaluation issue).
"""
from __future__ import annotations
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def _body(name="Daniel", email="daniel@example.com"):
    from backend.routes.auth import TriageSignupBody
    return TriageSignupBody(name=name, email=email)


def test_signup_creates_user_with_null_unipile_account(db):
    from backend.routes.auth import triage_signup
    result = triage_signup(_body(), db)
    payload = json.loads(result.body)
    assert payload["ok"] is True
    assert payload["mode"] == "triage_only"

    user = db.query(models.User).filter_by(email="daniel@example.com").first()
    assert user is not None
    assert user.name == "Daniel"
    assert user.unipile_account_id is None


def test_signup_normalizes_email_lowercase(db):
    from backend.routes.auth import triage_signup
    triage_signup(_body(email="Daniel@Example.COM"), db)
    user = db.query(models.User).filter_by(email="daniel@example.com").first()
    assert user is not None


def test_signup_returns_existing_user_for_same_email(db):
    from backend.routes.auth import triage_signup
    triage_signup(_body(name="Daniel"), db)
    triage_signup(_body(name="Different Name"), db)
    users = db.query(models.User).filter_by(email="daniel@example.com").all()
    assert len(users) == 1


def test_signup_rejects_empty_name(db):
    from backend.routes.auth import triage_signup
    with pytest.raises(HTTPException) as exc:
        triage_signup(_body(name=""), db)
    assert exc.value.status_code == 400


def test_signup_rejects_invalid_email(db):
    from backend.routes.auth import triage_signup
    with pytest.raises(HTTPException) as exc:
        triage_signup(_body(email="not-an-email"), db)
    assert exc.value.status_code == 400


def test_signup_sets_session_cookie_on_returned_response(db):
    """Regression : we used to set the cookie on a dep-injected Response
    parameter and then return a different JSONResponse, so the cookie
    silently dropped. Now the cookie has to land on result.headers."""
    from backend.routes.auth import triage_signup
    result = triage_signup(_body(), db)
    # FastAPI JSONResponse carries Set-Cookie via raw_headers
    cookie_headers = [
        v.decode() for k, v in result.raw_headers
        if k.decode().lower() == "set-cookie"
    ]
    assert any("surplus_session=" in c for c in cookie_headers), \
        f"no session cookie set on response : raw_headers={result.raw_headers}"


def test_quick_start_mints_anonymous_user_and_session(db):
    """Triage quick-start : no body, just creates a User + session."""
    from backend.routes.auth import triage_quick_start
    result = triage_quick_start(db)
    payload = json.loads(result.body)
    assert payload["ok"] is True
    assert payload["mode"] == "triage_only"
    # Anonymous user has no Unipile connection : App.jsx auto-routes to triage
    user = db.query(models.User).filter_by(id=payload["user_id"]).first()
    assert user is not None
    assert user.unipile_account_id is None
    assert user.email.startswith("triage-")
    # Cookie set on the returned response (FastAPI gotcha pinned earlier)
    cookie_headers = [
        v.decode() for k, v in result.raw_headers
        if k.decode().lower() == "set-cookie"
    ]
    assert any("surplus_session=" in c for c in cookie_headers)


def test_quick_start_creates_separate_users_per_call(db):
    """Two clicks on 'Triage mode' from different visitors must mint
    distinct User rows : the email-tag suffix avoids the unique constraint."""
    from backend.routes.auth import triage_quick_start
    r1 = triage_quick_start(db)
    r2 = triage_quick_start(db)
    p1 = json.loads(r1.body)
    p2 = json.loads(r2.body)
    assert p1["user_id"] != p2["user_id"]


def test_signup_does_not_touch_existing_linkedin_user(db):
    """A pre-existing User row with a unipile_account_id shouldn't be
    affected by a triage signup with a different email."""
    existing = models.User(unipile_account_id="acct-abc",
                           name="Existing", email="existing@example.com")
    db.add(existing)
    db.commit()
    from backend.routes.auth import triage_signup
    triage_signup(_body(email="new@example.com"), db)
    refreshed = db.query(models.User).filter_by(
        unipile_account_id="acct-abc").first()
    assert refreshed.email == "existing@example.com"
    new_user = db.query(models.User).filter_by(email="new@example.com").first()
    assert new_user.unipile_account_id is None


def test_signup_race_converges_on_oldest_row(db):
    """TOCTOU backstop. users.email has no unique constraint, so two
    concurrent signups can BOTH miss the existence read and BOTH insert.
    Simulate the loser: the competitor's row is committed, but OUR first
    read returns None (it ran before their commit landed). The route must
    detect the duplicate on re-read, delete its own insert, and hand back
    the oldest row — one user per email, deterministically."""
    from backend.routes.auth import triage_signup

    competitor = models.User(name="First Wins", email="daniel@example.com")
    db.add(competitor)
    db.commit()
    db.refresh(competitor)

    class _RaceSession:
        """Pass-through session whose FIRST User query returns None,
        replaying the stale read that opens the race window."""
        def __init__(self, real):
            self._real = real
            self._stale_reads = 1

        def __getattr__(self, name):
            return getattr(self._real, name)

        def query(self, *a, **kw):
            q = self._real.query(*a, **kw)
            if self._stale_reads and a and a[0] is models.User:
                self._stale_reads -= 1
                outer = self

                class _StaleQuery:
                    def __init__(self, inner): self._inner = inner
                    def filter(self, *fa, **fk):
                        return _StaleQuery(self._inner.filter(*fa, **fk))
                    def first(self):
                        return None  # the competitor's commit isn't visible yet
                return _StaleQuery(q)
            return q

    result = triage_signup(_body(name="Second Loses"), _RaceSession(db))
    payload = json.loads(result.body)

    users = db.query(models.User).filter_by(email="daniel@example.com").all()
    assert len(users) == 1                      # the duplicate was removed
    assert users[0].id == competitor.id         # oldest row won
    assert payload["user_id"] == competitor.id  # and the session points at it
