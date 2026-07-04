"""LinkedIn extended-network search for the relationship agent.

When the host asks about 2nd/3rd-degree connections or mutuals through someone,
we pre-fetch Unipile people-search hits and inject them into triage — the model
does not call search itself.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .....providers import get_provider_for_user

_NETWORK_INTENT_PATTERNS = (
    re.compile(r"\b(?:2nd|second)[-\s]?degree\b", re.I),
    re.compile(r"\b(?:3rd|third)[-\s]?degree\b", re.I),
    re.compile(r"\bmutual\s+connections?\b", re.I),
    re.compile(r"\bconnections?\s+of\b", re.I),
    re.compile(r"\bthrough\b", re.I),
    re.compile(r"\b(?:extended|outer)\s+network\b", re.I),
    re.compile(r"\bwho\s+(?:do\s+)?(?:i|we)\s+know\b", re.I),
)

_REFERRAL_INTENT_PATTERNS = (
    re.compile(r"\b(?:warm\s+)?intro(?:duction)?s?\b", re.I),
    re.compile(r"\breferral\b", re.I),
    re.compile(r"\bintroduce\s+me\b", re.I),
    re.compile(r"\bconnect\s+me\b", re.I),
    re.compile(r"\bpath\s+to\b", re.I),
    re.compile(r"\bwho\s+can\s+(?:intro|connect|refer)\b", re.I),
    re.compile(r"\bask\s+(?:\w+\s+)?to\s+intro\b", re.I),
)

_EXPLICIT_CONNECTOR_RE = re.compile(
    r"(?:through|via|connections?\s+of)\s+"
    r"([A-Za-z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){0,3})",
    re.I,
)
_POSSESSIVE_CONNECTOR_RE = re.compile(
    r"([A-Za-z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){0,3})'s\s+"
    r"(?:network|connections?|mutuals?)",
    re.I,
)

_AT_COMPANY_RE = re.compile(
    r"\b(?:at|@|from|with)\s+([A-Za-z][A-Za-z0-9&.\- ]{1,40}?)"
    r"(?:\s|$|[?.!,])",
    re.I,
)
_WORKS_AT_RE = re.compile(
    r"\bworks?\s+(?:at|for|with)\s+([A-Za-z][A-Za-z0-9&.\- ]{1,40}?)"
    r"(?:\s|$|[?.!,])",
    re.I,
)

_STRIP_KEYWORDS_RE = re.compile(
    r"\b(?:2nd|second|3rd|third)[-\s]?degree\b|\bmutual\s+connections?\b|"
    r"\bconnections?\s+of\b|\bthrough\b|\bvia\b|\bwho\s+(?:do\s+)?(?:i|we)\s+know\b|"
    r"\b(?:find|show|list|search|anyone|anybody|people|person)\b|"
    r"\b(?:warm\s+)?intro(?:duction)?s?\b|\breferral\b|\bintroduce\s+me\b|"
    r"\bconnect\s+me\b|\bpath\s+to\b|\bmy\s+network\b|\bin\s+my\s+connections\b",
    re.I,
)

_MAX_CONNECTOR_FANOUT = 5
_PER_CONNECTOR_LIMIT = 8


@dataclass
class NetworkHit:
    name: str
    headline: str = ""
    location: str = ""
    linkedin_slug: str = ""
    linkedin_url: str = ""
    network_degree: str = ""
    via_connector: str = ""
    connector_contact_id: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "headline": self.headline,
            "location": self.location,
            "linkedin_slug": self.linkedin_slug,
            "linkedin_url": self.linkedin_url,
            "network_degree": self.network_degree,
            "via_connector": self.via_connector,
            "connector_contact_id": self.connector_contact_id,
        }


@dataclass
class NetworkSearchResult:
    hits: list[NetworkHit] = field(default_factory=list)
    intent: dict = field(default_factory=dict)
    error: str = ""
    skipped_reason: str = ""


def detect_referral_intent(instruction: str) -> bool:
    text = (instruction or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _REFERRAL_INTENT_PATTERNS)


def detect_network_intent(instruction: str) -> bool:
    text = (instruction or "").strip()
    if not text:
        return False
    if detect_referral_intent(text):
        return True
    return any(p.search(text) for p in _NETWORK_INTENT_PATTERNS)


def is_broad_degree_search(instruction: str) -> bool:
    text = (instruction or "").strip()
    return bool(re.search(r"\b(?:2nd|second|3rd|third)[-\s]?degree\b", text, re.I))


def parse_degrees(instruction: str) -> list[int]:
    text = (instruction or "").lower()
    degs: list[int] = []
    if re.search(r"\b(?:3rd|third)[-\s]?degree\b", text):
        degs.append(3)
    if re.search(r"\b(?:2nd|second)[-\s]?degree\b", text) or not degs:
        if 2 not in degs:
            degs.insert(0, 2)
    return degs or [2]


def _contact_display_name(contact: Any) -> str:
    return (getattr(contact, "name", None) or "").strip()


def _name_matches_contact(name_hint: str, contact: Any) -> bool:
    hint = (name_hint or "").strip().lower()
    full = _contact_display_name(contact).lower()
    if not hint or not full:
        return False
    if hint == full or hint in full or full in hint:
        return True
    hint_parts = hint.split()
    full_parts = full.split()
    if len(hint_parts) == 1 and hint_parts[0] == full_parts[0]:
        return True
    return False


def match_connector(instruction: str, contacts: list[Any]) -> Any:
    """Match a 1st-degree contact named explicitly in the instruction."""
    text = (instruction or "").strip()
    if not text:
        return None

    explicit_names: list[str] = []
    for pat in (_EXPLICIT_CONNECTOR_RE, _POSSESSIVE_CONNECTOR_RE):
        explicit_names.extend(m.group(1).strip() for m in pat.finditer(text))

    for hint in explicit_names:
        matches = [c for c in contacts if _name_matches_contact(hint, c)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            matches.sort(key=lambda c: len(_contact_display_name(c)), reverse=True)
            return matches[0]

    # Substring match on full roster names (longest wins).
    lower = text.lower()
    best = None
    best_len = 0
    for c in contacts:
        name = _contact_display_name(c)
        if len(name) < 3:
            continue
        if name.lower() in lower and len(name) > best_len:
            best = c
            best_len = len(name)
    return best


def extract_keywords(instruction: str, *, via_name: str = "") -> str:
    text = instruction or ""
    if via_name:
        text = re.sub(re.escape(via_name), " ", text, flags=re.I)
    for pat in (_EXPLICIT_CONNECTOR_RE, _POSSESSIVE_CONNECTOR_RE):
        text = pat.sub(" ", text)

    topic = ""
    for pat in (_WORKS_AT_RE, _AT_COMPANY_RE):
        m = pat.search(text)
        if m:
            topic = m.group(1).strip(" .,?!\"'")
            break

    text = _STRIP_KEYWORDS_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,?!\"'")
    if topic:
        return topic[:80]
    # Drop common question scaffolding; keep substantive tokens.
    drop = {
        "who", "in", "my", "the", "a", "an", "works", "work", "know", "does",
        "do", "connections", "connection", "network", "someone", "anyone",
    }
    kept = [w for w in text.split() if w.lower() not in drop and len(w) > 2]
    return " ".join(kept)[:80]


def rank_connectors_for_referral(
    contacts: list[Any],
    keywords: str,
    *,
    limit: int = _MAX_CONNECTOR_FANOUT,
) -> list[Any]:
    """Pick 1st-degree contacts likely to bridge to the target topic."""
    kw = (keywords or "").strip().lower()
    kw_tokens = [t for t in re.split(r"\W+", kw) if len(t) >= 3]
    scored: list[tuple[int, Any]] = []

    for c in contacts:
        if not _contact_slug(c):
            continue
        blob = " ".join(filter(None, [
            _contact_display_name(c),
            getattr(c, "company", None) or "",
            getattr(c, "headline", None) or "",
            getattr(c, "title", None) or "",
        ])).lower()
        score = 0
        if getattr(c, "vip", False):
            score += 2
        for tok in kw_tokens:
            if tok in blob:
                score += 4
        if kw and kw in blob:
            score += 6
        scored.append((score, c))

    scored.sort(key=lambda pair: (-pair[0], _contact_display_name(pair[1]).lower()))
    ranked = [c for score, c in scored if score > 0][:limit]
    if ranked:
        return ranked
    # No keyword overlap — still try a few linkedin-connected bridges.
    return [c for _, c in scored[:limit]]


def _contact_slug(contact: Any) -> str:
    slug = (getattr(contact, "linkedin_public_id", None) or "").strip()
    if slug:
        return slug.lower()
    url = getattr(contact, "linkedin_url", None) or ""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.I)
    return (m.group(1) if m else "").lower()


def _roster_slugs(contacts: list[Any]) -> set[str]:
    return {s for c in contacts if (s := _contact_slug(c))}


def _resolve_member_id(prov: Any, contact: Any) -> str:
    slug = _contact_slug(contact)
    if not slug:
        return ""
    try:
        return prov._lookup_provider_id(slug)
    except Exception:  # noqa: BLE001 : best-effort connector resolution
        return ""


def _degree_from_item(item: dict, fallback: str) -> str:
    nd = item.get("network_distance") or ""
    if isinstance(nd, str):
        up = nd.upper()
        if "DISTANCE_2" in up or up.endswith("_2"):
            return "2"
        if "DISTANCE_3" in up or up.endswith("_3"):
            return "3"
    if isinstance(nd, (int, float)):
        return str(int(nd))
    return fallback


def _normalize_hit(
    item: dict,
    *,
    degree: str,
    via_connector: str = "",
    connector_contact_id: Optional[int] = None,
) -> Optional[NetworkHit]:
    name = (item.get("name") or "").strip()
    if not name:
        return None
    slug = (item.get("public_identifier") or item.get("public_id") or "").strip()
    url = (item.get("profile_url") or item.get("linkedin_url") or "").strip()
    if not slug and url:
        m = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.I)
        slug = m.group(1) if m else ""
    if slug and not url:
        url = f"https://www.linkedin.com/in/{slug}"
    loc = (item.get("location") or item.get("city") or "").strip()
    headline = (item.get("headline") or item.get("title") or "").strip()
    return NetworkHit(
        name=name,
        headline=headline,
        location=loc,
        linkedin_slug=slug,
        linkedin_url=url,
        network_degree=_degree_from_item(item, degree),
        via_connector=via_connector,
        connector_contact_id=connector_contact_id,
    )


def _append_hits(
    hits: list[NetworkHit],
    items: list[dict],
    *,
    roster_slugs: set[str],
    seen: set[str],
    limit: int,
    degree: str,
    via_connector: str = "",
    connector_contact_id: Optional[int] = None,
) -> None:
    for item in items:
        if len(hits) >= limit:
            return
        hit = _normalize_hit(
            item,
            degree=degree,
            via_connector=via_connector,
            connector_contact_id=connector_contact_id,
        )
        if not hit:
            continue
        slug = hit.linkedin_slug.lower()
        if slug and (slug in roster_slugs or slug in seen):
            continue
        if slug:
            seen.add(slug)
        hits.append(hit)


def _search_through_connectors(
    contacts: list[Any],
    prov: Any,
    *,
    keywords: str,
    search_fn: Any,
    roster_slugs: set[str],
    seen: set[str],
    hits: list[NetworkHit],
    limit: int,
    connectors: list[Any],
) -> None:
    if not connectors:
        return
    per = max(3, limit // max(1, len(connectors)))
    for conn in connectors:
        if len(hits) >= limit:
            break
        member_id = _resolve_member_id(prov, conn)
        if not member_id:
            continue
        items = search_fn(
            keywords=keywords,
            connections_of=[member_id],
            limit=per,
        )
        _append_hits(
            hits, items,
            roster_slugs=roster_slugs,
            seen=seen,
            limit=limit,
            degree="2",
            via_connector=_contact_display_name(conn),
            connector_contact_id=getattr(conn, "id", None),
        )


def search_linkedin_network(
    user: Any,
    instruction: str,
    contacts: list[Any],
    *,
    limit: int = 15,
    search_fn: Any = None,
) -> NetworkSearchResult:
    """Run Unipile people search when the host instruction implies network intent."""
    out = NetworkSearchResult()
    steer = (instruction or "").strip()
    if not detect_network_intent(steer):
        out.skipped_reason = "no_intent"
        return out

    if not getattr(user, "unipile_account_id", None):
        out.error = "Connect LinkedIn to search your extended network."
        return out

    try:
        prov = get_provider_for_user(user)
    except ValueError as exc:
        out.error = str(exc)
        return out

    if getattr(prov, "_dry_run", False) and search_fn is None:
        out.skipped_reason = "dry_run"
        return out

    degrees = parse_degrees(steer)
    via = match_connector(steer, contacts)
    keywords = extract_keywords(steer, via_name=_contact_display_name(via) if via else "")
    roster_slugs = _roster_slugs(contacts)
    seen: set[str] = set()
    hits: list[NetworkHit] = []

    def _search(**kwargs: Any) -> list[dict]:
        if search_fn is not None:
            return list(search_fn(**kwargs) or [])
        return prov.search_people(**kwargs)

    out.intent = {
        "degrees": degrees,
        "via_connector": (_contact_display_name(via) if via else None),
        "keywords": keywords,
        "referral": detect_referral_intent(steer),
        "connector_fanout": False,
    }

    if via:
        member_id = _resolve_member_id(prov, via)
        if not member_id:
            out.error = f"Could not resolve LinkedIn profile for {_contact_display_name(via)}."
            return out
        items = _search(
            keywords=keywords,
            connections_of=[member_id],
            limit=limit,
        )
        _append_hits(
            hits, items,
            roster_slugs=roster_slugs,
            seen=seen,
            limit=limit,
            degree="2",
            via_connector=_contact_display_name(via),
            connector_contact_id=getattr(via, "id", None),
        )
    elif contacts:
        # Referral-first: search mutuals through likely 1st-degree bridges
        # even when the host didn't name someone explicitly.
        connectors = rank_connectors_for_referral(contacts, keywords)
        out.intent["connector_fanout"] = bool(connectors)
        _search_through_connectors(
            contacts, prov,
            keywords=keywords or steer[:80],
            search_fn=_search,
            roster_slugs=roster_slugs,
            seen=seen,
            hits=hits,
            limit=limit,
            connectors=connectors,
        )

    if is_broad_degree_search(steer) or not hits:
        per_degree = max(1, limit // len(degrees))
        for deg in degrees:
            items = _search(
                keywords=keywords or steer[:80],
                network_distance=[deg],
                limit=per_degree,
            )
            _append_hits(
                hits, items,
                roster_slugs=roster_slugs,
                seen=seen,
                limit=limit,
                degree=str(deg),
            )
            if len(hits) >= limit:
                break

    # Referral results: paths with a connector first (actionable intro ask).
    hits.sort(key=lambda h: (0 if h.via_connector else 1, h.name.lower()))
    out.hits = hits[:limit]
    return out


def format_network_block(result: NetworkSearchResult) -> str:
    if result.error:
        return f"NETWORK SEARCH NOTE: {result.error}"
    if not result.hits:
        return ""
    rows = [h.as_dict() for h in result.hits]
    via = result.intent.get("via_connector")
    intro = (
        "NETWORK SEARCH RESULTS (people NOT in your contact list — they have "
        "no contact_id; do NOT add them to selections):\n"
    )
    if via:
        intro += f"Searched mutual connections through {via}.\n"
    elif result.intent.get("connector_fanout"):
        intro += (
            "Searched mutual connections through several of your 1st-degree "
            "contacts (referral paths — each row shows via_connector).\n"
        )
    intro += json.dumps(rows, indent=2, default=str)
    return intro


def network_summary_from_hits(hits: list[NetworkHit], instruction: str = "") -> str:
    if not hits:
        return "No extended-network matches turned up."
    with_path = [h for h in hits if h.via_connector]
    ordered = with_path + [h for h in hits if not h.via_connector]
    parts: list[str] = []
    for h in ordered[:8]:
        bit = h.name
        if h.network_degree:
            bit += f" ({h.network_degree}°"
            if h.via_connector:
                bit += f" via {h.via_connector}"
            bit += ")"
        elif h.via_connector:
            bit += f" (via {h.via_connector})"
        if h.headline:
            bit += f" — {h.headline[:80]}"
        parts.append(bit)
    if with_path:
        lead = "Warm paths I found"
    else:
        lead = "Here are people in your extended network"
    if instruction.strip():
        lead += " for your ask"
    tail = ""
    if with_path:
        connectors = sorted({h.via_connector for h in with_path if h.via_connector})
        if connectors:
            tail = f" Ask {connectors[0]} for an intro first."
    return f"{lead}: " + "; ".join(parts) + "." + tail


def enrich_book_ask(
    user: Any,
    query: str,
    contacts: list[Any],
    book_answer: dict,
    *,
    search_fn: Any = None,
) -> dict:
    """Merge LinkedIn network hits into a BookApp /ask response."""
    out = dict(book_answer or {})
    out.setdefault("people", [])
    out["network_hits"] = []
    steer = (query or "").strip()
    if not detect_network_intent(steer):
        return out

    nr = search_linkedin_network(user, steer, contacts, search_fn=search_fn)
    if nr.error and not nr.hits:
        out["answer"] = nr.error
        return out

    if nr.hits:
        out["network_hits"] = [h.as_dict() for h in nr.hits]
        summary = network_summary_from_hits(nr.hits, steer)
        if not out.get("people"):
            out["answer"] = summary
        else:
            out["answer"] = f"{summary} From your book: {out.get('answer') or ''}".strip()
        return out

    # Network-shaped ask but no LinkedIn hits — don't imply "in your book" only.
    ans = (out.get("answer") or "").strip()
    if ans and ("in your book" in ans.lower() or "identified in your book" in ans.lower()):
        out["answer"] = network_summary_from_hits([], steer)
    return out
