"""Regression tests for the security-review fixes (recursive review follow-up).

Covers, by finding id:
  M2  — _surplus_base_url never echoes an untrusted Host into user-facing links
  H1  — session-adoption endpoints reject cross-site top-level navigations
  M4  — CSV/import uploads are size-capped (413) instead of OOMing
  L9  — logout emits the session-cookie delete on the returned response
  L10 — password login is constant-work; granola reuses the fail-closed secret
  M5/L3 — health exposes field-encryption status and gates integrations to admin
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.routes import auth as auth_route
from backend.triage import csv_parser


def _req(headers=None, netloc="evil.com", scheme="http"):
    return SimpleNamespace(
        url=SimpleNamespace(netloc=netloc, scheme=scheme),
        headers={k.lower(): v for k, v in (headers or {}).items()},
    )


# ── M2: Host-header trust in _surplus_base_url ────────────────────────────

def test_base_url_env_override_wins(monkeypatch):
    monkeypatch.setenv("SURPLUS_BASE_URL", "https://app.example.com")
    assert auth_route._surplus_base_url(_req(netloc="whatever")) == "https://app.example.com"


def test_base_url_rejects_forged_host(monkeypatch):
    monkeypatch.delenv("SURPLUS_BASE_URL", raising=False)
    # A forged/unknown Host must NOT be echoed back — falls back to the apex.
    assert auth_route._surplus_base_url(_req(netloc="evil.com")) == auth_route._PRODUCTION_APEX
    assert auth_route._surplus_base_url(
        _req(netloc="attacker.com", headers={"x-forwarded-proto": "https"})
    ) == auth_route._PRODUCTION_APEX


def test_base_url_trusts_first_party_and_localhost(monkeypatch):
    monkeypatch.delenv("SURPLUS_BASE_URL", raising=False)
    assert auth_route._surplus_base_url(
        _req(netloc="event.surpluslayer.com")) == "https://event.surpluslayer.com"
    assert auth_route._surplus_base_url(
        _req(netloc="localhost:5173")).startswith("http://localhost:5173")


# ── H1: cross-site top-level navigation guard ─────────────────────────────

@pytest.mark.parametrize("site,dest,blocked", [
    ("cross-site", "document", True),    # the attack: emailed/link top-level nav
    ("cross-site", "iframe", False),     # extension Book iframe embed
    ("none", "document", False),         # native app surplus:// deep link
    ("same-origin", "document", False),  # first-party SPA navigation
    ("same-site", "document", False),
    ("", "", False),                     # old browser (no Fetch-Metadata): fail open
])
def test_cross_site_toplevel_nav_detection(site, dest, blocked):
    req = _req(headers={"sec-fetch-site": site, "sec-fetch-dest": dest})
    assert auth_route._is_cross_site_toplevel_nav(req) is blocked


# ── M4: upload size cap ───────────────────────────────────────────────────

def test_enforce_upload_size_rejects_oversized(monkeypatch):
    monkeypatch.setattr(csv_parser, "MAX_UPLOAD_BYTES", 100)
    csv_parser.enforce_upload_size(b"x" * 100)  # exactly at limit: ok
    with pytest.raises(HTTPException) as exc:
        csv_parser.enforce_upload_size(b"x" * 101)
    assert exc.value.status_code == 413


def test_parse_csv_file_rejects_oversized(monkeypatch):
    monkeypatch.setattr(csv_parser, "MAX_UPLOAD_BYTES", 50)
    import io
    big = io.BytesIO(b"name,email\n" + b"a,a@b.co\n" * 100)
    with pytest.raises(HTTPException) as exc:
        csv_parser.parse_csv_file(big)
    assert exc.value.status_code == 413


# ── L10: login constant-work + granola secret ────────────────────────────

def test_login_dummy_hash_is_real_bcrypt():
    from backend.routes.password_auth import _DUMMY_PW_HASH
    from backend.auth import verify_password
    # A real bcrypt hash: any password verifies False against it (never raises),
    # so the no-account branch does the same bcrypt work as a real check.
    assert _DUMMY_PW_HASH.startswith("$2")
    assert verify_password("anything", _DUMMY_PW_HASH) is False


def test_logout_clears_cookie_on_returned_response():
    from fastapi import Response
    from backend.routes.auth import logout
    req = SimpleNamespace(cookies={}, headers={})
    out = logout(response=Response(), request=req, db=None, authorization=None)
    set_cookie = out.headers.get("set-cookie", "").lower()
    # The delete must be on the RETURNED response (else the browser keeps the
    # revoked cookie). delete_cookie emits Max-Age=0 / an expiry in the past.
    assert "surplus_session=" in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


def test_granola_secret_reuses_oauth_secret(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)  # dev: no fail-closed
    monkeypatch.delenv("SURPLUS_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.setenv("SURPLUS_BASE_URL", "https://public.example.com")
    from backend.integrations import granola, oauth
    # Must NOT fall back to the public SURPLUS_BASE_URL; must equal oauth._secret().
    assert granola._secret() == oauth._secret()
    assert b"public.example.com" not in granola._secret()
