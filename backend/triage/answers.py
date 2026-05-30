"""
triage/answers.py : parse an applicant's submitted answers into structured claims.

The CSV parser (csv_parser.py) maps a handful of headers onto canonical fields
(name / email / role / company / website / linkedin_url) and dumps everything
else into raw_application_data. That leftover blob is where the signal lives :
the host's custom questions ('Do you use Stripe?', 'Are you a creator?',
'What are you building?', 'Why do you want to come?').

This module turns that blob into a flat Claims object so the reconciler and the
scorer can reason over *what the applicant says about themselves* separately
from *what external enrichment found*. It is deterministic and LLM-free :
header matching only, first non-empty wins.

Claims are the applicant's OWN assertions — they are not verified here. The
reconciler weighs them against enrichment evidence; the scorer judges whether
they hold up.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


# Header → claim-field mapping. Same fuzzy spirit as csv_parser._HEADER_MAP :
# Luma hosts phrase questions differently per event, so we match on normalized
# substrings. Keys are normalized (lowercased, punctuation→space) the same way
# csv_parser normalizes headers, so 'What are you building?' → 'what are you
# building' matches the 'building' key.
_PROJECT_HINTS: tuple[str, ...] = (
    "building", "what are you working on", "your project", "your startup",
    "what do you build", "product", "what are you making", "describe your",
)
_INDUSTRY_HINTS: tuple[str, ...] = (
    "industry", "vertical", "sector", "space", "category", "what field",
    "what space",
)
_REASON_HINTS: tuple[str, ...] = (
    "why do you want", "why are you", "why attend", "reason for", "what brings",
    "what do you hope", "what are you looking", "goal for", "hoping to get",
    "why this event",
)
_STRIPE_HINTS: tuple[str, ...] = (
    "stripe", "payment", "process payments", "do you charge", "do you sell",
    "revenue", "mrr", "arr", "do you have customers",
)
_CREATOR_HINTS: tuple[str, ...] = (
    "creator", "audience", "following", "content", "youtube", "podcast",
    "newsletter", "do you create",
)
_STAGE_HINTS: tuple[str, ...] = (
    "stage", "how big", "team size", "headcount", "raised", "funding",
    "employees", "how many people",
)


# Phrases in an answer that are soft red flags worth surfacing for review.
# Deliberately conservative : these are *signals to look closer*, not verdicts.
_RED_FLAG_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bstealth\b", "stealth (unverifiable company claim)"),
    (r"\bpre[\s-]?(launch|product|seed|revenue|idea)\b", "pre-launch / idea-stage"),
    (r"\bjust\s+(getting\s+started|started|exploring)\b", "very early / exploring"),
    (r"\b(looking|want)\s+to\s+(start|build|launch)\b", "aspirational, not yet building"),
    (r"\bn/?a\b", "non-answer (N/A)"),
    (r"\bnot\s+sure\b", "uncertain self-description"),
    (r"\bstudent\b", "student"),
    (r"\bbetween\s+jobs\b", "between jobs"),
    (r"\b(consult|agency|freelanc|service)\w*\b", "agency / service-provider signal"),
)

_URL_RE = re.compile(r"https?://[^\s,)\]]+", re.IGNORECASE)


def _normalize_header(h: str) -> str:
    """Match csv_parser._normalize_header so the two modules agree on shape."""
    h = (h or "").strip().lower()
    h = re.sub(r"[_:?!.\-/\\]+", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


def _first_match(raw: dict[str, str], hints: tuple[str, ...]) -> str:
    """First raw answer whose normalized header contains any hint. First
    non-empty wins, matching csv_parser's 'first column wins' convention."""
    for header, value in raw.items():
        v = (value or "").strip()
        if not v:
            continue
        norm = _normalize_header(header)
        if any(hint in norm for hint in hints):
            return v
    return ""


def _extract_links(values) -> list[str]:
    """Pull every URL out of a set of answer strings, deduped, order-preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        for m in _URL_RE.findall(v or ""):
            url = m.rstrip(".,);")
            key = url.lower()
            if key not in seen:
                seen.add(key)
                out.append(url)
    return out


def _detect_red_flags(values) -> list[str]:
    """Scan answer text for conservative red-flag phrases. Deduped."""
    blob = " \n ".join(v for v in values if v).lower()
    out: list[str] = []
    seen: set[str] = set()
    for pattern, label in _RED_FLAG_PATTERNS:
        if re.search(pattern, blob) and label not in seen:
            seen.add(label)
            out.append(label)
    return out


@dataclass
class Claims:
    """The applicant's own self-reported assertions, parsed from their answers.

    Everything here is UNVERIFIED — it's what they said, not what's true.
    """
    claimed_role: str = ""
    claimed_company: str = ""
    claimed_project: str = ""
    claimed_industry: str = ""
    claimed_stage: str = ""
    reason_for_attending: str = ""
    stripe_answer: str = ""
    creator_answer: str = ""
    provided_links: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    raw_answers: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "claimed_role": self.claimed_role,
            "claimed_company": self.claimed_company,
            "claimed_project": self.claimed_project,
            "claimed_industry": self.claimed_industry,
            "claimed_stage": self.claimed_stage,
            "reason_for_attending": self.reason_for_attending,
            "stripe_answer": self.stripe_answer,
            "creator_answer": self.creator_answer,
            "provided_links": list(self.provided_links),
            "red_flags": list(self.red_flags),
            "raw_answers": dict(self.raw_answers),
        }

    def is_empty(self) -> bool:
        return not (self.claimed_company or self.claimed_role
                    or self.claimed_project or self.reason_for_attending
                    or self.raw_answers)


def _coerce_raw(applicant) -> dict[str, str]:
    """raw_application_data is stored as a JSON string on the ORM row but is a
    plain dict on the parsed-CSV dict. Accept either."""
    if isinstance(applicant, dict):
        raw = applicant.get("raw_application_data")
    else:
        raw = getattr(applicant, "raw_application_data", None)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return {str(k): str(val) for k, val in v.items()} if isinstance(v, dict) else {}
        except (json.JSONDecodeError, TypeError, AttributeError):
            return {}
    return {}


def _get_field(applicant, name: str) -> str:
    if isinstance(applicant, dict):
        v = applicant.get(name)
    else:
        v = getattr(applicant, name, None)
    return (v or "").strip() if isinstance(v, str) else ""


def parse_claims(applicant) -> Claims:
    """Parse an applicant (ORM Applicant or parsed-CSV dict) into Claims.

    Canonical fields (role, company) seed claimed_role / claimed_company; the
    custom Q&A in raw_application_data fills the rest by fuzzy header match.
    """
    raw = _coerce_raw(applicant)
    answer_values = list(raw.values())
    website = _get_field(applicant, "website")

    links = _extract_links(answer_values)
    if website and website.lower() not in {l.lower() for l in links}:
        links.insert(0, website)

    return Claims(
        claimed_role=_get_field(applicant, "role") or _first_match(raw, ("role", "title", "what do you do")),
        claimed_company=_get_field(applicant, "company") or _first_match(raw, ("company", "startup", "organization")),
        claimed_project=_first_match(raw, _PROJECT_HINTS),
        claimed_industry=_first_match(raw, _INDUSTRY_HINTS),
        claimed_stage=_first_match(raw, _STAGE_HINTS),
        reason_for_attending=_first_match(raw, _REASON_HINTS),
        stripe_answer=_first_match(raw, _STRIPE_HINTS),
        creator_answer=_first_match(raw, _CREATOR_HINTS),
        provided_links=links,
        red_flags=_detect_red_flags(answer_values),
        raw_answers=raw,
    )
