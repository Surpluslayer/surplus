"""
Full-app HTTP tests for the surfaces that survived the events-side
retirement: the Unipile webhook edge cases and the gated diagnostics.

History: this file was the end-to-end five-stage pipeline test (intake ->
prospect -> outreach -> match -> ROI over HTTP). That surface was retired
on 2026-07-07 (routers unmounted in main.py; models and data kept) — the
pipeline flow tests went with it. Webhook BEHAVIOR coverage for the
relationship side lives on in test_inperson.py / test_providers.py /
test_webhook_ai_reply.py.
"""
from __future__ import annotations
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from backend.db import reset_db, SessionLocal
from backend.main import app
from backend import models
from backend.providers import reset_provider_cache


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Default: PROVIDER=unipile + DRY_RUN. No real network ever touched."""
    monkeypatch.setenv("PROVIDER", "unipile")
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    monkeypatch.delenv("UNIPILE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("UNIPILE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("UNIPILE_REQUIRE_SIGNATURE", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    reset_provider_cache()
    reset_db()
    yield
    reset_provider_cache()


@pytest.fixture
def client(fresh_db):
    """An AUTHENTICATED client (webhook + diagnostics routes live on the
    real app; a signed-in user mirrors the real path)."""
    from datetime import datetime, timezone

    from backend import auth
    db = SessionLocal()
    user = models.User(name="Test Op", email="test@op.com",
                       unipile_account_id="test_acct",
                       paid_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    tok = auth.create_session(db, user).session_token
    db.close()
    with TestClient(app) as c:
        c.cookies.set("surplus_session", tok)
        yield c


def _post_unipile_webhook(client, payload: dict, secret: str | None = None):
    body = json.dumps(payload).encode()
    headers = {}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["x-unipile-signature"] = f"sha256={sig}"
    return client.post("/webhooks/unipile", content=body, headers=headers)


def test_webhook_unknown_provider_id_no_crash(client):
    """Webhook for a LinkedIn user we don't have in our DB: 200, no mutation."""
    r = _post_unipile_webhook(client, {
        "event": "new_relation",
        "user_provider_id": "ACoAAA_unknown_to_us",
    })
    assert r.status_code == 200
    assert r.json()["applied"] is False


def test_diagnostics_endpoints_require_admin_token(client):
    """Security review H-2: the operator diagnostics endpoints make BILLED
    upstream calls; unauthenticated access is a cost-DoS + config leak, so they
    404 without the admin token (same posture as /admin)."""
    for path in ("/api/diagnostics/anthropic", "/api/diagnostics/exa",
                 "/api/diagnostics/exa/discover"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} should be gated, got {r.status_code}"


def test_retired_pipeline_surface_stays_dark(client):
    """The events-side routers are unmounted: their paths must 404. This
    test is the tripwire against someone re-registering the retired surface
    by accident (e.g. a merge resurrecting the include_router lines)."""
    for path in ("/api/events", "/api/matching/1/run", "/api/roi/1",
                 "/api/triage/1/upload", "/api/curation/events"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} resurfaced: {r.status_code}"
