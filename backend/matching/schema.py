"""Canonical Person + EnrichedPerson schema.

Single source of truth for the data shape that flows through the pipeline.
Every module reads/writes these dataclasses. Adding fields is fine; renaming
or removing existing fields is breaking.

Designed to be a SUPERSET of event-v1's PERSON_CSV_COLUMNS so a future merge
into ~/event-v1/packages/matching/ doesn't require schema migrations.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# Canonical experience buckets. Free-text experience answers from a CSV are
# normalized into one of these by the ingest layer.
EXP_LEVELS = ("beginner", "intermediate", "advanced", "expert", "unknown")

# Note: ticket_type is event-agnostic free-text — values depend on the event
# (e.g. Luma uses Attendee/Investor/Judge; a salon might use VIP/General/Speaker).
# The per-event rubric synthesizer decides how to interpret the values.


@dataclass
class Person:
    """Raw person record straight from the CSV. No enrichment yet.

    Mirrors event-v1's PERSON_CSV_COLUMNS plus Luma-specific fields.
    """
    # Identity
    id: str                       # stable hash of (name + linkedin or email)
    name: str
    email: str = ""

    # Role + affiliation (from CSV)
    role: str = ""                # "Founder", "CEO", "Engineer", etc.
    title: str = ""               # raw job title
    company: str = ""

    # Profile URLs (the enrichment inputs)
    linkedin_url: str = ""
    x_handle: str = ""            # normalized to handle without @ or URL
    github_username: str = ""

    # Event-context fields (informational; values vary per event)
    ticket_type: str = "unknown"  # free-text from the CSV; meaning is event-specific
    exp_level: str = "unknown"    # normalized: beginner/intermediate/advanced/expert/unknown
    checked_in: bool = False      # informational — NOT used as a matching input

    # Provenance — which CSV columns mapped to what
    raw_row: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichedPerson:
    """Person + fields extracted from X / LinkedIn / GitHub enrichment.

    All enrichment fields are optional — degrade gracefully when a source fails.
    `enrichment_sources` records per-source status so downstream code can reason
    about data quality per person.
    """
    # Everything from Person — flatten rather than nest for downstream simplicity
    id: str
    name: str
    email: str = ""
    role: str = ""
    title: str = ""
    company: str = ""
    linkedin_url: str = ""
    x_handle: str = ""
    github_username: str = ""
    ticket_type: str = "unknown"
    exp_level: str = "unknown"
    checked_in: bool = False

    # === Enriched fields ===

    # Roles: structured history from LinkedIn + bio
    roles_history: list[dict] = field(default_factory=list)
    # each: {title, company, years, level, domain}

    # What they do / build with
    tech_stack: list[str] = field(default_factory=list)
    # normalized tech tags: ["python", "pytorch", "react", "rust"]

    # What spaces they operate in
    domains: list[str] = field(default_factory=list)
    # e.g. ["robotics-manipulation", "ml-infra", "fintech-b2b"]

    # What they obsess about (from X bio + recent posts) — the conviction signal
    conviction_themes: list[str] = field(default_factory=list)
    # e.g. ["humanoid robotics", "vertical AI evals", "sim-to-real"]

    # Notable shipped things, exits, prior wins
    previous_experiences: list[str] = field(default_factory=list)

    # Synthesized bio paragraph — used for embedding
    bio_text: str = ""

    # GitHub specifics (from direct API)
    github_languages: dict[str, int] = field(default_factory=dict)
    # {language: lines_of_code}
    github_top_repos: list[dict] = field(default_factory=list)
    # each: {name, description, stars, language, topics}
    github_followers: int = 0
    github_public_repos: int = 0

    # X signals
    x_bio: str = ""
    x_recent_post_themes: list[str] = field(default_factory=list)

    # LinkedIn signals
    linkedin_headline: str = ""
    linkedin_about: str = ""

    # What they're explicitly looking for (extracted from bios)
    explicit_asks: list[str] = field(default_factory=list)
    # e.g. ["technical cofounder", "first enterprise customers", "seed investors"]

    # What they can offer / advise on
    mentor_signals: list[str] = field(default_factory=list)

    # Location (free-form, normalized lightly)
    city: str = ""

    # === Computed (filled later by embeddings.py) ===

    bio_embedding: list[float] = field(default_factory=list)
    skill_embedding: list[float] = field(default_factory=list)

    # === Metadata ===

    enrichment_status: str = "pending"  # pending | ok | partial | failed
    enrichment_sources: dict[str, str] = field(default_factory=dict)
    # {x: ok|failed|skipped, linkedin: ..., github: ...}
    enrichment_errors: list[str] = field(default_factory=list)
    enriched_at: str = ""              # ISO timestamp

    raw_row: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_person(cls, p: Person) -> "EnrichedPerson":
        """Lift a Person into an EnrichedPerson with empty enrichment fields."""
        return cls(
            id=p.id,
            name=p.name,
            email=p.email,
            role=p.role,
            title=p.title,
            company=p.company,
            linkedin_url=p.linkedin_url,
            x_handle=p.x_handle,
            github_username=p.github_username,
            ticket_type=p.ticket_type,
            exp_level=p.exp_level,
            checked_in=p.checked_in,
            raw_row=p.raw_row,
        )
