"""Double-send protection + 429 backoff on the live Unipile send path.

The failure this guards against: a send request that times out AFTER Unipile
accepted it. The invite/DM may be live on LinkedIn, but the caller saw an
error — and the natural retry sends the contact a SECOND message from the
user's real account. Three layers under test:

  1. UnipileProvider._post separates CLEAN failures (4xx/5xx/connect error:
     nothing happened, retry freely; 429 retried with bounded backoff) from
     AMBIGUOUS ones (read timeout after dispatch -> AmbiguousSendError).
  2. send_connection/send_message map ambiguity to state="unconfirmed".
  3. send_flow._assert_no_recent_send 409s a re-send while an unconfirmed
     send is fresh, and absorbs double-clicks after a confirmed send.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents import send_flow
from backend.providers.base import AmbiguousSendError
from backend.providers.unipile import UnipileProvider


# ── fakes ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code=200, body='{"id":"inv_1"}', headers=None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.text)


class _FakeHttpxClient:
    """Stands in for httpx.Client; `script` is a list of responses or
    exceptions to produce, one per .post() call."""
    script: list = []
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kw):
        _FakeHttpxClient.calls.append(url)
        item = _FakeHttpxClient.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def live_provider(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    monkeypatch.setattr("time.sleep", lambda s: None)  # no real backoff waits
    _FakeHttpxClient.script = []
    _FakeHttpxClient.calls = []
    p = UnipileProvider(dry_run=False, api_key="k",
                        dsn="https://api.unipile.test", account_id="acc")
    return p


def _lead(p):
    prospect = SimpleNamespace(
        id=1, identity="maya", name="Maya Rodriguez", role="Eng",
        company="Lo91r", seniority="Staff+", side="Builds",
        works_on="infra", offers="", seeks="", gh_stars=0, x_followers=0,
        li_resolved=True, linkedin_url="https://linkedin.com/in/maya",
        sources="linkedin", fit_score=80, fit_reason="", status="approved")
    event = SimpleNamespace(
        id=7, role="Engineers", seniority="Senior", co_stage="Seed",
        headcount=9, format="Dinner", city="SF", goal="Hiring",
        budget=1000, threshold=70)
    return p.build_lead_payload(prospect, event, "note", "message")


# ── 1. _post : 429 backoff + ambiguity separation ───────────────────────────

def test_post_retries_429_then_succeeds(live_provider):
    _FakeHttpxClient.script = [_Resp(429, "rate limited"),
                               _Resp(429, "rate limited"),
                               _Resp(200, '{"ok":true}')]
    out = live_provider._post("/api/v1/chats", {})
    assert out == {"ok": True}
    assert len(_FakeHttpxClient.calls) == 3


def test_post_429_gives_up_after_bounded_retries(live_provider):
    _FakeHttpxClient.script = [_Resp(429, "rl")] * 5
    with pytest.raises(RuntimeError) as ei:
        live_provider._post("/api/v1/chats", {})
    assert "429" in str(ei.value)
    assert len(_FakeHttpxClient.calls) == 3  # 1 + 2 retries, bounded


def test_post_read_timeout_is_ambiguous_not_failed(live_provider):
    import httpx
    _FakeHttpxClient.script = [httpx.ReadTimeout("read timed out")]
    with pytest.raises(AmbiguousSendError):
        live_provider._post("/api/v1/users/invite", {})


def test_post_connect_error_is_clean_failure(live_provider):
    """A connect error can't have delivered anything: plain RuntimeError,
    safe for callers to retry."""
    import httpx
    _FakeHttpxClient.script = [httpx.ConnectError("refused")]
    with pytest.raises(RuntimeError) as ei:
        live_provider._post("/api/v1/users/invite", {})
    assert not isinstance(ei.value, AmbiguousSendError)


# ── 2. send paths map ambiguity to "unconfirmed" ────────────────────────────

def test_send_message_timeout_returns_unconfirmed(live_provider):
    import httpx
    _FakeHttpxClient.script = [httpx.ReadTimeout("read timed out")]
    res = live_provider.send_message(_lead(live_provider),
                                     linkedin_provider_id="ACo123")
    assert res.state == "unconfirmed"
    assert "unconfirmed" in (res.error or "")


def test_send_connection_timeout_returns_unconfirmed(live_provider, monkeypatch):
    import httpx
    monkeypatch.setattr(live_provider, "_lookup_provider_id", lambda h: "ACo123")
    _FakeHttpxClient.script = [httpx.ReadTimeout("read timed out")]
    res = live_provider.send_connection(_lead(live_provider))
    assert res.state == "unconfirmed"
    # provider_id still cached so the eventual retry can skip the lookup.
    assert res.linkedin_provider_id == "ACo123"


def test_send_message_4xx_is_failed_not_unconfirmed(live_provider):
    _FakeHttpxClient.script = [_Resp(422, "bad payload")]
    res = live_provider.send_message(_lead(live_provider),
                                     linkedin_provider_id="ACo123")
    assert res.state == "failed"


# ── 3. send_flow recent-send guard ──────────────────────────────────────────

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _prospect(db):
    u = models.User(email="host@x.com", name="Host")
    db.add(u); db.flush()
    ev = models.Event(user_id=u.id, kind="in_person", label="Dinner", city="SF")
    db.add(ev); db.flush()
    p = models.Prospect(event_id=ev.id, identity="maya", name="Maya",
                        linkedin_url="https://linkedin.com/in/maya",
                        status="approved", source="scan")
    db.add(p); db.commit()
    return p


def _log(db, p, state, *, age_secs):
    db.add(models.OutreachLog(
        prospect_id=p.id, channel="linkedin", state=state, body="x",
        ts=datetime.now(timezone.utc) - timedelta(seconds=age_secs),
        provider="unipile"))
    db.commit()


def test_guard_blocks_resend_while_unconfirmed_is_fresh(db):
    p = _prospect(db)
    _log(db, p, "unconfirmed", age_secs=60)
    with pytest.raises(HTTPException) as ei:
        send_flow._assert_no_recent_send(db, p)
    assert ei.value.status_code == 409
    assert "didn't confirm" in ei.value.detail


def test_guard_lifts_after_unconfirmed_window(db):
    p = _prospect(db)
    _log(db, p, "unconfirmed", age_secs=11 * 60)
    send_flow._assert_no_recent_send(db, p)  # no raise


def test_guard_absorbs_double_click_after_confirmed_send(db):
    p = _prospect(db)
    _log(db, p, "invite_sent", age_secs=5)
    with pytest.raises(HTTPException) as ei:
        send_flow._assert_no_recent_send(db, p)
    assert ei.value.status_code == 409
    assert "seconds ago" in ei.value.detail


def test_guard_allows_send_after_doubleclick_window(db):
    p = _prospect(db)
    _log(db, p, "invite_sent", age_secs=120)
    send_flow._assert_no_recent_send(db, p)  # no raise


def test_guard_ignores_dry_run_and_failed_rows(db):
    p = _prospect(db)
    _log(db, p, "dry_run_queued", age_secs=1)
    _log(db, p, "failed", age_secs=1)
    send_flow._assert_no_recent_send(db, p)  # neither state blocks


def test_guard_no_history_is_clean(db):
    p = _prospect(db)
    send_flow._assert_no_recent_send(db, p)  # no raise
