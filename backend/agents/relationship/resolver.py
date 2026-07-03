"""
agents/resolver.py : one place that turns any of three in-person inputs into a
resolved LinkedIn identity.

Three entry points, from highest to lowest confidence:

  normalize_linkedin_url(text) -> str | None
      Canonicalize a QR-decoded string OR a pasted URL to
      https://www.linkedin.com/in/<handle>, stripping tracking params.

  resolve_by_url(linkedin_url, provider) -> dict
      HIGH confidence. The operator scanned a QR or pasted a real profile URL,
      so we trust the handle and resolve it to the provider's internal id.

  resolve_by_text(name, title, company) -> list[dict]
      LOW confidence. Only a typed name/title/company : returns RANKED
      candidates for the operator to pick from. Never auto-picks.

Reuse over new code:
  - provider.resolve_linkedin_user (providers/unipile.py) for the URL -> id hop,
    which already respects dry-run (returns a deterministic fake id, no HTTP).
  - exa.fetch_url_snippet / exa.resolve_person / exa._api_key for the Exa path,
    all gated by EXA_API_KEY : no key means no Exa call.

REAL LinkedIn "My Code" QR payload
----------------------------------
The app's Search bar -> QR icon -> "My code" tab encodes a STANDARD profile URL
with share-tracking query params appended, e.g. (iOS):

    https://www.linkedin.com/in/maya-rodriguez?utm_source=share_via&utm_content=profile&utm_medium=member_ios

(Android swaps the last value for member_android; clean copies have no params at
all; country accounts can use a subdomain like uk.linkedin.com.) We do NOT
special-case the utm_* names : normalize_linkedin_url drops the ENTIRE query +
fragment, so it's robust to whatever tracking params LinkedIn changes them to,
and only trusts inputs we can pin to a linkedin.com host with an /in/<handle>
path.
"""
from __future__ import annotations
import re
from typing import Optional
from urllib.parse import urlsplit

from .. import exa


# LinkedIn public profile path is /in/<handle>. The handle is the vanity slug :
# ASCII letters/digits/hyphens for the vast majority of accounts, but non-Latin
# vanity URLs surface percent-encoded (e.g. /in/%E5%BC%A0%E4%BC%9F), so we grab
# everything up to the next /, ?, or # and keep it verbatim in the canonical
# form rather than truncating it.
_IN_PATH_RE = re.compile(r"/in/([^/?#]+)", re.I)


def normalize_linkedin_url(text: Optional[str]) -> Optional[str]:
    """Canonicalize a raw QR-decoded string or pasted URL to
    https://www.linkedin.com/in/<handle>, or None if it isn't a LinkedIn
    profile link.

    Handles the shapes we actually see: the "My Code" tracking-param form,
    a clean URL, a trailing slash, deeper paths (/in/<handle>/detail/...),
    a country subdomain (uk.linkedin.com), and a scheme-less paste
    (www.linkedin.com/in/<handle>). Anything not pinned to a linkedin.com
    host with an /in/<handle> path returns None.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    # Scheme-less paste ("www.linkedin.com/in/x") : urlsplit would put the
    # whole thing in .path, so give it a scheme to parse host + path cleanly.
    candidate = raw if "://" in raw else "https://" + raw.lstrip("/")

    parts = urlsplit(candidate)
    # Drop any userinfo / port, lowercase for the host check.
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None

    m = _IN_PATH_RE.search(parts.path)
    if not m:
        return None
    handle = m.group(1).rstrip("/").strip()
    if not handle:
        return None

    # Reuse Exa's canonical formatter (always emits www.linkedin.com/in/<handle>).
    return exa._normalize_linkedin_url(candidate, handle)


def _name_from_snippet(snippet: str) -> Optional[str]:
    """Pull the person's name from an Exa /contents markdown snippet : it's the
    first top-level "# Name" header line. Best-effort; returns None if absent."""
    for line in snippet.split("\n"):
        s = line.strip()
        if s.startswith("#") and not s.startswith("##"):
            name = s.lstrip("#").strip()
            if name:
                return name[:120]
    return None


def resolve_by_url(linkedin_url: str, provider) -> dict:
    """HIGH-confidence resolve from a known profile URL.

    Canonicalizes the URL, then reuses provider.resolve_linkedin_user to get
    the provider's internal id (dry-run returns a deterministic fake, no HTTP).
    When EXA_API_KEY is set, optionally enriches with a light name/headline from
    a page snippet : best-effort, so an empty snippet just omits those keys.

    Returns {linkedin_url, provider_id, name?, headline?, confidence: "high"}.

    Provider-degraded soft-fail: a live Unipile that is throttled / down makes
    resolve_linkedin_user raise. This is called from request paths, so instead
    of letting that 500 the caller we return {..., confidence: "failed"} and let
    the send path re-resolve lazily later. (Both current callers also wrap this;
    the internal guard makes the soft-fail behavior the contract, not the luck of
    the call site.)
    """
    canonical = normalize_linkedin_url(linkedin_url) or linkedin_url
    try:
        provider_id = provider.resolve_linkedin_user(canonical)
    except Exception as exc:  # noqa: BLE001 : degraded provider must not 500
        return {
            "linkedin_url": canonical,
            "provider_id": None,
            "confidence": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    out: dict = {
        "linkedin_url": canonical,
        "provider_id": provider_id,
        "confidence": "high",
    }

    # Optional enrichment. NB: exa.fetch_url_snippet intentionally short-circuits
    # linkedin.com/in/ URLs (Cloudflare anti-bot 502s them), so this is usually
    # an empty string for profile links : name/headline then stay absent, which
    # is fine since they're optional. Kept as the spec asks, and harmless if Exa
    # ever starts returning content for these.
    if exa.exa_available():
        snippet = exa.fetch_url_snippet(canonical)
        if snippet:
            headline = exa._extract_headline_from_text(snippet)
            if headline:
                out["headline"] = headline
            name = _name_from_snippet(snippet)
            if name:
                out["name"] = name
    return out


def resolve_by_text(name: str, title: str = "", company: str = "") -> list[dict]:
    """LOW-confidence resolve from typed identifiers : returns RANKED candidates
    for the operator to choose from, never an auto-pick.

    Delegates to exa.resolve_person (the Exa /search path). Returns [] when
    EXA_API_KEY is unset, so the caller can surface a "type the link instead"
    fallback. Each candidate is stamped confidence="low" to match the shape of
    resolve_by_url's result.
    """
    candidates = exa.resolve_person(name, title, company)
    for c in candidates:
        c["confidence"] = "low"
    return candidates
