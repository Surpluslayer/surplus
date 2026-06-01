"""
triage/rubric.py : per-event scoring rubric synthesis.

Sonnet reads the operator's triage_config (sponsor, goal, ideal profile,
hard filters, nice-to-haves, anti-fit examples) plus a summary of the
applicant pool, and emits a JSON rubric that the per-applicant scorer
applies deterministically.

Why bother synthesizing instead of using a fixed rubric :
  - The 'photography founder uses Stripe' problem is event-specific.
    Vanilla relevance scoring would pass them. A sponsor-aware rubric
    written FOR Stripe x ElevenLabs encodes the right anti-fit guidance.
  - Different sponsors care about different things. JPMorgan dinners want
    later-stage applicants who need a bank; Stripe cafes want earlier
    builders shipping at scale.
  - Cached per event: synth runs once, applies to all N applicants.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from ..jsonx import extract_json
from .recommend import Thresholds


RUBRIC_MODEL = os.environ.get("TRIAGE_RUBRIC_MODEL", "claude-sonnet-4-6")
RUBRIC_MAX_TOKENS = int(os.environ.get("TRIAGE_RUBRIC_MAX_TOKENS", "4500"))
RUBRIC_CACHE_TTL_S = 60 * 60 * 6  # 6h : refresh if the operator edits config
# Sonnet rubric synth routinely takes 20-40s on Railway (TCP/TLS cold start
# + model warmup). 30s hardcode was causing ~50% timeout rate in prod.
RUBRIC_TIMEOUT_S = float(os.environ.get("TRIAGE_RUBRIC_TIMEOUT", "60"))


@dataclass
class Rubric:
    dimensions: list[dict]    # [{name, weight, rubric}, ...]
    hard_gates: list[str]
    thresholds: Thresholds = None   # per-event accept/maybe/reject cutoffs
    notes: str = ""
    error: Optional[str] = None
    model_version: str = ""

    def __post_init__(self):
        if self.thresholds is None:
            self.thresholds = Thresholds.default()

    def weights(self) -> dict[str, float]:
        """Normalized weight dict the recommend module expects."""
        total = sum(float(d.get("weight") or 0) for d in self.dimensions) or 1.0
        return {
            d["name"]: float(d.get("weight") or 0) / total
            for d in self.dimensions if d.get("name")
        }

    def as_json(self) -> str:
        return json.dumps({
            "dimensions": self.dimensions,
            "hard_gates": self.hard_gates,
            "thresholds": self.thresholds.as_dict(),
            "notes": self.notes,
            "model_version": self.model_version,
        })


# Module-level cache : (event_id, config_hash) -> (cached_at, Rubric).
_RUBRIC_CACHE: dict[tuple[int, str], tuple[float, Rubric]] = {}


def reset_rubric_cache() -> None:
    _RUBRIC_CACHE.clear()


def _config_hash(triage_config_json: str) -> str:
    """Stable fingerprint of the config so cache invalidates on edit."""
    return hashlib.sha256((triage_config_json or "").encode("utf-8")).hexdigest()[:16]


_DIMENSION_NAMES: tuple[str, ...] = (
    "sponsor_fit", "event_fit", "role_relevance", "company_relevance",
    "stage_relevance", "seriousness_legitimacy", "room_value", "application_quality",
)


_RUBRIC_SYSTEM = """You synthesize a scoring rubric for an event sponsor's applicant triage.

The host has shared:
  - the OPERATOR ICP : the structured ideal-attendee profile captured when the
    event was created (ideal role, seniority, company stage, location, years of
    experience, format). This is the operator's PRIMARY statement of who they
    want in the room — the rubric MUST be anchored to it.
  - sponsor identity + what they're sponsoring
  - the event goal + format
  - an 'ideal attendee' description
  - hard filters (drop the applicant if these aren't met)
  - nice-to-have signals
  - anti-fit examples (categories of applicant the host does NOT want)

HOW TO USE THE OPERATOR ICP
  - ideal_role / ideal_seniority anchor role_relevance and (where relevant)
    seniority expectations: an applicant matching the ICP role/seniority scores
    high on role_relevance; a clear mismatch scores low.
  - ideal_company_stage sets the correct answer for stage_relevance — do NOT
    leave stage_relevance as 'any' when the ICP names a stage.
  - city, when set, is a strong candidate for a location hard_gate
    ('Must be based in <city>') UNLESS the format implies remote/virtual.
  - The ICP CONSTRAINS but does not REPLACE sponsor_fit. A perfect-ICP applicant
    who is useless to the sponsor still scores low on sponsor_fit. Keep both.
  - If the ICP and the free-text triage_config conflict, prefer the triage_config
    (it is the more specific, later signal) and note the conflict.

Your job: produce a JSON rubric covering EXACTLY these 8 dimensions, each
with a weight (0-1, sum to 1.0) and a 'rubric' field that tells the
per-applicant scorer how to score that dimension 0-100.

  sponsor_fit             : Is this applicant valuable for the SPONSOR?
                            Could they buy, use, invest in, partner with,
                            or generate referrals for the sponsor at MEANINGFUL SCALE?
                            Anti-fit examples should score AT MOST 30 here.
  event_fit               : Are they appropriate for the event format?
                            Dinners stricter than cafes; cafes stricter than mixers.
  role_relevance          : Founder, operator, engineer, creator, investor,
                            researcher, etc. Match against host's ideal.
  company_relevance       : Right kind of company? Startup vs agency vs service
                            business vs creator vs research lab. Encode the
                            distinction the sponsor cares about.
  stage_relevance         : Pre-revenue / early-stage / scaling / enterprise.
                            Some events want 'too early'; others want 'too late'.
  seriousness_legitimacy  : Real and substantial vs overstating or thin?
                            Do public claims match private application answers?
  room_value              : Would this person make the room BETTER for the
                            other attendees + the sponsor? (Network density,
                            specific expertise, follow-up generation.)
  application_quality     : Did they fill it in thoughtfully?
                            Did their answers match the event goal?

KEY RULES
  - The rubric for sponsor_fit MUST encode the anti-fit examples explicitly :
    'An applicant matching <anti_fit_example> scores AT MOST 30.'
  - The rubric for stage_relevance MUST take a clear position on what stage
    is correct for THIS event (not 'any').
  - Weights should reflect what matters MOST for this specific event:
    sponsor_fit + role_relevance are typically heaviest for sponsored events.
  - Weights should sum to 1.0 (or very close : we normalize).
  - hard_gates : 0-3 short statements like 'Must be in NYC' that, if violated,
    cap the overall score at 30 regardless of dimension scores.
  - notes : 1-2 sentences for the human reviewer about how to interpret edge
    cases, especially borderline applicants.

THRESHOLDS
Set accept/maybe/reject cutoffs based on the event format:

  Casual mixer / café / open coworking  →  accept_fit_min: 65, maybe_fit_min: 50, reject_fit_max: 35
  Standard sponsored dinner / reception →  accept_fit_min: 72, maybe_fit_min: 55, reject_fit_max: 40
  Exclusive/invite-only / small dinner  →  accept_fit_min: 78, maybe_fit_min: 60, reject_fit_max: 45

  accept_confidence_min: always 60 (we need evidence to commit to an accept)
  maybe_confidence_min:  always 45

  Use your judgment — a 300-person mixer should accept more broadly than a
  20-person intimate dinner. The thresholds determine what fraction of the
  pool gets auto-accepted vs flagged for human review.

OUTPUT FORMAT
Return ONLY JSON. No prose, no markdown fences. Schema:

{
  "dimensions": [
    { "name": "sponsor_fit", "weight": 0.30,
      "rubric": "Score 0-100. ..." },
    ...8 dimensions...
  ],
  "hard_gates": ["...", "..."],
  "thresholds": {
    "accept_fit_min": 65,
    "accept_confidence_min": 60,
    "maybe_fit_min": 50,
    "maybe_confidence_min": 45,
    "reject_fit_max": 35
  },
  "notes": "..."
}"""


def icp_from_event(event) -> dict:
    """The operator's structured ideal-attendee profile, captured at event setup.

    These are the SAME canonical ICP fields the outbound curation path scores
    against (see curation.scoring.ICP.from_event: role, seniority, co_stage).
    Surfacing them here is the whole point of this hook — it anchors the INBOUND
    triage rubric to the operator's stated ICP instead of relying only on the
    free-text triage_config. Empty/blank fields are dropped so they don't dilute
    the synthesis prompt.
    """
    def _g(name: str) -> str:
        v = getattr(event, name, None)
        return v.strip() if isinstance(v, str) else ("" if v is None else str(v))

    icp = {
        "ideal_role": _g("role"),
        "ideal_seniority": _g("seniority"),
        "ideal_company_stage": _g("co_stage"),
        "event_format": _g("format"),
        "city": _g("city"),
        "yoe_buckets": _g("yoe"),
        "event_goal": _g("goal"),
    }
    return {k: v for k, v in icp.items() if v}


def _build_user_message(triage_config: dict, pool_summary: dict,
                        icp: Optional[dict] = None) -> str:
    cfg = {k: v for k, v in triage_config.items() if k != "intake_snapshot"}
    parts: list[str] = []
    if icp:
        parts += ["OPERATOR ICP (from event setup — anchor the rubric to this)",
                  json.dumps(icp, indent=2), ""]
    parts += ["TRIAGE CONFIG", json.dumps(cfg, indent=2),
              "", "APPLICANT POOL SUMMARY",
              json.dumps(pool_summary, indent=2),
              "", "Generate the rubric JSON now."]
    return "\n".join(parts)


def _summarize_pool(applicants: list) -> dict:
    """Light stats about who actually applied : helps the rubric calibrate
    to the real pool rather than a hypothetical one."""
    if not applicants:
        return {"total": 0, "with_linkedin": 0, "with_company": 0,
                "top_roles": [], "top_companies": []}
    role_counts: dict[str, int] = {}
    company_counts: dict[str, int] = {}
    with_linkedin = 0
    with_company = 0
    for a in applicants:
        r = (getattr(a, "role", None) or "").strip().lower()
        c = (getattr(a, "company", None) or "").strip().lower()
        if r: role_counts[r] = role_counts.get(r, 0) + 1
        if c: company_counts[c] = company_counts.get(c, 0) + 1
        if (getattr(a, "linkedin_url", None) or "").strip(): with_linkedin += 1
        if c: with_company += 1
    top_n = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])[:10]]
    return {
        "total": len(applicants),
        "with_linkedin": with_linkedin,
        "with_company": with_company,
        "top_roles": top_n(role_counts),
        "top_companies": top_n(company_counts),
    }


def _default_rubric(error: str = "") -> Rubric:
    """Fallback when synthesis fails. Equal weights, generic rubric text.
    Better than no scoring at all."""
    dims = [
        {"name": n, "weight": round(1.0 / len(_DIMENSION_NAMES), 4),
         "rubric": f"Score {n} 0-100 based on the applicant's fit."}
        for n in _DIMENSION_NAMES
    ]
    return Rubric(dimensions=dims, hard_gates=[],
                  notes="Default rubric (synthesis failed or skipped).",
                  error=error, model_version="default-v1")


def synthesize_rubric(event_id: int, triage_config_json: str,
                     applicants: list, *, icp: Optional[dict] = None) -> Rubric:
    """Run rubric synthesis for this event. Cached by (event_id, config_hash).

    `icp` is the operator's structured ideal-attendee profile from event setup
    (see icp_from_event). It anchors the synthesized rubric to the operator's
    stated ICP; it is folded into the cache fingerprint so editing the event's
    ICP invalidates a stale rubric just like editing triage_config does.

    Falls back to a generic equal-weight rubric on any failure so the
    scoring pipeline always has something to apply. Synthesis failures
    are logged + recorded on Rubric.error.
    """
    # Fingerprint config + ICP together so a change to EITHER busts the cache.
    icp_blob = json.dumps(icp or {}, sort_keys=True)
    cfg_hash = _config_hash((triage_config_json or "") + "\x00" + icp_blob)
    key = (event_id, cfg_hash)
    now = time.time()
    hit = _RUBRIC_CACHE.get(key)
    if hit and now - hit[0] < RUBRIC_CACHE_TTL_S:
        return hit[1]

    try:
        triage_config = json.loads(triage_config_json) if triage_config_json else {}
    except json.JSONDecodeError:
        triage_config = {}

    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        rubric = _default_rubric(error="ANTHROPIC_API_KEY unset")
        _RUBRIC_CACHE[key] = (now, rubric)
        return rubric

    pool_summary = _summarize_pool(applicants)
    user_msg = _build_user_message(triage_config, pool_summary, icp=icp)

    try:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=RUBRIC_MODEL,
            max_tokens=RUBRIC_MAX_TOKENS,
            timeout=RUBRIC_TIMEOUT_S,
            system=[{"type": "text", "text": _RUBRIC_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [triage.rubric] Sonnet failed: {type(exc).__name__}: {exc}")
        rubric = _default_rubric(error=f"{type(exc).__name__}: {exc}")
        _RUBRIC_CACHE[key] = (now, rubric)
        return rubric

    # Claude 4.x dropped assistant-message prefill. extract_json finds the
    # first complete JSON object in whatever preamble Sonnet emits.
    text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    full = "\n".join(text_chunks)
    parsed = extract_json(full)
    if not parsed or not isinstance(parsed.get("dimensions"), list):
        print(f"  [triage.rubric] couldn't parse JSON from Sonnet output")
        rubric = _default_rubric(error="couldn't parse rubric JSON")
        _RUBRIC_CACHE[key] = (now, rubric)
        return rubric

    dims = [d for d in parsed["dimensions"] if d.get("name") in _DIMENSION_NAMES]
    rubric = Rubric(
        dimensions=dims,
        hard_gates=[str(g) for g in (parsed.get("hard_gates") or []) if g],
        thresholds=Thresholds.from_dict(parsed.get("thresholds") or {}),
        notes=str(parsed.get("notes") or ""),
        model_version=RUBRIC_MODEL,
    )
    _RUBRIC_CACHE[key] = (now, rubric)
    return rubric
