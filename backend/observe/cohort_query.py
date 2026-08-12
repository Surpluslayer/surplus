"""backend/observe/cohort_query.py : shared cohort -> (User, [Contact]) lookup
and outcome-relevance grading, used by every harness that evaluates against a
demo cohort (ablation, relationship_eval, signal_library_eval). One place so
the DemoProvenance join logic isn't copy-pasted across three harness modules
-- the exact pattern backend/demo/eval_backtest.py already established, made
reusable.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from .. import models
from .metrics import DISPOSITION_GRADE


def users_and_contacts(db, cohort_id: str) -> list:
    """[(User, [Contact, ...]), ...] for every lawyer in the cohort who has
    at least one generated contact."""
    user_tags = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == cohort_id,
        models.DemoProvenance.table_name == "users")).scalars().all()
    users = db.execute(select(models.User).where(
        models.User.id.in_([t.row_id for t in user_tags]))).scalars().all()

    contact_tags = db.execute(select(models.DemoProvenance).where(
        models.DemoProvenance.cohort_id == cohort_id,
        models.DemoProvenance.table_name == "contacts")).scalars().all()
    contact_ids = {t.row_id for t in contact_tags}

    out = []
    for user in users:
        contacts = db.execute(select(models.Contact).where(
            models.Contact.id.in_(contact_ids),
            models.Contact.user_id == user.id,
        )).scalars().all()
        if contacts:
            out.append((user, contacts))
    return out


def most_recent_draft_relevance(db, contact_id: int):
    """Graded relevance (see metrics.DISPOSITION_GRADE) for a contact's most
    recent drafted signal. None if no draft exists to evaluate against --
    excluded from a case list, never silently scored as 0 (0 means "resolved
    and discarded", not "unknown")."""
    row = db.execute(
        select(models.RelationshipInteraction)
        .where(models.RelationshipInteraction.contact_id == contact_id,
               models.RelationshipInteraction.title.like("Drafted follow-up%"))
        .order_by(models.RelationshipInteraction.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    meta = json.loads(row.meta_json or "{}")
    return float(DISPOSITION_GRADE.get(meta.get("disposition"), 0))
