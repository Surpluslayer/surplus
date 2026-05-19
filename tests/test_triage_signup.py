"""
Tests for POST /api/auth/triage/signup : the skip-LinkedIn path for users
who only want Applicant Triage (no outbound, no Unipile connection).

Verifies:
  - new User row gets unipile_account_id=NULL
  - existing email returns the existing User (no duplicate row)
  - bad email / empty name → 400
  - session cookie is set on the response
  - existing User rows with LinkedIn aren't affected

Patterns mirrors test_followups.py : exercise the route function directly
to avoid the FastAPI app import (Python 3.9 / str | None evaluation issue).
"""
from __future__ import annotations
from unittest.mock import MagicMock

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


def _response():
    """Minimal FastAPI Response stub : signup just calls set_cookie."""
    resp = MagicMock()
    resp.set_cookie = MagicMock()
    return resp


def test_signup_creates_user_with_null_unipile_account(db):
    from backend.routes.auth import triage_signup
    resp = _response()
    result = triage_signup(_body(), resp, db)
    # JSONResponse body is bytes; pull from .body via json
    import json
    payload = json.loads(result.body)
    assert payload["ok"] is True
    assert payload["mode"] == "triage_only"

    user = db.query(models.User).filter_by(email="daniel@example.com").first()
    assert user is not None
    assert user.name == "Daniel"
    assert user.unipile_account_id is None


def test_signup_normalizes_email_lowercase(db):
    from backend.routes.auth import triage_signup
    triage_signup(_body(email="Daniel@Example.COM"), _response(), db)
    user = db.query(models.User).filter_by(email="daniel@example.com").first()
    assert user is not None


def test_signup_returns_existing_user_for_same_email(db):
    from backend.routes.auth import triage_signup
    triage_signup(_body(name="Daniel"), _response(), db)
    # Second signup with same email : should reuse the User row.
    triage_signup(_body(name="Different Name"), _response(), db)
    users = db.query(models.User).filter_by(email="daniel@example.com").all()
    assert len(users) == 1


def test_signup_rejects_empty_name(db):
    from backend.routes.auth import triage_signup
    with pytest.raises(HTTPException) as exc:
        triage_signup(_body(name=""), _response(), db)
    assert exc.value.status_code == 400


def test_signup_rejects_invalid_email(db):
    from backend.routes.auth import triage_signup
    with pytest.raises(HTTPException) as exc:
        triage_signup(_body(email="not-an-email"), _response(), db)
    assert exc.value.status_code == 400


def test_signup_sets_session_cookie(db):
    from backend.routes.auth import triage_signup
    resp = _response()
    triage_signup(_body(), resp, db)
    resp.set_cookie.assert_called_once()
    # Verify the cookie name is the surplus session cookie
    call = resp.set_cookie.call_args
    assert call.kwargs.get("key") == "surplus_session"


def test_signup_does_not_touch_existing_linkedin_user(db):
    """A pre-existing User row with a unipile_account_id shouldn't be
    affected by a triage signup with a different email."""
    existing = models.User(unipile_account_id="acct-abc",
                           name="Existing", email="existing@example.com")
    db.add(existing)
    db.commit()
    from backend.routes.auth import triage_signup
    triage_signup(_body(email="new@example.com"), _response(), db)
    # Existing user untouched
    refreshed = db.query(models.User).filter_by(
        unipile_account_id="acct-abc").first()
    assert refreshed.email == "existing@example.com"
    # New user created with NULL unipile_account_id
    new_user = db.query(models.User).filter_by(email="new@example.com").first()
    assert new_user.unipile_account_id is None
