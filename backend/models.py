"""
models.py : the persistence layer.

The schema mirrors the five stages:
  Event         -> stage 01, the intake profile (the mechanism's inputs)
  Prospect      -> stage 02-03, a surfaced + scored candidate
  OutreachLog   -> stage 03, one autonomous outreach event (sent/opened/replied)
  MatchEdge     -> stage 04, a predicted-value edge between two guests
  Conversion    -> stage 05, one row of the verified ROI ledger
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (datetime.utcnow() is deprecated)."""
    return datetime.now(timezone.utc)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owner : every event belongs to exactly one signed-in user. Nullable for
    # backwards compatibility with rows that pre-date multi-tenant; new rows
    # always have it set by the events POST handler. Backfilled to the operator
    # user on first migration via _migrate_event_user_id() in db.py.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), default=None, index=True
    )
    # Event provenance. "planned" = the classic intake-form event that drives
    # prospecting/outreach. "in_person" = a row created on the fly at a real
    # event by the scan-to-connect entry point, where the only inputs are a
    # human-readable label + city + the owning user; all the planning-only ICP
    # fields below are defaulted so an in_person row needs none of them.
    kind: Mapped[str] = mapped_column(String(20), default="planned")
    # Free-text label for in_person events (e.g. "NYC Tech Week — Founders Inc
    # mixer"). NULL for planned events, which use event_name instead.
    label: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # ICP. These are planning-only : required in spirit for a "planned" event
    # but defaulted at the model level so an "in_person" row can omit every one
    # of them (it only needs label + city + user_id). Existing planned-event
    # creation still supplies real values via EventCreate.
    role: Mapped[str] = mapped_column(String(200), default="")
    # seniority / co_stage / goal are multi-select on the frontend; stored
    # CSV-joined ("Senior,Staff+") in these String columns so we don't need a
    # migration. Widths bumped to fit the longest plausible CSV concatenation.
    seniority: Mapped[str] = mapped_column(String(200), default="")
    co_stage: Mapped[str] = mapped_column(String(120), default="")
    # event shape
    headcount: Mapped[int] = mapped_column(default=0)
    format: Mapped[str] = mapped_column(String(40), default="")
    city: Mapped[str] = mapped_column(String(80))
    # ISO-8601 date string (YYYY-MM-DD) for the event itself. Stored as
    # a string so the frontend's <input type="date"> value round-trips
    # untouched and we don't have to deal with TZ. Empty when unset.
    event_date: Mapped[str] = mapped_column(String(20), default="")
    # Operator-supplied display name (e.g. "Founders Dinner · April"). Empty
    # when unset; the topbar falls back to "event #<id> · live" in that case.
    event_name: Mapped[str] = mapped_column(String(160), default="")
    # Host's plain-English description of the event : the "Describe your event"
    # box from intake. Captures intent the chip fields can't (theme, who it's
    # really for, the vibe). Fed into outreach compose so the LinkedIn message
    # reflects what the host actually said, not just a canned per-goal template.
    # Empty when the host skipped the describe box.
    brief: Mapped[str] = mapped_column(Text, default="")
    # goal + budget (planning-only : defaulted so in_person rows can omit them)
    goal: Mapped[str] = mapped_column(String(300), default="")
    budget: Mapped[int] = mapped_column(default=0)
    # which prospect sources to fan out across, CSV-joined. LinkedIn is
    # always forced in by adapters_for() regardless of what's stored here.
    sources: Mapped[str] = mapped_column(String(120), default="linkedin")
    # Years-of-experience buckets, CSV-joined ("3-5,6-10"). Empty string ==
    # "no preference" (skip the YOE clause in the Exa query).
    yoe: Mapped[str] = mapped_column(String(80), default="")
    # Applicant Triage mode : JSON-encoded sponsor / event criteria used to
    # score Luma CSV applicants. Empty string means outbound-only event.
    # See backend/triage/ for the scoring + review pipeline.
    triage_config: Mapped[str] = mapped_column(Text, default="")
    # derived once the pipeline runs
    threshold: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    prospects: Mapped[list["Prospect"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    edges: Mapped[list["MatchEdge"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    applicants: Mapped[list["Applicant"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    sponsors: Mapped[list["Sponsor"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    user: Mapped[Optional["User"]] = relationship(foreign_keys=[user_id])


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    identity: Mapped[str] = mapped_column(String(120))  # stable cross-source key

    name: Mapped[str] = mapped_column(String(120))
    # 300 (not 160) because the in-person resolver can put a full LinkedIn
    # headline here, not just a short title (e.g. "... founder of Jetzy
    # (Building Agentic AI with VIP perks ...)"). Postgres 500s on overflow
    # rather than truncating, so the column must be wide enough.
    role: Mapped[str] = mapped_column(String(300), default="Unknown")
    company: Mapped[str] = mapped_column(String(120), default="Unknown")
    seniority: Mapped[str] = mapped_column(String(40), default="Mid")

    # market side + value vectors : what the matcher pairs on
    side: Mapped[str] = mapped_column(String(20), default="Builds")
    works_on: Mapped[str] = mapped_column(String(60), default="general")
    offers: Mapped[str] = mapped_column(String(200), default="")
    seeks: Mapped[str] = mapped_column(String(200), default="")

    # Discovery-time profile context, kept so outreach can ground on real
    # specifics instead of a canned template. `headline` is the one-liner
    # under the name (e.g. "Founding eng @ Acme · ex-Stripe"); `bio` is the
    # longer profile snippet Exa returns (~500 chars). Both NULL for rows
    # discovered before this column existed or via sources that don't carry
    # them. Fed into compose() so the note references something true.
    headline: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    bio: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # Their recent LinkedIn posts/activity (newline-joined text), pulled live
    # from Unipile so the note can reference something they actually said
    # recently. NULL until enriched. `enriched_at` gates the lazy fetch : set
    # once we've pulled the live profile so we never re-hit Unipile for the
    # same person (cached). NULL = not yet enriched.
    recent_activity: Mapped[Optional[str]] = mapped_column(Text, default=None)
    enriched_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    # raw source signal
    gh_stars: Mapped[int] = mapped_column(default=0)
    x_followers: Mapped[int] = mapped_column(default=0)
    # academic / research signal. Bolted on by ScholarAdapter when an
    # identity slug matches across sources; 0 when the person has no
    # visible Scholar / Semantic Scholar / arXiv footprint.
    scholar_citations: Mapped[int] = mapped_column(default=0)
    li_resolved: Mapped[bool] = mapped_column(default=False)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # the provider's internal LinkedIn user ID. Resolved once on first
    # send_connection (Unipile requires this) and cached for webhook matching.
    linkedin_provider_id: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    sources: Mapped[str] = mapped_column(String(80), default="")  # comma-joined adapter keys

    # scoring
    fit_score: Mapped[int] = mapped_column(default=0)
    fit_reason: Mapped[str] = mapped_column(Text, default="")

    # surfaced -> below | contacted | rsvp
    # "pending" : scanned in person via scan-to-connect but not yet sent
    # (awaiting the operator to fire the connect request from the entry point).
    status: Mapped[str] = mapped_column(String(20), default="surfaced")
    group_id: Mapped[Optional[int]] = mapped_column(default=None)

    # In-person capture fields (scan-to-connect). NULL for web-discovered
    # prospects. `note` is the "fun fact" / what-you-talked-about line that
    # PERSONALIZES the composed connection note + post-accept DM (≤300 to fit
    # LinkedIn's note cap). `private_note` is a separate operator-only memo that
    # is NEVER sent : it exists purely for the operator's own reference.
    # `captured_at` is when the row was scanned; `source` records the capture
    # channel ("scan" | "link" | "text").
    note: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    private_note: Mapped[Optional[str]] = mapped_column(String(500), default=None)
    # Optional, opt-in capture extras (in-person). `contact_type` tags what this
    # person is to you ("sales" | "recruiting" | "follow_up" | "other") for
    # later triage; `next_step` is the concrete follow-up to weave into the
    # first message, e.g. "grab a coffee — book a time: <calendly link>".
    contact_type: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    next_step: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    # The person's email address, when known : captured at scan time or
    # backfilled by enrichment. Unlocks the email channel for this contact
    # (send a follow-up from the host's connected mailbox instead of, or in
    # addition to, LinkedIn). NULL = unknown — email sends are gated on it.
    email: Mapped[Optional[str]] = mapped_column(String(200), default=None,
                                                 index=True)
    # VIP flag : the operator starred this person at capture time as someone
    # to prioritize. Icon-only toggle in the in-person UI. False is the norm.
    vip: Mapped[bool] = mapped_column(default=False)
    captured_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    source: Mapped[Optional[str]] = mapped_column(String(20), default=None)

    # LinkedIn connection state. Drives whether a "reach out" action sends a
    # connection request (cold) or a direct DM (warm). Default "unknown"
    # until the first Unipile relation check; flipped to "connected" by the
    # invite_accepted webhook so subsequent actions take the warm path.
    connection_status: Mapped[str] = mapped_column(String(20), default="unknown")
    connection_checked_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    # Lazy link to the cross-event Contact spine (the relationship graph). NULL
    # is the norm and fully supported : a Prospect is the per-event record; the
    # Contact is the durable person across events. Set opportunistically when a
    # stable identity (LinkedIn slug / email) is known (see agents/relationships
    # .link_contact). Event-scoped Prospect flows never depend on this being set.
    contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("contacts.id"), default=None, index=True,
    )

    event: Mapped["Event"] = relationship(back_populates="prospects")
    contact: Mapped[Optional["Contact"]] = relationship(back_populates="prospects")
    outreach: Mapped[list["OutreachLog"]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan"
    )
    conversion: Mapped[Optional["Conversion"]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan", uselist=False
    )


class OutreachLog(Base):
    """
    One row per state transition in the outreach lifecycle.

    Canonical states (provider-agnostic):
        dry_run_queued, queued, invite_sent, invite_accepted,
        message_sent, message_replied, follow_up_sent, failed

    Legacy email-flavored states (still supported for the simulator):
        sent, opened, replied
    """
    __tablename__ = "outreach_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"))
    channel: Mapped[str] = mapped_column(String(20), default="email")  # email | linkedin
    state: Mapped[str] = mapped_column(String(30))
    body: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(default=_utcnow)

    # provider tracking (nullable : only set when a real provider was invoked)
    provider: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    provider_lead_id: Mapped[Optional[str]] = mapped_column(String(80), default=None)

    prospect: Mapped["Prospect"] = relationship(back_populates="outreach")


class PendingReply(Base):
    """One AI-drafted reply waiting for a human decision.

    The reply agent (agents/reply_agent.py) runs on every inbound LinkedIn
    message and produces a `ReplyDecision`. When the classification isn't
    in the auto-send allow-list (or the loop guard fires), the draft lands
    here for an operator to approve / edit / reject via /admin/pending-replies.

    classification : the agent's bucket : clarifying | commitment | off_topic
                     | negative | ambiguous
    draft_text     : what the agent wrote, exactly as the model produced it
    reasoning      : the agent's own explanation; surfaced in the approval UI
                     so the operator understands WHY this was drafted this way
    status         : pending | approved | rejected | auto_sent (audit trail only)
    final_text     : what was actually sent (may differ from draft after edit)
    """
    __tablename__ = "pending_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"), index=True)
    inbound_body: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(30))
    draft_text: Mapped[str] = mapped_column(Text)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    final_text: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    prospect: Mapped["Prospect"] = relationship()


class Applicant(Base):
    """One row per CSV-parsed applicant for an event in triage mode.

    Canonical fields (name, email, role, company, linkedin_url, website)
    are extracted by the CSV parser's flexible field-mapping. Anything the
    parser doesn't recognize (custom Luma questions like 'Do you use Stripe?'
    or 'Are you a creator?') is preserved verbatim in raw_application_data
    as a JSON dict, so the scoring step can read everything the applicant
    actually wrote.
    """
    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)

    name: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[Optional[str]] = mapped_column(String(200), default=None, index=True)
    role: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    company: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    website: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(400), default=None)

    # Everything the CSV had that didn't map to a canonical field. JSON dict.
    raw_application_data: Mapped[str] = mapped_column(Text, default="{}")
    # Optional enrichment data (LinkedIn snippet, company info). JSON dict.
    # Holds the reconciled EvidencePacket (derived from enrichment_raw each run).
    enrichment_data: Mapped[str] = mapped_column(Text, default="{}")
    # Frozen RAW enrichment (RawEvidence.as_dict): the unreconciled Unipile/Exa
    # output. Persisted on the FIRST evaluation and reused on every re-run so the
    # non-deterministic network layer is captured once. reconcile + score then
    # run deterministically off this. Empty "" means "never enriched yet".
    enrichment_raw: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)

    event: Mapped["Event"] = relationship(back_populates="applicants")
    evaluation: Mapped[Optional["ApplicantEvaluation"]] = relationship(
        back_populates="applicant", cascade="all, delete-orphan", uselist=False,
    )
    decision: Mapped[Optional["ReviewDecision"]] = relationship(
        back_populates="applicant", cascade="all, delete-orphan", uselist=False,
    )


class ApplicantEvaluation(Base):
    """LLM-scored evaluation of an Applicant for their Event. Empty until
    the triage scoring pipeline (PR C) runs. One row per Applicant."""
    __tablename__ = "applicant_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(
        ForeignKey("applicants.id"), index=True, unique=True,
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)

    fit_score: Mapped[int] = mapped_column(default=0)        # 0-100
    confidence_score: Mapped[int] = mapped_column(default=0) # 0-100
    # accept | maybe | reject | needs_review
    recommendation: Mapped[str] = mapped_column(String(20), default="needs_review")
    # founder | operator | engineer | creator | investor | researcher |
    # student | service_provider | community_member | other
    archetype: Mapped[str] = mapped_column(String(40), default="other")

    # 8 sub-dimensions, each 0-100. Stored as discrete columns (not a JSON
    # blob) so the review UI can sort / filter by any of them.
    sponsor_fit: Mapped[int] = mapped_column(default=0)
    event_fit: Mapped[int] = mapped_column(default=0)
    role_relevance: Mapped[int] = mapped_column(default=0)
    company_relevance: Mapped[int] = mapped_column(default=0)
    stage_relevance: Mapped[int] = mapped_column(default=0)
    seriousness_legitimacy: Mapped[int] = mapped_column(default=0)
    room_value: Mapped[int] = mapped_column(default=0)
    application_quality: Mapped[int] = mapped_column(default=0)

    one_sentence_summary: Mapped[str] = mapped_column(Text, default="")
    why_fit: Mapped[str] = mapped_column(Text, default="")
    why_not_fit: Mapped[str] = mapped_column(Text, default="")
    # JSON list of evidence strings the scorer leaned on
    evidence_used: Mapped[str] = mapped_column(Text, default="[]")
    # JSON list of fields the scorer wishes it had
    missing_info: Mapped[str] = mapped_column(Text, default="[]")
    suggested_review_action: Mapped[str] = mapped_column(Text, default="")

    # --- Judge B (evidence auditor) outcome -------------------------------
    # The verifier is gated to risky applicants only (see should_verify), so
    # verifier_ran is False for the clean majority. When it ran, the
    # deterministic consolidator may have lowered confidence and/or downgraded
    # an accept/maybe to needs_review — verifier_adjustments records exactly
    # what it changed (JSON list of human-readable strings) and verifier_reason
    # is Judge B's one-sentence audit summary. The final recommendation column
    # above already reflects any downgrade.
    verifier_ran: Mapped[bool] = mapped_column(default=False)
    verifier_adjustments: Mapped[str] = mapped_column(Text, default="[]")
    verifier_reason: Mapped[str] = mapped_column(Text, default="")

    model_version: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)

    applicant: Mapped["Applicant"] = relationship(back_populates="evaluation")


class TriageEnrichmentCache(Base):
    """Cross-event, identity-keyed enrichment cache.

    WHY THIS EXISTS
    ---------------
    Applicant.enrichment_raw freezes evidence per *applicant row*, so it makes a
    re-run of ONE event free — but the same person applying to a DIFFERENT event
    is a different applicant row and gets re-enriched from scratch, burning a real
    LinkedIn (Unipile) account action every time. At prod scale the same people
    apply to many events, so we re-fetch identical profiles repeatedly.

    This table caches the frozen RawEvidence by *resolved identity* instead of by
    row, shared across events, so a person enriched once is reused everywhere:

      - KEYED on a STRONG identity key only — a LinkedIn slug ("li:<slug>") or a
        salted email hash ("em:<sha256>"). Never a name (name-only collides across
        people — the Brittany/Kyndred class of bug). One logical profile is written
        under EVERY strong key we have, so a future event that knows EITHER the
        email OR the LinkedIn URL hits without a fetch.
      - email-first matters: the email hash is derivable from every Luma row for
        free, whereas the slug costs a people-search to obtain — so an email hit
        short-circuits the search itself.
      - VALUE is json.dumps(RawEvidence.as_dict()); rehydrated with from_dict.
      - fetched_at drives a TTL (see enrichment_cache._ttl_days): stale entries are
        treated as a miss so job/company facts don't ossify.
      - PII-safe: the email appears only as a salted hash in the KEY, never stored
        in cleartext. The evidence blob holds the same profile facts we already
        persist on Applicant.enrichment_raw.

    This is a brand-new table, so Base.metadata.create_all builds it on startup;
    no hand-rolled ALTER migration is needed (those are only for adding columns to
    existing tables)."""
    __tablename__ = "triage_enrichment_cache"

    # "li:<linkedin-slug>" or "em:<salted-email-sha256>". The only PK.
    cache_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    # json.dumps(RawEvidence.as_dict()) — the frozen, trimmed evidence.
    evidence: Mapped[str] = mapped_column(Text, default="")
    # Which provider produced it: "unipile" | "data_api" | "exa" | ...
    source: Mapped[str] = mapped_column(String(20), default="")
    # When the live fetch happened — drives the freshness/TTL check.
    fetched_at: Mapped[datetime] = mapped_column(default=_utcnow)


class ReviewDecision(Base):
    """Operator's accept/reject decision on an Applicant. Empty until the
    review UI (PR E) lets the operator act. One row per Applicant."""
    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(
        ForeignKey("applicants.id"), index=True, unique=True,
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)

    # The recommendation snapshot AT THE TIME of human decision : lets us
    # measure operator-override rate later (how often humans disagree with
    # the model). Populated from ApplicantEvaluation.recommendation.
    system_recommendation: Mapped[str] = mapped_column(String(20), default="")
    # accept | maybe | reject | needs_review
    human_decision: Mapped[str] = mapped_column(String(20))
    reviewer_notes: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime] = mapped_column(default=_utcnow)

    applicant: Mapped["Applicant"] = relationship(back_populates="decision")


class MatchEdge(Base):
    __tablename__ = "match_edges"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    a_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"))
    b_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"))
    edge_type: Mapped[str] = mapped_column(String(20))  # symbiotic | affinity
    weight: Mapped[float]

    event: Mapped["Event"] = relationship(back_populates="edges")


class Conversion(Base):
    __tablename__ = "conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"))
    goal: Mapped[str] = mapped_column(String(60))
    tier: Mapped[str] = mapped_column(String(10))            # high | mid | low
    state: Mapped[str] = mapped_column(String(10))           # won | partial | lost
    label: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(120))
    value: Mapped[int]

    prospect: Mapped["Prospect"] = relationship(back_populates="conversion")


# ─── Sponsors (Stage 04 extension) ───────────────────────────────────
# An event can declare ≥1 sponsor at intake; each sponsor brings a
# buyer_profile (target_role / seniority / company_stage / industry,
# implicit intent="buying") that the existing pairwise scorer pits
# against every attending Prospect's OFFERS/SEEKS vector.
#
# Same machinery as guest-pair scoring : matcher_lib pair-score logic
# when the LLM is available, heuristic fallback otherwise. SponsorMatch
# rows carry score + reasons[] so the same WHY? popover renders the same
# way for sponsor↔attendee pairs.


class Sponsor(Base):
    """One sponsor row tied to an event.

    `buyer_profile` is JSON-encoded and reuses the candidate vector
    schema (target_role / seniority / company_stage / industry / intent),
    so the existing pairwise scorer can consume it without a second
    code path : sponsor.buyer_profile is just "another candidate" from
    the scorer's perspective.
    """
    __tablename__ = "sponsors"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    tier: Mapped[str] = mapped_column(String(40), default="")
    # JSON: {target_role, seniority, company_stage, industry, intent}
    buyer_profile: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    event: Mapped["Event"] = relationship(back_populates="sponsors")
    matches: Mapped[list["SponsorMatch"]] = relationship(
        back_populates="sponsor", cascade="all, delete-orphan",
    )


class SponsorMatch(Base):
    """One sponsor↔prospect pair score for an event.

    Idempotent: re-running /match wipes prior SponsorMatch rows for the
    event before recomputing, mirroring how MatchEdge is handled.
    """
    __tablename__ = "sponsor_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    sponsor_id: Mapped[int] = mapped_column(ForeignKey("sponsors.id"), index=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"), index=True)
    # 0-100, same scale as MatchEdge.weight
    score: Mapped[float]
    # JSON list of short reason strings. Same provenance shape as the
    # guest-pair reasons that pair_explainer surfaces.
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    sponsor: Mapped["Sponsor"] = relationship(back_populates="matches")


# ─── Identity ──────────────────────────────────────────────────────
# Surplus auth = LinkedIn auth via Unipile's hosted flow. There's no
# separate email/password layer. A User row is created the first time
# someone successfully completes the Sign-in-with-LinkedIn flow; the
# Unipile account_id is the durable link to their LinkedIn.

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable Unipile id : same across re-connects of the same LinkedIn account
    # NULL for users who signed up via the triage-only path (no LinkedIn /
    # Unipile connection). Those users can use Applicant Triage features but
    # not outbound LinkedIn outreach until they connect later. Existing
    # rows from before this change all have values set.
    unipile_account_id: Mapped[Optional[str]] = mapped_column(
        String(80), unique=True, index=True, default=None,
    )
    # Profile data pulled from Unipile after auth (best-effort, refreshable)
    email: Mapped[Optional[str]] = mapped_column(String(200), default=None, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    headline: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    linkedin_public_id: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    linkedin_provider_id: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_login_at: Mapped[datetime] = mapped_column(default=_utcnow)
    # Connection health : flipped to "disconnected" if Unipile webhook fires
    # CREDENTIALS / DISCONNECTED. Re-auth flips it back to "active".
    linkedin_status: Mapped[str] = mapped_column(String(20), default="active")

    # ─── Email channel (Unipile GOOGLE / MICROSOFT account) ─────────────
    # A SECOND Unipile account on the same workspace, pointing at the user's
    # real mailbox (Gmail / Outlook). Independent of the LinkedIn seat above:
    # either can be connected without the other. Connected via the hosted-auth
    # flow in routes/auth.py (/email/start → /email/webhook), which is the
    # only writer of these fields.
    unipile_email_account_id: Mapped[Optional[str]] = mapped_column(
        String(80), unique=True, index=True, default=None,
    )
    # The mailbox address Unipile reports for the connected account (e.g.
    # "daniel@gmail.com"). Display-only; may differ from `email` above (the
    # profile/login email). Best-effort: NULL if the fetch didn't surface it.
    email_account_address: Mapped[Optional[str]] = mapped_column(
        String(200), default=None)
    # "disconnected" | "active" | "credentials" — mirrors linkedin_status
    # semantics (credentials = OAuth lapsed, needs the reconnect flow).
    email_status: Mapped[str] = mapped_column(String(20), default="disconnected")
    email_connected_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    # Operator-curated outreach exemplars used as style guides when Claude
    # composes personalized notes/DMs for their events. JSON-encoded list
    # of strings (each = one past outreach message). Empty / unset means
    # compose falls back to the env-var defaults or just generic personalized
    # output. Set via POST /admin/voice-examples.
    voice_examples: Mapped[str] = mapped_column(Text, default="")
    # When we last auto-synced voice_examples from this user's real LinkedIn
    # sent-messages (via Unipile). NULL = never synced; gates the lazy pull so
    # we don't re-scan their inbox on every compose. Manually-curated examples
    # (set via POST /admin/voice-examples) leave this NULL and are never
    # overwritten by the auto-sync.
    voice_synced_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    # Cached structured voice profile (distilled style rules) for this host,
    # built from voice_examples by agents/voice.build_host_voice_profile. JSON
    # object: {"fingerprint": <hash of the examples it was built from>,
    # "profile": {...}}. Empty / unset means "no cache" — the drafting surfaces
    # rebuild the profile inline (it's cheap + deterministic). The fingerprint
    # lets us invalidate the cache when voice_examples change.
    voice_profile: Mapped[str] = mapped_column(Text, default="")

    # Opt-in toggle for the "Gmail Schedule Send" auto follow-up feature. When
    # False (the default), sending a first DM does NOT auto-stage a scheduled
    # follow-up : the host has not asked us to. Flipped via the followups
    # settings route. Gated in agents/followup_scheduler.stage_followup so the
    # whole feature is off for a user until they explicitly turn it on.
    auto_followups_enabled: Mapped[bool] = mapped_column(default=False)

    # ─── First-time-user onboarding (in-person coachmark tour) ──────────
    # Lifecycle of the guided coachmark flow that walks a brand-new user
    # through their first event → contact → send → relationships hub.
    #   ""        : never armed (guest / pre-feature account, or not yet
    #               connected). The default for a fresh row.
    #   "active"  : armed — set the INSTANT the user first gains a LinkedIn
    #               connection (see routes/auth: webhook + callback). The
    #               in-person surface runs the tour from onboarding_step.
    #   "done"    : finished the flow (or auto-backfilled for users who were
    #               already connected before this feature shipped).
    #   "skipped" : dismissed the whole flow. Re-runnable from settings,
    #               which flips this back to "active" + step 0.
    # Gated on the empty default so the arm fires exactly once per user and
    # never on a re-connect / profile refresh.
    onboarding_status: Mapped[str] = mapped_column(String(20), default="")
    # Which coachmark the user is on (0-based index into the 7-step flow).
    # Persisted on every advance so the tour resumes in place after a refresh
    # or a device switch — the server is the source of truth.
    onboarding_step: Mapped[int] = mapped_column(default=0)
    # The host's reusable demo / Calendly link. Captured once during the
    # "attach a link" onboarding step (or any in-person capture whose next
    # step is a URL) and then pre-filled / auto-suggested on every future
    # send. NULL until the first link is captured.
    saved_send_link: Mapped[Optional[str]] = mapped_column(String(400), default=None)

    # ─── Billing ───────────────────────────────────────────────────────
    # Stripe customer id, set by the checkout webhook on first successful
    # payment. Indexed because the webhook path looks users up by it.
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String(120), default=None, index=True,
    )
    # When the user's most recent Stripe Checkout completed. NULL = never
    # paid (or refunded out). require_can_send_linkedin() blocks real LinkedIn
    # sends when NULL : free tier can browse + run prospecting + see
    # composed previews, paid tier unlocks the actual outreach.
    #
    # NOTE: paid_at is the LEGACY one-time-unlock gate for LinkedIn SENDS and
    # stays independent of the plan/usage fields below. The plan tier meters a
    # DIFFERENT surface — the relationship layer's drafting + contact scanning —
    # so a user can be on a paid plan without paid_at set, and vice versa.
    paid_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    # ─── Subscription plan + metered usage (relationship layer) ─────────
    # Tier the user is on. One of "free" | "starter" | "pro". Drives the
    # per-period draft + contact-scan limits in backend/billing_plans.py.
    # Stamped by the Stripe pricing-table webhook (price_id -> plan); demo
    # accounts (is_demo_user) bypass limits entirely regardless of plan.
    plan: Mapped[str] = mapped_column(String(20), default="free")
    # Mirrors the Stripe Subscription.status ("active", "trialing",
    # "past_due", "canceled", ...) or "free" when there's no subscription.
    subscription_status: Mapped[str] = mapped_column(String(30), default="free")
    # Stripe Subscription / Price ids, set by the subscription webhooks. Both
    # NULL on the free tier. subscription_id is how subscription.updated /
    # .deleted events resolve back to this row.
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(120), default=None, index=True,
    )
    stripe_price_id: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    # Metered usage in the CURRENT billing period. Reset to 0 by the webhook on
    # a fresh checkout/renewal and by the in-app period roll when now passes
    # billing_period_end. Each staged follow-up DRAFT card increments drafts;
    # each contact the agent triages increments contacts_scanned.
    drafts_used_this_period: Mapped[int] = mapped_column(default=0)
    contacts_scanned_this_period: Mapped[int] = mapped_column(default=0)
    # Current period bounds. For paid plans these come from Stripe
    # (current_period_start/end). For the free tier we roll a 30-day window
    # in-app (NULL until the user's first metered action seeds it).
    billing_period_start: Mapped[Optional[datetime]] = mapped_column(default=None)
    billing_period_end: Mapped[Optional[datetime]] = mapped_column(default=None)


# ─── Curation (Stage 1-5: ingested-audience workflow) ─────────────────
# A separate row-type from Prospect (outbound-sourced) and Applicant
# (Luma triage). Attendees come from CSV imports the operator already
# owns : alumni, members, past attendees, nominees, sponsor target lists.
# The curation module (backend/curation/) runs scoring + matching +
# outreach + attribution on these rows.


class Attendee(Base):
    """One person ingested from a CSV / RSVP list, scoped to an Event.

    Identity comes from the operator, not from web discovery : we treat
    `email` (when present) or normalized `name+company` as the dedupe key.
    """
    __tablename__ = "curation_attendees"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)

    # Canonical CSV fields
    name: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[Optional[str]] = mapped_column(String(200), default=None, index=True)
    role: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    company: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    seniority: Mapped[Optional[str]] = mapped_column(String(60), default=None)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    # Source list this attendee came from (alumni / members / past_attendees /
    # nominees / sponsor_targets / rsvp / other). Operator-supplied at import.
    list_source: Mapped[str] = mapped_column(String(40), default="other")
    # For guest-list imports: invited | rsvp_yes | rsvp_no | waitlist | attended
    rsvp_status: Mapped[Optional[str]] = mapped_column(String(20), default=None)

    # Everything else from the CSV that didn't map. JSON dict.
    raw: Mapped[str] = mapped_column(Text, default="{}")

    # Enrichment cache : firmographic, role, seniority data from Claude.
    # JSON dict; empty until enrich_attendee() runs. Keyed by source
    # (e.g. "claude_firmographic", "claude_role"). See curation/enrichment.py
    # for the schema.
    enrichment: Mapped[str] = mapped_column(Text, default="{}")
    enriched_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    # Near-term feature payloads (always stored, only computed when the
    # corresponding feature flag is on : see curation/features.py).
    news_signal: Mapped[str] = mapped_column(Text, default="{}")           # JSON
    recognition_flags: Mapped[str] = mapped_column(Text, default="[]")     # JSON list
    warm_connection: Mapped[str] = mapped_column(Text, default="{}")       # JSON

    # Rule-based ICP fit, 0-100. Stored alongside its rule-based reasoning
    # (machine-readable list of triggered rules) and the optional LLM-written
    # rationale that explains the rules in plain English.
    fit_score: Mapped[int] = mapped_column(default=0)
    fit_rule_trace: Mapped[str] = mapped_column(Text, default="[]")        # JSON list of rule hits
    fit_rationale: Mapped[str] = mapped_column(Text, default="")            # plain-English from Claude

    # Near-term: predicted no-show probability 0.0-1.0. Empty when feature off.
    no_show_probability: Mapped[Optional[float]] = mapped_column(default=None)
    no_show_rationale: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)

    event: Mapped["Event"] = relationship()


class AttendeeIntro(Base):
    """A rule-based intro recommendation between two attendees at one event.

    Direction-asymmetric pairing: row stores the "introduce A to B" framing
    so each side gets a tailored reason. Two rows per pair (A→B and B→A)
    when both directions make sense.
    """
    __tablename__ = "curation_intros"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    from_attendee_id: Mapped[int] = mapped_column(ForeignKey("curation_attendees.id"))
    to_attendee_id: Mapped[int] = mapped_column(ForeignKey("curation_attendees.id"))
    # 0.0-1.0 rule-based pairing weight
    weight: Mapped[float] = mapped_column(default=0.0)
    # Machine-readable rule trace : which complementary signals triggered
    # the recommendation. JSON list of strings.
    rule_trace: Mapped[str] = mapped_column(Text, default="[]")
    # Optional human-readable reason. NOT claimed as AI unless generated
    # by Claude : current rule-based builder leaves this empty.
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AttendeeFollowUp(Base):
    """One logged post-event follow-up touchpoint on an attendee."""
    __tablename__ = "curation_followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    attendee_id: Mapped[int] = mapped_column(ForeignKey("curation_attendees.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # meeting | email | dm | call | intro | other
    kind: Mapped[str] = mapped_column(String(20), default="other")
    notes: Mapped[str] = mapped_column(Text, default="")
    # ISO date string when the follow-up happened; falls back to created_at
    occurred_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AttendeeAttribution(Base):
    """Claude-derived outcome attribution : maps an event to an outcome on
    an attendee (meeting, hire, partnership, pipeline) with reasoning."""
    __tablename__ = "curation_attributions"

    id: Mapped[int] = mapped_column(primary_key=True)
    attendee_id: Mapped[int] = mapped_column(ForeignKey("curation_attendees.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # meeting | hire | partnership | pipeline | revenue | other | none
    outcome: Mapped[str] = mapped_column(String(20))
    # 0.0-1.0 model confidence that the event drove this outcome
    confidence: Mapped[float] = mapped_column(default=0.0)
    # Dollar value (optional, 0 when unmonetary).
    value: Mapped[int] = mapped_column(default=0)
    # Claude's reasoning : auditable.
    rationale: Mapped[str] = mapped_column(Text, default="")
    # Source signals the attribution leaned on. JSON list.
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class LLMCall(Base):
    """Audit log for every Claude call the curation module makes.

    Stores prompt + raw output so the rationale on any scored / attributed
    row stays auditable after the fact. Indexed by (event_id, purpose) so
    operators can pull every score-rationale call for one event in one query.
    """
    __tablename__ = "curation_llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable so unit tests / smoke calls without an event still log
    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("events.id"), default=None, index=True,
    )
    attendee_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("curation_attendees.id"), default=None, index=True,
    )
    # score_rationale | enrichment | outreach | attribution | gap_analysis | other
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(60), default="")
    # The system + user prompt concatenated as one string. Capped at 16k
    # so we don't blow up the DB; longer prompts get a "[truncated]" tail.
    prompt: Mapped[str] = mapped_column(Text, default="")
    # Raw model output. Cap mirrors prompt.
    output: Mapped[str] = mapped_column(Text, default="")
    # ok | error | parse_error | disabled (no API key) | dry_run
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    latency_ms: Mapped[Optional[int]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class StripeWebhookEvent(Base):
    """One row per Stripe event the billing webhook has fully processed.

    Stripe delivery is at-least-once: a timeout / deploy / transient non-2xx
    means the SAME event is re-sent (possibly hours later, possibly out of
    order). This table is the idempotency ledger — the handler acks any
    event_id it has already seen without re-running side effects, instead of
    relying on every write happening to be harmless to repeat.

    The marker is committed in the SAME transaction as the handler's
    mutations: a crash mid-handler rolls back both, so Stripe's retry
    processes the event cleanly (never half-applied, never double-applied).
    """
    __tablename__ = "stripe_webhook_events"

    # Stripe event ids ("evt_...") are globally unique; natural primary key.
    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    processed_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AuthState(Base):
    """Short-lived state token created when a user clicks Sign in with LinkedIn.

    Bridges the race between Unipile's webhook (fires when account is created
    on their side) and the user's browser landing on /api/auth/linkedin/callback.
    Whichever arrives first writes; the second reads and completes the flow.
    """
    __tablename__ = "auth_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    state_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | webhook_done | callback_done | failed
    error: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(default=None)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(default=_utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(default=None)


# ─── Relationship graph (the cross-event spine) ──────────────────────
# Surplus is event-native relationship intelligence. A Prospect is the
# per-event record of someone you met; a Contact is the DURABLE person
# across every event, owned by one user. RelationshipInteraction is the
# normalized, append-only touch log that net-new touch types (manual
# notes, email, calendar, intros) write into — derived touches
# (capture / OutreachLog / Conversion) are NOT duplicated here; the
# timeline assembler in agents/relationships.py unions both sources.


class Contact(Base):
    """One durable person in a user's relationship graph, deduped across events
    by a strong identity key (LinkedIn slug or salted email hash — never a
    name). Lazily created : a Prospect links to a Contact only when a stable
    identity is known. Event-scoped Prospect flows work fine with no Contact."""
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "primary_identity_key",
                         name="uq_contact_owner_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owner : the relationship graph is per-user, never shared across users.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Strongest identity_keys() key for this person ("li:<slug>" | "em:<hash>").
    primary_identity_key: Mapped[str] = mapped_column(String(120), index=True)

    name: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    linkedin_public_id: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    email: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    company: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    company_domain: Mapped[Optional[str]] = mapped_column(String(160), default=None)
    # The Unipile email thread the HOST CONFIRMED as "my thread with this
    # person" (manual link via /contacts/{id}/email-thread — never guessed).
    # Once set, the email channel reads (pull) and replies (push) within
    # this one thread, so Gmail/Outlook threading stays intact.
    email_thread_id: Mapped[Optional[str]] = mapped_column(String(160), default=None)

    # --- Relationship-watch snapshot (CRM auto-updates) ---------------------
    # The last-seen LinkedIn state for this person, refreshed by the scheduled
    # CRM refresh job (agents/relationship_watch.py). Diffing a fresh Unipile
    # fetch against these is how we detect "changed jobs" / "new headline" and
    # emit an activity_update RelationshipInteraction. `company` above doubles
    # as the last-seen company (so a company change updates it in place).
    headline: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    title: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # JSON list of post ids already surfaced, so we alert on each new post once.
    seen_post_ids: Mapped[str] = mapped_column(Text, default="[]")
    # Last successful poll (NULL = never polled -> first poll seeds silently).
    watched_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    # Last poll error message (ops visibility; cleared on a clean poll).
    watch_error: Mapped[Optional[str]] = mapped_column(String(300), default=None)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    prospects: Mapped[list["Prospect"]] = relationship(back_populates="contact")
    interactions: Mapped[list["RelationshipInteraction"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan",
    )


class RelationshipInteraction(Base):
    """One stored touch in the relationship graph : a manual note, an email, a
    calendar meeting, an intro. Append-only. Derived touches (capture,
    OutreachLog, Conversion) are reconstructed on read, NOT stored here, so this
    table only holds net-new signal the rest of the schema can't reproduce."""
    __tablename__ = "relationship_interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Who recorded it (the owning user).
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Either / both may be set : prospect_id ties it to a per-event record,
    # contact_id to the durable person. Both nullable so a touch can attach to
    # whichever exists.
    prospect_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("prospects.id"), default=None, index=True,
    )
    contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("contacts.id"), default=None, index=True,
    )
    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("events.id"), default=None, index=True,
    )
    company_domain: Mapped[Optional[str]] = mapped_column(String(160), default=None)

    # Timeline-item shape (see agents/relationships.py).
    source_type: Mapped[str] = mapped_column(String(40))
    interaction_type: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(10), default="none")
    occurred_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    # JSON-encoded dict (Text for cross-dialect portability — same convention as
    # Sponsor.buyer_profile). Named meta_json because `metadata` is reserved on
    # the SQLAlchemy declarative Base.
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    # private = visible only to the owner; team = shared with the owner's team.
    visibility: Mapped[str] = mapped_column(String(10), default="private")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    contact: Mapped[Optional["Contact"]] = relationship(back_populates="interactions")
    prospect: Mapped[Optional["Prospect"]] = relationship()


class Job(Base):
    """An async background job (search / match) executed off the request path.

    The heavy outbound stages — prospecting ("search") and matching — used to
    run inline in the HTTP handler, blocking the request for tens of seconds.
    They now run as a Job: the route creates a queued Job, dispatches the work
    (to Modal when USE_MODAL=1, else a local BackgroundTask), and returns the
    job id immediately. The frontend polls GET .../jobs/{id} until status flips
    to "done", then reads result_json (a serialized PipelineResult/MatchResult).

    The row lives in Postgres so it's visible across workers AND across the
    Railway↔Modal boundary: whichever process does the work updates this row,
    and any web worker serving a poll request reads it back.
    """
    __tablename__ = "jobs"

    # UUID hex string : generated app-side so the route can hand it back before
    # the worker has touched the DB. String PK keeps it dialect-portable.
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # Owner, for the poll-auth check. Nullable to tolerate operator/legacy paths.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), default=None, index=True
    )
    # "prospect" (search) | "match".
    kind: Mapped[str] = mapped_column(String(20))
    # queued -> running -> done | error.
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # Serialized PipelineResult / MatchResult JSON, set when status == done.
    result_json: Mapped[str] = mapped_column(Text, default="")
    # Human-readable failure message, set when status == error.
    error: Mapped[str] = mapped_column(Text, default="")
    # Where the work ran : "modal" | "local". Diagnostics only.
    runner: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class ScheduledFollowup(Base):
    """One auto-drafted follow-up DM staged to send at a user-chosen time.

    The "Gmail Schedule Send" model: the moment a first DM goes out we draft
    a context-aware follow-up and pick a sensible send time, then stage it
    HERE for the host to review, edit, reschedule, or cancel. The dispatch
    cron (admin.run-followups) sends every row whose send_at has arrived and
    is still `scheduled`. An inbound reply auto-cancels any pending row so we
    never "circle back" to someone who already responded.

    Exactly one pending (status="scheduled") row exists per prospect at a
    time : stage_followup() is idempotent on (prospect_id, status="scheduled").

    status     : scheduled -> sent | cancelled | failed
    body       : the editable draft; what actually gets sent at dispatch time
    send_at    : the user-controlled fire time (defaults to suggested_send_at)
    suggested_send_at : the original system suggestion, kept for audit/UX even
                        after the user reschedules
    cancel_reason : "replied" (auto) | "user" (manual) | "" : audit only
    """
    __tablename__ = "scheduled_followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Transport for the dispatch : "linkedin" (historical default) | "email"
    # (sends via the owner's connected mailbox + linked thread).
    channel: Mapped[str] = mapped_column(String(20), default="linkedin")
    prospect_id: Mapped[int] = mapped_column(
        ForeignKey("prospects.id"), index=True
    )
    body: Mapped[str] = mapped_column(Text, default="")
    send_at: Mapped[datetime] = mapped_column(index=True)
    suggested_send_at: Mapped[datetime] = mapped_column(default=_utcnow)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)
    cancel_reason: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    prospect: Mapped["Prospect"] = relationship()
