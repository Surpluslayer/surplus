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

from sqlalchemy import ForeignKey, String, Text
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
    role: Mapped[str] = mapped_column(String(160), default="Unknown")
    company: Mapped[str] = mapped_column(String(120), default="Unknown")
    seniority: Mapped[str] = mapped_column(String(40), default="Mid")

    # market side + value vectors : what the matcher pairs on
    side: Mapped[str] = mapped_column(String(20), default="Builds")
    works_on: Mapped[str] = mapped_column(String(60), default="general")
    offers: Mapped[str] = mapped_column(String(200), default="")
    seeks: Mapped[str] = mapped_column(String(200), default="")

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
    # prospects. `note` is the optional personal line the operator wants on the
    # LinkedIn connect request (≤300 to fit LinkedIn's note cap); `captured_at`
    # is when the row was scanned; `source` records the capture channel
    # ("scan" | "link" | "text").
    note: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    captured_at: Mapped[Optional[datetime]] = mapped_column(default=None)
    source: Mapped[Optional[str]] = mapped_column(String(20), default=None)

    # LinkedIn connection state. Drives whether a "reach out" action sends a
    # connection request (cold) or a direct DM (warm). Default "unknown"
    # until the first Unipile relation check; flipped to "connected" by the
    # invite_accepted webhook so subsequent actions take the warm path.
    connection_status: Mapped[str] = mapped_column(String(20), default="unknown")
    connection_checked_at: Mapped[Optional[datetime]] = mapped_column(default=None)

    event: Mapped["Event"] = relationship(back_populates="prospects")
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
    # Operator-curated outreach exemplars used as style guides when Claude
    # composes personalized notes/DMs for their events. JSON-encoded list
    # of strings (each = one past outreach message). Empty / unset means
    # compose falls back to the env-var defaults or just generic personalized
    # output. Set via POST /admin/voice-examples.
    voice_examples: Mapped[str] = mapped_column(Text, default="")

    # ─── Billing ───────────────────────────────────────────────────────
    # Stripe customer id, set by the checkout webhook on first successful
    # payment. Indexed because the webhook path looks users up by it.
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String(120), default=None, index=True,
    )
    # When the user's most recent Stripe Checkout completed. NULL = never
    # paid (or refunded out). require_linkedin_send() blocks real LinkedIn
    # sends when NULL : free tier can browse + run prospecting + see
    # composed previews, paid tier unlocks the actual outreach.
    paid_at: Mapped[Optional[datetime]] = mapped_column(default=None)


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
