"""
db.py — SQLite engine + session.

One file SQLite database at backend/data/surplus.db. Swap the URL for Postgres
in production; nothing else in the codebase needs to change.
"""
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = Path(__file__).parent / "data" / "surplus.db"
ENGINE = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},  # FastAPI uses a threadpool
)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db():
    """FastAPI dependency — yields a session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they don't exist. Called on app startup.

    Also runs lightweight in-place migrations for SQLite (no alembic):
    - events.user_id   added when missing
    - operator User row auto-created from UNIPILE_ACCOUNT_ID env var
    - existing events with NULL user_id backfilled to the operator user
    """
    from . import models  # noqa: F401  (import registers the models)
    Base.metadata.create_all(ENGINE)
    _migrate_event_user_id()
    _ensure_operator_user_and_backfill()


def _migrate_event_user_id() -> None:
    """Add events.user_id to legacy DBs that pre-date multi-tenant.

    SQLAlchemy's create_all only creates missing tables — it doesn't ALTER
    existing ones to add columns. For the single column we needed to add this
    week, hand-rolling the ALTER is simpler than introducing alembic.
    """
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "user_id" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN user_id INTEGER"))
        # SQLite doesn't enforce FK in ALTER but ORM relationship still works
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_user_id ON events (user_id)"))


def _ensure_operator_user_and_backfill() -> None:
    """Make the env-var operator account a real User row + claim orphan events.

    Why this exists:
      Before multi-tenant, every Event was anonymous and every send used
      UNIPILE_ACCOUNT_ID from env. After the migration, every Event needs an
      owner (a User row). The cleanest backfill is to invent a "operator" User
      whose unipile_account_id matches the env var, then reassign every
      orphaned event to that operator. This way:
        - Existing events stay reachable (visible to operator, sends still
          go through the env-var account)
        - New events created by signed-in users belong to those users
        - The webhook handler has a deterministic fallback when an event's
          user is the operator (it just uses the env-var provider)

    Idempotent — safe to run on every startup. No-op when:
      - UNIPILE_ACCOUNT_ID env var is unset (e.g. fresh dev machine)
      - The operator User already exists (subsequent startups)
      - There are no orphan events
    """
    import os
    from .models import Event, User
    from datetime import datetime, timezone

    operator_account_id = (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    if not operator_account_id:
        return  # no env operator configured; nothing to backfill against

    db = SessionLocal()
    try:
        operator = db.query(User).filter(User.unipile_account_id == operator_account_id).first()
        if operator is None:
            operator = User(
                unipile_account_id=operator_account_id,
                name="Operator",
                email=None,
                headline="Operator account configured via UNIPILE_ACCOUNT_ID env var",
                avatar_url=None,
                linkedin_status="active",
                last_login_at=datetime.now(timezone.utc),
            )
            db.add(operator)
            db.flush()  # need operator.id for backfill
        # Backfill any events that pre-date multi-tenant
        orphan_count = db.query(Event).filter(Event.user_id.is_(None)).count()
        if orphan_count:
            db.query(Event).filter(Event.user_id.is_(None)).update(
                {Event.user_id: operator.id}, synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def reset_db() -> None:
    """Drop + recreate every table. Used by tests and the seed script."""
    from . import models  # noqa: F401
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
