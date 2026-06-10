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
    # Connection-pool sizing for prod. The pool is PER WORKER PROCESS, so the
    # ceiling that matters is:
    #     WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW)  ≤  Postgres max
    # Exceed it and you get "QueuePool limit ... connection timed out" under
    # burst load, which looks like a crash. Both are env-driven so you can tune
    # for instance size / Postgres plan in Railway WITHOUT a code change.
    #
    # Defaults: pool 5 + overflow 3 = 8 connections PER WORKER. With the default
    # WEB_CONCURRENCY=1 that's 8 total; if you raise workers to N, the ceiling is
    # N × 8 — keep it under your Postgres cap (drop DB_POOL_SIZE on a smaller
    # ~20-conn Postgres). pool_pre_ping survives idle
    # disconnects; pool_recycle=300 kills connections older than 5 min so
    # Railway/Postgres side-disconnects don't surface as "connection
    # invalidated" on the next query.
    def _int_env(name: str, default: int) -> int:
        try:
            return max(1, int((os.environ.get(name) or "").strip()))
        except ValueError:
            return default

    ENGINE = create_engine(
        DB_URL,
        pool_pre_ping=True,
        pool_size=_int_env("DB_POOL_SIZE", 5),
        max_overflow=_int_env("DB_MAX_OVERFLOW", 3),
        pool_timeout=_int_env("DB_POOL_TIMEOUT", 10),
        pool_recycle=300,
    )
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


def _is_benign_migration_error(exc: Exception) -> bool:
    """True if a migration error is the expected idempotent race (the column
    already exists because a sibling replica, or a previous boot, added it).

    These surface differently per dialect — Postgres says "already exists" /
    "duplicate column", SQLite says "duplicate column name". We match on the
    message text because the SQLAlchemy/DBAPI error types don't distinguish
    "already exists" from "real DDL failure" cleanly across drivers."""
    msg = str(exc).lower()
    benign_markers = (
        "already exists",
        "duplicate column",
    )
    return any(marker in msg for marker in benign_markers)


def init_db() -> None:
    """Create tables if they don't exist. Called on app startup.

    Also runs lightweight in-place migrations (no alembic). Each
    _migrate_* function is wrapped in a try/except so one botched
    migration doesn't kill the lifespan — important when two replicas
    boot in parallel against the same Postgres : Postgres serializes
    DDL but the "already exists" race surface is real. Failures get
    logged loudly to Railway logs.
    """
    from . import models  # noqa: F401  (import registers the models)
    try:
        Base.metadata.create_all(ENGINE)
    except Exception as exc:  # noqa: BLE001
        print(f"  [init_db] create_all failed: {type(exc).__name__}: {exc}")

    migrations = [
        _migrate_event_user_id,
        _migrate_event_sources,
        _migrate_event_yoe,
        _migrate_prospect_connection_status,
        _migrate_prospect_scholar_citations,
        _migrate_user_voice_examples,
        _migrate_user_unipile_account_id_nullable,
        _migrate_event_triage_config,
        _migrate_event_event_date,
        _migrate_event_event_name,
        _migrate_user_billing_columns,
        _migrate_user_plan_usage_columns,
        _migrate_applicant_evaluation_verifier,
        _migrate_applicant_enrichment_raw,
        _migrate_event_kind_label,
        _migrate_prospect_capture_fields,
        _migrate_prospect_enrichment_text,
        _migrate_event_brief,
        _migrate_prospect_live_enrichment,
        _migrate_user_voice_synced_at,
        _migrate_user_voice_profile,
        _migrate_prospect_contact_id,
        _migrate_prospect_role_width,
        _migrate_contact_watch,
        _migrate_user_auto_followups,
        _migrate_user_onboarding,
        _migrate_prospect_vip,
        _migrate_user_email_account,
    ]
    for migration in migrations:
        try:
            migration()
        except Exception as exc:  # noqa: BLE001
            # Two replicas can race the same ALTER and one returns "column
            # already exists" / "duplicate column". That's benign — the
            # other replica did the work, so log + continue.
            #
            # Anything else (lock timeout, permission error, bad SQL, a
            # rolled-back transaction) is a REAL failure that would silently
            # ship a half-applied schema and 500 every write to the table.
            # We learned this the hard way : a swallowed enrichment_raw
            # migration left prod inserting into a missing column. Re-raise
            # so the deploy fails its healthcheck loudly instead of serving
            # a broken schema.
            if _is_benign_migration_error(exc):
                print(f"  [init_db] {migration.__name__} skipped (benign "
                      f"idempotent race): {type(exc).__name__}: {exc}")
                continue
            print(f"  [init_db] {migration.__name__} FAILED with a non-benign "
                  f"error — aborting startup so this doesn't silently ship a "
                  f"broken schema: {type(exc).__name__}: {exc}")
            raise
    try:
        _ensure_operator_user_and_backfill()
    except Exception as exc:  # noqa: BLE001
        print(f"  [init_db] operator backfill failed: {type(exc).__name__}: {exc}")


def _migrate_event_event_name() -> None:
    """Add events.event_name (VARCHAR(160), default '') for the operator-
    supplied display name. Empty string for existing rows means 'unnamed'."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "event_name" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN event_name VARCHAR(160) DEFAULT ''"))


def _migrate_event_event_date() -> None:
    """Add events.event_date (VARCHAR(20), default '') for the intake-form
    date field. Empty string for existing rows means 'date not yet set'."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "event_date" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN event_date VARCHAR(20) DEFAULT ''"))


def _migrate_event_kind_label() -> None:
    """Add events.kind (VARCHAR(20), default 'planned') and events.label
    (VARCHAR(200), NULL) for the in-person scan-to-connect entry point.

    Existing rows default to kind='planned' (the classic intake-form event),
    so the new in_person path is purely additive : nothing about how planned
    events are created/read changes. label is NULL for planned events, which
    keep using event_name."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "kind" not in cols:
            conn.execute(text(
                f"ALTER TABLE events ADD COLUMN {ine}kind "
                "VARCHAR(20) DEFAULT 'planned'"
            ))
        if "label" not in cols:
            conn.execute(text(
                f"ALTER TABLE events ADD COLUMN {ine}label VARCHAR(200)"
            ))


def _migrate_prospect_capture_fields() -> None:
    """Add the in-person capture columns to prospects: note (VARCHAR(300),
    NULL), captured_at (TIMESTAMP, NULL), source (VARCHAR(20), NULL).

    All nullable / undefaulted : web-discovered prospects leave them NULL,
    scan-to-connect rows fill them in. The "pending" status value needs no
    DDL : status is already VARCHAR(20) and "pending" fits."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "note" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}note VARCHAR(300)"
            ))
        if "private_note" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}private_note VARCHAR(500)"
            ))
        if "contact_type" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}contact_type VARCHAR(20)"
            ))
        if "next_step" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}next_step VARCHAR(300)"
            ))
        if "captured_at" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}captured_at TIMESTAMP"
            ))
        if "source" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}source VARCHAR(20)"
            ))


def _migrate_prospect_enrichment_text() -> None:
    """Add prospects.headline (VARCHAR(300), NULL) and prospects.bio (TEXT,
    NULL) for the discovery-time profile context fed into outreach compose.

    Both nullable / undefaulted : rows discovered before this column existed,
    or via sources that don't carry a headline/bio, leave them NULL and
    compose falls back to the chip fields it already used."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "headline" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}headline VARCHAR(300)"
            ))
        if "bio" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}bio TEXT"
            ))


def _migrate_contact_watch() -> None:
    """Add the relationship-watch snapshot columns to contacts:
    headline (VARCHAR(300)), title (VARCHAR(200)), seen_post_ids (TEXT,
    default '[]'), watched_at (TIMESTAMP), watch_error (VARCHAR(300)).

    All nullable / safely-defaulted : pre-existing contacts get NULL snapshot +
    NULL watched_at, so their first poll seeds the baseline silently (no spam
    of 'changed jobs' for state we never recorded). seen_post_ids defaults to
    '[]' so the JSON parse in relationship_watch never sees NULL."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "contacts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("contacts")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "headline" not in cols:
            conn.execute(text(
                f"ALTER TABLE contacts ADD COLUMN {ine}headline VARCHAR(300)"
            ))
        if "title" not in cols:
            conn.execute(text(
                f"ALTER TABLE contacts ADD COLUMN {ine}title VARCHAR(200)"
            ))
        if "seen_post_ids" not in cols:
            conn.execute(text(
                f"ALTER TABLE contacts ADD COLUMN {ine}seen_post_ids TEXT "
                f"DEFAULT '[]'"
            ))
        if "watched_at" not in cols:
            conn.execute(text(
                f"ALTER TABLE contacts ADD COLUMN {ine}watched_at TIMESTAMP"
            ))
        if "watch_error" not in cols:
            conn.execute(text(
                f"ALTER TABLE contacts ADD COLUMN {ine}watch_error VARCHAR(300)"
            ))


def _migrate_prospect_live_enrichment() -> None:
    """Add prospects.recent_activity (TEXT, NULL) and prospects.enriched_at
    (TIMESTAMP, NULL) for the lazy live-LinkedIn enrichment cache.

    Both nullable / undefaulted : enriched_at NULL means 'not yet pulled from
    Unipile', which is the gate the lazy enrichment checks."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "recent_activity" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}recent_activity TEXT"
            ))
        if "enriched_at" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}enriched_at TIMESTAMP"
            ))


def _migrate_user_voice_synced_at() -> None:
    """Add users.voice_synced_at (TIMESTAMP, NULL) : gates the lazy auto-sync
    of voice_examples from the user's real LinkedIn sent-messages. NULL =
    never synced."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "voice_synced_at" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE users ADD COLUMN {ine}voice_synced_at TIMESTAMP"
        ))


def _migrate_prospect_contact_id() -> None:
    """Add prospects.contact_id (INTEGER, NULL) : the lazy link to the Contact
    spine (relationship graph). NULL is fully supported — event-scoped Prospect
    flows never require it.

    Added as a plain INTEGER with no inline FK : SQLite's ALTER TABLE can't add
    a column with a REFERENCES clause. On a fresh DB, Base.metadata.create_all
    wires the real FK; on an existing DB the value is just an int we resolve in
    Python. The contacts/relationship_interactions TABLES themselves are created
    by create_all (no migration needed — they're brand new)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    if "contact_id" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE prospects ADD COLUMN {ine}contact_id INTEGER"
        ))


def _migrate_event_brief() -> None:
    """Add events.brief (TEXT, default '') for the host's plain-English event
    description. Empty string for existing rows means 'no describe-box text';
    outreach compose then relies on the per-goal framing template alone."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "brief" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN brief TEXT DEFAULT ''"))


def _migrate_event_triage_config() -> None:
    """Add events.triage_config (TEXT, default '') for Applicant Triage.
    Empty string for existing rows means 'outbound-only event'."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "events" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("events")}
    if "triage_config" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN triage_config TEXT DEFAULT ''"))


def _migrate_applicant_evaluation_verifier() -> None:
    """Add the Judge B (evidence auditor) columns to applicant_evaluations:
    verifier_ran (BOOLEAN), verifier_adjustments (TEXT JSON list), and
    verifier_reason (TEXT). Existing rows pre-date the verifier, so they
    default to 'did not run' — their recommendation came from Judge A +
    the deterministic floor alone, which is still valid."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "applicant_evaluations" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("applicant_evaluations")}
    # SQLite wants a literal 0/1 default for BOOLEAN; Postgres accepts FALSE.
    is_pg = ENGINE.dialect.name == "postgresql"
    bool_default = "FALSE" if is_pg else "0"
    # Postgres supports IF NOT EXISTS, making each ALTER idempotent so racing
    # replicas can't error. SQLite lacks it, but the inspect-guard covers the
    # single-writer local case.
    ine = "IF NOT EXISTS " if is_pg else ""
    with ENGINE.begin() as conn:
        if "verifier_ran" not in cols:
            conn.execute(text(
                "ALTER TABLE applicant_evaluations "
                f"ADD COLUMN {ine}verifier_ran BOOLEAN DEFAULT {bool_default}"
            ))
        if "verifier_adjustments" not in cols:
            conn.execute(text(
                "ALTER TABLE applicant_evaluations "
                f"ADD COLUMN {ine}verifier_adjustments TEXT DEFAULT '[]'"
            ))
        if "verifier_reason" not in cols:
            conn.execute(text(
                "ALTER TABLE applicant_evaluations "
                f"ADD COLUMN {ine}verifier_reason TEXT DEFAULT ''"
            ))


def _migrate_applicant_enrichment_raw() -> None:
    """Add applicants.enrichment_raw (TEXT, default '') to hold the frozen raw
    enrichment (unreconciled Unipile/Exa output). Persisted once on first
    evaluation and reused on re-runs so the inbound triage path is reproducible.
    Existing rows default to '' = 'never enriched', so their next evaluation
    enriches + persists as normal."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "applicants" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("applicants")}
    if "enrichment_raw" in cols:
        return
    # Postgres supports IF NOT EXISTS, which makes the ALTER itself idempotent
    # so two replicas racing this can't error. SQLite doesn't support it, but
    # the inspect-guard above already covers the single-writer local case.
    if_not_exists = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE applicants ADD COLUMN {if_not_exists}"
            "enrichment_raw TEXT DEFAULT ''"
        ))


def _migrate_prospect_role_width() -> None:
    """Widen prospects.role from VARCHAR(160) to VARCHAR(300).

    The in-person scan resolver can put a full LinkedIn headline (not a short
    title) into `role`, e.g. "Seasoned entrepreneur, ... founder of Jetzy
    (Building Agentic AI with VIP perks ...)" — past 160 chars. Postgres then
    raises StringDataRightTruncation on INSERT and 500s the capture instead of
    truncating, so the column must be wide enough. No-op on SQLite (it doesn't
    enforce VARCHAR length) and idempotent on Postgres (skips when already >=300)."""
    from sqlalchemy import inspect, text
    if ENGINE.dialect.name != "postgresql":
        return  # SQLite ignores VARCHAR length; nothing to do.
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    role = next((c for c in insp.get_columns("prospects")
                 if c["name"] == "role"), None)
    if role is None:
        return
    length = getattr(role.get("type"), "length", None)
    if length is not None and length >= 300:
        return  # already widened
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE prospects ALTER COLUMN role TYPE VARCHAR(300)"
        ))


def _migrate_user_billing_columns() -> None:
    """Add users.stripe_customer_id (VARCHAR(120), NULL) and users.paid_at
    (DATETIME, NULL). NULL paid_at = free tier; webhook stamps it on
    successful Stripe Checkout. Cross-dialect-safe : SQLite + Postgres
    both accept these ADD COLUMNs without a default."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    with ENGINE.begin() as conn:
        if "stripe_customer_id" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR(120)"
            ))
            # Indexed on the model; SQLite ignores unique-but-indexed ADD,
            # Postgres needs an explicit CREATE INDEX.
            if ENGINE.dialect.name == "postgresql":
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_users_stripe_customer_id "
                    "ON users (stripe_customer_id)"
                ))
        if "paid_at" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN paid_at TIMESTAMP"
            ))


def _migrate_user_plan_usage_columns() -> None:
    """Add the subscription-plan + metered-usage columns to users.

    Tier (plan/subscription_status), Stripe linkage (subscription_id/price_id),
    per-period counters (drafts_used_this_period / contacts_scanned_this_period)
    and the period bounds. Cross-dialect-safe: every ADD COLUMN carries a
    server-side DEFAULT so existing rows backfill without a follow-up UPDATE,
    and the whole thing is idempotent (skips any column already present)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    # name -> column DDL (type + default). TIMESTAMP/varchar columns are NULL.
    additions = {
        "plan": "VARCHAR(20) DEFAULT 'free'",
        "subscription_status": "VARCHAR(30) DEFAULT 'free'",
        "stripe_subscription_id": "VARCHAR(120)",
        "stripe_price_id": "VARCHAR(120)",
        "drafts_used_this_period": "INTEGER DEFAULT 0",
        "contacts_scanned_this_period": "INTEGER DEFAULT 0",
        "billing_period_start": "TIMESTAMP",
        "billing_period_end": "TIMESTAMP",
    }
    with ENGINE.begin() as conn:
        for name, ddl in additions.items():
            if name in cols:
                continue
            conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
        if "stripe_subscription_id" not in cols and ENGINE.dialect.name == "postgresql":
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_users_stripe_subscription_id "
                "ON users (stripe_subscription_id)"
            ))


def _migrate_user_unipile_account_id_nullable() -> None:
    """Drop the NOT NULL constraint on users.unipile_account_id so triage-only
    users (no LinkedIn / Unipile connection) can have a User row. SQLite is
    permissive enough that older rows are unaffected; Postgres needs the
    explicit ALTER."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    dialect = ENGINE.dialect.name
    # SQLite stores column nullability differently and won't accept the
    # Postgres-style ALTER; create_all already allows NULL there because we
    # changed the Mapped[] annotation. So this is Postgres-only.
    if dialect != "postgresql":
        return
    cols = insp.get_columns("users")
    target = next((c for c in cols if c["name"] == "unipile_account_id"), None)
    if target is None or target.get("nullable") is True:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ALTER COLUMN unipile_account_id DROP NOT NULL"
        ))


def _migrate_user_voice_examples() -> None:
    """Add users.voice_examples (TEXT, default '') for the voice-matching
    feature. Old User rows get an empty string, which compose() treats as
    'no per-user examples, fall through to env var.'"""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "voice_examples" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN voice_examples TEXT DEFAULT ''"))


def _migrate_user_voice_profile() -> None:
    """Add users.voice_profile (TEXT, default '') : the cached structured voice
    profile (distilled style rules + the fingerprint of the examples it was built
    from). Old rows get an empty string, which the drafting surfaces treat as
    'no cache, rebuild the profile inline from voice_examples.'"""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "voice_profile" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE users ADD COLUMN {ine}voice_profile TEXT DEFAULT ''"))


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


def _migrate_user_auto_followups() -> None:
    """Add users.auto_followups_enabled (BOOLEAN, default False) : the per-user
    opt-in for the Gmail-style scheduled follow-up feature.

    Defaulted to 0/false so every existing user stays opted OUT until they
    explicitly turn it on : sending a first DM never auto-stages a follow-up
    for a user who hasn't asked for it."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "auto_followups_enabled" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    default = "FALSE" if ENGINE.dialect.name == "postgresql" else "0"
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE users ADD COLUMN {ine}auto_followups_enabled "
            f"BOOLEAN DEFAULT {default}"
        ))


def _migrate_user_onboarding() -> None:
    """Add the first-time-user onboarding columns to users:
      onboarding_status (VARCHAR(20), default ''),
      onboarding_step   (INTEGER, default 0),
      saved_send_link   (VARCHAR(400), NULL).

    Critically, BACKFILL every already-connected user to 'done' : the tour is
    only for people adding LinkedIn for the FIRST time, so users who were
    already connected before this feature shipped must never see it. Fresh
    rows default to '' and get armed to 'active' at the moment of their first
    LinkedIn connect (routes/auth)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "onboarding_status" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}onboarding_status "
                f"VARCHAR(20) DEFAULT ''"
            ))
            # Existing connected users have already used the product : mark
            # them done so the post-deploy boot doesn't drop everyone into a
            # tour. Pre-feature rows that never connected stay '' and arm
            # naturally if/when they connect LinkedIn.
            conn.execute(text(
                "UPDATE users SET onboarding_status='done' "
                "WHERE unipile_account_id IS NOT NULL"
            ))
        if "onboarding_step" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}onboarding_step "
                f"INTEGER DEFAULT 0"
            ))
        if "saved_send_link" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}saved_send_link "
                f"VARCHAR(400)"
            ))


def _migrate_prospect_vip() -> None:
    """Add prospects.vip (BOOLEAN, default False) : the operator's icon-only
    'star this person as a VIP' toggle at in-person capture time. Existing
    rows default to not-VIP."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    if "vip" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    default = "FALSE" if ENGINE.dialect.name == "postgresql" else "0"
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE prospects ADD COLUMN {ine}vip BOOLEAN DEFAULT {default}"
        ))


def _migrate_user_email_account() -> None:
    """Add the email-channel columns to users : a SECOND Unipile account id
    pointing at the user's real mailbox (Gmail / Outlook), plus its display
    address, health status, and connect timestamp. All nullable / defaulted
    so existing rows are untouched (email starts disconnected for everyone).
    Cross-dialect-safe : SQLite + Postgres."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "unipile_email_account_id" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}unipile_email_account_id "
                "VARCHAR(80)"
            ))
            if ENGINE.dialect.name == "postgresql":
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_users_unipile_email_account_id "
                    "ON users (unipile_email_account_id)"
                ))
        if "email_account_address" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}email_account_address "
                "VARCHAR(200)"
            ))
        if "email_status" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}email_status VARCHAR(20) "
                "DEFAULT 'disconnected'"
            ))
        if "email_connected_at" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}email_connected_at TIMESTAMP"
            ))


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
