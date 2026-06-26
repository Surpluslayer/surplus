"""
Unit tests for Exa discovery : parsing logic only, no network.

The HTTP path is gated by EXA_API_KEY and patched out so we never hit
api.exa.ai from CI / local test runs.
"""
from __future__ import annotations
import os
from unittest.mock import patch, MagicMock

from backend.agents import exa


# ---- availability ---------------------------------------------------------

def test_exa_unavailable_when_no_key(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert exa.exa_available() is False


def test_exa_available_with_key(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-key-test")
    assert exa.exa_available() is True


def test_exa_strips_whitespace(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "  exa-key-test\n")
    assert exa._api_key() == "exa-key-test"


# ---- parsing --------------------------------------------------------------

def test_parse_linkedin_title_full():
    name, role, company = exa._parse_linkedin_title(
        "Daniel Wang - Software Engineer at Acme | LinkedIn"
    )
    assert name == "Daniel Wang"
    assert role == "Software Engineer"
    assert company == "Acme"


def test_parse_linkedin_title_no_company():
    name, role, company = exa._parse_linkedin_title(
        "Jiahui Jin - ML Engineer | LinkedIn"
    )
    assert name == "Jiahui Jin"
    assert role == "ML Engineer"
    assert company == ""


def test_parse_linkedin_title_name_only():
    name, role, company = exa._parse_linkedin_title("Maya Rodriguez | LinkedIn")
    assert name == "Maya Rodriguez"
    assert role == ""
    assert company == ""


def test_parse_github_title_with_realname():
    assert exa._parse_github_title("danielwang (Daniel Wang) · GitHub") == "Daniel Wang"


def test_parse_github_title_handle_only():
    assert exa._parse_github_title("danielwang · GitHub") == ""


def test_parse_x_title():
    assert exa._parse_x_title("Daniel Wang (@daniel04wang) / X") == "Daniel Wang"
    assert exa._parse_x_title("Random Person (@rp) on X: 'thoughts'") == "Random Person"


# ---- result parsing -------------------------------------------------------

def test_parse_linkedin_result_extracts_handle():
    result = {
        "url": "https://www.linkedin.com/in/daniel04wang/",
        "title": "Daniel Wang - Software Engineer at Acme | LinkedIn",
        "text": "Profile text...",
    }
    cand = exa._parse_result("linkedin", result)
    assert cand is not None
    assert cand["identity"] == "daniel04wang"
    assert cand["name"] == "Daniel Wang"
    assert cand["role"] == "Software Engineer"
    assert cand["company"] == "Acme"
    assert cand["linkedin_url"] == "https://www.linkedin.com/in/daniel04wang"
    assert cand["contact_resolved"] is True


def test_parse_linkedin_result_skips_non_profile_urls():
    # LinkedIn search / company / posts shouldn't match /in/
    for bad_url in [
        "https://www.linkedin.com/company/acme/",
        "https://www.linkedin.com/posts/some-post",
        "https://www.linkedin.com/jobs/view/123",
    ]:
        cand = exa._parse_result("linkedin", {"url": bad_url, "title": "x"})
        assert cand is None, f"should not match: {bad_url}"


def test_parse_github_result_skips_orgs_and_special_paths():
    for special in ["search", "topics", "settings"]:
        cand = exa._parse_result("github", {
            "url": f"https://github.com/{special}",
            "title": f"{special} · GitHub",
        })
        assert cand is None


def test_parse_x_result_skips_special_paths():
    for special in ["home", "explore", "messages"]:
        cand = exa._parse_result("x", {
            "url": f"https://x.com/{special}",
            "title": "x",
        })
        assert cand is None


# ---- end-to-end with mocked HTTP ----------------------------------------

def test_discover_via_exa_filters_and_dedups(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "results": [
            {
                "url": "https://www.linkedin.com/in/maya-rodriguez/",
                "title": "Maya Rodriguez - Staff Infra Engineer at Lo91r | LinkedIn",
            },
            # duplicate handle : should be deduped
            {
                "url": "https://www.linkedin.com/in/maya-rodriguez",
                "title": "Maya Rodriguez | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/jiahui-jin/",
                "title": "Jiahui Jin - ML Engineer | LinkedIn",
            },
            # non-profile URL : should be filtered
            {
                "url": "https://www.linkedin.com/company/foo/",
                "title": "Foo Inc | LinkedIn",
            },
        ]
    }
    fake_client = MagicMock()
    fake_client.post.return_value = fake_response
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    with patch("httpx.Client", return_value=fake_client):
        out = exa.discover_via_exa("linkedin",
                                   {"role": "infra engineer", "seniority": "Senior"},
                                   max_candidates=5)

    assert len(out) == 2
    handles = {c["identity"] for c in out}
    assert handles == {"maya-rodriguez", "jiahui-jin"}

    # Verify the request body includes the LinkedIn-specific category filter
    call_kwargs = fake_client.post.call_args.kwargs
    body = call_kwargs["json"]
    assert body["category"] == "linkedin profile"
    assert body["includeDomains"] == ["linkedin.com"]
    assert body["type"] == "neural"


def test_discover_via_exa_category_per_source(monkeypatch):
    """Each source maps to the right Exa category label."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"results": []}
    fake_client = MagicMock()
    fake_client.post.return_value = fake_response
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    expected = {"linkedin": "linkedin profile", "github": "github"}
    for source, category in expected.items():
        with patch("httpx.Client", return_value=fake_client):
            exa.discover_via_exa(source, {"role": "engineer"})
        body = fake_client.post.call_args.kwargs["json"]
        assert body["category"] == category, f"source={source}"
    # x: Exa DEPRECATED the "tweet" category (it now 400s), so the x source
    # intentionally sends NO category -- neural match + includeDomains=x.com.
    with patch("httpx.Client", return_value=fake_client):
        exa.discover_via_exa("x", {"role": "engineer"})
    assert "category" not in fake_client.post.call_args.kwargs["json"]


def test_discover_via_exa_returns_empty_on_http_error(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "unauthorized"
    fake_client = MagicMock()
    fake_client.post.return_value = fake_response
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    with patch("httpx.Client", return_value=fake_client):
        out = exa.discover_via_exa("linkedin", {"role": "x"})
    assert out == []


def test_discover_via_exa_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert exa.discover_via_exa("linkedin", {"role": "x"}) == []


# ---- _build_query: city threading ----------------------------------------

def test_build_query_includes_city_when_present():
    q = exa._build_query("linkedin", {
        "role": "Infra engineer",
        "seniority": "Senior",
        "co_stage": "Seed",
        "city": "San Francisco",
    })
    assert "san francisco" in q
    # city should land after the stage clause so the natural reading is
    # "...at seed startups in san francisco"
    assert q.index("seed startups") < q.index("san francisco")


def test_build_query_omits_city_when_blank():
    q = exa._build_query("linkedin", {
        "role": "Infra engineer",
        "seniority": "Senior",
        "co_stage": "Seed",
        "city": "",
    })
    assert " in " not in q  # no trailing "in <city>" clause

    q_missing = exa._build_query("linkedin", {
        "role": "Infra engineer",
        "seniority": "Senior",
        "co_stage": "Seed",
    })
    assert " in " not in q_missing


def test_build_query_city_applies_to_all_sources():
    for src in ("linkedin", "github", "x"):
        q = exa._build_query(src, {"role": "engineer", "city": "Brooklyn"})
        assert "brooklyn" in q


# ---- city normalization --------------------------------------------------

def test_resolve_city_known_aliases_share_canonical():
    """SF / San Francisco / Bay Area all resolve to the same config."""
    a = exa._resolve_city("sf")
    b = exa._resolve_city("San Francisco")
    c = exa._resolve_city("bay area")
    assert a and b and c
    assert a["canonical_phrase"] == b["canonical_phrase"] == c["canonical_phrase"]
    assert a["include_text"] == "San Francisco"


def test_resolve_city_unknown_falls_back_to_raw():
    """Unknown cities still work : synthesize a config from the raw input."""
    cfg = exa._resolve_city("Tokyo")
    assert cfg is not None
    assert cfg["include_text"] == "Tokyo"
    assert "tokyo" in cfg["aliases"]


def test_resolve_city_empty_is_none():
    assert exa._resolve_city("") is None
    assert exa._resolve_city("   ") is None


def test_build_query_uses_canonical_phrase_for_known_city():
    """User types 'SF' → query should say 'the san francisco bay area'
    (LinkedIn pages literally use that phrase, so neural match is stronger)."""
    cfg = exa._resolve_city("sf")
    q = exa._build_query("linkedin", {"role": "engineer"}, cfg)
    assert "san francisco bay area" in q
    # raw "sf" alone should NOT appear as a standalone word
    assert " sf " not in q


# ---- _location_matches: post-filter --------------------------------------

def test_location_matches_returns_true_when_alias_present():
    snippet = "# Daniel\nSenior Engineer at Acme\nSan Francisco Bay Area\n"
    aliases = ("san francisco", "bay area")
    assert exa._location_matches(snippet, aliases) is True


def test_location_matches_returns_false_when_wrong_city_present():
    """NYC profile snuck through ranking : post-filter drops it."""
    snippet = "# Daniel\nSenior Engineer at Acme\nNew York, New York, United States (US)\n"
    aliases = ("san francisco", "bay area", "oakland")
    assert exa._location_matches(snippet, aliases) is False


def test_location_matches_returns_true_when_no_location_line():
    """No location signal at all → keep (can't disprove)."""
    snippet = "# Daniel\nSenior Engineer at Acme\nLoves rust and coffee\n"
    aliases = ("san francisco",)
    assert exa._location_matches(snippet, aliases) is True


def test_location_matches_empty_snippet_keeps():
    assert exa._location_matches("", ("san francisco",)) is True


# ---- _parse_result: city filter integration ------------------------------

def test_parse_result_drops_wrong_city_linkedin():
    cfg = exa._resolve_city("sf")
    nyc_result = {
        "url": "https://www.linkedin.com/in/someone",
        "title": "Some One - Engineer at Acme | LinkedIn",
        "text": "# Some One\nEngineer at Acme\nNew York, New York, United States (US)\n",
    }
    assert exa._parse_result("linkedin", nyc_result, cfg) is None


def test_parse_result_keeps_matching_city_linkedin():
    cfg = exa._resolve_city("sf")
    sf_result = {
        "url": "https://www.linkedin.com/in/someone",
        "title": "Some One - Engineer at Acme | LinkedIn",
        "text": "# Some One\nEngineer at Acme\nSan Francisco Bay Area\n",
    }
    cand = exa._parse_result("linkedin", sf_result, cfg)
    assert cand is not None
    assert cand["name"] == "Some One"


def test_parse_scholar_result_google_scholar():
    """Google Scholar profile URL : extract author user id + cited-by count."""
    result = {
        "url": "https://scholar.google.com/citations?user=ABCD1234&hl=en",
        "title": "Priya Natarajan - Google Scholar",
        "text": "Priya Natarajan\nML Platform Lead at Cohere\nCited by 1,240\n",
    }
    cand = exa._parse_result("scholar", result)
    assert cand is not None
    assert cand["name"] == "Priya Natarajan"
    assert cand["identity"] == "priya-natarajan"  # name-slug for cross-source merge
    assert cand["scholar_citations"] == 1240
    assert "scholar.google.com" in cand["scholar_url"]


def test_parse_scholar_result_semantic_scholar():
    result = {
        "url": "https://www.semanticscholar.org/author/Maya-Rodriguez/12345",
        "title": "Maya Rodriguez | Semantic Scholar",
        "text": "180 citations · h-index 7",
    }
    cand = exa._parse_result("scholar", result)
    assert cand is not None
    assert cand["name"] == "Maya Rodriguez"
    assert cand["identity"] == "maya-rodriguez"
    assert cand["scholar_citations"] == 180


def test_parse_scholar_result_arxiv():
    result = {
        "url": "https://arxiv.org/a/wang_d_1.html",
        "title": "Daniel Wang's arXiv author page",
        "text": "Recent submissions...",
    }
    cand = exa._parse_result("scholar", result)
    # No citation count visible in snippet → 0 (still emitted; merge step
    # may attach to a stronger record)
    assert cand is not None
    assert cand["scholar_citations"] == 0


def test_parse_scholar_result_skips_non_profile():
    result = {
        "url": "https://scholar.google.com/scholar?q=infra+engineer",
        "title": "Search results - Google Scholar",
        "text": "",
    }
    assert exa._parse_result("scholar", result) is None


def test_parse_scholar_title_unicode_directional_marks():
    """Scholar wraps names in invisible directional marks : strip them."""
    name = exa._parse_scholar_title("‪Maya Rodriguez‬ - ‪Google Scholar‬")
    assert name == "Maya Rodriguez"


def test_extract_citations_picks_first_match():
    assert exa._extract_citations("Cited by 1,234 · h-index 7") == 1234
    assert exa._extract_citations("180 citations on Semantic Scholar") == 180
    assert exa._extract_citations("no number here") == 0


def test_name_slug_matches_pool_identities():
    """Scholar's name-slug must be identical to the convention used in
    discover_candidates and the mock pool, so the merge attaches signal
    to the right LinkedIn record."""
    assert exa._name_slug("Maya Rodriguez") == "maya-rodriguez"
    assert exa._name_slug("Jiahui Jin") == "jiahui-jin"
    assert exa._name_slug("O'Brien Marsh-Williams") == "o-brien-marsh-williams"


def test_build_query_scholar_uses_research_phrasing():
    q = exa._build_query("scholar", {
        "role": "ML engineer",
        "seniority": "Senior",
        "co_stage": "Seed",
    })
    assert q.startswith("google scholar researcher")


def test_discover_via_exa_scholar_passes_multi_domain(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"results": []}
    fake_client = MagicMock()
    fake_client.post.return_value = fake_response
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    with patch("httpx.Client", return_value=fake_client):
        exa.discover_via_exa("scholar", {"role": "ml engineer"})

    body = fake_client.post.call_args.kwargs["json"]
    # No `category` filter for scholar : Exa's "research paper" category
    # surfaces PDFs / paper pages, not author profiles, which is what our
    # parser needs.
    assert "category" not in body
    assert "scholar.google.com" in body["includeDomains"]
    assert "semanticscholar.org" in body["includeDomains"]
    assert "arxiv.org" in body["includeDomains"]


def test_parse_result_no_city_cfg_is_no_op():
    """Calling without city_cfg shouldn't filter anything."""
    nyc_result = {
        "url": "https://www.linkedin.com/in/someone",
        "title": "Some One - Engineer at Acme | LinkedIn",
        "text": "# Some One\nEngineer at Acme\nNew York, New York, United States (US)\n",
    }
    cand = exa._parse_result("linkedin", nyc_result, None)
    assert cand is not None


# ---- snippet skip-list ----------------------------------------------------

def test_should_skip_snippet_fetch_linkedin_profile():
    assert exa.should_skip_snippet_fetch("https://www.linkedin.com/in/daniel-wang") is True
    assert exa.should_skip_snippet_fetch("https://linkedin.com/company/acme") is True


def test_should_skip_snippet_fetch_luma_checkin():
    assert exa.should_skip_snippet_fetch("https://luma.com/check-in/evt-x?pk=abc") is True
    assert exa.should_skip_snippet_fetch("https://lu.ma/e?pk=secret") is False  # not luma.com host
    assert exa.should_skip_snippet_fetch("https://luma.com/event/xyz?pk=secret") is True


def test_should_skip_snippet_fetch_keeps_other_urls():
    assert exa.should_skip_snippet_fetch("https://acme.com/about") is False
    assert exa.should_skip_snippet_fetch("") is False
    assert exa.should_skip_snippet_fetch(None) is False


def test_fetch_url_snippet_skips_known_blocked_without_network(monkeypatch):
    """Blocked URLs should return "" without ever calling httpx."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    def explode(*a, **kw):  # pragma: no cover : asserts via raise on call
        raise AssertionError("httpx should not be called for skipped URLs")

    monkeypatch.setattr("httpx.Client", explode)
    assert exa.fetch_url_snippet("https://www.linkedin.com/in/daniel-wang") == ""
    assert exa.fetch_url_snippet("https://luma.com/check-in/evt-x?pk=abc") == ""


def test_fetch_url_snippet_returns_empty_on_502(monkeypatch):
    """HTTP errors return "" without raising : enrichment is best-effort."""
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    fake_resp = MagicMock(status_code=502, text="Bad gateway")
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.post.return_value = fake_resp
    monkeypatch.setattr("httpx.Client", lambda *a, **kw: fake_client)

    # acme.com isn't in the skip list, so it goes through the network path
    assert exa.fetch_url_snippet("https://acme.com/about") == ""


# ---- discover_candidates short-circuit -----------------------------------
# Regression: when Exa is configured but returns empty for an ICP, we must
# NOT fall through to the Claude + web_search fallback. That call takes
# 60-110s but prospector.py caps each adapter at 30s, so the fallback is
# always killed mid-flight and surfaces a misleading "source took too long"
# instead of an honest empty pool. The short-circuit keeps the source fast.

def test_discover_candidates_short_circuits_when_exa_empty(monkeypatch):
    from backend.agents import llm

    monkeypatch.setattr(exa, "exa_available", lambda: True)
    monkeypatch.setattr(exa, "discover_via_exa", lambda *a, **kw: [])

    # If the Claude fallback were reached it would touch the SDK client;
    # make that an explicit failure so the test catches a regression.
    def _boom(*a, **kw):  # pragma: no cover : asserts via raise on call
        raise AssertionError(
            "Claude fallback must not run when Exa is configured but empty")

    monkeypatch.setattr(llm, "_client", _boom)

    out = llm.discover_candidates("linkedin", {"role": "founder"})
    assert out == []


def test_discover_candidates_returns_exa_results_when_present(monkeypatch):
    from backend.agents import llm

    monkeypatch.setattr(exa, "exa_available", lambda: True)
    monkeypatch.setattr(
        exa, "discover_via_exa",
        lambda *a, **kw: [{"name": "Ada", "profile_url": "https://x/in/ada"}])
    monkeypatch.setattr(
        llm, "_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("Exa returned results : Claude must not run")))

    out = llm.discover_candidates("linkedin", {"role": "founder"})
    assert out == [{"name": "Ada", "profile_url": "https://x/in/ada"}]


def test_discover_candidates_relaxes_city_filter_when_strict_empty(monkeypatch):
    """The strict city includeText filter is the usual cause of an empty Exa
    result. When it zeroes out, we retry with city_hard_filter=False (city
    stays in the neural query) and return those candidates instead of mock."""
    from backend.agents import llm

    monkeypatch.setattr(exa, "exa_available", lambda: True)

    calls = []

    def fake_discover(source, icp, max_candidates=5, city_hard_filter=True):
        calls.append({"city": icp.get("city"), "hard": city_hard_filter})
        # Strict pass (hard filter on, city present) finds nothing; the
        # relaxed pass (hard filter off) recovers a real candidate.
        if city_hard_filter and icp.get("city"):
            return []
        return [{"name": "Real Person", "identity": "real-person"}]

    monkeypatch.setattr(exa, "discover_via_exa", fake_discover)
    monkeypatch.setattr(
        llm, "_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("relaxation recovered : Claude must not run")))

    out = llm.discover_candidates("linkedin", {"role": "founder", "city": "NYC"})
    assert out == [{"name": "Real Person", "identity": "real-person"}]
    # Strict pass ran first, then the relaxed (hard filter off) pass.
    assert calls[0] == {"city": "NYC", "hard": True}
    assert calls[1]["hard"] is False


def test_discover_candidates_drops_city_when_relaxed_still_empty(monkeypatch):
    """If relaxing the hard filter still finds nobody, drop the city entirely
    before giving up (last resort, still fast Exa calls)."""
    from backend.agents import llm

    monkeypatch.setattr(exa, "exa_available", lambda: True)

    def fake_discover(source, icp, max_candidates=5, city_hard_filter=True):
        # Anything with a city set is empty; only the city-dropped query hits.
        if icp.get("city"):
            return []
        return [{"name": "Anywhere Person", "identity": "anywhere"}]

    monkeypatch.setattr(exa, "discover_via_exa", fake_discover)
    monkeypatch.setattr(
        llm, "_client",
        lambda: (_ for _ in ()).throw(AssertionError("Claude must not run")))

    out = llm.discover_candidates("linkedin", {"role": "founder", "city": "NYC"})
    assert out == [{"name": "Anywhere Person", "identity": "anywhere"}]
