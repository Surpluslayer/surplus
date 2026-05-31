"""
Cookie-scope tests for the session + last-account cookies.

The in-person surface lives on event.surpluslayer.com while sign-in / payment
happen on the apex. The session cookie must be shareable across those
first-party subdomains via SESSION_COOKIE_DOMAIN, while staying host-only when
the env is unset (localhost / *.railway.app / *.fly.dev) so a non-matching
Domain can't make browsers drop the cookie.
"""
from __future__ import annotations

from fastapi import Response

from backend import auth


def _set_cookies(monkeypatch, domain_env):
    if domain_env is None:
        monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    else:
        monkeypatch.setenv("SESSION_COOKIE_DOMAIN", domain_env)
    r = Response()
    auth.set_session_cookie(r, "tok")
    auth.set_last_account_cookie(r, "acct")
    c = Response()
    auth.clear_session_cookie(c)
    sets = [v.decode() for k, v in r.raw_headers if k == b"set-cookie"]
    clear = [v.decode() for k, v in c.raw_headers if k == b"set-cookie"][0]
    return sets, clear


def test_cookie_host_only_when_env_unset(monkeypatch):
    sets, clear = _set_cookies(monkeypatch, None)
    assert sets and all("Domain=" not in s for s in sets)
    assert "Domain=" not in clear
    # core attributes preserved
    assert all("HttpOnly" in s and "SameSite=lax" in s and "Path=/" in s for s in sets)


def test_cookie_shared_across_subdomains_when_env_set(monkeypatch):
    sets, clear = _set_cookies(monkeypatch, ".surpluslayer.com")
    # Both the session and last-account cookies carry the shared Domain.
    assert len(sets) == 2
    assert all("Domain=.surpluslayer.com" in s for s in sets)
    # Logout must clear with the SAME Domain or the cookie survives.
    assert "Domain=.surpluslayer.com" in clear
    assert "Max-Age=0" in clear


def test_cookie_domain_blank_env_is_treated_as_unset(monkeypatch):
    sets, _ = _set_cookies(monkeypatch, "   ")
    assert all("Domain=" not in s for s in sets)


def _set_with_host(monkeypatch, host):
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)  # no env : derive from host
    r = Response()
    auth.set_session_cookie(r, "tok", host=host)
    return [v.decode() for k, v in r.raw_headers if k == b"set-cookie"][0]


def test_cookie_domain_auto_derived_from_surpluslayer_host(monkeypatch):
    # The whole point: no env var set, yet a *.surpluslayer.com request still
    # gets a shared Domain so the LinkedIn callback cookie survives the next hop.
    for host in ("event.surpluslayer.com", "www.surpluslayer.com", "surpluslayer.com"):
        sc = _set_with_host(monkeypatch, host)
        assert "Domain=.surpluslayer.com" in sc, host


def test_cookie_host_only_for_non_first_party_hosts(monkeypatch):
    for host in ("localhost", "surplus-production.up.railway.app", "surplus.fly.dev", None):
        sc = _set_with_host(monkeypatch, host)
        assert "Domain=" not in sc, host


def test_env_override_still_wins_over_host(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", ".surpluslayer.com")
    r = Response()
    auth.set_session_cookie(r, "tok", host="localhost")  # env wins despite host
    sc = [v.decode() for k, v in r.raw_headers if k == b"set-cookie"][0]
    assert "Domain=.surpluslayer.com" in sc
