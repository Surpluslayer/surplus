"""backend/demo/provenance.py : tag/query helpers over models.DemoProvenance.

One rule enforced here, nowhere else: every row this package writes into a
production table gets exactly one DemoProvenance tag, written in the SAME
transaction (flush -> tag -> the caller commits once). A generated row that
exists without a provenance tag is a bug -- it would be silently
indistinguishable from real data to anything that doesn't know to look.
"""
from __future__ import annotations

from typing import Optional

from .. import models

# The only values this package writes. Kept as a closed set so a typo can't
# silently invent a new, unqueryable provenance category.
BASELINE = "generated_from_assumed_distribution"
EXTENDED = "generated_beyond_baseline"
PROVENANCE_VALUES = frozenset({BASELINE, EXTENDED})


def tag(db, row, *, provenance: str, cohort_id: str) -> models.DemoProvenance:
    """Tag one already-flushed row (must have a real .id). Call after
    db.flush(), before the caller's own db.commit() -- see module docstring."""
    if provenance not in PROVENANCE_VALUES:
        raise ValueError(f"unknown provenance value: {provenance!r}")
    if getattr(row, "id", None) is None:
        raise ValueError(f"{row.__class__.__name__} row has no id -- flush before tagging")
    p = models.DemoProvenance(
        table_name=row.__tablename__,
        row_id=row.id,
        data_provenance=provenance,
        cohort_id=cohort_id,
    )
    db.add(p)
    return p


def cohort_row_counts(db, cohort_id: str) -> dict:
    """What a generation run actually wrote, by table -- the sanity-check
    query for "did this do what it claims", and the query an analytics
    consumer would run to scope generated data out of a real report."""
    from sqlalchemy import func, select
    rows = db.execute(
        select(models.DemoProvenance.table_name, models.DemoProvenance.data_provenance,
               func.count())
        .where(models.DemoProvenance.cohort_id == cohort_id)
        .group_by(models.DemoProvenance.table_name, models.DemoProvenance.data_provenance)
    ).all()
    out: dict = {}
    for table_name, provenance, count in rows:
        out.setdefault(table_name, {})[provenance] = count
    return out


def delete_cohort(db, cohort_id: str) -> int:
    """Delete every row this cohort generated, across every tagged table, by
    walking the provenance index -- not by re-deriving which rows belong to
    the cohort from product logic. Returns the number of rows deleted (not
    counting the provenance rows themselves). Used by tests and by a
    reset-the-demo-cohort operator action; never called from product code."""
    from sqlalchemy import select
    tags = db.execute(
        select(models.DemoProvenance).where(models.DemoProvenance.cohort_id == cohort_id)
    ).scalars().all()
    by_table: dict[str, list[int]] = {}
    for t in tags:
        by_table.setdefault(t.table_name, []).append(t.row_id)

    # Delete children before parents to respect FKs on backends without
    # cascade configured for these ad-hoc bulk deletes (SQLite in tests).
    order = ["relationship_interactions", "prospects", "contacts", "events", "users"]
    deleted = 0
    model_by_table = _table_to_model()
    for table in order:
        ids = by_table.get(table)
        if not ids:
            continue
        model = model_by_table.get(table)
        if model is None:
            continue
        rows = db.execute(select(model).where(model.id.in_(ids))).scalars().all()
        for r in rows:
            db.delete(r)
            deleted += 1
    for t in tags:
        db.delete(t)
    db.commit()
    return deleted


def _table_to_model() -> dict:
    return {m.__tablename__: m for m in _all_models()}


def _all_models():
    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, type) and issubclass(obj, models.Base) and obj is not models.Base:
            yield obj
