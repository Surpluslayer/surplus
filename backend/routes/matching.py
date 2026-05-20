"""routes/matching.py : stage 04. Build the symbiotic value graph + groups."""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import current_user, get_owned_event
from ..db import get_db
from ..agents.matcher import build_edges, form_groups
from ..agents.sponsor_matcher import score_event_sponsors
# Gap #1+#2+#3a: inbound applicants are bridged through this adapter so
# matcher.py / roi.py see attendee-shaped objects without schema changes.
from ..triage.matcher_adapter import is_inbound_event, run_inbound_match

router = APIRouter(prefix="/events", tags=["04 · matching"])


def _confirmed(ev: models.Event) -> list[models.Prospect]:
    return [p for p in ev.prospects if p.status == "rsvp"]


# --- manual RSVP override --------------------------------------------------
# For demo/testing: flip prospect.status -> "rsvp" without round-tripping
# through the LinkedIn webhook. Either bulk (all approved+contacted) or
# specific ids. Idempotent: re-flipping an already-rsvp'd prospect is a no-op.

class RsvpRequest(BaseModel):
    all: bool = False
    prospect_ids: list[int] = []


class RsvpResponse(BaseModel):
    event_id: int
    flipped: int
    already_rsvp: int
    rsvp_total: int
    prospect_ids: list[int]


@router.post("/{event_id}/rsvp", response_model=RsvpResponse)
def mark_rsvp(
    event_id: int,
    payload: RsvpRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    ev = get_owned_event(event_id, user, db)
    if not payload.all and not payload.prospect_ids:
        raise HTTPException(422, "pass either {all: true} or {prospect_ids: [...]}")

    if payload.all:
        targets = [p for p in ev.prospects
                   if p.status in ("approved", "contacted", "rsvp")]
    else:
        idset = set(payload.prospect_ids)
        targets = [p for p in ev.prospects if p.id in idset]
        missing = idset - {p.id for p in targets}
        if missing:
            raise HTTPException(
                404, f"prospects not in event {event_id}: {sorted(missing)}")

    flipped, already = 0, 0
    for p in targets:
        if p.status == "rsvp":
            already += 1
        else:
            p.status = "rsvp"
            flipped += 1
    db.commit()

    rsvp_total = sum(1 for p in ev.prospects if p.status == "rsvp")
    return RsvpResponse(
        event_id=ev.id,
        flipped=flipped,
        already_rsvp=already,
        rsvp_total=rsvp_total,
        prospect_ids=[p.id for p in targets],
    )


@router.post("/{event_id}/match", response_model=schemas.MatchResult)
def match(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """
    Score every pair of confirmed guests (symbiotic / affinity) and pack them
    into the format's groups, balancing market sides. Idempotent.
    """
    ev = get_owned_event(event_id, user, db)

    # Gap #3a: inbound (triage_config + no prospects) computes matches in
    # memory only. MatchEdge / SponsorMatch FK to prospects.id, so we'd
    # violate FK if we persisted rows for applicant ids. Smoke #6 — we
    # still compute the sponsor-match wire payload in memory so the
    # operator's sponsor chips actually produce results on the screen.
    if is_inbound_event(ev):
        result = run_inbound_match(ev)
        if result is None:
            raise HTTPException(
                409, "no accepted applicants : review the CSV first"
            )
        attending, edges, groups = result
        sponsor_payload = _compute_inbound_sponsor_matches(ev, attending)
        return schemas.MatchResult.build(
            ev, attending, edges, groups, sponsor_matches=sponsor_payload,
        )

    attending = _confirmed(ev)
    if not attending:
        raise HTTPException(409, "no confirmed guests : run the pipeline first")

    # idempotent : clear prior edges + group assignments + sponsor matches
    for e in list(ev.edges):
        db.delete(e)
    for p in attending:
        p.group_id = None
    # Wipe SponsorMatch rows for this event before recomputing (mirrors
    # the MatchEdge wipe above : matching is idempotent end-to-end).
    sponsor_ids = [s.id for s in (ev.sponsors or [])]
    if sponsor_ids:
        db.query(models.SponsorMatch).filter(
            models.SponsorMatch.sponsor_id.in_(sponsor_ids)
        ).delete(synchronize_session="fetch")
    db.flush()

    edges = build_edges(attending, event=ev)
    for e in edges:
        db.add(models.MatchEdge(event_id=ev.id, **e))

    groups = form_groups(attending, ev)
    for gid, members in groups.items():
        for p in members:
            p.group_id = gid

    # Sponsor matching : skipped silently when the event has no sponsors,
    # which is the only condition the brief requires for the section
    # to not render.
    sponsor_match_payload = _persist_sponsor_matches(db, ev, attending)

    db.commit()
    return schemas.MatchResult.build(ev, attending, edges, groups,
                                      sponsor_matches=sponsor_match_payload)


def _compute_inbound_sponsor_matches(ev: models.Event, attending: list) -> list[dict]:
    """Smoke #6: sponsor matches for inbound events. Same wire shape as
    _persist_sponsor_matches, but no SponsorMatch DB writes — applicants
    aren't in the prospects table so the FK would violate. The matcher
    duck-types on attributes, and applicants_as_attending exposes the
    fields score_event_sponsors reads (role / works_on / offers / seeks /
    seniority / fit_score / id), so we can score in memory without DB.
    """
    import json
    sponsors = list(ev.sponsors or [])
    if not sponsors:
        return []
    scored = score_event_sponsors(ev, attending)
    by_pid = {p.id: p for p in attending}
    payload: list[dict] = []
    for sponsor in sponsors:
        rows = scored.get(sponsor.id, [])
        match_rows: list[dict] = []
        for row in rows:
            prospect = by_pid.get(row["prospect_id"])
            if prospect is None:
                continue
            match_rows.append({
                "sponsor_id": sponsor.id,
                "sponsor_name": sponsor.name,
                "prospect_id": prospect.id,
                "prospect_name": prospect.name,
                "score": row["score"],
                "reasons": row["reasons"],
            })
        payload.append({
            "sponsor_id": sponsor.id,
            "sponsor_name": sponsor.name,
            "tier": sponsor.tier or "",
            "matches": match_rows,
        })
    return payload


def _persist_sponsor_matches(db: Session, ev: models.Event,
                              attending: list[models.Prospect]) -> list[dict]:
    """Run the heuristic sponsor scorer over `attending`, persist
    SponsorMatch rows, and return the wire-shape MatchResult.build
    expects in sponsor_matches.

    Returns [] when the event has no sponsors so the frontend's
    "render only if sponsors exist" guard reads cleanly.
    """
    import json
    sponsors = list(ev.sponsors or [])
    if not sponsors:
        return []
    scored = score_event_sponsors(ev, attending)
    by_pid = {p.id: p for p in attending}
    payload: list[dict] = []
    for sponsor in sponsors:
        rows = scored.get(sponsor.id, [])
        match_rows: list[dict] = []
        for row in rows:
            prospect = by_pid.get(row["prospect_id"])
            if prospect is None:
                continue
            db.add(models.SponsorMatch(
                sponsor_id=sponsor.id,
                prospect_id=row["prospect_id"],
                score=row["score"],
                reasons=json.dumps(row["reasons"]),
            ))
            match_rows.append({
                "sponsor_id": sponsor.id,
                "sponsor_name": sponsor.name,
                "prospect_id": prospect.id,
                "prospect_name": prospect.name,
                "score": row["score"],
                "reasons": row["reasons"],
            })
        payload.append({
            "sponsor_id": sponsor.id,
            "sponsor_name": sponsor.name,
            "tier": sponsor.tier or "",
            "matches": match_rows,
        })
    return payload


class ExplainRequest(BaseModel):
    a_id: int
    b_id: int
    # "prospect" (default) or "sponsor". When either side is a sponsor we
    # synthesize an EnrichedPerson from the Sponsor row + persisted
    # SponsorMatch reasons so the SAME pair_explainer is reused.
    a_kind: str = "prospect"
    b_kind: str = "prospect"


class ExplainResponse(BaseModel):
    a_id: int
    b_id: int
    explanation: str
    source: str   # "llm" | "cached" | "error"


def _sponsor_to_enriched_person(sponsor: models.Sponsor):
    """Synthesize an EnrichedPerson from a Sponsor row so the SAME
    pair_explainer can score sponsor↔attendee pairs with no second code
    path. buyer_profile fields become domains / conviction_themes / role
    so _profile_lines renders something meaningful."""
    from ..matching.schema import EnrichedPerson
    from ..agents.sponsor_matcher import parse_buyer_profile
    buyer = parse_buyer_profile(sponsor.buyer_profile)
    bio_parts: list[str] = [f"Sponsor : {sponsor.name}."]
    if sponsor.tier:
        bio_parts.append(f"Tier: {sponsor.tier}.")
    if buyer["target_role"]:
        bio_parts.append(f"Buying for: {buyer['target_role']}.")
    if buyer["seniority"]:
        bio_parts.append(f"Target seniority: {buyer['seniority']}.")
    if buyer["company_stage"]:
        bio_parts.append(f"Target stage: {buyer['company_stage']}.")
    if buyer["industry"]:
        bio_parts.append(f"Target industry: {buyer['industry']}.")
    bio_parts.append(f"Intent: {buyer['intent']}.")
    return EnrichedPerson(
        id=f"sponsor-{sponsor.id}",
        name=sponsor.name,
        role="Sponsor",
        title=f"{sponsor.tier} Sponsor".strip() or "Sponsor",
        company=sponsor.name,
        domains=[buyer["industry"]] if buyer["industry"] else [],
        conviction_themes=[
            t for t in (buyer["target_role"], buyer["company_stage"]) if t
        ],
        explicit_asks=[buyer["target_role"]] if buyer["target_role"] else [],
        bio_text=" ".join(bio_parts),
        enrichment_status="ok",
    )


def _prospect_to_enriched_fallback(prospect: models.Prospect):
    """Best-effort EnrichedPerson from a Prospect ORM row when the
    matcher_lib cache hasn't run (heuristic-only events). Lets
    pair_explainer's structured fallback still render something."""
    from ..matching.schema import EnrichedPerson
    return EnrichedPerson(
        id=f"prospect-{prospect.id}",
        name=prospect.name or f"Prospect {prospect.id}",
        role=prospect.role or "",
        title=prospect.role or "",
        company=prospect.company or "",
        linkedin_url=prospect.linkedin_url or "",
        domains=[prospect.works_on] if prospect.works_on else [],
        explicit_asks=[prospect.seeks] if prospect.seeks else [],
        mentor_signals=[prospect.offers] if prospect.offers else [],
        bio_text=(
            f"{prospect.name}, {prospect.role} at {prospect.company}. "
            f"Side: {prospect.side}. Works on: {prospect.works_on}. "
            f"Offers: {prospect.offers}. Seeks: {prospect.seeks}."
        ).strip(),
        enrichment_status="ok",
    )


@router.post("/{event_id}/pairs/explain", response_model=ExplainResponse)
def explain_pair_endpoint(
    event_id: int,
    payload: ExplainRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """On-demand LLM explanation for one pair.

    Supports two pair kinds, but the SAME LLM call:
      - prospect ⟷ prospect : the original guest-pair path. Reads
        matcher_lib's cached EnrichedPerson + pair-score components.
      - prospect ⟷ sponsor : sponsor side is synthesized from the
        Sponsor row + persisted SponsorMatch reasons. No cache lookup.

    Cache miss on a guest pair falls back to a minimal EnrichedPerson
    built from the Prospect row, so heuristic-only events (no LLM ran)
    still produce structured explanations.
    """
    import asyncio
    import json as _json
    from ..agents import matcher_lib, pair_explainer

    ev = get_owned_event(event_id, user, db)

    attending = _confirmed(ev)
    enriched = matcher_lib.get_cached_enriched(ev, attending) or {}
    matrix = matcher_lib.get_cached_matrix(ev, attending) or {}

    def _resolve(side_id: int, kind: str):
        """Return the EnrichedPerson for one side of the pair."""
        if kind == "sponsor":
            sponsor = db.get(models.Sponsor, side_id)
            if sponsor is None or sponsor.event_id != ev.id:
                raise HTTPException(404, f"sponsor {side_id} not on this event")
            return _sponsor_to_enriched_person(sponsor)
        # prospect : prefer cached enriched, fall back to ORM row
        cached = enriched.get(f"prospect-{side_id}")
        if cached is not None:
            return cached
        prospect = db.get(models.Prospect, side_id)
        if prospect is None or prospect.event_id != ev.id:
            raise HTTPException(404, f"prospect {side_id} not on this event")
        return _prospect_to_enriched_fallback(prospect)

    a_person = _resolve(payload.a_id, payload.a_kind)
    b_person = _resolve(payload.b_id, payload.b_kind)

    # Pair dict : prefer the matcher_lib pair when both sides are
    # prospects AND the cache has it; otherwise synthesize one from the
    # SponsorMatch reasons so explain_pair's structured fallback has
    # ground truth to lean on.
    pair = None
    if payload.a_kind == "prospect" and payload.b_kind == "prospect":
        a_key = f"prospect-{payload.a_id}"
        b_key = f"prospect-{payload.b_id}"
        pair = next(
            (p for p in matrix.get("pairs", [])
             if {p.get("a_id"), p.get("b_id")} == {a_key, b_key}),
            None,
        )
    else:
        # Sponsor pair : pull reasons + score off the persisted SponsorMatch
        sponsor_id = payload.a_id if payload.a_kind == "sponsor" else payload.b_id
        prospect_id = payload.b_id if payload.a_kind == "sponsor" else payload.a_id
        match = (db.query(models.SponsorMatch)
                   .filter(models.SponsorMatch.sponsor_id == sponsor_id,
                           models.SponsorMatch.prospect_id == prospect_id)
                   .first())
        if match is not None:
            try:
                reasons = _json.loads(match.reasons or "[]")
                if not isinstance(reasons, list):
                    reasons = []
            except _json.JSONDecodeError:
                reasons = []
            # Shape mirrors matcher_lib's pair dict enough that
            # _structured_fallback's components-summary still works
            # (it walks .components.similar / .components.complementary).
            pair = {
                "a_id": a_person.id, "b_id": b_person.id,
                "composite": (match.score or 0) / 100.0,
                "components": {
                    "complementary": {f"reason_{i}": 1.0 for i in range(len(reasons))},
                    "similar": {},
                },
                "reasons": [str(r) for r in reasons],
            }

    result = asyncio.run(pair_explainer.explain_pair(a_person, b_person, pair))
    return ExplainResponse(
        a_id=payload.a_id, b_id=payload.b_id,
        explanation=result["text"],
        source=result["source"],
    )


@router.get("/{event_id}/matches", response_model=schemas.MatchResult)
def get_matches(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Read the stored value graph without recomputing it."""
    ev = get_owned_event(event_id, user, db)
    if not ev.edges:
        raise HTTPException(409, "matching has not been run for this event yet")

    attending = _confirmed(ev)
    edges = [{"a_id": e.a_id, "b_id": e.b_id,
              "edge_type": e.edge_type, "weight": e.weight} for e in ev.edges]
    groups: dict[int, list] = {}
    for p in attending:
        if p.group_id is not None:
            groups.setdefault(p.group_id, []).append(p)
    return schemas.MatchResult.build(
        ev, attending, edges, groups,
        sponsor_matches=_load_persisted_sponsor_matches(db, ev),
    )


def _load_persisted_sponsor_matches(db: Session, ev: models.Event) -> list[dict]:
    """Read SponsorMatch rows for `ev` back into the wire shape. Used by
    GET /matches so the read path matches what POST /match returns."""
    import json
    sponsors = list(ev.sponsors or [])
    if not sponsors:
        return []
    payload: list[dict] = []
    prospect_lookup = {p.id: p for p in ev.prospects}
    for sponsor in sponsors:
        match_rows: list[dict] = []
        for m in sorted(sponsor.matches, key=lambda r: -r.score):
            prospect = prospect_lookup.get(m.prospect_id)
            if prospect is None:
                continue
            try:
                reasons = json.loads(m.reasons or "[]")
                if not isinstance(reasons, list):
                    reasons = []
            except json.JSONDecodeError:
                reasons = []
            match_rows.append({
                "sponsor_id": sponsor.id,
                "sponsor_name": sponsor.name,
                "prospect_id": prospect.id,
                "prospect_name": prospect.name,
                "score": m.score,
                "reasons": [str(r) for r in reasons],
            })
        payload.append({
            "sponsor_id": sponsor.id,
            "sponsor_name": sponsor.name,
            "tier": sponsor.tier or "",
            "matches": match_rows,
        })
    return payload


# ─── Sponsor CRUD ──────────────────────────────────────────────────────
# Sponsors are added on the Matching screen, not at Intake. Three endpoints:
# POST creates one, PATCH edits one, DELETE removes one. Each mutation
# wipes the cached sponsor_match rows for that sponsor so the next
# /match call (or the optional re-score below) rebuilds them.
#
# We deliberately DO NOT auto-trigger a full /match recompute here :
# the caller decides when to refetch. Frontend posts a sponsor then
# re-calls /match in the same flow.


class SponsorCreate(BaseModel):
    name: str
    tier: str = ""
    buyer_profile: schemas.SponsorBuyerProfile = schemas.SponsorBuyerProfile()


class SponsorUpdate(BaseModel):
    """All fields optional : PATCH semantics. Send only what changed."""
    name: str | None = None
    tier: str | None = None
    buyer_profile: schemas.SponsorBuyerProfile | None = None


@router.get("/{event_id}/sponsors", response_model=list[schemas.SponsorOut])
def list_sponsors(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Every sponsor on this event, in creation order."""
    ev = get_owned_event(event_id, user, db)
    rows = sorted(ev.sponsors or [], key=lambda s: s.id)
    return [schemas.SponsorOut.of(s) for s in rows]


@router.post("/{event_id}/sponsors", response_model=schemas.SponsorOut,
             status_code=201)
def create_sponsor(
    event_id: int,
    body: SponsorCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Add a sponsor to an existing event. Triggers no recompute by
    itself : the caller re-runs /match to refresh SponsorMatch rows."""
    ev = get_owned_event(event_id, user, db)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "sponsor name is required")
    sponsor = models.Sponsor(
        event_id=ev.id,
        name=name,
        tier=(body.tier or "").strip(),
        buyer_profile=json.dumps(body.buyer_profile.model_dump()),
    )
    db.add(sponsor)
    db.commit()
    db.refresh(sponsor)
    return schemas.SponsorOut.of(sponsor)


def _get_owned_sponsor(db: Session, event_id: int, sponsor_id: int,
                        user: models.User) -> models.Sponsor:
    ev = get_owned_event(event_id, user, db)
    sponsor = db.get(models.Sponsor, sponsor_id)
    if sponsor is None or sponsor.event_id != ev.id:
        raise HTTPException(404, "sponsor not on this event")
    return sponsor


@router.patch("/{event_id}/sponsors/{sponsor_id}",
              response_model=schemas.SponsorOut)
def update_sponsor(
    event_id: int,
    sponsor_id: int,
    body: SponsorUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Edit a sponsor in place. Wipes the sponsor's existing
    SponsorMatch rows so a stale buyer_profile can't linger as match
    rationale : the next /match call rebuilds them."""
    sponsor = _get_owned_sponsor(db, event_id, sponsor_id, user)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "sponsor name cannot be empty")
        sponsor.name = name
    if body.tier is not None:
        sponsor.tier = body.tier.strip()
    if body.buyer_profile is not None:
        sponsor.buyer_profile = json.dumps(body.buyer_profile.model_dump())
    # Wipe cached matches for this sponsor : the next /match recomputes.
    db.query(models.SponsorMatch).filter(
        models.SponsorMatch.sponsor_id == sponsor.id
    ).delete(synchronize_session="fetch")
    db.commit()
    db.refresh(sponsor)
    return schemas.SponsorOut.of(sponsor)


@router.delete("/{event_id}/sponsors/{sponsor_id}", status_code=204)
def delete_sponsor(
    event_id: int,
    sponsor_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Remove one sponsor. Cascade clears its SponsorMatch rows via
    the relationship's `cascade='all, delete-orphan'`."""
    sponsor = _get_owned_sponsor(db, event_id, sponsor_id, user)
    db.delete(sponsor)
    db.commit()
    return None
