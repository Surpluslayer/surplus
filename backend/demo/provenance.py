"""backend/demo/provenance.py : tag/query helpers over models.DemoProvenance.

One rule enforced here, nowhere else: every row this package writes into a
production table gets exactly one DemoProvenance tag, written in the SAME
transaction (flush -> tag -> the caller commits once). A generated row that
exists without a provenance tag is a bug -- it would be silently
indistinguishable from real data to anything that doesn't know to look.
"""
from __future__ import annotations

from .. import models

# The only values this package writes. Kept as a closed set so a typo can't
# silently invent a new, unqueryable provenance category.
BASELINE = "generated_from_assumed_distribution"
EXTENDED = "generated_beyond_baseline"
# A hand-constructed known-answer scenario (backend/observe/harnesses/
# synthetic_scenarios.py) -- NOT a scaled reproduction of real usage like
# BASELINE/EXTENDED. Reuses this same tag/cohort_row_counts/delete_cohort
# machinery rather than building a parallel one.
SYNTHETIC = "synthetic_known_answer_scenario"
# A REAL production row -- a genuinely synced contact or interaction --
# REFERENCED into an evaluation cohort. Nothing generated it; the tag only
# records "this row is part of the set the harnesses evaluate over".
#
# It exists because the alternative was labelling real synced rows BASELINE,
# i.e. "generated_from_assumed_distribution", which is simply false about
# where the data came from. In a system whose entire claim is that its
# provenance is honest, a tag that lies about origin is the worst possible
# thing to leave in the table.
OBSERVED = "observed_real_account_data"
PROVENANCE_VALUES = frozenset({BASELINE, EXTENDED, SYNTHETIC, OBSERVED})

# The values that mark a row this package CREATED, and may therefore destroy.
# OBSERVED is deliberately absent: see delete_cohort.
GENERATED_VALUES = frozenset({BASELINE, EXTENDED, SYNTHETIC})

# Cohorts that are legitimate EVALUATION datasets. SYNTHETIC is deliberately
# absent: those four hand-built fixtures are a known-answer correctness check,
# and because synthetic_scenarios.setup() writes them lazily during its own
# run they are always the newest rows in the table -- so "newest cohort wins"
# silently pointed every aggregate harness at a 4-contact fixture set. See
# observe/logstream.evaluation_cohort_id.
EVALUATION_VALUES = frozenset({BASELINE, EXTENDED, OBSERVED})


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
    """Delete every row this cohort GENERATED, across every tagged table, by
    walking the provenance index -- not by re-deriving which rows belong to
    the cohort from product logic. Returns the number of rows deleted (not
    counting the provenance rows themselves). Used by tests and by a
    reset-the-demo-cohort operator action; never called from product code.

    OBSERVED rows are REFERENCES to real production data, not things this
    package created, so they are never deleted here -- only their tags are,
    which removes them from the cohort and leaves the rows untouched.

    This is not a hypothetical guard. When seed_operator began tagging real
    synced rows instead of generating new ones, this function -- recommended
    by that module's own docstring as the way to clean up -- deleted a real
    account's entire book: 40 contacts and 40 interactions, gone. Deleting
    only what we created is the invariant that makes tagging real rows safe
    at all.
    """
    from sqlalchemy import select
    tags = db.execute(
        select(models.DemoProvenance).where(models.DemoProvenance.cohort_id == cohort_id)
    ).scalars().all()
    by_table: dict[str, list[int]] = {}
    for t in tags:
        if t.data_provenance not in GENERATED_VALUES:
            continue        # a reference to real data -- untag, never delete
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
