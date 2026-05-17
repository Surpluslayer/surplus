"""
models.py — the persistence layer.

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
    # Owner — every event belongs to exactly one signed-in user. Nullable for
    # backwards compatibility with rows that pre-date multi-tenant; new rows
    # always have it set by the events POST handler. Backfilled to the operator
    # user on first migration via _migrate_event_user_id() in db.py.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), default=None, index=True
    )
    # ICP
    role: Mapped[str] = mapped_column(String(200))
    # seniority / co_stage / goal are multi-select on the frontend; stored
    # CSV-joined ("Senior,Staff+") in these String columns so we don't need a
    # migration. Widths bumped to fit the longest plausible CSV concatenation.
    seniority: Mapped[str] = mapped_column(String(200))
    co_stage: Mapped[str] = mapped_column(String(120))
    # event shape
    headcount: Mapped[int]
    format: Mapped[str] = mapped_column(String(40))
    city: Mapped[str] = mapped_column(String(80))
    # goal + budget
    goal: Mapped[str] = mapped_column(String(300))
    budget: Mapped[int]
    # derived once the pipeline runs
    threshold: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    prospects: Mapped[list["Prospect"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    edges: Mapped[list["MatchEdge"]] = relationship(
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

    # market side + value vectors — what the matcher pairs on
    side: Mapped[str] = mapped_column(String(20), default="Builds")
    works_on: Mapped[str] = mapped_column(String(60), default="general")
    offers: Mapped[str] = mapped_column(String(200), default="")
    seeks: Mapped[str] = mapped_column(String(200), default="")

    # raw source signal
    gh_stars: Mapped[int] = mapped_column(default=0)
    x_followers: Mapped[int] = mapped_column(default=0)
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
    status: Mapped[str] = mapped_column(String(20), default="surfaced")
    group_id: Mapped[Optional[int]] = mapped_column(default=None)

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

    # provider tracking (nullable — only set when a real provider was invoked)
    provider: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    provider_lead_id: Mapped[Optional[str]] = mapped_column(String(80), default=None)

    prospect: Mapped["Prospect"] = relationship(back_populates="outreach")


class PendingReply(Base):
    """One AI-drafted reply waiting for a human decision.

    The reply agent (agents/reply_agent.py) runs on every inbound LinkedIn
    message and produces a `ReplyDecision`. When the classification isn't
    in the auto-send allow-list (or the loop guard fires), the draft lands
    here for an operator to approve / edit / reject via /admin/pending-replies.

    classification : the agent's bucket — clarifying | commitment | off_topic
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


# ─── Identity ──────────────────────────────────────────────────────
# Surplus auth = LinkedIn auth via Unipile's hosted flow. There's no
# separate email/password layer. A User row is created the first time
# someone successfully completes the Sign-in-with-LinkedIn flow; the
# Unipile account_id is the durable link to their LinkedIn.

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable Unipile id — same across re-connects of the same LinkedIn account
    unipile_account_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
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
    # Connection health — flipped to "disconnected" if Unipile webhook fires
    # CREDENTIALS / DISCONNECTED. Re-auth flips it back to "active".
    linkedin_status: Mapped[str] = mapped_column(String(20), default="active")


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
