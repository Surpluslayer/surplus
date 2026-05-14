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
    # ICP
    role: Mapped[str] = mapped_column(String(200))
    seniority: Mapped[str] = mapped_column(String(40))
    co_stage: Mapped[str] = mapped_column(String(40))
    # event shape
    headcount: Mapped[int]
    format: Mapped[str] = mapped_column(String(40))
    city: Mapped[str] = mapped_column(String(80))
    # goal + budget
    goal: Mapped[str] = mapped_column(String(60))
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
    __tablename__ = "outreach_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id"))
    channel: Mapped[str] = mapped_column(String(20), default="email")
    state: Mapped[str] = mapped_column(String(20))  # sent | opened | replied
    body: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(default=_utcnow)

    prospect: Mapped["Prospect"] = relationship(back_populates="outreach")


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
