"""
schemas.py — API request/response shapes.

Pydantic models with `build()` / `from_*` classmethods that assemble a clean
response from ORM objects. Keeping the assembly here means routes stay thin and
the wire format is defined in exactly one place.
"""
from __future__ import annotations
from datetime import datetime

from pydantic import BaseModel

from . import config


# ── stage 01: intake ──────────────────────────────────────────────────────
class EventCreate(BaseModel):
    """Intake profile. Defaults match the demo so `POST /events {}` just works."""
    role: str = "Infrastructure / ML platform engineers"
    seniority: str = "Staff+"
    co_stage: str = "Seed"
    headcount: int = 40
    format: str = "Sit-down dinner"
    city: str = "San Francisco"
    goal: str = "Hiring pipeline"
    budget: int = 12000


class EventOut(BaseModel):
    id: int
    role: str
    seniority: str
    co_stage: str
    headcount: int
    format: str
    city: str
    goal: str
    budget: int
    threshold: int
    funnel_target: int
    cost_per_seat: int
    created_at: datetime

    @classmethod
    def of(cls, ev) -> "EventOut":
        return cls(
            id=ev.id, role=ev.role, seniority=ev.seniority, co_stage=ev.co_stage,
            headcount=ev.headcount, format=ev.format, city=ev.city, goal=ev.goal,
            budget=ev.budget, threshold=ev.threshold,
            funnel_target=round(ev.headcount / config.FUNNEL_CONVERSION),
            cost_per_seat=round(ev.budget / ev.headcount) if ev.headcount else 0,
            created_at=ev.created_at,
        )


# ── stage 02-03: prospects + outreach ─────────────────────────────────────
class OutreachOut(BaseModel):
    state: str
    body: str
    ts: datetime


class ProspectOut(BaseModel):
    id: int
    name: str
    role: str
    company: str
    seniority: str
    side: str
    works_on: str
    offers: str
    seeks: str
    gh_stars: int
    x_followers: int
    li_resolved: bool
    sources: str
    fit_score: int
    fit_reason: str
    status: str
    above_threshold: bool
    group_id: int | None
    outreach: list[OutreachOut]

    @classmethod
    def of(cls, p, threshold: int) -> "ProspectOut":
        return cls(
            id=p.id, name=p.name, role=p.role, company=p.company,
            seniority=p.seniority, side=p.side, works_on=p.works_on,
            offers=p.offers, seeks=p.seeks, gh_stars=p.gh_stars,
            x_followers=p.x_followers, li_resolved=p.li_resolved,
            sources=p.sources, fit_score=p.fit_score, fit_reason=p.fit_reason,
            status=p.status, above_threshold=p.fit_score >= threshold,
            group_id=p.group_id,
            outreach=[OutreachOut(state=o.state, body=o.body, ts=o.ts)
                      for o in sorted(p.outreach, key=lambda o: o.ts)],
        )


class PipelineResult(BaseModel):
    event: EventOut
    counts: dict[str, int]
    prospects: list[ProspectOut]

    @classmethod
    def build(cls, ev, prospects) -> "PipelineResult":
        rows = [ProspectOut.of(p, ev.threshold) for p in
                sorted(prospects, key=lambda p: -p.fit_score)]
        counts = {
            "surfaced": len(rows),
            "above_threshold": sum(r.above_threshold for r in rows),
            "contacted": sum(r.status == "contacted" for r in rows),
            "rsvp": sum(r.status == "rsvp" for r in rows),
            "below": sum(r.status == "below" for r in rows),
        }
        return cls(event=EventOut.of(ev), counts=counts, prospects=rows)


# ── stage 04: matching ────────────────────────────────────────────────────
class EdgeOut(BaseModel):
    a_id: int
    b_id: int
    edge_type: str
    weight: float


class GroupOut(BaseModel):
    group_id: int
    group_word: str
    members: list[dict]      # {id, name, side, company}
    builds: int
    counterparts: int


class MatchResult(BaseModel):
    event_id: int
    group_word: str
    topology: str
    edges: list[EdgeOut]
    groups: list[GroupOut]
    top_symbiotic: list[dict]   # {a, b, weight, flow}

    @classmethod
    def build(cls, ev, attending, edges, groups) -> "MatchResult":
        fcfg = config.format_cfg(ev.format)
        by_id = {p.id: p for p in attending}

        group_rows = []
        for gid, members in sorted(groups.items()):
            group_rows.append(GroupOut(
                group_id=gid,
                group_word=fcfg["group_word"],
                members=[{"id": m.id, "name": m.name, "side": m.side,
                          "company": m.company} for m in members],
                builds=sum(m.side == "Builds" for m in members),
                counterparts=sum(m.side != "Builds" for m in members),
            ))

        top = sorted((e for e in edges if e["edge_type"] == "symbiotic"),
                     key=lambda e: -e["weight"])[:4]
        top_rows = []
        for e in top:
            a, b = by_id[e["a_id"]], by_id[e["b_id"]]
            top_rows.append({
                "a": a.name, "b": b.name, "weight": e["weight"],
                "flow": [f"{a.offers} -> {b.seeks}", f"{b.offers} -> {a.seeks}"],
            })

        return cls(
            event_id=ev.id,
            group_word=fcfg["group_word"],
            topology=fcfg["topology"],
            edges=[EdgeOut(**e) for e in edges],
            groups=group_rows,
            top_symbiotic=top_rows,
        )


# ── stage 05: ROI ─────────────────────────────────────────────────────────
class LedgerRow(BaseModel):
    prospect_id: int
    name: str
    company: str
    side: str
    tier: str
    state: str
    label: str
    detail: str
    value: int


class RoiResult(BaseModel):
    event_id: int
    metrics: dict
    ledger: list[LedgerRow]

    @classmethod
    def build(cls, ev, ledger, metrics) -> "RoiResult":
        return cls(
            event_id=ev.id,
            metrics=metrics,
            ledger=[LedgerRow(**{k: r[k] for k in (
                "prospect_id", "name", "company", "side",
                "tier", "state", "label", "detail", "value")}) for r in ledger],
        )
