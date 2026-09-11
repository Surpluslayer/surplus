"""
Civic policy search: the rules that make the answer trustworthy.

The evidence hierarchy is a product rule, not a prompt suggestion, so the
tests that matter here are the ones that hold when the model misbehaves: an
invented URL is dropped, a thin answer is searched again, prose instead of
JSON fails loudly with the raw text kept for the UI.
"""
from __future__ import annotations

import json
import re
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from backend import civic, civic_sources
from backend.main import app


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------

GOOD_URL = "https://www.census.gov/data/rent"               # a government host: tier A
STUDY_URL = "https://www.aeaweb.org/articles/rent-supply"   # a journal: tier B by host
NEWS_URL = "https://news.example.com/story"                 # no signal in the host: tier as claimed
INVENTED = "https://totally-made-up.example.org/never-searched"


def _search_block(*urls):
    return {"type": "web_search_tool_result",
            "content": [{"type": "web_search_result", "url": u} for u in urls]}


def _text_block(payload):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return {"type": "text", "text": body}


class _Response:
    def __init__(self, content):
        self.content = content


def _answer_payload(**over):
    payload = {
        "headline": "Rents rose because supply stalled while demand kept climbing.",
        "summary": "Median asking rent is up 15% since 2023.",
        "mechanisms": [
            {"mechanism": "Permits fell", "confidence": "high", "because": "Filings are down 40%."},
            {"mechanism": "In-migration", "confidence": "medium", "because": "Net inflow doubled."},
            {"mechanism": "Rate lock-in", "confidence": "low", "because": "Owners are not selling."},
        ],
        "evidence": [
            {"tier": "A", "claim": "Median rent +15%", "source": "Census", "url": GOOD_URL},
            {"tier": "B", "claim": "Supply cuts rent 2%", "source": "Study", "url": STUDY_URL},
            {"tier": "E", "claim": "Council debated the cap", "source": "Local paper", "url": NEWS_URL},
        ],
        "disputed": ["Whether the cap lowers rents."],
        "live_decisions": ["Council vote on the zoning overlay, Oct 2."],
        "actions": [
            {"effort": 1, "what": "Email your council member", "why": "Comment is counted", "url": ""},
            {"effort": 2, "what": "Read the staff report", "why": "It names the tradeoff", "url": NEWS_URL},
        ],
        "caveat": "None of this says what your own landlord will do.",
    }
    payload.update(over)
    return payload


@pytest.fixture(autouse=True)
def _clear_cache():
    civic.cache_clear()
    yield
    civic.cache_clear()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_extract_json_strips_fences_and_prose():
    text = 'Sure, here you go:\n```json\n{"headline": "hi"}\n```\nHope that helps.'
    assert civic.extract_json(text) == {"headline": "hi"}


def test_extract_json_keeps_raw_text_for_the_show_what_we_found_toggle():
    with pytest.raises(civic.CivicError) as exc:
        civic.extract_json("I could not find anything about that.")
    assert exc.value.code == "not_json"
    assert "could not find anything" in exc.value.raw


def test_extract_json_rejects_a_json_array():
    with pytest.raises(civic.CivicError):
        civic.extract_json("[1, 2, 3]")


def test_search_urls_reads_results_and_citations():
    content = [
        _search_block(GOOD_URL),
        {"type": "text", "text": "x", "citations": [{"url": "http://WWW.News.Example.com/story/"}]},
    ]
    urls = civic.search_urls(content)
    assert civic.normalise_url(GOOD_URL) in urls
    # Host case, www and a trailing slash are noise, not a different source.
    assert civic.normalise_url(NEWS_URL) in urls


def test_normalise_url_rejects_non_web_urls():
    assert civic.normalise_url("javascript:alert(1)") == ""
    assert civic.normalise_url("") == ""
    assert civic.normalise_url(None) == ""


# --------------------------------------------------------------------------
# Validation : the hierarchy, enforced in code
# --------------------------------------------------------------------------

def _allowed(*urls):
    return {civic.normalise_url(u) for u in urls}


def test_validate_drops_evidence_whose_url_was_never_searched():
    payload = _answer_payload(evidence=[
        {"tier": "A", "claim": "Real", "source": "Census", "url": GOOD_URL},
        {"tier": "A", "claim": "Invented", "source": "Census", "url": INVENTED},
        {"tier": "B", "claim": "No link at all", "source": "Someone", "url": ""},
    ])
    answer, notes = civic.validate(payload, _allowed(GOOD_URL))
    assert [e["claim"] for e in answer["evidence"]] == ["Real"]
    assert notes["evidence_dropped"] == 2


def test_validate_puts_a_nonsense_tier_where_the_publisher_says():
    payload = _answer_payload(evidence=[
        {"tier": "G", "claim": "Off the ladder", "source": "Census", "url": GOOD_URL},
    ])
    answer, notes = civic.validate(payload, _allowed(GOOD_URL))
    assert answer["tiers"] == ["A"]          # census.gov is official data
    assert notes["tiers_corrected"] == 1


def test_validate_demotes_a_reddit_thread_cited_as_official_data():
    payload = _answer_payload(evidence=[
        {"tier": "A", "claim": "Everyone says rents doubled", "source": "r/oakland",
         "url": "https://www.reddit.com/r/oakland/comments/abc"},
    ])
    answer, notes = civic.validate(payload, _allowed("https://reddit.com/r/oakland/comments/abc"))
    # The claim survives, but only as a social signal -- which the ladder
    # renders as "what people are worried about", never as support.
    assert answer["tiers"] == ["F"]
    assert notes["tiers_corrected"] == 1


def test_validate_caps_a_think_tank_claiming_to_be_peer_reviewed():
    payload = _answer_payload(evidence=[
        {"tier": "B", "claim": "Supply cuts rents", "source": "Brookings",
         "url": "https://www.brookings.edu/articles/supply"},
    ])
    answer, _ = civic.validate(payload, _allowed("https://brookings.edu/articles/supply"))
    assert answer["tiers"] == ["C"]


def test_validate_trusts_a_university_host_that_claims_something_weaker():
    payload = _answer_payload(evidence=[
        {"tier": "E", "claim": "The university paper covered the vote",
         "source": "Campus paper", "url": "https://news.stanford.edu/story"},
    ])
    answer, _ = civic.validate(payload, _allowed("https://news.stanford.edu/story"))
    assert answer["tiers"] == ["E"]


def test_validate_reports_the_tiers_reached_in_ladder_order():
    answer, _ = civic.validate(_answer_payload(), _allowed(GOOD_URL, STUDY_URL, NEWS_URL))
    assert answer["tiers"] == ["A", "B", "E"]


def test_validate_keeps_urlless_actions_but_drops_invented_links():
    payload = _answer_payload(actions=[
        {"effort": 1, "what": "Email your council member", "why": "It is counted", "url": ""},
        {"effort": 2, "what": "Read the fabricated report", "why": "", "url": INVENTED},
    ])
    answer, notes = civic.validate(payload, _allowed(GOOD_URL))
    assert [a["what"] for a in answer["actions"]] == ["Email your council member"]
    assert notes["actions_dropped"] == 1


def test_validate_allows_only_one_two_minute_action():
    payload = _answer_payload(actions=[
        {"effort": 1, "what": "First quick thing", "why": "", "url": ""},
        {"effort": 1, "what": "Second quick thing", "why": "", "url": ""},
        {"effort": 3, "what": "Evening thing", "why": "", "url": ""},
        {"effort": 3, "what": "Another evening thing", "why": "", "url": ""},
        {"effort": 2, "what": "Hour thing", "why": "", "url": ""},
    ])
    answer, _ = civic.validate(payload, set())
    efforts = [a["effort"] for a in answer["actions"]]
    assert efforts == [1, 2, 3]          # sorted, and one slot each at 1 and 3


def test_validate_coerces_a_nonsense_confidence_to_low():
    payload = _answer_payload(mechanisms=[
        {"mechanism": "Something", "confidence": "very sure", "because": ""},
    ])
    answer, _ = civic.validate(payload, set())
    assert answer["mechanisms"][0]["confidence"] == "low"


def test_validate_caps_mechanisms_and_evidence():
    payload = _answer_payload(
        mechanisms=[{"mechanism": f"m{i}", "confidence": "high"} for i in range(9)],
        evidence=[{"tier": "A", "claim": f"c{i}", "source": "s", "url": GOOD_URL}
                  for i in range(20)],
    )
    answer, _ = civic.validate(payload, _allowed(GOOD_URL))
    assert len(answer["mechanisms"]) == civic.MAX_MECHANISMS
    assert len(answer["evidence"]) == civic.MAX_EVIDENCE


def test_validate_requires_a_headline():
    with pytest.raises(civic.CivicError) as exc:
        civic.validate(_answer_payload(headline="  "), set())
    assert exc.value.code == "no_headline"


# --------------------------------------------------------------------------
# synthesize : one call, one retry, no more
# --------------------------------------------------------------------------

def test_synthesize_returns_a_grounded_answer_in_one_call():
    calls = []

    def create(messages):
        calls.append(messages)
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(_answer_payload())])

    answer, notes = civic.synthesize("Why did my rent go up?", "Oakland, CA", create=create)
    assert len(calls) == 1
    assert answer["tiers"] == ["A", "B", "E"]
    assert notes["retried"] is False
    assert "Oakland, CA" in calls[0][0]["content"]


def test_synthesize_searches_harder_when_the_ladder_is_thin():
    thin = _answer_payload(evidence=[
        {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL},
    ])
    responses = [
        _Response([_search_block(NEWS_URL), _text_block(thin)]),
        _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL), _text_block(_answer_payload())]),
    ]
    sent = []

    def create(messages):
        sent.append(messages[0]["content"])
        return responses[len(sent) - 1]

    answer, notes = civic.synthesize("Why?", "Oakland, CA", create=create)
    assert len(sent) == 2
    assert "tiers A and B" in sent[1]
    assert answer["tiers"] == ["A", "B", "E"]   # the better pass wins
    assert notes["attempts"] == 2


def test_synthesize_keeps_the_thin_answer_when_the_retry_is_no_better():
    thin = _answer_payload(evidence=[
        {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL},
    ])

    def create(messages):
        return _Response([_search_block(NEWS_URL), _text_block(thin)])

    answer, _ = civic.synthesize("Why?", "", create=create)
    # An empty rung is an honest answer; two thin passes still return one.
    assert answer["tiers"] == ["E"]


def test_synthesize_retries_prose_once_then_raises_with_the_raw_text():
    calls = []

    def create(messages):
        calls.append(messages)
        return _Response([_text_block("I am afraid I could not find that.")])

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why?", "", create=create)
    assert len(calls) == 2
    assert exc.value.code == "not_json"
    assert "could not find" in exc.value.raw


def test_synthesize_does_not_retry_a_vague_question():
    vague = {"headline": civic.VAGUE_HEADLINE, "summary": "", "mechanisms": [],
             "evidence": [], "disputed": [], "live_decisions": [], "actions": [],
             "caveat": "", "rewrites": ["Why is my rent up?", "Who sets my property tax?",
                                        "What is on my ballot?"]}
    calls = []

    def create(messages):
        calls.append(messages)
        return _Response([_text_block(vague)])

    answer, _ = civic.synthesize("politics?", "", create=create)
    assert len(calls) == 1                     # searching harder finds nothing
    assert len(answer["rewrites"]) == 3


def test_synthesize_rejects_an_empty_question():
    with pytest.raises(civic.CivicError):
        civic.synthesize("   ", "", create=lambda m: None)


def test_synthesize_wraps_an_upstream_failure():
    def create(messages):
        raise RuntimeError("connection reset")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why?", "", create=create)
    assert exc.value.code == "upstream_error"


def test_user_message_names_the_dropped_pin():
    msg = civic.user_message("Why?", "", lat=37.7749, lon=-122.4194)
    assert "37.77490" in msg and "-122.41940" in msg


def test_user_message_says_when_no_place_was_given():
    assert "national level" in civic.user_message("Why?", "")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def test_cache_key_ignores_case_and_spacing():
    assert civic.cache_key("Why  is RENT up?", "Oakland") == \
           civic.cache_key("why is rent up?", "  oakland ")


def test_cache_key_separates_places():
    assert civic.cache_key("Why is rent up?", "Oakland") != \
           civic.cache_key("Why is rent up?", "Austin")


def test_cache_expires_after_24h():
    civic.cache_put("k", {"answer": 1}, now=1000.0)
    assert civic.cache_get("k", now=1000.0 + civic.CACHE_TTL_S - 1) == {"answer": 1}
    assert civic.cache_get("k", now=1000.0 + civic.CACHE_TTL_S + 1) is None


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------

_IP = iter(range(1, 250))


@pytest.fixture
def client():
    # A fresh forwarded IP per test: the rate-limit window is process-global
    # (backend/rate_limit.py), so sharing one would make unrelated tests
    # start failing as soon as this file grows past ten requests.
    with TestClient(app) as c:
        c.headers["X-Forwarded-For"] = f"198.51.100.{next(_IP)}"
        yield c


def _stub_synthesis(monkeypatch, answer=None, notes=None):
    answer = answer or civic.validate(_answer_payload(),
                                      _allowed(GOOD_URL, STUDY_URL, NEWS_URL))[0]
    calls = []

    def fake(question, location="", **kw):
        calls.append((question, location, kw))
        return answer, dict(notes or {"evidence_dropped": 0, "actions_dropped": 0,
                                      "mechanisms_dropped": 0, "latency_ms": 12,
                                      "retried": False, "attempts": 1})

    monkeypatch.setattr(civic, "available", lambda: True)
    monkeypatch.setattr(civic, "synthesize", fake)
    return calls


def test_ask_returns_an_answer_and_a_permalink_id(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?",
                                            "location": "Oakland, CA",
                                            "lat": 37.8, "lon": -122.27})
    assert r.status_code == 200, r.text
    body = r.json()
    # The id is a share token, not a cache id: it carries the question, so a
    # permalink works on the replica that never answered it.
    assert civic.read_token(body["id"]) == {"question": "Why is rent up?",
                                            "location": "Oakland, CA",
                                            "brief": False}
    assert body["cached"] is False
    assert body["answer"]["tiers"] == ["A", "B", "E"]
    assert calls[0][2]["lat"] == 37.8


def test_ask_serves_the_second_identical_question_from_cache(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    payload = {"question": "Why is rent up?", "location": "Oakland, CA"}
    client.post("/api/civic/ask", json=payload)
    r = client.post("/api/civic/ask", json=payload)
    assert r.json()["cached"] is True
    assert len(calls) == 1


def test_ask_rejects_an_empty_question(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    r = client.post("/api/civic/ask", json={"question": "   "})
    assert r.status_code == 400


def test_ask_says_so_when_the_key_is_missing(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: False)
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "unconfigured"


def test_ask_surfaces_the_raw_text_when_synthesis_fails(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", **kw):
        raise civic.CivicError("prose, not JSON", raw="here is what I found", code="not_json")

    monkeypatch.setattr(civic, "synthesize", boom)
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
    assert r.status_code == 422
    assert r.json()["detail"]["raw"] == "here is what I found"


def test_ask_is_rate_limited_per_ip(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    codes = [client.post("/api/civic/ask",
                         json={"question": f"Question number {i}?"},
                         headers={"X-Forwarded-For": "203.0.113.9"}).status_code
             for i in range(12)]
    assert codes[:10] == [200] * 10
    assert codes[10:] == [429, 429]


def test_permalink_returns_the_cached_answer(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    posted = client.post("/api/civic/ask", json={"question": "Why is rent up?",
                                                 "location": "Oakland, CA"}).json()
    r = client.get(f"/api/civic/answer/{posted['id']}")
    assert r.status_code == 200
    assert r.json()["answer"]["headline"] == posted["answer"]["headline"]
    assert r.json()["question"] == "Why is rent up?"


def test_expired_permalink_is_a_404(client):
    assert client.get("/api/civic/answer/deadbeefdeadbeef").status_code == 404


def test_the_map_page_is_served_on_civic_and_on_a_permalink(client):
    for path in ("/civic", "/civic/r/abc123"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        assert "The evidence, strongest first" in r.text
        assert "/api/civic/ask" in r.text


def test_a_broken_retry_does_not_erase_a_usable_first_answer():
    thin = _answer_payload(evidence=[
        {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL},
    ])
    responses = [
        _Response([_search_block(NEWS_URL), _text_block(thin)]),
        _Response([_text_block("On reflection I would rather write an essay.")]),
    ]
    seen = []

    def create(messages):
        seen.append(messages)
        return responses[len(seen) - 1]

    answer, _ = civic.synthesize("Why?", "", create=create)
    assert len(seen) == 2
    assert answer["tiers"] == ["E"]


# --------------------------------------------------------------------------
# Retrieval mode (EXA_API_KEY set) : we search, the model synthesizes
# --------------------------------------------------------------------------

def _results(*urls):
    return [{"tier": "A", "found_by": "A", "title": "t", "url": u,
             "host": "h", "published": "", "snippet": "s"} for u in urls]


def test_retrieval_puts_the_sources_in_the_prompt_and_asks_for_no_tools():
    seen = []

    def retrieve(question, location, *, harder=False):
        seen.append((question, location, harder))
        return _results(GOOD_URL, STUDY_URL, NEWS_URL)

    sent = []

    def create(messages):
        sent.append(messages[0]["content"])
        return _Response([_text_block(_answer_payload())])   # no search blocks

    answer, notes = civic.synthesize("Why is rent up?", "Oakland, CA",
                                     create=create, retrieve=retrieve)
    assert seen == [("Why is rent up?", "Oakland, CA", False)]
    assert "Search results you may use" in sent[0]
    assert GOOD_URL in sent[0]
    # Grounding now comes from what we retrieved, not from tool-result blocks.
    assert len(answer["evidence"]) == 3
    assert notes["sources"] == "exa"
    assert notes["sources_found"] == 3


def test_retrieval_drops_a_citation_we_never_retrieved():
    def retrieve(question, location, *, harder=False):
        return _results(GOOD_URL)

    def create(messages):
        return _Response([_text_block(_answer_payload(evidence=[
            {"tier": "A", "claim": "Real", "source": "Census", "url": GOOD_URL},
            {"tier": "A", "claim": "Invented", "source": "Census", "url": INVENTED},
        ]))])

    answer, _ = civic.synthesize("Why?", "", create=create, retrieve=retrieve)
    assert [e["claim"] for e in answer["evidence"]] == ["Real"]


def test_retrieval_retries_by_searching_harder():
    calls = []

    def retrieve(question, location, *, harder=False):
        calls.append(harder)
        return _results(NEWS_URL) if not harder else _results(GOOD_URL, STUDY_URL, NEWS_URL)

    def create(messages):
        payload = _answer_payload() if calls[-1] else _answer_payload(evidence=[
            {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL}])
        return _Response([_text_block(payload)])

    answer, notes = civic.synthesize("Why?", "", create=create, retrieve=retrieve)
    assert calls == [False, True]
    assert answer["tiers"] == ["A", "B", "E"]
    assert notes["retried"] is True


def test_empty_retrieval_falls_back_to_the_models_own_search():
    def retrieve(question, location, *, harder=False):
        return []           # bad key, quota, outage

    def create(messages):
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(_answer_payload())])

    answer, notes = civic.synthesize("Why?", "", create=create, retrieve=retrieve)
    assert notes["sources"] == "web_search"
    assert len(answer["evidence"]) == 3


# --------------------------------------------------------------------------
# The fence : Civic shares a process, a threadpool and two API keys with the
# CRM, so what it may take of them is capped and tested.
# --------------------------------------------------------------------------

from backend.routes import civic as civic_routes  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_budget():
    civic_routes._reset_daily_budget()
    yield
    civic_routes._reset_daily_budget()


def test_civic_imports_nothing_from_the_crm():
    """The only app module Civic may import is the per-IP rate limiter.

    This is the guarantee that a Civic change cannot break the CRM: no models,
    no db session, no auth, no agents. If this test fails, the surface stopped
    being standalone.
    """
    import ast
    import pathlib

    allowed = {"civic", "civic_sources", "civic_geo", "rate_limit"}
    root = pathlib.Path(__file__).resolve().parent.parent / "backend"
    for path in (root / "civic.py", root / "civic_sources.py", root / "civic_geo.py",
                 root / "routes" / "civic.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # `from . import x` / `from ..thing import y` -- the relative ones
            # are the only way to reach the rest of the app.
            if isinstance(node, ast.ImportFrom) and node.level:
                reached = {node.module} if node.module else {a.name for a in node.names}
                stray = {m for m in reached if m and m not in allowed}
                assert not stray, f"{path.name} imports {stray} from the app"


def test_only_two_syntheses_run_at_once(client, monkeypatch):
    """Past the cap the request is shed, not queued.

    A queued request holds a threadpool thread, and that pool is the CRM's.
    """
    _stub_synthesis(monkeypatch)
    held = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    try:
        assert all(held)
        r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "busy"
        assert r.headers["Retry-After"] == "20"
    finally:
        for _ in held:
            civic_routes._SLOTS.release()


def test_a_slot_is_returned_after_a_failed_synthesis(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", **kw):
        raise civic.CivicError("nope", code="upstream_error")

    monkeypatch.setattr(civic, "synthesize", boom)
    for i in range(3):
        assert client.post("/api/civic/ask",
                           json={"question": f"Question {i}?"}).status_code == 422
    # Every slot is free again : a run of failures cannot wedge the surface.
    free = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    for _ in [f for f in free if f]:
        civic_routes._SLOTS.release()
    assert all(free)


def test_the_daily_budget_caps_what_civic_spends_of_the_shared_keys(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    monkeypatch.setattr(civic_routes, "_DAILY_CAP", 2)
    codes = [client.post("/api/civic/ask", json={"question": f"Question {i}?"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]
    assert len(calls) == 2


def test_a_cached_answer_is_free_of_the_daily_budget(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    monkeypatch.setattr(civic_routes, "_DAILY_CAP", 1)
    payload = {"question": "Why is rent up?", "location": "Oakland, CA"}
    assert client.post("/api/civic/ask", json=payload).status_code == 200
    again = client.post("/api/civic/ask", json=payload)
    assert again.status_code == 200 and again.json()["cached"] is True


def test_civic_enabled_0_takes_the_whole_surface_down(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    monkeypatch.setenv("CIVIC_ENABLED", "0")
    assert client.get("/civic").status_code == 404
    assert client.get("/civic/r/abc123").status_code == 404
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "disabled"


def test_the_rest_of_the_app_still_serves_when_civic_is_off(client, monkeypatch):
    monkeypatch.setenv("CIVIC_ENABLED", "0")
    assert client.get("/api/health").status_code == 200


def test_a_shed_request_does_not_spend_the_daily_budget(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    monkeypatch.setattr(civic_routes, "_DAILY_CAP", 1)
    held = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    try:
        assert client.post("/api/civic/ask",
                           json={"question": "Shed me?"}).status_code == 503
    finally:
        for _ in [h for h in held if h]:
            civic_routes._SLOTS.release()
    # The one answer in today's budget is still there.
    assert client.post("/api/civic/ask",
                       json={"question": "Why is rent up?"}).status_code == 200


# --------------------------------------------------------------------------
# Streaming : the same answer, but the page can say what is happening
# --------------------------------------------------------------------------

def _events(response_text):
    """Parse an SSE body into [(event, data), ...]."""
    out = []
    for block in response_text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def test_the_stream_reports_progress_and_then_the_answer(client, monkeypatch):
    answer = civic.validate(_answer_payload(), _allowed(GOOD_URL, STUDY_URL, NEWS_URL))[0]

    def fake(question, location="", *, on_event=None, **kw):
        on_event("started")
        on_event("retrieved", count=24, tiers=["A", "B", "E"])
        on_event("headline", text=answer["headline"])
        return answer, {"evidence_dropped": 0, "actions_dropped": 0,
                        "mechanisms_dropped": 0, "latency_ms": 10,
                        "sources": "exa", "sources_found": 24, "retried": False}

    monkeypatch.setattr(civic, "available", lambda: True)
    monkeypatch.setattr(civic, "synthesize", fake)

    r = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _events(r.text)
    names = [name for name, _ in events]
    assert names == ["started", "retrieved", "headline", "answer"]
    # The headline arrives before the answer : that is the whole point.
    assert events[2][1]["text"] == answer["headline"]
    assert events[-1][1]["answer"]["tiers"] == ["A", "B", "E"]
    assert events[-1][1]["cached"] is False


def test_the_stream_serves_a_cached_answer_immediately(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    payload = {"question": "Why is rent up?", "location": "Oakland, CA"}
    client.post("/api/civic/ask", json=payload)
    r = client.post("/api/civic/ask/stream", json=payload)
    events = _events(r.text)
    assert [name for name, _ in events] == ["answer"]
    assert events[0][1]["cached"] is True
    assert len(calls) == 1


def test_the_stream_ends_with_an_error_event_not_a_dead_connection(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", *, on_event=None, **kw):
        on_event("started")
        raise civic.CivicError("prose, not JSON", raw="what we found", code="not_json")

    monkeypatch.setattr(civic, "synthesize", boom)
    r = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    events = _events(r.text)
    assert [name for name, _ in events] == ["started", "error"]
    assert events[-1][1]["raw"] == "what we found"


def test_the_stream_releases_its_slot_when_synthesis_fails(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", *, on_event=None, **kw):
        raise civic.CivicError("nope", code="upstream_error")

    monkeypatch.setattr(civic, "synthesize", boom)
    client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    free = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    for _ in [f for f in free if f]:
        civic_routes._SLOTS.release()
    assert all(free)


def test_the_stream_obeys_the_same_fence(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    held = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    try:
        r = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
        assert r.status_code == 503 and r.json()["detail"]["code"] == "busy"
    finally:
        for _ in [h for h in held if h]:
            civic_routes._SLOTS.release()

    monkeypatch.setenv("CIVIC_ENABLED", "0")
    off = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    assert off.status_code == 503 and off.json()["detail"]["code"] == "disabled"


# --------------------------------------------------------------------------
# The streaming call itself : the part that talks to the SDK
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, type_): self.type = type_


class _Event:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Delta:
    def __init__(self, text): self.text = text


class _FakeStream:
    def __init__(self, events, final):
        self._events, self._final = events, final

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self._events)
    def get_final_message(self): return self._final


class _FakeClient:
    def __init__(self, events, final):
        self._events, self._final = events, final
        self.kwargs = None

    def with_options(self, **kw): return self

    @property
    def messages(self): return self

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._events, self._final)

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._final


def test_the_streaming_call_reports_searches_writing_and_the_headline(monkeypatch):
    payload = _answer_payload()
    body = json.dumps(payload)
    events = [
        _Event("content_block_start", content_block=_Block("server_tool_use")),
        _Event("content_block_start", content_block=_Block("web_search_tool_result")),
        _Event("content_block_start", content_block=_Block("server_tool_use")),
        _Event("content_block_start", content_block=_Block("text")),
        # The headline is the first key, so it completes early in the write.
        _Event("content_block_delta", delta=_Delta(body[:120])),
        _Event("content_block_delta", delta=_Delta(body[120:])),
    ]
    final = _Response([_search_block(GOOD_URL), _text_block(payload)])
    fake = _FakeClient(events, final)
    monkeypatch.setattr(civic, "_client", lambda: fake)

    seen = []
    response = civic._create([{"role": "user", "content": "x"}],
                             on_event=lambda name, **f: seen.append((name, f)))

    assert response.content == final.content        # one turn, passed through
    names = [n for n, _ in seen]
    assert names.count("searching") == 2
    assert "writing" in names
    assert ("headline", {"text": payload["headline"]}) in seen


def test_without_a_callback_the_call_does_not_stream(monkeypatch):
    final = _Response([_text_block(_answer_payload())])
    fake = _FakeClient([], final)
    monkeypatch.setattr(civic, "_client", lambda: fake)
    assert civic._create([{"role": "user", "content": "x"}]).content == final.content
    assert "tools" in fake.kwargs          # web_search mode by default


def test_retrieval_mode_sends_no_tools(monkeypatch):
    final = _Response([_text_block(_answer_payload())])
    fake = _FakeClient([], final)
    monkeypatch.setattr(civic, "_client", lambda: fake)
    civic._create([{"role": "user", "content": "x"}], retrieval=True)
    assert "tools" not in fake.kwargs


def test_effort_rides_in_extra_body_only_when_configured(monkeypatch):
    final = _Response([_text_block(_answer_payload())])
    fake = _FakeClient([], final)
    monkeypatch.setattr(civic, "_client", lambda: fake)

    civic._create([{"role": "user", "content": "x"}])
    assert "extra_body" not in fake.kwargs          # unset by default

    monkeypatch.setattr(civic, "EFFORT", "low")
    civic._create([{"role": "user", "content": "x"}])
    assert fake.kwargs["extra_body"] == {"output_config": {"effort": "low"}}


@pytest.mark.parametrize("partial,expected", [
    ('{"headline": "Rents rose because supply stalled."', "Rents rose because supply stalled."),
    ('{"headline": "A \\"quoted\\" claim", "summary"', 'A "quoted" claim'),
    ('{"headl', ""),
    ('{"headline": "half writ', ""),
])
def test_peek_headline_reads_a_half_written_answer(partial, expected):
    assert civic._peek_headline(partial) == expected


# --------------------------------------------------------------------------
# When every question fails, the surface has to be able to say why
# --------------------------------------------------------------------------

class _ModelGone(Exception):
    status_code = 404

    def __str__(self):
        return "model: claude-sonnet-5 not_found_error"


def test_an_unusable_model_falls_back_to_the_one_the_app_already_runs(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    monkeypatch.setattr(civic, "MODEL", "claude-sonnet-5")
    seen = []

    def create(messages):
        seen.append(civic.active_model())
        if civic.active_model() != civic.FALLBACK_MODEL:
            raise _ModelGone()
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(_answer_payload())])

    answer, _ = civic.synthesize("Why is rent up?", "", create=create)
    assert seen == ["claude-sonnet-5", civic.FALLBACK_MODEL]
    assert len(answer["evidence"]) == 3


def test_a_failure_that_is_not_about_the_model_is_not_retried(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    calls = []

    def create(messages):
        calls.append(1)
        raise RuntimeError("connection reset by peer")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why?", "", create=create)
    assert len(calls) == 1
    assert exc.value.code == "upstream_error"
    # The technical cause travels in `raw`, so the page shows a plain sentence
    # and keeps the detail behind "show what we found".
    assert "RuntimeError" in exc.value.raw
    assert str(exc.value) == "The search could not be completed."


def test_the_last_failure_is_remembered_for_selftest(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        raise RuntimeError("connection reset by peer")

    with pytest.raises(civic.CivicError):
        civic.synthesize("Why?", "", create=create)
    assert civic.LAST_ERROR["type"] == "RuntimeError"
    assert "connection reset" in civic.LAST_ERROR["message"]
    assert civic.LAST_ERROR["mode"] in ("exa", "web_search")


def test_selftest_reports_configuration_and_the_last_failure(client, monkeypatch):
    civic.LAST_ERROR.clear()
    civic.LAST_ERROR.update({"type": "NotFoundError", "message": "model: nope",
                             "status": 404, "model": "nope", "mode": "web_search"})
    monkeypatch.setenv("EXA_API_KEY", "")
    r = client.get("/api/civic/selftest")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["sources"] == "keyless"      # no Exa key, so the fan-out only
    assert body["model_in_use"] == civic.active_model()
    assert body["last_error"]["type"] == "NotFoundError"
    # No key material, ever: the key fields say whether one is set, never what
    # it is, and nothing in the payload looks like a token.
    assert body["anthropic_key"] is True or body["anthropic_key"] is False
    assert body["exa_key"] is True or body["exa_key"] is False
    blob = json.dumps(body)
    assert "sk-ant" not in blob
    assert not re.search(r"[A-Za-z0-9_-]{32,}", blob)
    civic.LAST_ERROR.clear()


class _ToolNotEnabled(Exception):
    status_code = 400

    def __str__(self):
        return "tools.0: web_search_20260209 is not available to this account"


def test_a_missing_web_search_entitlement_says_what_to_do(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        raise _ToolNotEnabled()

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why is rent up?", "", create=create)
    assert exc.value.code == "no_search_backend"
    assert "EXA_API_KEY" in str(exc.value)
    # Never the workaround of answering from memory: an unsourced answer is
    # the failure this product exists to prevent.
    assert "web_search tool" in str(exc.value)


# --------------------------------------------------------------------------
# Dropping a pin is a briefing, not a vague question
# --------------------------------------------------------------------------

def test_a_briefing_tells_the_model_not_to_ask_for_a_narrower_question():
    msg = civic.user_message("What is happening in Oakland right now?",
                             "Oakland, California", brief=True)
    assert "place briefing" in msg
    assert "Do NOT ask for a narrower question" in msg
    assert "rewrites" in msg


def test_an_ordinary_question_is_not_a_briefing():
    assert "place briefing" not in civic.user_message("Why is my rent up?", "Oakland")


def test_the_brief_flag_reaches_the_prompt(client, monkeypatch):
    seen = {}

    def fake(question, location="", **kw):
        seen.update(kw)
        return civic.validate(_answer_payload(), _allowed(GOOD_URL, STUDY_URL, NEWS_URL))[0], {
            "evidence_dropped": 0, "actions_dropped": 0, "mechanisms_dropped": 0,
            "latency_ms": 5, "sources": "exa", "sources_found": 12, "retried": False}

    monkeypatch.setattr(civic, "available", lambda: True)
    monkeypatch.setattr(civic, "synthesize", fake)
    r = client.post("/api/civic/ask", json={
        "question": "What is happening in Oakland right now?",
        "location": "Oakland, CA", "brief": True})
    assert r.status_code == 200
    assert seen["brief"] is True


def test_selftest_names_the_replica_and_its_recent_failures(client):
    civic.RECENT_ERRORS.clear()
    civic.LAST_ERROR.clear()
    body = client.get("/api/civic/selftest").json()
    assert body["boot"] == civic.BOOT_ID
    assert body["uptime_s"] >= 0
    assert body["recent_errors"] == []
    assert "sources_last_run" in body


def test_every_failure_is_kept_in_the_ring_buffer(monkeypatch):
    civic.RECENT_ERRORS.clear()
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        raise RuntimeError("connection reset by peer")

    for _ in range(3):
        with pytest.raises(civic.CivicError):
            civic.synthesize("Why?", "", create=create)
    assert len(civic.RECENT_ERRORS) == 3
    assert all(e["boot"] == civic.BOOT_ID for e in civic.RECENT_ERRORS)
    civic.RECENT_ERRORS.clear()


# --------------------------------------------------------------------------
# A long search loop pauses the turn. Resuming it is the difference between
# an answer and "the model answered in prose".
# --------------------------------------------------------------------------

class _Paused:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _TurnClient:
    """Returns a queued response per call, recording what it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    def with_options(self, **kw): return self

    @property
    def messages(self): return self

    def create(self, **kwargs):
        self.sent.append(kwargs["messages"])
        return self._responses.pop(0)


def test_a_paused_turn_is_resumed_and_its_searches_are_kept(monkeypatch):
    payload = _answer_payload()
    fake = _TurnClient([
        # Searched, ran out of turn before writing anything.
        _Paused([_search_block(GOOD_URL, STUDY_URL)], "pause_turn"),
        # Resumed: more searching, then the answer.
        _Paused([_search_block(NEWS_URL), _text_block(payload)], "end_turn"),
    ])
    monkeypatch.setattr(civic, "_client", lambda: fake)

    response = civic._create([{"role": "user", "content": "Why is rent up?"}])
    assert response.stop_reason == "end_turn"
    # Both turns' blocks are kept, so the citations found before the pause
    # still count as grounded.
    urls = civic.search_urls(response.content)
    assert civic.normalise_url(GOOD_URL) in urls
    assert civic.normalise_url(NEWS_URL) in urls
    # The second call carried the first turn's content back as an assistant turn.
    assert len(fake.sent) == 2
    assert fake.sent[1][-1]["role"] == "assistant"


def test_resuming_gives_up_rather_than_looping_forever(monkeypatch):
    fake = _TurnClient([_Paused([_search_block(GOOD_URL)], "pause_turn")
                        for _ in range(civic.MAX_TURNS + 2)])
    monkeypatch.setattr(civic, "_client", lambda: fake)
    response = civic._create([{"role": "user", "content": "x"}])
    assert len(fake.sent) == civic.MAX_TURNS
    assert response.stop_reason == "pause_turn"


def test_a_cut_off_answer_says_so_instead_of_blaming_prose(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        return _Paused([_search_block(GOOD_URL),
                        _text_block('{"headline": "Rents rose because')], "max_tokens")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why is rent up?", "", create=create)
    assert exc.value.code == "truncated"
    assert "cut off" in str(exc.value)


def test_an_unfinished_search_says_so_too(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        return _Paused([_search_block(GOOD_URL)], "pause_turn")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why is rent up?", "", create=create)
    assert exc.value.code == "unfinished"


# --------------------------------------------------------------------------
# An answer with nothing under it is the one thing this must never publish
# --------------------------------------------------------------------------

def _tool_error_block(code="max_uses_exceeded"):
    """What a failed web_search looks like: HTTP 200, error object inside."""
    return {"type": "web_search_tool_result",
            "content": {"type": "web_search_tool_result_error", "error_code": code}}


def test_a_failed_web_search_stops_the_answer_instead_of_writing_from_memory(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    # The model happily writes prose with no citations when its search fails.
    unsourced = _answer_payload(evidence=[], headline="Live search wasn't available")

    def create(messages):
        return _Response([_tool_error_block("web_search_unavailable"),
                          _text_block(unsourced)])

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("What is proposed near Mira Mesa Blvd?", "San Diego", create=create)
    assert exc.value.code == "no_search_backend"
    assert "web_search_unavailable" in str(exc.value)


def test_search_errors_are_read_off_the_tool_result_block():
    assert civic.search_errors([_tool_error_block("max_uses_exceeded")]) == ["max_uses_exceeded"]
    # A healthy result carries a LIST of results, and is not an error.
    assert civic.search_errors([_search_block(GOOD_URL)]) == []


def test_an_answer_with_an_empty_ladder_is_refused(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        # Searches ran, nothing was cited: every claim came from the model.
        return _Response([_search_block(GOOD_URL),
                          _text_block(_answer_payload(evidence=[]))])

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("What is proposed near Mira Mesa Blvd?", "San Diego", create=create)
    assert exc.value.code == "no_evidence"
    assert "answer from memory" in str(exc.value)


def test_a_vague_question_is_still_allowed_to_have_no_evidence(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    vague = {"headline": civic.VAGUE_HEADLINE, "summary": "", "mechanisms": [],
             "evidence": [], "disputed": [], "live_decisions": [], "actions": [],
             "caveat": "", "rewrites": ["Why is my rent up?"]}

    def create(messages):
        return _Response([_text_block(vague)])

    answer, _ = civic.synthesize("politics?", "", create=create)
    assert answer["headline"] == civic.VAGUE_HEADLINE


def test_one_sourced_pass_survives_an_unsourced_one(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    payloads = [_answer_payload(evidence=[]),                     # thin first pass
                _answer_payload()]                                 # sourced retry
    calls = []

    def create(messages):
        calls.append(1)
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(payloads[len(calls) - 1])])

    answer, _ = civic.synthesize("Why?", "", create=create)
    assert len(answer["evidence"]) == 3


# --------------------------------------------------------------------------
# Permalinks : two replicas, one link, no database
# --------------------------------------------------------------------------

def test_a_share_token_round_trips():
    token = civic.share_token("Why did my rent go up 15%?", "Oakland, CA", brief=True)
    assert civic.read_token(token) == {"question": "Why did my rent go up 15%?",
                                       "location": "Oakland, CA", "brief": True}


@pytest.mark.parametrize("bad", ["", "   ", "not a token", "abc123def4567890",
                                 "x" * 3000, "!!!!"])
def test_read_token_refuses_anything_that_is_not_one(bad):
    assert civic.read_token(bad) is None


def test_read_token_will_not_unpack_a_bomb():
    # A few hundred bytes of base64 must not become a gigabyte of JSON.
    import base64 as b64
    import zlib as z
    bomb = b64.urlsafe_b64encode(z.compress(b"[" + b"0," * 2_000_000 + b"0]")).decode()
    assert civic.read_token(bomb.rstrip("=")) is None


def test_a_permalink_this_replica_never_answered_hands_back_the_question(client):
    # The exact production case: two replicas, and the link lands on the one
    # without the answer (or on any replica after a deploy).
    civic.cache_clear()
    token = civic.share_token("Why is rent up?", "Oakland, CA")
    r = client.get(f"/api/civic/answer/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["rebuild"] is True
    assert body["answer"] is None
    assert body["question"] == "Why is rent up?"
    assert body["location"] == "Oakland, CA"


def test_a_permalink_this_replica_did_answer_is_served_from_cache(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    posted = client.post("/api/civic/ask", json={"question": "Why is rent up?",
                                                 "location": "Oakland, CA"}).json()
    r = client.get(f"/api/civic/answer/{posted['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["rebuild"] is False
    assert body["answer"]["headline"] == posted["answer"]["headline"]


def test_a_link_that_is_not_ours_is_still_a_404(client):
    assert client.get("/api/civic/answer/deadbeefdeadbeef").status_code == 404


# --------------------------------------------------------------------------
# The fast lane: tap a thing on the map, no model involved
# --------------------------------------------------------------------------

def test_the_place_endpoint_returns_headlines_without_calling_the_model(
        client, monkeypatch):
    called = []
    monkeypatch.setattr(civic, "synthesize",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(civic_sources, "headlines", lambda subject, near="", limit=6: {
        "items": [{"tier": "E", "title": "School board votes on the budget",
                   "url": "https://www.mercurynews.com/story", "host": "mercurynews.com",
                   "published": "2026-03-02", "snippet": "", "found_by": "google_news"}],
        "ran": {"google_news": 1, "gdelt": "rate_limited"}})

    r = client.get("/api/civic/place?subject=Lincoln Elementary&near=Oakland, CA")
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["host"] == "mercurynews.com"
    assert body["ran"]["gdelt"] == "rate_limited"
    assert called == []                 # the fast lane never synthesizes


def test_the_place_endpoint_needs_a_subject(client):
    assert client.get("/api/civic/place?subject=%20%20").status_code == 400


def test_the_place_endpoint_is_off_when_civic_is(client, monkeypatch):
    monkeypatch.setenv("CIVIC_ENABLED", "0")
    assert client.get("/api/civic/place?subject=Lincoln").status_code == 503


# --------------------------------------------------------------------------
# The stack of governments over a point
# --------------------------------------------------------------------------

from backend import civic_geo  # noqa: E402

_CENSUS = {
    "Congressional Districts": [{"NAME": "Congressional District 12", "GEOID": "0612"}],
    "State Legislative Districts - Upper Chamber": [{"NAME": "State Senate District 7"}],
    "State Legislative Districts - Lower Chamber": [{"NAME": "Assembly District 18"}],
    "Counties": [{"NAME": "Alameda County"}],
    "Incorporated Places": [{"NAME": "Oakland"}],
    "Unified School Districts": [{"NAME": "Oakland Unified School District"}],
    "Census Tracts": [{"NAME": "Census Tract 4030"}],
}

_OSM = [
    {"type": "relation", "id": 77, "tags": {"boundary": "political",
     "political_division": "city_council", "name": "Council District 3"}},
    {"type": "relation", "id": 12, "tags": {"boundary": "administrative",
     "admin_level": "8", "name": "Oakland"}},
    {"type": "way", "id": 9, "tags": {"landuse": "residential"}},
]


def _stack(monkeypatch, census=None, osm=None):
    civic_geo._CACHE.clear()
    monkeypatch.setattr(civic_geo, "census_geographies",
                        lambda lat, lon: census if census is not None else _CENSUS)
    monkeypatch.setattr(civic_geo, "osm_areas",
                        lambda lat, lon: osm if osm is not None else _OSM)
    return civic_geo.stack(37.8044, -122.2712)


def test_the_stack_names_every_layer_closest_to_home_first(monkeypatch):
    found = _stack(monkeypatch)
    assert [l["key"] for l in found["layers"]] == [
        "landuse", "council", "place", "state_lower", "state_upper", "congress"]
    by_key = {l["key"]: l for l in found["layers"]}
    assert by_key["congress"]["name"] == "Congressional District 12"
    assert by_key["council"]["name"] == "Council District 3"      # OSM only
    assert by_key["state_upper"]["name"] == "State Senate District 7"
    # The school and county lenses were removed ; a geocoder that still names
    # them must not put them back on the rail.
    assert "school" not in by_key and "county" not in by_key


def test_every_layer_says_what_it_decides(monkeypatch):
    for layer in _stack(monkeypatch)["layers"]:
        assert layer["decides"], layer["key"]
        assert layer["elected"]
        assert layer["color"].startswith("#")


def test_the_council_district_carries_the_relation_the_outline_needs(monkeypatch):
    council = next(l for l in _stack(monkeypatch)["layers"] if l["key"] == "council")
    assert council["relation"] == 77
    assert council["source"] == "openstreetmap"


def test_land_use_says_plainly_that_it_is_not_the_zoning_code(monkeypatch):
    landuse = next(l for l in _stack(monkeypatch)["layers"] if l["key"] == "landuse")
    assert "not the legal zoning code" in landuse["detail"]


def test_one_source_failing_still_names_what_the_other_knows(monkeypatch):
    civic_geo._CACHE.clear()

    def boom(lat, lon):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(civic_geo, "census_geographies", boom)
    monkeypatch.setattr(civic_geo, "osm_areas", lambda lat, lon: _OSM)
    found = civic_geo.stack(37.8, -122.2)
    assert [l["key"] for l in found["layers"]] == ["landuse", "council", "place"]
    assert found["sources"]["census"] == "RuntimeError"


def test_the_question_speaks_the_layer_s_own_vocabulary(monkeypatch):
    by_key = {l["key"]: l for l in _stack(monkeypatch)["layers"]}
    council = civic_geo.question_for(by_key["council"], "Oakland")
    assert "Council District 3" in council and "hearings" in council
    congress = civic_geo.question_for(by_key["congress"], "Oakland")
    assert "Congressional District 12" in congress and "Oakland" in congress


def test_the_jurisdictions_endpoint_returns_the_stack(client, monkeypatch):
    _stack(monkeypatch)
    civic_geo._CACHE.clear()
    r = client.get("/api/civic/jurisdictions?lat=37.8044&lon=-122.2712")
    assert r.status_code == 200
    assert {l["key"] for l in r.json()["layers"]} >= {"place", "congress"}


def test_the_jurisdictions_endpoint_rejects_a_point_off_the_earth(client):
    assert client.get("/api/civic/jurisdictions?lat=999&lon=0").status_code == 400


def test_a_census_named_district_is_outlined_by_looking_it_up(monkeypatch):
    calls = []

    def fake_outline(relation_id):
        calls.append(relation_id)
        return {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]]]}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"elements": [{"type": "relation", "id": 4242}]}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(civic_geo, "outline", fake_outline)
    shape = civic_geo.outline_by_name("Oakland Unified School District")
    assert calls == [4242]
    assert shape["type"] == "MultiLineString"


@pytest.mark.parametrize("bad", ["", "   ", 'name" with a quote', "back\\slash"])
def test_outline_by_name_refuses_anything_that_could_escape_the_query(bad):
    # The name goes inside an Overpass query string, so it is checked before
    # it gets there rather than escaped afterwards.
    assert civic_geo.outline_by_name(bad) == {}


# --------------------------------------------------------------------------
# The two lookups no synthesis replaces
# --------------------------------------------------------------------------

def test_each_layer_says_what_to_look_up_on_its_own_site(monkeypatch):
    by_key = {l["key"]: l for l in _stack(monkeypatch)["layers"]}
    assert "permit" in by_key["place"]["lookup"]
    assert "permit" in by_key["place"]["lookup"]
    assert "agenda" in by_key["council"]["lookup"]
    assert "bills" in by_key["congress"]["lookup"]


def test_a_body_s_official_site_comes_off_the_boundary_relation(monkeypatch):
    osm = [{"type": "relation", "id": 12,
            "tags": {"boundary": "administrative", "admin_level": "8",
                     "name": "Oakland", "website": "oaklandca.gov"}}]
    by_key = {l["key"]: l for l in _stack(monkeypatch, osm=osm)["layers"]}
    # Named by the Census, linked by OpenStreetMap, and the scheme is added.
    assert by_key["place"]["name"] == "Oakland"
    assert by_key["place"]["website"] == "https://oaklandca.gov"


def test_the_zoning_question_asks_for_the_section_not_the_vibe(monkeypatch):
    landuse = next(l for l in _stack(monkeypatch)["layers"] if l["key"] == "landuse")
    question = civic_geo.question_for(landuse, "1418 Ninth Street")
    assert "municipal code" in question
    assert "height, density, parking" in question


def test_the_prompt_tells_the_model_to_cite_the_ordinance():
    prompt = civic.system_prompt(retrieval=True)
    assert "cite the ORDINANCE" in prompt
    assert "17.13.040" in prompt          # the shape of a section citation
    assert "assessor" in prompt and "permit" in prompt


# --------------------------------------------------------------------------
# The selftest tells you what broke without telling everyone what we run
# --------------------------------------------------------------------------

def _seed_failure():
    civic.LAST_ERROR.clear()
    civic.LAST_ERROR.update({
        "boot": civic.BOOT_ID, "at": "2026-09-04T07:00:00Z", "type": "NotFoundError",
        "status": 404, "mode": "web_search", "model": "claude-sonnet-5",
        "message": "model: claude-sonnet-5 not found for account acct_12345",
    })


def test_an_anonymous_reader_gets_the_shape_of_a_failure_not_its_text(client):
    _seed_failure()
    body = client.get("/api/civic/selftest").json()
    # Enough to diagnose a broken deploy...
    assert body["last_error"]["type"] == "NotFoundError"
    assert body["last_error"]["status"] == 404
    # ...without the upstream text, which can carry accounts, quotas and URLs.
    assert "message" not in body["last_error"]
    assert "acct_12345" not in json.dumps(body)
    civic.LAST_ERROR.clear()


def test_an_operator_gets_the_whole_thing(client, monkeypatch):
    _seed_failure()
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
    body = client.get("/api/civic/selftest",
                      headers={"X-Admin-Token": "s3cret-token"}).json()
    assert "acct_12345" in body["last_error"]["message"]
    civic.LAST_ERROR.clear()


def test_a_wrong_token_is_treated_as_anonymous(client, monkeypatch):
    _seed_failure()
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
    body = client.get("/api/civic/selftest",
                      headers={"X-Admin-Token": "not-the-token"}).json()
    assert "message" not in body["last_error"]
    civic.LAST_ERROR.clear()


def test_no_admin_token_configured_means_nobody_is_an_operator(client, monkeypatch):
    _seed_failure()
    monkeypatch.setenv("ADMIN_TOKEN", "")
    body = client.get("/api/civic/selftest", headers={"X-Admin-Token": ""}).json()
    assert "message" not in body["last_error"]
    civic.LAST_ERROR.clear()


def test_the_probe_needs_the_token_because_it_makes_ten_outbound_calls(client, monkeypatch):
    called = []
    monkeypatch.setattr(civic_sources, "probe", lambda *a, **k: called.append(1) or {})
    body = client.get("/api/civic/selftest?probe=1").json()
    assert called == []
    assert "X-Admin-Token" in body["probe"]


def test_the_probe_runs_for_an_operator(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
    monkeypatch.setattr(civic_sources, "probe", lambda *a, **k: {"backends": {}})
    monkeypatch.setattr(civic_geo, "probe", lambda *a, **k: {"census": {}})
    body = client.get("/api/civic/selftest?probe=1",
                      headers={"X-Admin-Token": "s3cret-token"}).json()
    assert "backends" in body["probe"]


@pytest.mark.parametrize("bad", ['name" or true', "nul\x00byte", "back\\slash"])
def test_outline_by_name_rejects_anything_that_could_break_the_query(bad):
    assert civic_geo.outline_by_name(bad) == {}


def test_outline_by_name_flattens_whitespace_before_it_reaches_the_query(monkeypatch):
    # A newline never reaches the Overpass filter : it is collapsed to a space
    # by the normalisation, which is why it is not in the rejection list.
    sent = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"elements": []}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None, headers=None):
            sent.append(data["data"])
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    civic_geo.outline_by_name("Council\nDistrict 3")
    assert "\n" not in sent[0].split("[out:json]")[1]
    assert '"Council District 3"' in sent[0]


# --------------------------------------------------------------------------
# Why the rail was three chips long
# --------------------------------------------------------------------------

# The names the Census actually uses. The old matcher looked for the phrase
# "upper chamber", which appears in none of them, so both legislative layers
# were dropped even when the call succeeded.
_REAL_CENSUS = {
    "119th Congressional Districts": [{"NAME": "Congressional District 12"}],
    "2024 State Legislative Districts - Upper": [{"NAME": "State Senate District 7"}],
    "2024 State Legislative Districts - Lower": [{"NAME": "Assembly District 18"}],
    "Counties": [{"NAME": "Alameda County"}],
    "Incorporated Places": [{"NAME": "Oakland"}],
    "Unified School Districts": [{"NAME": "Oakland Unified School District"}],
    "Census Blocks": [{"NAME": "Block 1042"}],
}


def test_the_census_collections_are_matched_by_the_names_it_really_uses(monkeypatch):
    found = _stack(monkeypatch, census=_REAL_CENSUS, osm=[])
    by_key = {l["key"]: l["name"] for l in found["layers"]}
    assert by_key["congress"] == "Congressional District 12"
    assert by_key["state_upper"] == "State Senate District 7"
    assert by_key["state_lower"] == "Assembly District 18"



def test_a_pin_with_both_sources_gives_the_whole_rail(monkeypatch):
    found = _stack(monkeypatch, census=_REAL_CENSUS, osm=_OSM)
    # Eight chips: what a resident is actually governed by.
    assert len(found["layers"]) == 6
    assert found["sources"] == {"census": 4, "openstreetmap": 3}


def test_a_congressional_boundary_in_osm_is_not_filed_as_a_council_district(monkeypatch):
    osm = [
        {"type": "relation", "id": 5, "tags": {"boundary": "political",
         "political_division": "us_congress", "name": "CA-12"}},
        {"type": "relation", "id": 77, "tags": {"boundary": "political",
         "political_division": "city_council", "name": "Council District 3"}},
    ]
    by_key = {l["key"]: l["name"] for l in _stack(monkeypatch, census={}, osm=osm)["layers"]}
    assert by_key["congress"] == "CA-12"
    assert by_key["council"] == "Council District 3"


def _census_client(monkeypatch, responses):
    """Stand in for httpx, handing back one queued response per attempt."""
    sent = []

    class _Resp:
        def __init__(self, payload, status=200):
            self.status_code, self._payload = status, payload
            self.text = json.dumps(payload)

        def json(self): return self._payload

    class _Client:
        def __init__(self, **kw): self.timeout = kw.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            sent.append({"vintage": params.get("vintage"), "layers": params.get("layers"),
                         "timeout": self.timeout})
            payload = responses[min(len(sent) - 1, len(responses) - 1)]
            if isinstance(payload, Exception):
                raise payload
            return _Resp(payload, payload.pop("_status", 200))

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    return sent


def test_a_second_vintage_is_tried_when_the_first_is_rejected(monkeypatch):
    sent = _census_client(monkeypatch, [
        {"errors": ["Vintage Current_Current is not valid for benchmark ..."]},
        {"result": {"geographies": _REAL_CENSUS}},
    ])
    found = civic_geo.census_geographies(37.8, -122.2)
    assert [s["vintage"] for s in sent] == ["Current_Current", "Census2020_Current"]
    assert "Counties" in found
    # And it waits long enough for a service that is routinely slow.
    assert sent[0]["timeout"] >= 30


def test_the_geocoder_s_own_words_are_what_gets_reported(monkeypatch):
    _census_client(monkeypatch, [{"errors": ["Vintage is not valid"]}])
    with pytest.raises(RuntimeError) as exc:
        civic_geo.census_geographies(37.8, -122.2)
    # "Vintage is not valid" diagnoses this in one read ; "it failed" does not.
    assert "Vintage is not valid" in str(exc.value)


def test_the_layer_filter_is_dropped_as_a_last_resort(monkeypatch):
    sent = _census_client(monkeypatch, [
        {"errors": ["bad layers"]}, {"errors": ["bad layers"]},
        {"result": {"geographies": _REAL_CENSUS}},
    ])
    civic_geo.census_geographies(37.8, -122.2)
    assert [s["layers"] for s in sent] == ["all", "all", None]


# --------------------------------------------------------------------------
# A district you can shade, not just trace
# --------------------------------------------------------------------------

def _ways(*lines):
    return {"elements": [{"type": "relation", "members": [
        {"type": "way", "role": "outer",
         "geometry": [{"lon": x, "lat": y} for x, y in line]}
        for line in lines]}]}


def _overpass(monkeypatch, payload):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)


def test_scattered_ways_are_stitched_into_a_polygon(monkeypatch):
    # Overpass returns a boundary as an unordered pile of ways, in any
    # direction. Only a closed ring can be shaded.
    _overpass(monkeypatch, _ways(
        [(0, 0), (1, 0)],            # bottom
        [(1, 1), (0, 1)],            # top, pointing the other way
        [(1, 0), (1, 1)],            # right
        [(0, 1), (0, 0)],            # left
    ))
    shape = civic_geo.outline(77)
    assert shape["type"] == "MultiPolygon"
    ring = shape["coordinates"][0][0]
    assert ring[0] == ring[-1]                       # closed
    assert len(ring) >= 5


def test_a_half_received_boundary_stays_a_line(monkeypatch):
    # Closing this would invent an edge that does not exist.
    _overpass(monkeypatch, _ways([(0, 0), (1, 0)], [(5, 5), (6, 6)]))
    shape = civic_geo.outline(77)
    assert shape["type"] == "MultiLineString"


def test_two_separate_rings_both_survive(monkeypatch):
    _overpass(monkeypatch, _ways(
        [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
        [(5, 5), (6, 5), (6, 6), (5, 6), (5, 5)],
    ))
    shape = civic_geo.outline(77)
    assert shape["type"] == "MultiPolygon"
    assert len(shape["coordinates"]) == 2


def test_a_closed_enclave_does_not_stand_in_for_a_broken_outer_ring(monkeypatch):
    # The small ring closes ; the district's own edge did not arrive whole.
    # Filling in the enclave would be a confident drawing of the wrong area,
    # so the whole thing falls back to lines.
    _overpass(monkeypatch, _ways(
        [(9.0, 9.0), (9.1, 9.0), (9.1, 9.1), (9.0, 9.1), (9.0, 9.0)],
        [(x / 10, 0) for x in range(40)],
        [(4, y / 10) for y in range(40)],
    ))
    shape = civic_geo.outline(77)
    assert shape["type"] == "MultiLineString"


# --- the page itself ------------------------------------------------------
# The UI is one file, so a few of its promises are worth pinning: the lens
# has to paint the map, sit in a single row, and stop preaching.

def _page() -> str:
    return (pathlib.Path(__file__).resolve().parents[1]
            / "backend" / "civic_ui" / "index.html").read_text()


def test_the_chosen_lens_fills_the_district_in_its_own_colour():
    page = _page()
    assert '{id: "layer-fill", type: "fill", source: "layer"' in page
    assert '"fill-color": ["get", "color"]' in page


def test_the_lens_rail_is_one_row():
    page = _page()
    rail = page.split(".rail{", 1)[1].split("}", 1)[0]
    assert "flex-wrap:nowrap" in rail
    assert "overflow-x:auto" in rail          # 8 lenses still fit on a laptop


def test_the_page_does_not_preach_its_own_method():
    assert "Social posts last" not in _page()


# --- the Census's own shapes, and who sits in them -------------------------

class _Reply:
    def __init__(self, payload, status=200):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def _http(monkeypatch, handler):
    """Route every httpx GET through one handler(url, params) -> _Reply."""
    calls = []

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, params=None, headers=None):
            calls.append((url, dict(params or {})))
            return handler(url, dict(params or {}))

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    civic_geo._CACHE.clear()
    return calls


_TIGER_SERVICE = {"layers": [
    {"id": 3, "name": "Counties"},
    {"id": 54, "name": "Congressional Districts"},
    {"id": 60, "name": "State Legislative Districts - Upper"},
    {"id": 62, "name": "State Legislative Districts - Lower"},
    {"id": 24, "name": "Unified School Districts"},
    {"id": 25, "name": "Elementary School Districts"},
]}
_SQUARE = {"type": "MultiPolygon",
           "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]]}


def test_the_census_district_is_drawn_from_the_census_own_shape(monkeypatch):
    def handler(url, params):
        if params.get("f") == "json":
            return _Reply(_TIGER_SERVICE)
        assert "/54/query" in url                  # Congressional Districts
        assert params["where"] == "GEOID='0612'"
        return _Reply({"features": [{"geometry": _SQUARE}]})

    _http(monkeypatch, handler)
    assert civic_geo.outline_by_geoid("congress", "0612") == _SQUARE


@pytest.mark.parametrize("bad", ["06'12", "0612 OR 1=1", "abc", "", "1" * 21])
def test_a_geoid_that_could_escape_the_query_never_reaches_the_network(monkeypatch, bad):
    def handler(url, params):
        raise AssertionError("must not call TIGERweb with " + repr(bad))

    _http(monkeypatch, handler)
    assert civic_geo.outline_by_geoid("congress", bad) == {}


def test_a_lens_with_no_tigerweb_layer_asks_nobody(monkeypatch):
    # The school and county lenses are gone, so a stale caller naming one must
    # not fall through to some other layer's geometry.
    def handler(url, params):
        if params.get("f") == "json":
            return _Reply(_TIGER_SERVICE)
        raise AssertionError("queried a layer for a lens that does not exist")

    _http(monkeypatch, handler)
    assert civic_geo.outline_by_geoid("school", "0612345") == {}
    assert civic_geo.outline_by_geoid("county", "06081") == {}


def test_a_tigerweb_error_body_is_raised_rather_than_drawn(monkeypatch):
    def handler(url, params):
        if params.get("f") == "json":
            return _Reply(_TIGER_SERVICE)
        return _Reply({"error": {"message": "Invalid where clause"}})

    _http(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        civic_geo._tiger_query(54, "0612")


def test_congress_members_come_back_with_their_seat(monkeypatch):
    def handler(url, params):
        assert params["state"] == "CA"
        if params["role_type"] == "representative":
            assert params["district"] == 12
            return _Reply({"objects": [{"title_long": "Representative", "party": "Democrat",
                                        "startdate": "2023-01-03",
                                        "person": {"name": "A. Rep",
                                                   "link": "https://www.govtrack.us/x"}}]})
        return _Reply({"objects": [{"title_long": "Senator", "party": "Democrat",
                                    "person": {"name": "B. Sen"}}]})

    _http(monkeypatch, handler)
    people = civic_geo.congress_members("0612")
    assert [p["name"] for p in people] == ["A. Rep", "B. Sen"]
    assert people[0]["since"] == "2023-01-03"
    assert people[0]["source"] == "govtrack"


@pytest.mark.parametrize("bad", ["", "06", "0612345", "CA12"])
def test_a_congress_geoid_that_is_not_a_district_asks_nobody(monkeypatch, bad):
    _http(monkeypatch, lambda url, params: (_ for _ in ()).throw(AssertionError("called")))
    assert civic_geo.congress_members(bad) == []


def test_state_legislators_split_into_their_two_chambers(monkeypatch):
    monkeypatch.setenv("OPENSTATES_API_KEY", "k")
    _http(monkeypatch, lambda url, params: _Reply({"results": [
        {"name": "U. Senator", "party": "Green",
         "current_role": {"org_classification": "upper", "title": "Senator", "district": "9"}},
        {"name": "L. Member", "party": "Green",
         "current_role": {"org_classification": "lower", "title": "Assemblymember", "district": "18"}},
        {"name": "Nobody", "current_role": {"org_classification": "executive"}},
    ]}))
    found = civic_geo.state_legislators(37.8, -122.27)
    assert found["state_upper"][0]["name"] == "U. Senator"
    assert found["state_lower"][0]["role"] == "Assemblymember, district 18"
    assert "executive" not in str(found)


def test_without_an_openstates_key_the_card_is_told_why(monkeypatch):
    monkeypatch.delenv("OPENSTATES_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENSTATES"):
        civic_geo.state_legislators(37.8, -122.27)


def test_one_broken_roster_does_not_take_the_other_down(monkeypatch):
    monkeypatch.setenv("OPENSTATES_API_KEY", "k")

    def handler(url, params):
        if "openstates" in url:
            return _Reply({}, status=503)
        return _Reply({"objects": [{"title_long": "Representative",
                                    "person": {"name": "A. Rep"}}]})

    _http(monkeypatch, handler)
    people = civic_geo.officials(37.8, -122.27, "0612")
    assert people["by_layer"]["congress"][0]["name"] == "A. Rep"
    assert people["sources"]["openstates"] == "RuntimeError"
    assert "state_upper" not in people["by_layer"]


def test_the_outline_route_prefers_the_census_shape_and_says_so(client, monkeypatch):
    monkeypatch.setattr(civic_geo, "outline_by_geoid", lambda *a, **k: _SQUARE)
    monkeypatch.setattr(civic_geo, "outline",
                        lambda *a: (_ for _ in ()).throw(AssertionError("asked OSM")))
    body = client.get("/api/civic/outline?layer=congress&geoid=0612&relation=77").json()
    assert body["geometry"] == _SQUARE
    assert body["basis"] == "census" and body["exact"] is True
    assert "Census" in body["note"]


def test_a_name_match_is_drawn_but_never_called_the_district(client, monkeypatch):
    monkeypatch.setattr(civic_geo, "outline_by_geoid", lambda *a, **k: {})
    monkeypatch.setattr(civic_geo, "outline_by_name", lambda name: _SQUARE)
    body = client.get("/api/civic/outline?layer=congress&geoid=0612"
                      "&name=Congressional+District+12").json()
    assert body["geometry"] == _SQUARE
    assert body["basis"] == "name" and body["exact"] is False
    assert "may be a different" in body["note"]


def test_a_half_received_relation_is_reported_as_a_fragment(client, monkeypatch):
    monkeypatch.setattr(civic_geo, "outline",
                        lambda r: {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]]]})
    body = client.get("/api/civic/outline?relation=77").json()
    assert body["basis"] == "partial" and body["exact"] is False
    assert "part of this boundary" in body["note"]


def test_a_source_that_throws_falls_through_to_the_next_one(client, monkeypatch):
    monkeypatch.setattr(civic_geo, "outline_by_geoid",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    monkeypatch.setattr(civic_geo, "outline", lambda r: _SQUARE)
    body = client.get("/api/civic/outline?layer=county&geoid=06001&relation=77").json()
    assert body["basis"] == "relation" and body["exact"] is True


def test_no_source_has_a_shape_and_the_map_is_told_that_plainly(client, monkeypatch):
    monkeypatch.setattr(civic_geo, "outline_by_geoid", lambda *a, **k: {})
    monkeypatch.setattr(civic_geo, "outline_by_name", lambda name: {})
    body = client.get("/api/civic/outline?layer=congress&geoid=0612&name=X").json()
    assert body["geometry"] == {} and body["basis"] == "" and body["exact"] is False


def test_officials_route_rejects_a_point_that_is_not_on_earth(client):
    assert client.get("/api/civic/officials?lat=999&lon=0").status_code == 400


def test_every_census_lens_has_a_tigerweb_layer_to_ask(monkeypatch):
    # A lens the Census can name but TIGERweb is never asked for is a lens
    # that silently falls back to a name guess.
    named = set(civic_geo._CENSUS_KEYS) - {"place"}
    assert named <= set(civic_geo._TIGER_KEYS)


def test_the_layer_card_carries_no_boilerplate_about_the_kind_of_body():
    # "Federal tax law, Medicare, housing vouchers" is true of the U.S. House
    # in every district in the country, so it tells a reader who dropped a pin
    # nothing. The card carries the seat, the record and the coverage instead.
    page = _page()
    assert "/api/civic/officials?lat=" in page
    assert "Who holds this seat" in page
    assert "What it decides" not in page
    assert "Look this up yourself" not in page
    assert "card-acts" in page                        # the votes go here


def test_an_estimated_outline_is_drawn_differently_from_a_surveyed_one():
    page = _page()
    fill = page.split('{id: "layer-fill"', 1)[1].split("}}", 1)[0]
    assert '["get", "exact"]' in fill                 # faint when not exact
    assert "line-dasharray" in page                   # and dashed


def test_a_members_recent_roll_calls_come_back_with_dates_and_links(monkeypatch):
    _http(monkeypatch, lambda url, params: _Reply({"objects": [
        {"option": {"value": "Yea"},
         "vote": {"question": "On Passage: H.R. 1", "result": "Passed",
                  "created": "2026-02-14T19:03:00Z",
                  "link": "https://www.govtrack.us/congress/votes/1"}},
        {"option": {"value": "Nay"}, "vote": {"question": ""}},   # no question, dropped
    ]}))
    votes = civic_geo.recent_votes(400001)
    assert len(votes) == 1
    assert votes[0] == {"how": "Yea", "what": "On Passage: H.R. 1",
                        "result": "Passed", "date": "2026-02-14",
                        "url": "https://www.govtrack.us/congress/votes/1"}


@pytest.mark.parametrize("bad", [0, -3])
def test_a_member_with_no_id_is_not_asked_for_votes(monkeypatch, bad):
    _http(monkeypatch, lambda url, params: (_ for _ in ()).throw(AssertionError("called")))
    assert civic_geo.recent_votes(bad) == []


def test_a_lens_with_no_record_at_all_says_nothing(monkeypatch):
    _http(monkeypatch, lambda url, params: (_ for _ in ()).throw(AssertionError("called")))
    assert civic_geo.activity("landuse", 400001, "Oakland, CA") == {
        "votes": [], "meetings": [], "matters": [], "source": ""}


def test_a_council_with_no_place_asks_nobody(monkeypatch):
    _http(monkeypatch, lambda url, params: (_ for _ in ()).throw(AssertionError("called")))
    assert civic_geo.activity("council", 0, "")["source"] == ""


def test_a_broken_vote_feed_is_an_empty_record_not_a_failed_card(monkeypatch):
    _http(monkeypatch, lambda url, params: _Reply({}, status=500))
    got = civic_geo.activity("congress", 400001)
    assert got["votes"] == [] and got["error"] == "RuntimeError"


# --- Overpass is one volunteer host away from taking the rail with it -------

def _overpass_hosts(monkeypatch, outcome):
    """outcome(host) -> a payload dict, or an exception to raise."""
    seen = []

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, data=None, headers=None):
            host = url.split("/")[2]
            seen.append(host)
            result = outcome(host)
            if isinstance(result, Exception):
                raise result
            return _Reply(result)

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    civic_geo._OVERPASS_FIRST = 0
    civic_geo._CACHE.clear()
    return seen


def test_a_refused_overpass_host_moves_on_to_the_next_mirror(monkeypatch):
    seen = _overpass_hosts(monkeypatch, lambda host: (
        ConnectionError("refused") if "overpass-api.de" in host else {"elements": [1]}))
    assert civic_geo.overpass("x") == {"elements": [1]}
    assert len(seen) == 2 and seen[0] == "overpass-api.de"


def test_a_hung_overpass_host_moves_on_too(monkeypatch):
    # The live failure was a read timeout, not a refusal: a busy Overpass
    # hangs rather than saying no.
    seen = _overpass_hosts(monkeypatch, lambda host: (
        TimeoutError("read timeout") if "overpass-api.de" in host else {"elements": []}))
    civic_geo.overpass("x")
    assert len(seen) == 2


def test_the_mirror_that_answered_is_the_one_asked_first_next_time(monkeypatch):
    seen = _overpass_hosts(monkeypatch, lambda host: (
        ConnectionError("refused") if "overpass-api.de" in host else {"elements": []}))
    civic_geo.overpass("x")
    civic_geo.overpass("x")
    assert seen[2] == seen[1]            # straight to the host that worked
    assert len(seen) == 3                # the dead host is not re-tried


def test_every_mirror_down_is_reported_as_the_last_failure(monkeypatch):
    _overpass_hosts(monkeypatch, lambda host: ConnectionError("refused"))
    with pytest.raises(ConnectionError):
        civic_geo.overpass("x")


def test_the_mirror_chain_is_capped_so_the_rail_still_loads(monkeypatch):
    # Four hosts hanging for the full per-mirror wait is a card that never
    # arrives, so the budget stops the chain part way.
    assert (civic_geo.OVERPASS_TIMEOUT_S * len(civic_geo.OVERPASS_URLS)
            > civic_geo.OVERPASS_BUDGET_S)
    assert civic_geo.OVERPASS_BUDGET_S <= 30


def test_both_congress_asks_go_out_at_once(monkeypatch):
    # Asking GovTrack for the representative and then the senators in series
    # is two round trips of latency on the thing the card leads with.
    import time as _t
    started, finished = [], []

    def handler(url, params):
        started.append(_t.monotonic())
        _t.sleep(0.25)
        finished.append(_t.monotonic())
        return _Reply({"objects": []})

    _http(monkeypatch, handler)
    civic_geo.congress_members("0612")
    assert len(started) == 2
    assert started[1] < finished[0]          # the second began before the first ended


def test_the_roster_is_on_a_short_leash():
    # The card leads with this ; a twelve-second wait reads as broken.
    assert civic_geo.ROSTER_TIMEOUT_S <= 8


def test_the_roster_starts_when_the_pin_lands_not_when_a_card_opens():
    page = _page()
    stack_fn = page.split("async function loadStack(", 1)[1].split("\nfunction ", 1)[0]
    assert "loadSeats()" in stack_fn


def test_a_body_with_no_national_roster_is_pointed_at_its_own_page():
    page = _page()
    for key in ("place", "council"):
        assert '  ' + key + ':' in page.split("const NO_ROSTER", 1)[1][:900]
    assert 'link(layer.website, "Members' in page


# --- state legislators without a key ---------------------------------------

_ROSTER_CSV = (
    "id,name,current_party,current_district,current_chamber,openstates_url\n"
    "ocd-person/1,U. Senator,Democratic,11,upper,https://openstates.org/person/1/\n"
    "ocd-person/2,L. Member,Republican,18,lower,https://openstates.org/person/2/\n"
    "ocd-person/3,Someone Else,Democratic,12,upper,https://openstates.org/person/3/\n"
)


def _csv(monkeypatch, text, status=200):
    class _Resp:
        status_code = status
        content = text.encode()

        def __init__(self):
            self.text = text

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    civic_geo._CACHE.clear()


def test_state_legislators_are_named_without_any_api_key(monkeypatch):
    monkeypatch.delenv("OPENSTATES_API_KEY", raising=False)
    _csv(monkeypatch, _ROSTER_CSV)
    found = civic_geo.state_legislators_keyless("CA", "06011", "06018")
    assert found["state_upper"][0]["name"] == "U. Senator"
    assert found["state_upper"][0]["role"] == "Senator, district 11"
    assert found["state_lower"][0]["name"] == "L. Member"
    assert len(found["state_upper"]) == 1              # district 12 is not ours


@pytest.mark.parametrize("geoid,want", [
    ("06011", "11"), ("06001", "1"), ("06110", "110"), ("", ""), ("06", ""),
])
def test_the_district_number_is_read_out_of_the_geoid(geoid, want):
    assert civic_geo._district_of(geoid) == want


def test_a_lettered_district_survives_the_geoid(monkeypatch):
    # Alaska letters its house districts ; stripping to an int would lose it.
    assert civic_geo._district_of("02A") == ""          # not digits, not a geoid
    _csv(monkeypatch, "name,current_party,current_district,current_chamber\n"
                      "A. Member,Independent,0A,upper\n")
    found = civic_geo.state_legislators_keyless("VT", "50" + "0A0"[:3], "")
    assert found == {} or "state_upper" in found        # never raises


def test_an_enormous_download_is_not_parsed_as_a_roster(monkeypatch):
    _csv(monkeypatch, "x" * 4_000_001)
    with pytest.raises(RuntimeError, match="too large"):
        civic_geo.state_roster("ca")


def test_the_keyless_roster_is_tried_before_the_keyed_one(monkeypatch):
    monkeypatch.setenv("OPENSTATES_API_KEY", "k")
    monkeypatch.setattr(civic_geo, "state_legislators",
                        lambda *a: (_ for _ in ()).throw(AssertionError("used the key")))
    _csv(monkeypatch, _ROSTER_CSV)
    found = civic_geo._state_seats(37.8, -122.27, "0612", "06011", "06018")
    assert found["state_upper"][0]["name"] == "U. Senator"


# --- the console stopped narrating its own machinery ------------------------

def test_the_search_reports_one_line_not_a_six_step_checklist():
    page = _page()
    assert "status-steps" not in page
    assert "Reading what is live in" not in page
    assert "Sorting it: what's settled" not in page


def test_an_error_is_not_buried_under_the_suggestions():
    # It answers the question you just asked ; it was sitting below five
    # suggested questions inside a box that scrolls.
    page = _page()
    assert page.index('id="error"') < page.index('class="chips"')
    assert page.index('id="status"') < page.index('class="chips"')


def test_the_two_rosters_agree_on_which_chamber_is_which(monkeypatch):
    # Both paths map chamber -> lens from the same table, so a row from either
    # source lands on the same lens.
    monkeypatch.setenv("OPENSTATES_API_KEY", "k")
    _http(monkeypatch, lambda url, params: _Reply({"results": [
        {"name": "U. Senator",
         "current_role": {"org_classification": "upper", "title": "Senator", "district": "11"}}]}))
    keyed = civic_geo.state_legislators(37.8, -122.27)
    _csv(monkeypatch, _ROSTER_CSV)
    keyless = civic_geo.state_legislators_keyless("CA", "06011", "")
    assert set(keyed) == set(keyless) == {"state_upper"}


def test_a_chamber_that_is_neither_house_is_dropped(monkeypatch):
    _csv(monkeypatch, "name,current_party,current_district,current_chamber\n"
                      "A. Governor,Democratic,11,executive\n")
    assert civic_geo.state_legislators_keyless("CA", "06011", "06011") == {}


def test_the_box_you_type_into_never_scrolls():
    # A scrollbar on the thing you just used is the interface hiding its own
    # output: the answer was below five suggestions inside a scrolling box.
    page = _page()
    console = page.split("\n  .console{", 1)[1].split("}", 1)[0]
    assert "max-height" not in console
    body = page.split("\n  .console-body{", 1)[1].split("}", 1)[0]
    assert "overflow" not in body


def test_no_scrollbar_is_drawn_over_the_card_either():
    page = _page()
    card = page.split("\n  .card{", 1)[1].split("}", 1)[0]
    assert "scrollbar-width:none" in card
    assert ".card::-webkit-scrollbar{display:none}" in page
    assert ".card.more{" in page          # the fade still says there is more


def test_three_suggestions_not_five():
    page = _page()
    static = page.split('class="chips" id="chips"', 1)[1].split("</div>", 1)[0]
    assert static.count("<button") == 3
    built = page.split("function placeChips(", 1)[1].split("];", 1)[0]
    assert built.count('short + "?"') == 3


# --- the two lenses that were removed ---------------------------------------

def test_the_school_and_county_lenses_are_gone_everywhere():
    # A lens half-removed is worse than either state: a chip with no question,
    # or a question for a chip nobody can reach.
    for table in (civic_geo.LAYERS, civic_geo.LOOKUPS, civic_geo._CENSUS_KEYS,
                  civic_geo._TIGER_KEYS):
        assert "school" not in table and "county" not in table
    assert "school" not in civic_geo.LAYER_ORDER
    assert "county" not in civic_geo.LAYER_ORDER
    page = _page()
    asks = page.split("const LAYER_ASKS", 1)[1].split("};", 1)[0]
    assert "\n  school:" not in asks and "\n  county:" not in asks
    pins = page.split("const LAYER_PINS", 1)[1].split("};", 1)[0]
    assert "\n  school:" not in pins and "\n  county:" not in pins


def test_every_lens_on_the_rail_has_a_question_and_a_pin_rule():
    page = _page()
    asks = page.split("const LAYER_ASKS", 1)[1].split("};", 1)[0]
    pins = page.split("const LAYER_PINS", 1)[1].split("};", 1)[0]
    for key in civic_geo.LAYER_ORDER:
        assert key + ":" in asks or key + ":" in pins, key
        assert key + ":" in pins, key


def test_the_school_and_hospital_pins_survive_under_the_city_lens():
    # They are places you can tap, not lenses. Removing the lenses must not
    # take the buildings off the map.
    page = _page()
    pins = page.split("const LAYER_PINS", 1)[1].split("};", 1)[0]
    place = pins.split("place:", 1)[1].split("]", 1)[0]
    assert '"school"' in place and '"hospital"' in place


# --- a thin answer says whose fault it is -----------------------------------

def test_an_out_of_credit_search_account_is_named_as_such(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "x")
    monkeypatch.setattr(civic_sources, "LAST_EXA_STATUS", "HTTP 402")
    monkeypatch.setattr(civic_sources, "_exa_paused_until", time.time() + 60)
    assert civic_sources.exa_degraded() == "the search account is out of credit"


def test_a_rejected_key_is_not_reported_as_an_empty_record(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "x")
    monkeypatch.setattr(civic_sources, "LAST_EXA_STATUS", "HTTP 401")
    monkeypatch.setattr(civic_sources, "_exa_paused_until", time.time() + 60)
    assert "rejected our key" in civic_sources.exa_degraded()


def test_a_working_search_says_nothing(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "x")
    monkeypatch.setattr(civic_sources, "LAST_EXA_STATUS", "")
    monkeypatch.setattr(civic_sources, "_exa_paused_until", 0.0)
    assert civic_sources.exa_degraded() == ""


def test_only_the_degraded_note_reaches_the_reader():
    # The rest of the notes are counters for the log, and one of them carries
    # upstream error text.
    from backend.routes import civic as routes
    assert routes._reader_notes({"latency_ms": 900, "search_errors": ["boom"],
                                 "sources_found": 3}) == {}
    assert routes._reader_notes({"search_degraded": "out of credit",
                                 "search_errors": ["boom"]}) == {
        "search_degraded": "out of credit"}


def test_the_page_blames_the_search_not_the_record():
    page = _page()
    assert "search_degraded" in page
    assert "ran short-handed" in page
    assert "not that there is" in page          # ... nothing to find


# --- the page answers on every host we own ----------------------------------
# `_is_landing_host` peels the apex and www off to the marketing landing, but
# only inside the SPA catch-all. /civic is a router route mounted above it, so
# it must survive on those hosts too -- that is what makes surpluslayer.com/civic
# work the moment the apex is pointed at this app.

@pytest.mark.parametrize("host", [
    "surpluslayer.com", "www.surpluslayer.com", "event.surpluslayer.com",
    "join.surpluslayer.com", "surplus-production.up.railway.app",
])
def test_the_civic_page_is_served_on_every_host(client, host):
    r = client.get("/civic", headers={"host": host})
    assert r.status_code == 200
    assert "Civic" in r.text


@pytest.mark.parametrize("host", ["surpluslayer.com", "www.surpluslayer.com"])
def test_the_civic_api_survives_the_landing_hosts(client, host):
    # The landing shell must never shadow the API the page calls.
    r = client.get("/api/civic/selftest", headers={"host": host})
    assert r.status_code == 200
    assert "boot" in r.json()


# --- nothing paid happens without a press -----------------------------------

def test_dropping_a_pin_no_longer_fires_a_search():
    # Every settle used to be a paid question: dragging the pin across a city
    # billed one per stop, and nobody had asked for any of them.
    page = _page()
    assert "scheduleBrief" not in page
    assert "briefTimer" not in page
    offer = page.split("function offerBrief(", 1)[1].split("\n}", 1)[0]
    assert "ask(" not in offer
    assert "setQuestion(question)" in offer


def test_the_pin_still_writes_the_question_it_raises():
    # Turning the search off must not leave the reader with an empty box and
    # nothing to press.
    page = _page()
    assert "const briefQuestion" in page
    assert "offerBrief(name)" in page


def test_a_typed_question_is_never_overwritten_by_a_later_pin():
    page = _page()
    offer = page.split("function offerBrief(", 1)[1].split("\n}", 1)[0]
    assert "typed !== state.offered" in offer          # ours may be replaced
    assert 'state.offered = ""' in page                # editing claims the box


def test_the_only_searches_left_are_presses_and_permalinks():
    page = _page()
    # Three: the lens card button, the site card button, and re-answering a
    # permalink this replica has no cached answer for.
    assert page.count("ask({question:") == 3
    site = page.split("function askAboutSite(", 1)[1].split("\n}", 1)[0]
    assert "if (andSearch) ask(" in site               # tapping the map does not


# --- the council agenda, straight from the clerk ----------------------------
# No search, no model: Legistar publishes the calendar and the legislation as
# JSON, and a large share of US city councils run it.

_EVENTS = [{"EventBodyName": "City Council", "EventDate": "2026-09-15T00:00:00",
            "EventTime": "5:30 PM", "EventLocation": "Council Chamber",
            "EventInSiteURL": "https://oakland.legistar.com/MeetingDetail.aspx?ID=1"}]
_MATTERS = [{"MatterFile": "26-0412", "MatterTitle": "Ordinance amending the rent board fee",
             "MatterTypeName": "Ordinance", "MatterStatusName": "In Committee",
             "MatterIntroDate": "2026-09-02T00:00:00"}]


def _legistar(monkeypatch, by_path, seen=None):
    def handler(url, params):
        path = url.rsplit("/", 1)[-1]
        if seen is not None:
            seen.append((url, dict(params)))
        return _Reply(by_path.get(path, []))

    return _http(monkeypatch, handler)


def test_the_council_card_reads_the_clerks_own_calendar(monkeypatch):
    _legistar(monkeypatch, {"bodies": [{"BodyId": 1}], "events": _EVENTS,
                            "matters": _MATTERS})
    got = civic_geo.council_agenda("Oakland")
    assert got["source"] == "legistar" and got["client"] == "oakland"
    assert got["meetings"][0]["body"] == "City Council"
    assert got["meetings"][0]["when"] == "2026-09-15 5:30 PM"
    assert got["matters"][0]["file"] == "26-0412"
    assert "rent board fee" in got["matters"][0]["what"]


def test_only_meetings_that_have_not_happened_yet_are_asked_for(monkeypatch):
    seen = []
    _legistar(monkeypatch, {"bodies": [{"BodyId": 1}], "events": [], "matters": []}, seen)
    civic_geo.council_agenda("Oakland")
    events = [p for url, p in seen if url.endswith("/events")][0]
    assert events["$filter"].startswith("EventDate ge datetime'")
    assert events["$orderby"] == "EventDate"


def test_a_city_that_is_not_on_legistar_is_not_probed_twice(monkeypatch):
    seen = []
    _legistar(monkeypatch, {}, seen)                 # every path 404s to []
    assert civic_geo.legistar_client("Nowheresville") == ""
    before = len(seen)
    assert civic_geo.legistar_client("Nowheresville") == ""
    assert len(seen) == before                       # the miss was cached


@pytest.mark.parametrize("city,first", [
    ("Oakland", "oakland"), ("San Francisco", "sfgov"), ("New York", "nyc"),
    ("St. Paul", "stpaul"), ("Ann Arbor", "annarbor"),
])
def test_the_client_name_is_guessed_or_known(city, first):
    assert civic_geo._slug_candidates(city)[0] == first


@pytest.mark.parametrize("bad", ["../admin", "oak land", "a" * 41, "oakland/x", ""])
def test_a_client_name_that_could_escape_the_url_is_refused(monkeypatch, bad):
    _http(monkeypatch, lambda url, params: (_ for _ in ()).throw(AssertionError("called")))
    with pytest.raises(ValueError):
        civic_geo._legistar(bad, "events", {})


def test_the_city_is_taken_off_the_front_of_the_place():
    assert civic_geo._city_of("Oakland, California, US") == "Oakland"
    assert civic_geo._city_of("  Oakland  ") == "Oakland"
    assert civic_geo._city_of("") == ""


def test_the_agenda_reaches_the_card_without_a_person(monkeypatch):
    _legistar(monkeypatch, {"bodies": [{"BodyId": 1}], "events": _EVENTS,
                            "matters": _MATTERS})
    got = civic_geo.activity("council", 0, "Oakland, California, US")
    assert got["meetings"] and got["source"] == "legistar"
    assert got["votes"] == []


def test_the_card_asks_for_an_agenda_even_with_nobody_named():
    page = _page()
    acts = page.split("function fillActs(", 1)[1].split("\n}", 1)[0]
    assert '["council", "place"].includes(layer.key)' in acts
    assert "&place=" in acts
    assert "Next meetings" in acts and "Newest on the docket" in acts


def test_the_agenda_does_not_wait_for_a_roster_that_will_never_come():
    # A council has no national roster and is exactly the lens whose agenda
    # is published, so hanging the record off "someone was named" hid it.
    page = _page()
    seats = page.split("function fillSeats(", 1)[1].split("\n}", 1)[0]
    before_branch = seats.split("if (people.length)", 1)[0]
    assert "fillActs(layer, people)" in before_branch


def test_a_broken_calendar_does_not_take_the_docket_with_it(monkeypatch):
    # Losing both because one endpoint errored reads as "nothing is happening
    # here", which is a claim about the council rather than about us.
    def handler(url, params):
        if url.endswith("/bodies"):
            return _Reply([{"BodyId": 1}])
        if url.endswith("/events"):
            return _Reply({}, status=500)
        return _Reply(_MATTERS)

    _http(monkeypatch, handler)
    got = civic_geo.council_agenda("Oakland")
    assert got["matters"] and got["meetings"] == []
    assert got["source"] == "legistar"
    assert any(p.startswith("meetings:") for p in got["partial"])


def test_a_city_whose_legistar_refuses_the_date_filter_still_gets_a_calendar(monkeypatch):
    # Not every Legistar accepts the same OData spelling of a date. A calendar
    # that loads beats a tidy query.
    future = "2099-01-01T00:00:00"
    asked = []

    def handler(url, params):
        if url.endswith("/bodies"):
            return _Reply([{"BodyId": 1}])
        if url.endswith("/events"):
            asked.append(params)
            if "$filter" in params:
                return _Reply({}, status=400)
            return _Reply([
                {"EventBodyName": "Board of Supervisors", "EventDate": future,
                 "EventTime": "2:00 PM"},
                {"EventBodyName": "Old Meeting", "EventDate": "2001-01-01T00:00:00"},
            ])
        return _Reply([])

    _http(monkeypatch, handler)
    got = civic_geo.council_agenda("San Francisco")
    assert [m["body"] for m in got["meetings"]] == ["Board of Supervisors"]
    assert len(asked) == 2 and "$filter" not in asked[1]


def test_a_starred_meeting_keeps_its_name(monkeypatch):
    # Legistar marks a special or amended meeting with a leading star.
    def handler(url, params):
        if url.endswith("/bodies"):
            return _Reply([{"BodyId": 1}])
        if url.endswith("/events"):
            return _Reply([{"EventBodyName": "*Rules & Legislation Committee",
                            "EventDate": "2099-01-01T00:00:00"}])
        return _Reply([])

    _http(monkeypatch, handler)
    got = civic_geo.council_agenda("Oakland")
    assert got["meetings"][0]["body"] == "Rules & Legislation Committee"


def test_a_city_on_legistar_with_nothing_to_show_is_not_called_a_source(monkeypatch):
    def handler(url, params):
        return _Reply([{"BodyId": 1}] if url.endswith("/bodies") else [])

    _http(monkeypatch, handler)
    assert civic_geo.council_agenda("Oakland")["source"] == ""
