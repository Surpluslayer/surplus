"""
Host-based SPA shell routing : event.surpluslayer.com must serve the in-person
shell, surpluslayer.com the desktop shell.

Regression for the production bug where, behind Cloudflare/Railway, the raw Host
header is rewritten to the origin's internal name : the shell selector then
served the DESKTOP shell on event.surpluslayer.com. The real user-facing host
survives in X-Forwarded-Host / Origin, which the selector must honor.
"""
from __future__ import annotations
import re

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INPERSON_HOSTS", "event.surpluslayer.com")
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html)
    return m.group(1) if m else ""


def _is_inperson(title: str) -> bool:
    return "in person" in title.lower()


# ── direct (no proxy) ────────────────────────────────────────────────────────

def test_direct_event_host_serves_inperson(client):
    assert _is_inperson(_title(client.get("/", headers={"host": "event.surpluslayer.com"}).text))


def test_direct_apex_host_serves_desktop(client):
    assert not _is_inperson(_title(client.get("/", headers={"host": "www.surpluslayer.com"}).text))


# ── behind a proxy that rewrites Host (the production bug) ────────────────────

def test_proxy_event_via_x_forwarded_host_serves_inperson(client):
    r = client.get("/", headers={"host": "surplus-production.up.railway.app",
                                 "x-forwarded-host": "event.surpluslayer.com"})
    assert _is_inperson(_title(r.text))


def test_proxy_apex_via_x_forwarded_host_serves_desktop(client):
    r = client.get("/", headers={"host": "surplus-production.up.railway.app",
                                 "x-forwarded-host": "www.surpluslayer.com"})
    assert not _is_inperson(_title(r.text))


def test_event_via_origin_fallback_serves_inperson(client):
    r = client.get("/", headers={"host": "surplus.up.railway.app",
                                 "origin": "https://event.surpluslayer.com"})
    assert _is_inperson(_title(r.text))


def test_client_side_route_on_event_host_serves_inperson(client):
    # A deep link / refresh on the in-person host must also get the in-person
    # shell (SPA fallback), through the proxy.
    r = client.get("/captures", headers={"host": "x.up.railway.app",
                                         "x-forwarded-host": "event.surpluslayer.com"})
    assert _is_inperson(_title(r.text))
