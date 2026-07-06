"""agents/relationship/conflict_import.py : deterministic conflict-import
gates (docs/accounts-architecture.md §6b).

Error asymmetry drives everything here: a false-positive wall is an
inconvenience an admin can remove; a false-negative (a dropped or mis-mapped
conflict) is a breach. So every gate fails toward over-walling, and in this
v1 there is NO LLM anywhere — the parse is code, the walls are code, the
coverage invariant is code:

  * Gate 1 — deterministic parse. Lines split, BOM/control chars/quotes
    stripped, first CSV cell taken; empties and an obvious header row are
    the only skips.
  * Gate 2 — provisional name-walls, instantly. Every parsed name gets a
    Wall(subject_name_norm=...) BEFORE any review: any Company matching the
    normalized string is walled from the moment of import. Over-broad by
    design; confirmation narrows, never widens.
  * Gate 3 — coverage invariant. Every input line ends in exactly one of
    {walled_provisional, duplicate, skipped_empty, skipped_header}; lines
    in must equal states out or the whole import raises (and, because the
    walls and the audit row share the caller's transaction, nothing
    commits). A parse that silently drops a line cannot pass.
  * Gate 4/5 — the admin confirms the mapping (single-match name-walls
    become entity walls, keeping name_norm as belt-and-braces; ambiguous
    and unmatched names STAY name-walled — the safe direction), and only
    that confirmation (or an audited skip with a reason) flips the strict
    team's view_state from "pending" to "live".

Commit discipline: import_text/confirm/skip each commit exactly once at the
end, so walls + view_state + audit row are atomic (audit.write flushes in
this same transaction — an unaudited wall change is impossible).
"""
from __future__ import annotations

import csv
import re
from typing import Dict, List, Optional

from ... import models
from . import audit
from .company_resolve import normalize_company_name

# The four terminal states of the coverage invariant. Every input line lands
# in exactly one; there is no fifth "dropped" state by construction.
STATE_WALLED = "walled_provisional"
STATE_DUPLICATE = "duplicate"
STATE_SKIPPED_EMPTY = "skipped_empty"
STATE_SKIPPED_HEADER = "skipped_header"
STATES = (STATE_WALLED, STATE_DUPLICATE, STATE_SKIPPED_EMPTY,
          STATE_SKIPPED_HEADER)

PROVISIONAL_REASON = "conflict import (pending confirmation)"

# Only an OBVIOUS header is skipped (exact match on the cleaned first cell,
# first content line only). Anything less obvious gets walled — a company
# improbably named "Client Services" must not slip through as a header.
_HEADER_TOKENS = frozenset({"company", "name", "client"})

# Control characters (weird encodings, pasted terminal output) are stripped,
# never allowed to break the parse: the line still lands in a state.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ── gate 1: deterministic parse ──────────────────────────────────────────────

def _clean_cell(raw_line: str) -> str:
    """One line -> one candidate company name, purely mechanically: strip
    BOM + control chars, take the first CSV cell if the line has commas
    (csv.reader so a quoted "Acme, Inc." stays whole), then shed wrapping
    quotes/whitespace. Any anomaly degrades to a smaller string, never to
    an exception — the caller's coverage invariant needs every line back."""
    s = _CONTROL_CHARS.sub("", (raw_line or "").replace("\ufeff", "")).strip()
    if "," in s:
        try:
            row = next(csv.reader([s]), [])
        except csv.Error:
            row = s.split(",")
        s = (row[0] if row else "").strip()
    return s.strip().strip("\"'").strip()


def parse_lines(text: str) -> List[dict]:
    """Split the pasted blob into per-line records: {line, name, state}
    where state is a SKIP state or None (= a real name, fate decided by the
    wall pass). Header skip applies only to the first line that has any
    content — a later literal "client" is a company name and gets walled."""
    records: List[dict] = []
    seen_content = False
    for i, raw in enumerate((text or "").splitlines(), start=1):
        name = _clean_cell(raw)
        if not name:
            records.append({"line": i, "name": "", "state": STATE_SKIPPED_EMPTY})
            continue
        if not seen_content and name.lower() in _HEADER_TOKENS:
            seen_content = True
            records.append({"line": i, "name": name,
                            "state": STATE_SKIPPED_HEADER})
            continue
        seen_content = True
        records.append({"line": i, "name": name, "state": None})
    return records


# ── entity matching (review mapping — resolution, not enforcement) ───────────

def _survivor(db, company: models.Company) -> Optional[models.Company]:
    """Follow merged_into_id tombstones to the surviving row (bounded)."""
    hops = 0
    while company is not None and company.merged_into_id and hops < 5:
        company = db.get(models.Company, company.merged_into_id)
        hops += 1
    if company is None or company.merged_into_id:
        return None
    return company


def match_companies(db, norms: List[str]) -> Dict[str, List[models.Company]]:
    """norm -> live Company rows matching it, via BOTH paths: the name_norm
    CompanyIdentity lookup and a normalize() scan of live canonical names
    (identities are best-effort; the scan catches companies that never got
    one). This mapping is for REVIEW and confirmation only — enforcement of
    a provisional wall is the query layer's own name_norm match, so a miss
    here can not unwall anything."""
    matches: Dict[str, Dict[int, models.Company]] = {n: {} for n in norms}
    if not matches:
        return {}
    for ident in (db.query(models.CompanyIdentity)
                    .filter(models.CompanyIdentity.kind == "name_norm",
                            models.CompanyIdentity.value.in_(list(matches)))
                    .all()):
        c = _survivor(db, db.get(models.Company, ident.company_id))
        if c is not None:
            matches[ident.value][c.id] = c
    for c in (db.query(models.Company)
                .filter(models.Company.merged_into_id.is_(None)).all()):
        norm = normalize_company_name(c.canonical_name)
        if norm in matches:
            matches[norm][c.id] = c
    return {n: [by_id[k] for k in sorted(by_id)]
            for n, by_id in matches.items()}


def _company_briefs(companies: List[models.Company]) -> List[dict]:
    return [{"id": c.id, "name": c.canonical_name} for c in companies]


# ── gates 2+3: import = provisional walls + coverage invariant ───────────────

def import_text(db, *, team: models.Team, actor_user_id: int,
                text: str) -> dict:
    """Parse and wall in one transaction. Every parsed name is provisionally
    walled (excluded_user_ids="[]" = ALL members) before anyone reviews
    anything; existing team+name_norm walls are not duplicated (idempotent
    re-import). Raises — committing nothing — if any line fails to land in
    exactly one state."""
    records = parse_lines(text)

    existing_norms = {
        w.subject_name_norm
        for w in db.query(models.Wall)
                   .filter(models.Wall.team_id == team.id,
                           models.Wall.subject_name_norm.isnot(None))
                   .all()}
    batch_norms: set = set()

    for rec in records:
        if rec["state"] is not None:            # skipped_empty / skipped_header
            rec["name_norm"] = ""
            continue
        norm = normalize_company_name(rec["name"])
        rec["name_norm"] = norm
        if not norm:                            # pure punctuation etc.
            rec["state"] = STATE_SKIPPED_EMPTY
        elif norm in existing_norms or norm in batch_norms:
            rec["state"] = STATE_DUPLICATE
        else:
            db.add(models.Wall(
                team_id=team.id,
                subject_kind="company",
                subject_name_norm=norm,
                excluded_user_ids="[]",
                reason=PROVISIONAL_REASON,
                created_by=actor_user_id,
            ))
            batch_norms.add(norm)
            rec["state"] = STATE_WALLED

    # Gate 3, enforced for real: lines in == states out, one state per line.
    counts = {s: 0 for s in STATES}
    for rec in records:
        if rec["state"] not in counts:
            db.rollback()
            raise RuntimeError(
                f"conflict import coverage violation: line {rec['line']} "
                f"landed in unknown state {rec['state']!r}")
        counts[rec["state"]] += 1
    if len(records) != sum(counts.values()):
        db.rollback()
        raise RuntimeError(
            f"conflict import coverage violation: {len(records)} lines in, "
            f"{sum(counts.values())} states out")

    matched = match_companies(
        db, [r["name_norm"] for r in records if r["name_norm"]])
    lines = [{
        "line": r["line"],
        "name": r["name"],
        "name_norm": r["name_norm"],
        "state": r["state"],
        "matched_companies": _company_briefs(matched.get(r["name_norm"], [])),
    } for r in records]

    audit.write(db, team_id=team.id, actor_user_id=actor_user_id,
                event="conflicts_imported",
                detail={"lines_in": len(records), **counts})
    db.commit()

    return {"team_id": team.id, "view_state": team.view_state,
            "counts": {"lines_in": len(records), **counts}, "lines": lines}


# ── review mapping ───────────────────────────────────────────────────────────

def _provisional_walls(db, team: models.Team) -> List[models.Wall]:
    """Provisional = name-walled but not yet entity-resolved. Once confirm
    sets subject_company_id the wall leaves this set (its kept name_norm is
    belt-and-braces, not pending review)."""
    return (db.query(models.Wall)
              .filter(models.Wall.team_id == team.id,
                      models.Wall.subject_name_norm.isnot(None),
                      models.Wall.subject_company_id.is_(None))
              .order_by(models.Wall.id)
              .all())


def review(db, *, team: models.Team) -> dict:
    walls = _provisional_walls(db, team)
    matched = match_companies(db, [w.subject_name_norm for w in walls])
    return {"team_id": team.id, "view_state": team.view_state,
            "conflicts": [{
                "wall_id": w.id,
                "name_norm": w.subject_name_norm,
                "reason": w.reason,
                "matched_companies": _company_briefs(
                    matched.get(w.subject_name_norm, [])),
            } for w in walls]}


# ── gates 4+5: confirm (narrow) / skip (audited bypass) ─────────────────────

def confirm(db, *, team: models.Team, actor_user_id: int) -> dict:
    """Convert each single-match provisional wall into an entity wall
    (subject_company_id set, name_norm KEPT — belt-and-braces per §6b) and
    flip the view live. Zero-match and multi-match walls stay name-walls:
    they still enforce, and over-walling is the safe direction — the admin
    narrows them by deleting/replacing walls, never this code."""
    walls = _provisional_walls(db, team)
    matched = match_companies(db, [w.subject_name_norm for w in walls])
    converted = kept = 0
    for w in walls:
        companies = matched.get(w.subject_name_norm, [])
        if len(companies) == 1:
            w.subject_company_id = companies[0].id      # name_norm stays
            converted += 1
        else:
            kept += 1
    old_state, team.view_state = team.view_state, "live"
    audit.write(db, team_id=team.id, actor_user_id=actor_user_id,
                event="conflicts_confirmed",
                detail={"converted": converted, "kept_name_walls": kept,
                        "view_state": [old_state, "live"]})
    db.commit()
    return {"team_id": team.id, "view_state": team.view_state,
            "converted": converted, "kept_name_walls": kept}


def skip(db, *, team: models.Team, actor_user_id: int, reason: str) -> dict:
    """The audited skip from §6b: exposure without import is allowed, but
    only as an explicit, reasoned, logged decision (the route 400s an empty
    reason before calling this)."""
    old_state, team.view_state = team.view_state, "live"
    audit.write(db, team_id=team.id, actor_user_id=actor_user_id,
                event="conflicts_skipped",
                detail={"reason": (reason or "")[:300],
                        "view_state": [old_state, "live"]})
    db.commit()
    return {"team_id": team.id, "view_state": team.view_state}


__all__ = ["parse_lines", "match_companies", "import_text", "review",
           "confirm", "skip", "STATES", "STATE_WALLED", "STATE_DUPLICATE",
           "STATE_SKIPPED_EMPTY", "STATE_SKIPPED_HEADER",
           "PROVISIONAL_REASON"]
