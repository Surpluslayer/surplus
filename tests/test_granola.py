"""Tests for the Granola MCP connector core (integrations/granola.py): PKCE (stateless,
recomputable), signed state, DCR caching, the PKCE<->state wiring, token exchange, and
the MCP initialize->tools/call sequence. All HTTP mocked (no live Granola)."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import urlparse, parse_qs

from backend.integrations import granola


class _Resp:
    def __init__(self, payload, headers=None):
        self._p = payload
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = json.dumps(payload)
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


def test_pkce_verifier_deterministic_and_challenge(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    v = granola.verifier_for("abc")
    assert v == granola.verifier_for("abc")            # recomputable from the nonce
    assert 43 <= len(v) <= 128                          # valid PKCE length
    assert granola.verifier_for("xyz") != v            # nonce-specific
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert granola.challenge_for("abc") == expect       # S256


def test_state_roundtrip_tamper_expiry(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    st = granola.sign_state({"u": 9, "n": "nn", "exp": time.time() + 100})
    assert granola.verify_state(st)["n"] == "nn"
    assert granola.verify_state(st[:-1] + ("0" if st[-1] != "0" else "1")) is None
    assert granola.verify_state(granola.sign_state(
        {"u": 9, "n": "nn", "exp": time.time() - 1})) is None


def test_register_client_caches(monkeypatch):
    granola._CLIENT.clear()
    calls = {"n": 0}
    def fake_post(url, **kw):
        calls["n"] += 1
        return _Resp({"client_id": "C123"})
    monkeypatch.setattr(granola.httpx, "post", fake_post)
    assert granola.register_client("https://x/cb") == "C123"
    assert granola.register_client("https://x/cb") == "C123"
    assert calls["n"] == 1                              # cached, not re-registered


def test_authorize_url_pkce_wired_to_state(monkeypatch):
    monkeypatch.setenv("SURPLUS_OAUTH_STATE_SECRET", "s")
    granola._CLIENT.clear()
    monkeypatch.setattr(granola.httpx, "post", lambda url, **kw: _Resp({"client_id": "C1"}))
    q = parse_qs(urlparse(granola.authorize_url(
        redirect_uri="https://x/cb", user_id=7)).query)
    assert q["client_id"] == ["C1"] and q["code_challenge_method"] == ["S256"]
    assert "offline_access" in q["scope"][0]
    nonce = granola.verify_state(q["state"][0])["n"]
    assert q["code_challenge"][0] == granola.challenge_for(nonce)   # challenge derives from state's nonce


def test_exchange_code_proves_pkce_with_recomputed_verifier(monkeypatch):
    granola._CLIENT.update({"client_id": "C1", "redirect_uris": ["https://x/cb"]})
    seen = {}
    def fake_post(url, **kw):
        seen.update(kw.get("data") or {})
        return _Resp({"access_token": "AT", "refresh_token": "RT"})
    monkeypatch.setattr(granola.httpx, "post", fake_post)
    tok = granola.exchange_code(code="c", redirect_uri="https://x/cb", nonce="nn")
    assert tok["access_token"] == "AT"
    assert seen["code_verifier"] == granola.verifier_for("nn")      # verifier recomputed, not stored
    assert seen["grant_type"] == "authorization_code"


def test_call_tool_initializes_then_calls(monkeypatch):
    seq = []
    def fake_post(url, **kw):
        method = (kw.get("json") or {})["method"]
        seq.append(method)
        if method == "initialize":
            return _Resp({"result": {"ok": True}},
                         headers={"Mcp-Session-Id": "S1", "Content-Type": "application/json"})
        return _Resp({"result": {"content": [{"type": "text", "text": "notes"}]}})
    monkeypatch.setattr(granola.httpx, "post", fake_post)
    res = granola.call_tool("tok", "list_meetings", {"limit": 5})
    assert seq == ["initialize", "tools/call"]
    assert res["content"][0]["text"] == "notes"


def test_call_tool_parses_sse_response(monkeypatch):
    # Streamable HTTP may answer as an SSE 'data:' stream
    def fake_post(url, **kw):
        if (kw.get("json") or {})["method"] == "initialize":
            return _Resp({"result": {}}, headers={"Mcp-Session-Id": "S1"})
        r = _Resp({"result": {"x": 1}})
        r.headers = {"Content-Type": "text/event-stream"}
        r.text = 'event: message\ndata: {"result": {"x": 1}}\n\n'
        return r
    monkeypatch.setattr(granola.httpx, "post", fake_post)
    assert granola.call_tool("tok", "get_account_info", {})["x"] == 1
