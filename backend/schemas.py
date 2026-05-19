"""
schemas.py : API request/response shapes.

Pydantic models with `build()` / `from_*` classmethods that assemble a clean
response from ORM objects. Keeping the assembly here means routes stay thin and
the wire format is defined in exactly one place.
"""
from __future__ import annotations
from datetime import datetime

from pydantic import BaseModel

from . import config


# ── stage 01: intake ──────────────────────────────────────────────────────
def _split_csv(v) -> list[str]:
    """Split a CSV-stored multi-select column back into a list. Accepts None,
    empty, or an already-list value (no-op) so callers don't need to care."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in (v or "").split(",") if s.strip()]


class EventCreate(BaseModel):
    """Intake profile. Defaults match the demo so `POST /events {}` just works.

    `seniority`, `co_stage`, and `goal` are multi-select : the frontend sends
    lists; storage is CSV-joined in the existing String columns to avoid a
    schema migration.
    """
    role: str = "Infrastructure / ML platform engineers"
    seniority: list[str] = ["Staff+"]
    co_stage: list[str] = ["Seed"]
    headcount: int = 40
    format: str = "Sit-down dinner"
    city: str = "San Francisco"
    # YYYY-MM-DD; empty when unset.
    event_date: str = ""
    # Operator-supplied display name. Empty falls back to "event #<id>".
    event_name: str = ""
    goal: list[str] = ["Hiring pipeline"]
    budget: int = 8000
    # Which prospect sources to fan out across (LinkedIn always forced in
    # server-side by adapters_for(); see backend/agents/sources/__init__.py).
    sources: list[str] = ["linkedin"]
    # Years-of-experience buckets. Empty list == no preference.
    yoe: list[str] = []


class EventOut(BaseModel):
    id: int
    role: str
    seniority: list[str]
    co_stage: list[str]
    headcount: int
    format: str
    city: str
    event_date: str
    event_name: str
    goal: list[str]
    budget: int
    sources: list[str]
    yoe: list[str]
    threshold: int
    funnel_target: int
    cost_per_seat: int
    created_at: datetime

    @classmethod
    def of(cls, ev) -> "EventOut":
        return cls(
            id=ev.id, role=ev.role,
            seniority=_split_csv(ev.seniority),
            co_stage=_split_csv(ev.co_stage),
            headcount=ev.headcount, format=ev.format, city=ev.city,
            event_date=getattr(ev, "event_date", "") or "",
            event_name=getattr(ev, "event_name", "") or "",
            goal=_split_csv(ev.goal),
            budget=ev.budget,
            sources=_split_csv(getattr(ev, "sources", None)) or ["linkedin"],
            yoe=_split_csv(getattr(ev, "yoe", None)),
            threshold=ev.threshold,
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
    scholar_citations: int
    li_resolved: bool
    linkedin_url: str | None
    sources: str
    fit_score: int
    fit_reason: str
    status: str
    above_threshold: bool
    group_id: int | None
    # unknown | not_connected | connected. Drives the cold-vs-warm send
    # routing and the dynamic button label on the auto-outreach screen.
    connection_status: str
    outreach: list[OutreachOut]

    @classmethod
    def of(cls, p, threshold: int) -> "ProspectOut":
        return cls(
            id=p.id, name=p.name, role=p.role, company=p.company,
            seniority=p.seniority, side=p.side, works_on=p.works_on,
            offers=p.offers, seeks=p.seeks, gh_stars=p.gh_stars,
            x_followers=p.x_followers,
            scholar_citations=getattr(p, "scholar_citations", 0) or 0,
            li_resolved=p.li_resolved,
            linkedin_url=p.linkedin_url,
            sources=p.sources, fit_score=p.fit_score, fit_reason=p.fit_reason,
            status=p.status, above_threshold=p.fit_score >= threshold,
            group_id=p.group_id,
            connection_status=getattr(p, "connection_status", "unknown") or "unknown",
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


# ── stage 03b: outreach run + preview + log ───────────────────────────────

class OutreachOverride(BaseModel):
    """Optional human edits to the agent-composed note/message before send."""
    note: str | None = None
    message: str | None = None



class OutreachActionResult(BaseModel):
    prospect_id: int
    state: str
    provider: str
    provider_lead_id: str | None
    dry_run: bool
    error: str | None = None


class OutreachRunResult(BaseModel):
    event_id: int
    provider: str
    dry_run: bool
    counts: dict[str, int]
    results: list[OutreachActionResult]
    event: EventOut
    prospects: list[ProspectOut]

    @classmethod
    def build(cls, ev, prospects, results) -> "OutreachRunResult":
        from .providers import get_provider
        prov = get_provider()
        rows = [ProspectOut.of(p, ev.threshold) for p in
                sorted(prospects, key=lambda p: -p.fit_score)]
        counts = {
            "results_total": len(results),
            "above_threshold": sum(r.above_threshold for r in rows),
            "approved": sum(r.status == "approved" for r in rows),
            "contacted": sum(r.status == "contacted" for r in rows),
            "rsvp": sum(r.status == "rsvp" for r in rows),
            "below": sum(r.status == "below" for r in rows),
        }
        return cls(
            event_id=ev.id,
            provider=prov.name,
            dry_run=prov.dry_run,
            counts=counts,
            results=[OutreachActionResult(
                prospect_id=r.prospect_id, state=r.state,
                provider=r.provider, provider_lead_id=r.provider_lead_id,
                dry_run=r.dry_run, error=r.error,
            ) for r in results],
            event=EventOut.of(ev),
            prospects=rows,
        )


class ProspectingPreviewCandidate(BaseModel):
    identity: str
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
    scholar_citations: int = 0
    li_resolved: bool
    linkedin_url: str | None
    sources: str
    llm_verdict: str | None
    # what compose() would produce if this candidate were persisted + outreached
    note: str
    note_chars: int
    message: str


class ProspectingPreview(BaseModel):
    event_id: int
    mode: str           # "llm" | "mock"
    count: int
    candidates: list[ProspectingPreviewCandidate]


class OutreachPreviewRow(BaseModel):
    prospect_id: int
    name: str
    company: str
    linkedin_url: str | None
    fit_score: int
    eligible: bool
    skip_reason: str | None
    note: str
    note_chars: int
    message: str
    payload: dict | None  # the provider-shaped payload that would be sent


class OutreachPreview(BaseModel):
    event_id: int
    provider: str
    dry_run: bool
    count_eligible: int
    count_skipped: int
    prospects: list[OutreachPreviewRow]


class OutreachLogEntry(BaseModel):
    id: int
    prospect_id: int
    prospect_name: str
    channel: str
    state: str
    provider: str | None
    provider_lead_id: str | None
    body_preview: str  # first 300 chars of body
    ts: datetime


class OutreachLogResult(BaseModel):
    event_id: int
    count: int
    entries: list[OutreachLogEntry]

    @classmethod
    def build(cls, ev) -> "OutreachLogResult":
        entries: list[OutreachLogEntry] = []
        for p in ev.prospects:
            for o in p.outreach:
                entries.append(OutreachLogEntry(
                    id=o.id,
                    prospect_id=p.id,
                    prospect_name=p.name,
                    channel=o.channel,
                    state=o.state,
                    provider=o.provider,
                    provider_lead_id=o.provider_lead_id,
                    body_preview=(o.body or "")[:300],
                    ts=o.ts,
                ))
        entries.sort(key=lambda e: (e.prospect_id, e.ts))
        return cls(event_id=ev.id, count=len(entries), entries=entries)


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

        # Top edges by weight, regardless of type : the LLM-derived weight
        # is the source of truth. (Old behavior filtered to symbiotic which
        # could leave this list empty for one-side pools.)
        top = sorted(edges, key=lambda e: -e["weight"])[:6]
        top_rows = []
        for e in top:
            a, b = by_id[e["a_id"]], by_id[e["b_id"]]
            top_rows.append({
                "a_id": a.id, "b_id": b.id,
                "a": a.name, "b": b.name, "weight": e["weight"],
                "edge_type": e["edge_type"],
                # Kept for backward-compat; UI no longer leans on these.
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
