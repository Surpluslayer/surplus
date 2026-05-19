"""
db.py : DB engine + session.

In production, reads DATABASE_URL (Railway provides a Postgres URL when a
Postgres service is attached). In local dev or when DATABASE_URL is unset,
falls back to a SQLite file at backend/data/surplus.db.

Why this matters: Railway's container filesystem is ephemeral by default :
every deploy gets a fresh disk, so the SQLite DB (and every Session/User
row in it) is wiped on each redeploy. Postgres survives deploys, so user
sessions don't get invalidated every time we push.
"""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_RAW_DB_URL = (os.environ.get("DATABASE_URL") or "").strip()

if _RAW_DB_URL:
    # Railway / Heroku style: postgres://... : SQLAlchemy 2.x wants postgresql://
    if _RAW_DB_URL.startswith("postgres://"):
        _RAW_DB_URL = _RAW_DB_URL.replace("postgres://", "postgresql://", 1)
    DB_URL = _RAW_DB_URL
    DB_PATH = None  # not used in Postgres mode
    ENGINE = create_engine(DB_URL, pool_pre_ping=True)
else:
    DB_PATH = Path(__file__).parent / "data" / "surplus.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_URL = f"sqlite:///{DB_PATH}"
    ENGINE = create_engine(
        DB_URL,
        connect_args={"check_same_thread": False},  # FastAPI uses a threadpool
    )

SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db():
    """FastAPI dependency : yields a session, always closes it."""
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
    _migrate_event_sources()
    _migrate_event_yoe()
    _migrate_prospect_connection_status()
    _migrate_prospect_scholar_citations()
    _ensure_operator_user_and_backfill()


def _migrate_event_yoe() -> None:
    """Add events.yoe to legacy DBs. Empty string == 'no preference'."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "yoe" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE events ADD COLUMN yoe VARCHAR(80) DEFAULT ''"
        ))


def _migrate_event_sources() -> None:
    """Add events.sources to legacy DBs. Defaults to 'linkedin' so existing
    events keep working (LinkedIn-only fan-out is the safe minimum)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "sources" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE events ADD COLUMN sources "
            "VARCHAR(120) DEFAULT 'linkedin'"
        ))


def _migrate_prospect_connection_status() -> None:
    """Add prospects.connection_status + connection_checked_at to legacy DBs.

    Same idea as _migrate_event_user_id : create_all doesn't ALTER existing
    tables, so we hand-roll the additions. Both columns nullable / defaulted
    so old rows just become "unknown" until the first Unipile relation
    check stamps them.
    """
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    with ENGINE.begin() as conn:
        if "connection_status" not in cols:
            conn.execute(text(
                "ALTER TABLE prospects ADD COLUMN connection_status "
                "VARCHAR(20) DEFAULT 'unknown'"
            ))
        if "connection_checked_at" not in cols:
            conn.execute(text(
                "ALTER TABLE prospects ADD COLUMN connection_checked_at "
                "TIMESTAMP"
            ))


def _migrate_prospect_scholar_citations() -> None:
    """Add prospects.scholar_citations to legacy DBs.

    The Scholar adapter attaches an approximate citation count to any
    record whose identity matches across sources. Old rows just default
    to 0 (no academic footprint visible) which is exactly what the scorer
    treats as "no signal".
    """
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    if "scholar_citations" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE prospects ADD COLUMN scholar_citations INTEGER DEFAULT 0"
        ))


def _migrate_event_user_id() -> None:
    """Add events.user_id to legacy DBs that pre-date multi-tenant.

    SQLAlchemy's create_all only creates missing tables : it doesn't ALTER
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

    Idempotent : safe to run on every startup. No-op when:
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
