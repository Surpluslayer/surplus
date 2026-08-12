"""backend/agents/relationship/outcomes.py : record what the lawyer actually
did with a drafted follow-up.

This is the missing half of the feedback loop. The ranking layer had a
`historical_behavior` factor and a `signal_taxonomy` that both read a draft's
`meta_json["disposition"]` -- but NOTHING in production ever wrote one. The
only dispositions in the database came from backend/demo/cohort.py, so
"the taxonomy learns from outcomes" was true of generated data and false of
real usage. Observe said so out loud rather than implying otherwise; this
module is the fix.

WHAT COUNTS AS AN OUTCOME

Only explicit decisions the lawyer actually made, both of which already exist
as product actions:

  - sending a follow-up  (routes/relationships schedule/send)  -> engaged
  - snoozing the contact (routes/book snooze)                  -> discarded

Sending is split by comparing the sent text to the stored draft:
`approved_as_is` when it went out unchanged, `edited_then_sent` when the
lawyer rewrote it. That distinction is derived from two strings we already
have, not guessed.

WHAT DELIBERATELY DOES NOT COUNT

A draft nobody has touched is left with no disposition. It is tempting to
age those out as rejections -- it would produce far more training data -- but
"has not been looked at yet" is not evidence of disinterest, and a rate
computed over invented negatives would be a fabricated number presented as a
measurement. Undecided drafts are excluded from the denominator entirely.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from ... import models

# Dispositions that mean the lawyer acted on the draft.
ENGAGED_DISPOSITIONS = ("approved_as_is", "edited_then_sent")
DISPOSITIONS = ENGAGED_DISPOSITIONS + ("discarded",)

_WS = re.compile(r"\s+")

# WHERE A DRAFT ACTUALLY LIVES
#
# Two shapes, and only one of them is production:
#
#   production -- updates_engine.autodraft() stashes the composed follow-up on
#     the EXISTING detected-signal row's meta_json["draft"]
#     (source_type == "activity_update"). There is no separate draft row and
#     no drafts table; that key IS the store.
#   demo cohort -- backend/demo/cohort.py writes a separate interaction
#     titled "Drafted follow-up (...)".
#
# Everything downstream was matching ONLY the demo title, so on a real
# account: the account summary reported "nothing drafted" while the pipeline
# section reported 250 stored autodrafts on the same screen, the harnesses
# had nothing to grade, and the outcome loop could never fire. Matching both
# shapes is what makes any of it work against real data.
_DEMO_DRAFT_TITLE = "Drafted follow-up%"


def is_drafted(row) -> bool:
    """True when this interaction carries a follow-up draft, either shape."""
    if (row.title or "").startswith("Drafted follow-up"):
        return True
    if row.source_type != "activity_update":
        return False
    try:
        return bool((json.loads(row.meta_json or "{}") or {}).get("draft"))
    except ValueError:
        return False


def drafted_filter():
    """SQL predicate for either shape, for use in a .where().

    meta_json is matched with LIKE rather than a JSON operator because the two
    backends disagree (SQLite has no jsonb); is_drafted() re-checks properly
    in Python, so the LIKE only has to be a superset."""
    from sqlalchemy import or_
    return or_(
        models.RelationshipInteraction.title.like(_DEMO_DRAFT_TITLE),
        (models.RelationshipInteraction.source_type == "activity_update")
        & models.RelationshipInteraction.meta_json.like('%"draft"%'),
    )


def _normalized(text: str | None) -> str:
    """Whitespace/case-insensitive comparison: a trailing newline or a
    capitalisation fix is not "the lawyer rewrote it"."""
    return _WS.sub(" ", (text or "").strip().lower())


def latest_undecided_draft(db, user_id: int, contact_id: int):
    """The most recent drafted follow-up for this contact that has no recorded
    disposition yet. None when there is nothing to attribute an action to --
    a lawyer can send a message that no draft prompted, and inventing a link
    to an unrelated draft would corrupt the very signal this exists to
    collect."""
    rows = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.actor_user_id == user_id,
               models.RelationshipInteraction.contact_id == contact_id,
               drafted_filter())
        .order_by(models.RelationshipInteraction.occurred_at.desc())
        .limit(10)
    ).scalars().all()
    for row in rows:
        if not is_drafted(row):
            continue
        try:
            meta = json.loads(row.meta_json or "{}") or {}
        except ValueError:
            meta = {}
        if not meta.get("disposition"):
            return row
    return None


def record_draft_outcome(db, user_id: int, contact_id: int, *,
                         disposition: str | None = None,
                         sent_body: str | None = None,
                         commit: bool = True):
    """Attribute a real user action to the draft that prompted it.

    Pass `sent_body` for a send (the disposition is then derived by comparing
    it to the stored draft), or `disposition` directly for a decision with no
    body, e.g. "discarded" on a snooze.

    Idempotent: a draft that already carries a disposition is never
    overwritten, so a retried request or a double-click cannot inflate the
    counts the taxonomy learns from. Returns the interaction it marked, or
    None when there was nothing to attribute.
    """
    row = latest_undecided_draft(db, user_id, contact_id)
    if row is None:
        return None

    try:
        meta = json.loads(row.meta_json or "{}") or {}
    except ValueError:
        meta = {}

    if disposition is None:
        stored = meta.get("draft")
        disposition = ("approved_as_is"
                       if stored and _normalized(stored) == _normalized(sent_body)
                       else "edited_then_sent")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition!r}")

    engaged = disposition in ENGAGED_DISPOSITIONS
    meta["disposition"] = disposition
    meta["engaged"] = engaged
    row.meta_json = json.dumps(meta)
    db.add(row)
    _apply_to_learned_affinity(db, row, meta, engaged)
    if commit:
        db.commit()
    return row


def _is_generated(db, interaction_id: int) -> bool:
    """True when this row was written by backend/demo/cohort.py rather than a
    real sync. Generated outcomes must never reach the learned affinity table
    -- synthetic data shaping real lawyers' rankings is the exact thing the
    provenance layer exists to prevent. refresh_from_outcomes() applies the
    same filter; this is its incremental counterpart."""
    from sqlalchemy import select
    from ...demo import provenance as prov
    hit = db.execute(
        select(models.DemoProvenance.id).where(
            models.DemoProvenance.table_name == "relationship_interactions",
            models.DemoProvenance.row_id == interaction_id,
            models.DemoProvenance.data_provenance.in_(tuple(prov.GENERATED_VALUES)))
        .limit(1)
    ).scalar_one_or_none()
    return hit is not None


def _apply_to_learned_affinity(db, row, meta: dict, engaged: bool) -> None:
    """Fold this outcome into the learned counts NOW, so the effect of a real
    decision is visible immediately rather than at the next daily sweep. The
    sweep still recomputes from scratch and reconciles any drift.

    Guarded: this is bookkeeping for a future ranking, and must never break
    the send it is attached to."""
    try:
        if _is_generated(db, row.id):
            return
        from ...demo import signal_taxonomy as tax
        user = db.get(models.User, row.actor_user_id)
        tax.apply_outcome(db, getattr(user, "practice_area", None),
                          meta.get("signal_category"), engaged, commit=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  [outcomes] learned-affinity update skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)


def record_outcome_safely(db, user_id: int, contact_id: int, **kw) -> None:
    """Fire-and-forget wrapper for product call sites.

    Recording an outcome is bookkeeping for a future ranking; a failure here
    must never turn a successful send into an error the lawyer sees. Mirrors
    the posture backend/observe/bus.py already takes.
    """
    try:
        record_draft_outcome(db, user_id, contact_id, **kw)
    except Exception as exc:  # noqa: BLE001
        print(f"  [outcomes] could not record draft outcome for contact "
              f"{contact_id}: {type(exc).__name__}: {exc}", flush=True)
