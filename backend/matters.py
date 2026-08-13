"""matters.py : reading the Matter entity, for the two things that needed it.

models.Matter is the engagement row. This module is the small amount of logic
that sits on top of it, kept out of models.py so both consumers can be read in
one place and neither grows a second copy:

  1. VENUE. venue_query_for() assembles a venue.VenueQuery from a matter and
     its client, which is what lets forum resolution get past the county to an
     actual court.
  2. CLIENT STATUS. derived_relationship_type() reports the Rule 7.3 exemption
     category a matter implies. READ ITS DOCSTRING BEFORE USING IT IN A SEND
     PATH -- it moves the solicitation gate in the permissive direction, and
     this module deliberately does not wire it there.

Pure: no network, no LLM. Takes a Session for the lookups and nothing else.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from . import venue as venue_mod
from .solicitation import RelationshipType

# The two statuses that make someone a client. "prospective" is deliberately
# absent: a matter under discussion is what solicitation is FOR, so treating it
# as an engagement would exempt exactly the people Rule 7.3 protects.
OPEN = "open"
CLOSED = "closed"
PROSPECTIVE = "prospective"
STATUSES = (PROSPECTIVE, OPEN, CLOSED)

# Open beats closed beats prospective: someone with a live engagement is an
# existing client even if they also have three closed matters.
_STATUS_RANK = {OPEN: 2, CLOSED: 1, PROSPECTIVE: 0}

_STATUS_RELATIONSHIP = {
    OPEN: RelationshipType.EXISTING_CLIENT,
    CLOSED: RelationshipType.FORMER_CLIENT,
}


def matters_for_contact(db, user_id: int, contact_id: Optional[int]) -> list:
    """This user's matters for one contact, newest first. Owner-scoped in the
    query rather than filtered afterwards, so a caller cannot accidentally read
    across accounts. [] on any error, matching the spine's read posture."""
    from . import models
    if contact_id is None:
        return []
    try:
        return (db.query(models.Matter)
                  .filter(models.Matter.user_id == user_id,
                          models.Matter.contact_id == contact_id)
                  .order_by(models.Matter.id.desc())
                  .all())
    except Exception:  # noqa: BLE001
        return []


def strongest(matters: Iterable[Any]) -> Optional[Any]:
    """The matter that decides the client relationship: open > closed >
    prospective, most recent within a rank."""
    best = None
    best_rank = -1
    for m in matters:
        # An unrecognized status ranks -1 and never wins, so a row with a typo'd
        # or imported-garbage status cannot become the one that decides whether
        # someone is a client.
        rank = _STATUS_RANK.get((getattr(m, "status", "") or "").strip(), -1)
        if rank > best_rank:
            best, best_rank = m, rank
    return best


def derived_relationship_type(matters: Iterable[Any]) -> Optional[RelationshipType]:
    """The Rule 7.3 exemption category these matters imply, or None.

    open -> EXISTING_CLIENT, closed -> FORMER_CLIENT, prospective -> None.

    WHY THIS IS NOT WIRED INTO THE SEND GATE. Every category this can return is
    an EXEMPTION: solicitation.evaluate() short-circuits to allowed for all of
    them. So a bug here -- a stray matter row, an import that sets status wrong,
    a merge that reattaches a matter to the wrong contact -- opens the gate for
    someone Rule 7.3 protects, silently, and the failure is a bar complaint
    rather than a stack trace. Deriving an exemption is exactly the direction
    that deserves a human deciding, so:

      * Contact.relationship_type stays the authority the gate reads. It is set
        explicitly and it wins.
      * Observe's trace REPORTS the divergence -- "this contact has an open
        matter, so 7.3 would exempt them, but the gate is still treating them
        as a prospect" -- so the lawyer can set it deliberately.

    Wiring this into solicitation_signals.check() is a one-line change and a
    real decision. It is the account owner's to make, not this module's."""
    m = strongest(matters)
    if m is None:
        return None
    return _STATUS_RELATIONSHIP.get((getattr(m, "status", "") or "").strip())


def _case_type(raw: Optional[str]) -> Optional[venue_mod.CaseType]:
    """A stored case_type string as its enum, or None. Unknown strings resolve
    to None rather than raising: the column is free text by design (adding a
    case type should be a code change, not a migration), and an unrecognized
    value means venue stops at the county -- which is the same honest partial
    answer as not knowing the case type at all."""
    try:
        return venue_mod.CaseType((raw or "").strip())
    except ValueError:
        return None


def venue_query_for(matter: Any, contact: Any = None) -> venue_mod.VenueQuery:
    """The VenueQuery a matter (and optionally its client) describes.

    Location precedence is matter over contact, per field rather than per
    object: a matter that records only a county still inherits the client's
    state. That is the right granularity because the two disagree in a normal
    way -- a New Jersey client can have a Kings County dispute -- and taking the
    contact's location wholesale whenever the matter is partial would
    contradict the county the user actually typed."""
    def pick(field: str) -> Optional[str]:
        for obj in (matter, contact):
            if obj is None:
                continue
            val = (getattr(obj, f"location_{field}", None) or "").strip()
            if val:
                return val
        return None

    return venue_mod.VenueQuery(
        state=pick("state") or "",
        county=pick("county"),
        case_type=_case_type(getattr(matter, "case_type", None)),
        amount_in_controversy=getattr(matter, "amount_in_controversy", None),
    )


def resolve(matter: Any, contact: Any = None) -> venue_mod.VenueResolution:
    """Candidate forums for this matter. Thin wrapper so callers do not have to
    know that venue.py is the thing that answers."""
    return venue_mod.resolve_venue(venue_query_for(matter, contact))
