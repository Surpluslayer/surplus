"""
Unit tests for Exa discovery — parsing logic only, no network.

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
            # duplicate handle — should be deduped
            {
                "url": "https://www.linkedin.com/in/maya-rodriguez",
                "title": "Maya Rodriguez | LinkedIn",
            },
            {
                "url": "https://www.linkedin.com/in/jiahui-jin/",
                "title": "Jiahui Jin - ML Engineer | LinkedIn",
            },
            # non-profile URL — should be filtered
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

    expected = {"linkedin": "linkedin profile", "github": "github", "x": "tweet"}
    for source, category in expected.items():
        with patch("httpx.Client", return_value=fake_client):
            exa.discover_via_exa(source, {"role": "engineer"})
        body = fake_client.post.call_args.kwargs["json"]
        assert body["category"] == category, f"source={source}"


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
