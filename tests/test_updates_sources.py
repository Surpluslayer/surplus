"""Multi-source update discovery (updates_watch news/web/X legs + additive
engine). In-memory SQLite + monkeypatched network; no real HTTP anywhere."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import updates_engine, updates_watch


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


def _contact(db, name="Salma Dias Saraswati", company="Tenang AI"):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key="li:salma",
                       name=name, company=company,
                       linkedin_url="https://linkedin.com/in/salma")
    db.add(c); db.commit()
    return u, c


# ── Google News RSS parsing ─────────────────────────────────────────────────

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>q</title>
<item>
  <title>Tenang AI raises $2M to expand Gen Z mental health platform</title>
  <link>https://betakit.com/tenang-ai-raises-2m/</link>
  <pubDate>{fresh}</pubDate>
  <description>&lt;a href="https://betakit.com/x"&gt;Tenang AI raises&lt;/a&gt; seed round led by...</description>
  <source url="https://betakit.com">BetaKit</source>
</item>
<item>
  <title>Old news from years ago</title>
  <link>https://example.com/old</link>
  <pubDate>Tue, 02 Jan 2018 10:00:00 GMT</pubDate>
  <description>stale</description>
</item>
<item>
  <title>No link item</title>
  <pubDate>{fresh}</pubDate>
</item>
</channel></rss>"""


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def test_google_news_parses_and_filters(monkeypatch):
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(days=2)) \
        .strftime("%a, %d %b %Y %H:%M:%S GMT")
    captured = {}

    def fake_get(url, **kw):
        captured["url"] = url
        return _Resp(_RSS.format(fresh=fresh))

    monkeypatch.setattr(updates_watch.httpx, "get", fake_get)
    out = updates_watch._google_news_search('"Salma" Tenang AI', lookback_days=30)
    assert "news.google.com/rss/search" in captured["url"]
    assert "when%3A30d" in captured["url"]
    assert len(out) == 1                       # stale + linkless items dropped
    hit = out[0]
    assert hit["url"] == "https://betakit.com/tenang-ai-raises-2m/"
    assert hit["via"] == "news"
    assert "raises $2M" in hit["title"]
    assert "<" not in hit["text"]              # description HTML stripped
    assert "BetaKit" in hit["text"]            # source attribution kept


def test_google_news_fail_soft(monkeypatch):
    def boom(url, **kw):
        raise OSError("network down")
    monkeypatch.setattr(updates_watch.httpx, "get", boom)
    assert updates_watch._google_news_search("anything") == []
    assert updates_watch._google_news_search("") == []


# ── X handle resolution + ContactFact caching ───────────────────────────────

def test_resolve_x_handle_stores_fact(db, monkeypatch):
    u, c = _contact(db)
    calls = []

    def fake_exa(query, **kw):
        calls.append(kw)
        return [
            {"url": "https://x.com/search?q=x", "title": "Search", "text": ""},
            {"url": "https://x.com/salma_dias", "title": "Salma Dias Saraswati",
             "text": "Salma Dias Saraswati building Tenang AI"},
        ]

    monkeypatch.setattr(updates_watch, "_exa_search", fake_exa)
    assert updates_watch._resolve_x_handle(db, c) == "salma_dias"
    assert calls[0]["include_domains"] == ["x.com"]
    db.commit()
    # Cached as a ContactFact: the second call must not hit Exa again.
    monkeypatch.setattr(updates_watch, "_exa_search",
                        lambda *a, **k: pytest.fail("should be cached"))
    assert updates_watch._resolve_x_handle(db, c) == "salma_dias"
    row = (db.query(models.ContactFact)
             .filter_by(contact_id=c.id, key="x_handle").one())
    assert row.value == "salma_dias"
    assert row.source == "enrichment"


def test_resolve_x_handle_rejects_wrong_person_and_caches_miss(db, monkeypatch):
    u, c = _contact(db)
    monkeypatch.setattr(updates_watch, "_exa_search", lambda *a, **k: [
        {"url": "https://x.com/someone_else", "title": "A different person",
         "text": "totally unrelated bio"}])
    assert updates_watch._resolve_x_handle(db, c) == ""
    db.commit()
    # The miss is cached too (no re-resolution every sweep).
    monkeypatch.setattr(updates_watch, "_exa_search",
                        lambda *a, **k: pytest.fail("miss should be cached"))
    assert updates_watch._resolve_x_handle(db, c) == ""


# ── Multi-source merge in find_updates ──────────────────────────────────────

def test_find_updates_merges_sources_and_dedupes(db, monkeypatch):
    u, c = _contact(db)
    dup = "https://betakit.com/tenang-ai-raises-2m/"
    monkeypatch.setattr(updates_watch, "_google_news_search", lambda *a, **k: [
        {"title": "Tenang AI raises $2M", "url": dup, "published": None,
         "text": "Tenang AI seed round", "via": "news"}])
    monkeypatch.setattr(updates_watch, "_exa_search",
                        lambda q, **kw: ([{"url": dup, "title": "Tenang AI raises",
                                           "text": "Tenang AI raised a seed"}]
                                         if "x.com" not in (kw.get("include_domains") or [])
                                         else [{"url": "https://x.com/salma/status/1",
                                                "title": "launch thread",
                                                "text": "Tenang AI launch thread"}]))
    monkeypatch.setattr(updates_watch, "_resolve_x_handle", lambda *a: "salma")

    seen_packed = {}

    def fake_llm(system, user, **kw):
        seen_packed["packed"] = json.loads(user.split("Recent web results:\n")[1])
        return {"has_update": True, "type": "new_post",
                "headline": "Tenang AI raises $2M",
                "summary": "Tenang AI raised a $2M seed round",
                "url": dup, "identity_confidence": "high",
                "matched_company": "Tenang AI"}

    monkeypatch.setattr(updates_watch, "_llm_json", fake_llm)
    changes = updates_watch.find_updates(db, c)
    db.commit()

    packed = seen_packed["packed"]
    urls = [p["url"] for p in packed]
    assert urls.count(dup) == 1                    # news+web same URL deduped
    assert "https://x.com/salma/status/1" in urls  # X leg contributed
    assert len(changes) == 1
    ri = db.query(models.RelationshipInteraction).one()
    assert ri.source_type == "activity_update"
    assert json.loads(ri.meta_json)["source"] == "news"  # winning leg attributed


def test_find_updates_source_kill_switches(db, monkeypatch):
    u, c = _contact(db)
    monkeypatch.setenv("UPDATES_NEWS_ENABLED", "0")
    monkeypatch.setenv("UPDATES_X_ENABLED", "0")
    monkeypatch.setattr(updates_watch, "_google_news_search",
                        lambda *a, **k: pytest.fail("news disabled"))
    monkeypatch.setattr(updates_watch, "_resolve_x_handle",
                        lambda *a: pytest.fail("x disabled"))
    monkeypatch.setattr(updates_watch, "_exa_search", lambda *a, **k: [])
    assert updates_watch.find_updates(db, c) == []


# ── Additive engine: Bright Data AND the web pass ───────────────────────────

def _patch_brightdata(monkeypatch, *, configured=True, triggered=None):
    from backend.providers import brightdata
    monkeypatch.setattr(brightdata, "configured", lambda: configured)
    monkeypatch.setattr(brightdata, "trigger_updates",
                        lambda urls: (triggered.extend(urls) or True)
                        if triggered is not None else True)


def test_scrape_contact_runs_both_legs(db, monkeypatch):
    u, c = _contact(db)
    triggered: list = []
    _patch_brightdata(monkeypatch, triggered=triggered)
    web_calls: list = []
    monkeypatch.setattr(updates_engine.updates_watch, "find_updates",
                        lambda _db, _c: web_calls.append(_c.id) or [])
    res = updates_engine.scrape_contact(db, c)
    assert res["mode"] == "brightdata+web"
    assert triggered == [c.linkedin_url]
    assert web_calls == [c.id]
    assert c.watched_at is not None


def test_scrape_contact_web_only_when_brightdata_off(db, monkeypatch):
    u, c = _contact(db)
    _patch_brightdata(monkeypatch, configured=False)
    monkeypatch.setattr(updates_engine.updates_watch, "find_updates",
                        lambda _db, _c: [])
    assert updates_engine.scrape_contact(db, c)["mode"] == "web"


def test_scrape_contact_additive_kill_switch(db, monkeypatch):
    u, c = _contact(db)
    monkeypatch.setenv("UPDATES_WEB_ADDITIVE", "0")
    _patch_brightdata(monkeypatch)
    monkeypatch.setattr(updates_engine.updates_watch, "find_updates",
                        lambda _db, _c: pytest.fail("web pass disabled"))
    assert updates_engine.scrape_contact(db, c)["mode"] == "brightdata"


def test_run_sweep_additive_mode(db, monkeypatch):
    u, c = _contact(db)
    triggered: list = []
    _patch_brightdata(monkeypatch, triggered=triggered)
    monkeypatch.setattr(updates_engine.updates_watch, "find_updates",
                        lambda _db, _c: [])
    monkeypatch.setattr(updates_engine, "due_contacts",
                        lambda _db, **kw: [c])
    from backend.agents.relationship import account_signals
    monkeypatch.setattr(account_signals, "account_pass",
                        lambda _db, **kw: {})
    res = updates_engine.run_sweep(db, user_id=u.id)
    assert res["mode"] == "brightdata+web"
    assert res["triggered"] == 1
    assert c.watched_at is not None
