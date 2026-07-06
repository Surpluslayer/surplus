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

    # Encrypt the app<->Postgres hop (checklist: "TLS on internal service-to-
    # service traffic, not just the edge"). Railway's private-network Postgres
    # accepts SSL; `require` encrypts without cert verification. Overridable via
    # DB_SSLMODE (set to `verify-full` with a CA for the strongest posture, or
    # `disable` as an escape hatch if a deploy can't negotiate SSL).
    _sslmode = (os.environ.get("DB_SSLMODE") or "require").strip()
    _pg_connect_args = {} if _sslmode in ("", "disable") else {"sslmode": _sslmode}

    ENGINE = create_engine(
        DB_URL,
        pool_pre_ping=True,
        pool_size=_int_env("DB_POOL_SIZE", 5),
        max_overflow=_int_env("DB_MAX_OVERFLOW", 3),
        pool_timeout=_int_env("DB_POOL_TIMEOUT", 10),
        pool_recycle=300,
        connect_args=_pg_connect_args,
    )

    # Request-path engine: lets REQUEST connections use a DIFFERENT role than the
    # background jobs. Set REQUEST_DATABASE_URL to the non-superuser surplus_app
    # role and the request path (get_db) connects through it so Postgres RLS
    # enforces per-user isolation -- while every background job keeps using
    # SessionLocal/ENGINE (the superuser role, which BYPASSES RLS, so sweeps that
    # scan all users still work). Unset -> requests reuse ENGINE (no change).
    _req_url = (os.environ.get("REQUEST_DATABASE_URL") or "").strip()
    if _req_url.startswith("postgres://"):
        _req_url = "postgresql://" + _req_url[len("postgres://"):]
    if _req_url:
        REQUEST_ENGINE = create_engine(
            _req_url,
            pool_pre_ping=True,
            pool_size=_int_env("DB_POOL_SIZE", 5),
            max_overflow=_int_env("DB_MAX_OVERFLOW", 3),
            pool_timeout=_int_env("DB_POOL_TIMEOUT", 10),
            pool_recycle=300,
            connect_args=_pg_connect_args,
        )
    else:
        REQUEST_ENGINE = ENGINE
else:
    DB_PATH = Path(__file__).parent / "data" / "surplus.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_URL = f"sqlite:///{DB_PATH}"
    ENGINE = create_engine(
        DB_URL,
        connect_args={"check_same_thread": False},  # FastAPI uses a threadpool
    )
    REQUEST_ENGINE = ENGINE

SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False)
# Request path only (get_db). Same as SessionLocal unless REQUEST_DATABASE_URL
# points requests at a lower-privilege role for RLS (see REQUEST_ENGINE note).
RequestSessionLocal = sessionmaker(bind=REQUEST_ENGINE, autoflush=False,
                                   autocommit=False)


# SQLite does NOT enforce foreign keys (and therefore ON DELETE CASCADE) unless
# PRAGMA foreign_keys is turned ON per connection. Postgres always enforces. We
# now rely on the DB to cascade-delete User/Contact children (identities, facts,
# outgoing messages, jobs, ...), so a SQLite deployment must enable it or the
# delete paths would silently orphan children instead of cascading. Postgres
# engines are untouched. Tests that build their own SQLite engine should install
# the same listener (see enable_sqlite_fk_pragma).
def enable_sqlite_fk_pragma(engine) -> None:
    """Attach a connect listener that runs `PRAGMA foreign_keys=ON` on every new
    SQLite connection. No-op for non-SQLite engines. Idempotent-safe to call once
    per engine."""
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import event as _sa_event

    @_sa_event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


enable_sqlite_fk_pragma(ENGINE)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _rls_enabled() -> bool:
    return (os.environ.get("SURPLUS_RLS_ENABLED") or "").strip().lower() in (
        "1", "true", "yes", "on")


def set_rls_user(db, user_id: int) -> None:
    """Scope this request's DB connection to `user_id` so Postgres Row-Level
    Security returns only that user's rows. No-op unless SURPLUS_RLS_ENABLED and
    Postgres. Harmless when the app still connects as a superuser (RLS bypassed);
    it only bites once the connection role is a non-bypass one (surplus_app).

    set_config (parameterized) instead of a literal SET so the uid can't be
    injected. Session-scoped (is_local=false) so it survives commits within the
    request; get_db RESETS it before the pooled connection is handed to the next.
    """
    if not _rls_enabled() or ENGINE.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    db.execute(text("SELECT set_config('app.user_id', :uid, false)"),
               {"uid": str(int(user_id))})


def reset_rls_user(db) -> None:
    """Clear the RLS scope before the pooled connection is reused by another
    request (otherwise the next request would inherit this user's scope)."""
    if not _rls_enabled() or ENGINE.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    try:
        db.execute(text("SELECT set_config('app.user_id', '', false)"))
    except Exception:  # noqa: BLE001 -- a broken txn must not block session close
        pass


def get_db():
    """FastAPI dependency : yields a session, always closes it. On release it
    clears any RLS scope set during the request so a pooled connection can't leak
    one user's scope into the next."""
    db = RequestSessionLocal()
    try:
        yield db
    finally:
        try:
            reset_rls_user(db)
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
    from . import models_monitoring  # noqa: F401  (continuous-enrichment tables)

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
        _migrate_user_whatsapp_account,
        _migrate_email_accounts,
        _migrate_prospect_email,
        _migrate_contact_email_thread,
        _migrate_followup_channel,
        _migrate_followup_booking_payload,
        _migrate_contact_vip,
        _migrate_contact_profile_baselined,
        _migrate_contact_preferred_channel,
        _migrate_user_is_demo,
        _migrate_user_google_sub,
        _migrate_user_microsoft_sub,
        _migrate_user_password_hash,
        _migrate_user_email_verified,
        _migrate_user_verify_code,
        _migrate_session_client,
        _migrate_contact_phone,
        _migrate_contact_identities,
        _migrate_prospect_draft_fields,
        _migrate_job_event_id_nullable,
        _migrate_user_linkedin_chat_synced_at,
        _migrate_user_autonomy_mode,
        _migrate_email_pending_outreach,
        # Backfill-encrypt OAuth tokens (no-op until SURPLUS_ENCRYPTION_KEK is
        # set). Appending it also bumps the schema revision so create_all picks
        # up the new tenant_keys table on existing databases.
        _migrate_encrypt_connected_account_tokens,
        # Bumps the schema revision so create_all picks up the deletion_audit
        # table (Phase 3 deletion audit log) on existing databases.
        _migrate_deletion_audit,
        # Bumps the schema revision so create_all picks up the audit_log table
        # (Phase 4 access-audit trail) on existing databases.
        _migrate_audit_log,
        # Runs LAST : re-points existing User/Contact child FKs to ON DELETE
        # CASCADE so the delete paths (merge/cleanup) stop 500ing on Postgres.
        _migrate_fk_cascade,
    ]

    # Schema-revision sentinel: the loop below plus create_all's checkfirst is
    # HUNDREDS of inspector round-trips. On a replica far from the DB (EU
    # replica, US Postgres) that is MINUTES of boot time, which blew the
    # deploy healthcheck window across 6+ consecutive deploys on 2026-07-03.
    # The schema only changes when this list changes, so: one query reads the
    # stamped revision, and a match skips ALL of it. The revision is
    # len(migrations), so appending a migration re-runs the loop with no
    # manual bump. FORCE_DB_MIGRATIONS=1 overrides for ops.
    rev = str(len(migrations))
    if (os.environ.get("FORCE_DB_MIGRATIONS") or "").strip() not in ("1", "true"):
        try:
            from sqlalchemy import text as _text
            with ENGINE.connect() as _conn:
                got = _conn.execute(_text(
                    "SELECT value FROM schema_meta WHERE key = 'schema_rev'"
                )).scalar()
            if got == rev:
                # Schema already at this revision: skip create_all + the whole
                # migration loop (the boot-time win). The operator seed below
                # is DATA, not schema, and tests reset data between runs, so
                # it must run on every init_db call: cheap (one SELECT when
                # the operator row already exists).
                try:
                    _ensure_operator_user_and_backfill()
                except Exception as exc:  # noqa: BLE001
                    print(f"  [init_db] operator backfill failed: "
                          f"{type(exc).__name__}: {exc}")
                return
        except Exception:  # noqa: BLE001 -- table missing / first boot: run all
            pass

    try:
        Base.metadata.create_all(ENGINE)
    except Exception as exc:  # noqa: BLE001
        print(f"  [init_db] create_all failed: {type(exc).__name__}: {exc}")

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

    # All migrations ran (benign races swallowed, real failures raised above):
    # stamp the revision so every subsequent boot on this schema is one query.
    try:
        from sqlalchemy import text as _text
        with ENGINE.begin() as _conn:
            _conn.execute(_text(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                "(key VARCHAR(64) PRIMARY KEY, value VARCHAR(255))"))
            _conn.execute(_text(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_rev', :v) "
                "ON CONFLICT (key) DO UPDATE SET value = :v"), {"v": rev})
    except Exception as exc:  # noqa: BLE001 -- stamping is best-effort
        print(f"  [init_db] schema_rev stamp failed: {type(exc).__name__}: {exc}")


def _migrate_user_google_sub() -> None:
    """Add users.google_sub (VARCHAR(80), NULL) + its unique index for Sign in with
    Google. Existing users are all NULL (LinkedIn-first); a Google login links to them
    by email or creates a new row. NULLs don't collide in a unique index on PG/SQLite."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "google_sub" not in cols:
        with ENGINE.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN google_sub VARCHAR(80)"))
    with ENGINE.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub "
                          "ON users (google_sub)"))


def _migrate_user_microsoft_sub() -> None:
    """Add users.microsoft_sub (VARCHAR(80), NULL) + its unique index for Sign in with
    Microsoft. Mirrors google_sub: existing users NULL, NULLs don't collide on PG/SQLite."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "microsoft_sub" not in cols:
        with ENGINE.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN microsoft_sub VARCHAR(80)"))
    with ENGINE.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_microsoft_sub "
                          "ON users (microsoft_sub)"))


def _migrate_user_password_hash() -> None:
    """Add users.password_hash (VARCHAR(200), NULL) for email+password signup. Existing
    users (OAuth/LinkedIn) stay NULL; no index (we look up by email)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "password_hash" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(200)"))


def _migrate_encrypt_connected_account_tokens() -> None:
    """Backfill-encrypt plaintext OAuth tokens on connected_accounts once a KEK
    is configured. No-op when encryption is disabled (no `SURPLUS_ENCRYPTION_KEK`)
    or the table doesn't exist yet. Idempotent: `crypto.encrypt_for` skips values
    already prefixed `enc:v1:`, so re-running only touches rows still in
    plaintext. Batched commit per row keeps a large table from one giant txn."""
    from sqlalchemy import inspect
    from . import crypto, models
    if not crypto.encryption_enabled():
        return
    insp = inspect(ENGINE)
    if "connected_accounts" not in insp.get_table_names():
        return
    db = SessionLocal()
    try:
        rows = db.query(models.ConnectedAccount).all()
        changed = 0
        for row in rows:
            new_access = crypto.encrypt_for(row.user_id, row.access_token, db)
            new_refresh = crypto.encrypt_for(row.user_id, row.refresh_token, db)
            if new_access != row.access_token or new_refresh != row.refresh_token:
                row.access_token = new_access
                row.refresh_token = new_refresh
                changed += 1
        if changed:
            db.commit()
            print(f"  [init_db] encrypted tokens on {changed} connected_accounts row(s)")
    finally:
        db.close()


def _migrate_deletion_audit() -> None:
    """Ensure the deletion_audit table exists (Phase 3 deletion audit log). It's
    a new table, so create_all (which runs before this loop) creates it; this
    migration exists so appending it bumps the schema revision and create_all
    re-runs on already-stamped databases."""
    from . import models  # noqa: F401  (registers DeletionAudit for create_all)
    return


def _migrate_audit_log() -> None:
    """Ensure the audit_log table exists (Phase 4 access-audit trail). New table,
    so create_all makes it; appending this migration bumps the schema revision so
    create_all re-runs on already-stamped databases (same pattern as
    _migrate_deletion_audit)."""
    from . import models  # noqa: F401  (registers AuditLog for create_all)
    return


def _migrate_user_linkedin_chat_synced_at() -> None:
    """Add users.linkedin_chat_synced_at (TIMESTAMP, NULL) -- the incremental
    watermark for the LinkedIn DM sync. NULL = never synced (full scan)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "linkedin_chat_synced_at" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN linkedin_chat_synced_at TIMESTAMP"))


def _migrate_user_autonomy_mode() -> None:
    """Add users.autonomy_mode (VARCHAR(8), default 'off') -- the per-user
    autonomy control over agent-initiated sends: 'off' | 'ask' | 'auto'.
    Existing rows default to 'off' (agent drafts; nothing agent-initiated
    sends), matching the safe product default for new users."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "autonomy_mode" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN autonomy_mode VARCHAR(8) DEFAULT 'off'"))


def _migrate_contact_phone() -> None:
    """Add contacts.phone (VARCHAR(40), NULL) for the actual number (SMS/iMessage/
    WhatsApp). The hashed ph: key dedupes; this stores the raw value."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "contacts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("contacts")}
    if "phone" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN phone VARCHAR(40)"))


def _migrate_contact_identities() -> None:
    """Create the contact_identities table (the identity-resolution spine : one
    STRONG identity -> one Contact) + its indexes + the (user_id, kind, value)
    dedup unique, then BACKFILL one row per identity-bearing field on each existing
    Contact (email -> kind=email, phone -> kind=phone, linkedin id -> kind=linkedin),
    with is_primary aligned to the contact's primary_identity_key, source='backfill'
    and confidence=1.0. Cross-dialect-safe (SQLite + Postgres). Idempotent :
    CREATE TABLE IF NOT EXISTS + the unique index makes the backfill INSERTs skip
    rows that already exist."""
    from datetime import datetime, timezone
    from sqlalchemy import inspect, text
    from .agents.relationship.identity import (
        normalize_email, normalize_phone, normalize_linkedin,
    )
    insp = inspect(ENGINE)
    is_pg = ENGINE.dialect.name == "postgresql"
    if "contact_identities" not in insp.get_table_names():
        pk = ("id SERIAL PRIMARY KEY" if is_pg
              else "id INTEGER PRIMARY KEY AUTOINCREMENT")
        with ENGINE.begin() as conn:
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS contact_identities ("
                f"{pk}, "
                "contact_id INTEGER NOT NULL REFERENCES contacts(id), "
                "user_id INTEGER NOT NULL REFERENCES users(id), "
                "kind VARCHAR(20) NOT NULL, "
                "value VARCHAR(200) NOT NULL, "
                f"is_primary BOOLEAN DEFAULT {'FALSE' if is_pg else '0'}, "
                "source VARCHAR(30) DEFAULT 'manual', "
                "confidence FLOAT DEFAULT 1.0, "
                "created_at TIMESTAMP"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_contact_identities_contact_id "
                "ON contact_identities (contact_id)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_contact_identities_user_id "
                "ON contact_identities (user_id)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_contact_identities_kind "
                "ON contact_identities (kind)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_contact_identities_value "
                "ON contact_identities (value)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_identity_value "
                "ON contact_identities (user_id, kind, value)"))

    # ── Backfill : one identity row per identity field on each Contact ─────────
    if "contacts" not in insp.get_table_names():
        return
    with ENGINE.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, user_id, primary_identity_key, email, phone, "
            "linkedin_public_id, linkedin_url FROM contacts"
        )).fetchall()
        # provider_id column may not exist on the contacts table; tolerate that.
        contact_cols = {c["name"] for c in insp.get_columns("contacts")}
        has_provider = "linkedin_provider_id" in contact_cols

        def _emit(cid, uid, kind, value, is_primary):
            if not value:
                return
            exists = conn.execute(text(
                "SELECT 1 FROM contact_identities "
                "WHERE user_id = :u AND kind = :k AND value = :v"
            ), {"u": uid, "k": kind, "v": value}).first()
            if exists:
                return
            conn.execute(text(
                "INSERT INTO contact_identities "
                "(contact_id, user_id, kind, value, is_primary, source, "
                " confidence, created_at) "
                "VALUES (:cid, :uid, :kind, :val, :prim, 'backfill', 1.0, :now)"
            ), {"cid": cid, "uid": uid, "kind": kind, "val": value,
                "prim": bool(is_primary),
                "now": datetime.now(timezone.utc)})

        for r in rows:
            cid, uid, prim_key = r[0], r[1], (r[2] or "")
            email, phone = r[3], r[4]
            li_public, li_url = r[5], r[6]
            li_provider = ""
            if has_provider:
                pr = conn.execute(text(
                    "SELECT linkedin_provider_id FROM contacts WHERE id = :i"
                ), {"i": cid}).first()
                li_provider = (pr[0] if pr else "") or ""
            em = normalize_email(email)
            ph = normalize_phone(phone)
            li = normalize_linkedin(public_id=li_public or "",
                                    provider_id=li_provider,
                                    url=li_url or "")
            _emit(cid, uid, "email", em, prim_key.startswith("em:"))
            _emit(cid, uid, "phone", ph, prim_key.startswith("ph:"))
            _emit(cid, uid, "linkedin", li, prim_key.startswith("li:"))


def _migrate_user_email_verified() -> None:
    """Add users.email_verified (BOOLEAN, default 0/false). Existing users default to
    not-verified; OAuth/LinkedIn users are unaffected (we don't gate on it)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "email_verified" in cols:
        return
    is_pg = ENGINE.dialect.name == "postgresql"
    bool_default = "FALSE" if is_pg else "0"
    with ENGINE.begin() as conn:
        conn.execute(text(f"ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT {bool_default}"))


def _migrate_user_verify_code() -> None:
    """Add users.email_verify_code_hash (VARCHAR(200)) + email_verify_code_expires
    (DATETIME) for the PIN/OTP email-confirmation code. Both NULL by default."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    with ENGINE.begin() as conn:
        if "email_verify_code_hash" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verify_code_hash VARCHAR(200)"))
        if "email_verify_code_expires" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verify_code_expires TIMESTAMP"))


def _migrate_session_client() -> None:
    """Add sessions.client (VARCHAR(20), default 'web'). Existing sessions become 'web'
    (the cookie flow), so multi-client (ios/plugin Bearer) is purely additive."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "sessions" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("sessions")}
    if "client" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE sessions ADD COLUMN client VARCHAR(20) DEFAULT 'web'"))


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


def _migrate_contact_vip() -> None:
    """Add contacts.vip (bool, default false). Starred contacts are monitored
    more often by agents/updates_engine. Old rows default to not-starred."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "contacts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("contacts")}
    if "vip" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN vip BOOLEAN DEFAULT FALSE"))


def _migrate_user_is_demo() -> None:
    """Add users.is_demo (bool, default false) + backfill existing demo rows
    (the per-visit demo email domain) so they're flagged, not just email-matched."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "is_demo" not in cols:
        with ENGINE.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_demo BOOLEAN DEFAULT FALSE"))
    # Backfill: flag legacy demo rows by the demo email convention.
    with ENGINE.begin() as conn:
        conn.execute(text(
            "UPDATE users SET is_demo = TRUE "
            "WHERE is_demo IS NOT TRUE AND "
            "(email LIKE '%@demo.surpluslayer.com' OR email LIKE 'demo-%' "
            "OR name = 'Surplus Demo')"))


def _migrate_contact_preferred_channel() -> None:
    """Add contacts.preferred_channel (varchar, null). Which channel the host
    follows up with this contact on (email|linkedin); NULL = auto-default."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "contacts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("contacts")}
    if "preferred_channel" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN preferred_channel VARCHAR(20)"))


def _migrate_contact_profile_baselined() -> None:
    """Add contacts.profile_baselined_at (datetime, default null). NULL means the
    contact's profile snapshot hasn't been baselined yet, so the first scrape
    adopts its current company/title silently instead of emitting a job change."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "contacts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("contacts")}
    if "profile_baselined_at" in cols:
        return
    with ENGINE.begin() as conn:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN profile_baselined_at TIMESTAMP"))


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
    """Add users.auto_followups_enabled (BOOLEAN, default False).

    LEGACY: the column no longer gates staging or dispatch (env gates
    SURPLUS_AUTO_FOLLOWUPS / SURPLUS_AUTOMATED_SENDS own that now) and its
    settings routes + UI toggle are gone. The migration stays so older DBs
    keep a consistent schema with models.User."""
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


def _migrate_prospect_email() -> None:
    """Add prospects.email (VARCHAR(200), NULL, indexed) : the contact's
    email address when known (captured at scan time or backfilled by
    enrichment). Gates the email send channel for that contact."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    if "email" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE prospects ADD COLUMN {ine}email VARCHAR(200)"
        ))
        if ENGINE.dialect.name == "postgresql":
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_prospects_email "
                "ON prospects (email)"
            ))


def _migrate_contact_email_thread() -> None:
    """Add contacts.email_thread_id (VARCHAR(160), NULL) : the host-confirmed
    Unipile email thread for this person. NULL = not linked yet."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "contacts" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("contacts")}
    if "email_thread_id" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE contacts ADD COLUMN {ine}email_thread_id VARCHAR(160)"
        ))


def _migrate_followup_channel() -> None:
    """Add scheduled_followups.channel (VARCHAR(20), default 'linkedin')."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "scheduled_followups" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("scheduled_followups")}
    if "channel" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE scheduled_followups ADD COLUMN {ine}channel "
            "VARCHAR(20) DEFAULT 'linkedin'"
        ))


def _migrate_followup_booking_payload() -> None:
    """Add scheduled_followups.booking_payload (TEXT, NULL). Carries the structured
    booking intent for a meeting-proposal draft so the SEND step can fire the
    calendar event + invite. NULL for an ordinary follow-up."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "scheduled_followups" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("scheduled_followups")}
    if "booking_payload" in cols:
        return
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE scheduled_followups ADD COLUMN {ine}booking_payload TEXT"
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


def _migrate_user_whatsapp_account() -> None:
    """Add the WhatsApp-channel columns to users : a Unipile account id for the
    user's connected WhatsApp account (a CLOUD seat, like the email one above),
    plus its health status and connect timestamp. All nullable / defaulted so
    existing rows are untouched (WhatsApp starts disconnected for everyone).
    Cross-dialect-safe : SQLite + Postgres. Mirrors _migrate_user_email_account."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "unipile_whatsapp_account_id" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}unipile_whatsapp_account_id "
                "VARCHAR(80)"
            ))
            if ENGINE.dialect.name == "postgresql":
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_users_unipile_whatsapp_account_id "
                    "ON users (unipile_whatsapp_account_id)"
                ))
        if "whatsapp_status" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}whatsapp_status VARCHAR(20) "
                "DEFAULT 'disconnected'"
            ))
        if "whatsapp_connected_at" not in cols:
            conn.execute(text(
                f"ALTER TABLE users ADD COLUMN {ine}whatsapp_connected_at TIMESTAMP"
            ))


def _migrate_email_accounts() -> None:
    """Create the email_accounts table (one row per connected mailbox) and
    BACKFILL a primary row for every user that has a legacy single-mailbox
    set on the users row. Cross-dialect-safe : SQLite + Postgres.

    Backward compatibility : the legacy User.* email fields stay as a MIRROR
    of the user's primary mailbox, so this migration never removes anything --
    it just lifts the existing single account into the new multi-account table.
    Idempotent : CREATE TABLE IF NOT EXISTS + skip users that already have a
    matching row."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    is_pg = ENGINE.dialect.name == "postgresql"
    if "email_accounts" not in insp.get_table_names():
        pk = ("id SERIAL PRIMARY KEY" if is_pg
              else "id INTEGER PRIMARY KEY AUTOINCREMENT")
        ts = "TIMESTAMP" if is_pg else "TIMESTAMP"
        boolt = "BOOLEAN" if is_pg else "BOOLEAN"
        with ENGINE.begin() as conn:
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS email_accounts ("
                f"{pk}, "
                "user_id INTEGER NOT NULL REFERENCES users(id), "
                "provider VARCHAR(40) DEFAULT '', "
                "address VARCHAR(200), "
                "unipile_account_id VARCHAR(80) NOT NULL, "
                "status VARCHAR(20) DEFAULT 'active', "
                f"is_primary {boolt} DEFAULT FALSE, "
                f"connected_at {ts}, "
                f"last_synced_at {ts}"
                ")"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_email_accounts_unipile_account_id "
                "ON email_accounts (unipile_account_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_email_accounts_user_id "
                "ON email_accounts (user_id)"
            ))

    # ── Backfill : lift each user's legacy single mailbox into a primary row.
    if "users" not in insp.get_table_names():
        return
    user_cols = {c["name"] for c in insp.get_columns("users")}
    if "unipile_email_account_id" not in user_cols:
        return  # legacy email columns not present yet : nothing to backfill
    with ENGINE.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, unipile_email_account_id, email_account_address, "
            "email_status, email_connected_at FROM users "
            "WHERE unipile_email_account_id IS NOT NULL"
        )).fetchall()
        for r in rows:
            uid, acct_id, addr, status, connected_at = r
            exists = conn.execute(text(
                "SELECT 1 FROM email_accounts WHERE unipile_account_id = :a"
            ), {"a": acct_id}).first()
            if exists:
                continue
            conn.execute(text(
                "INSERT INTO email_accounts "
                "(user_id, provider, address, unipile_account_id, status, "
                " is_primary, connected_at, last_synced_at) "
                "VALUES (:uid, '', :addr, :acct, :status, :prim, :conn, NULL)"
            ), {"uid": uid, "addr": addr, "acct": acct_id,
                "status": status or "active", "prim": True,
                "conn": connected_at})


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
    # SQLite with PRAGMA foreign_keys=ON (which we now enable so ON DELETE
    # CASCADE is enforced) REFUSES to DROP a table another table still
    # references, which breaks drop_all's teardown. Disable FK enforcement for
    # the DDL, then restore it. Postgres drops in dependency order regardless.
    if ENGINE.dialect.name == "sqlite":
        from sqlalchemy import text
        with ENGINE.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            Base.metadata.drop_all(conn)
            Base.metadata.create_all(conn)
            conn.execute(text("PRAGMA foreign_keys=ON"))
        return
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)


def _migrate_prospect_draft_fields() -> None:
    """Add prospects.draft_status / draft_note / draft_message : the persisted
    in-person draft. /scan now returns fast and a detached worker composes the
    note + DM off the request path, storing them here for the UI to poll.
    All nullable, so existing rows are untouched (NULL = no stored draft)."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "prospects" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("prospects")}
    ine = "IF NOT EXISTS " if ENGINE.dialect.name == "postgresql" else ""
    with ENGINE.begin() as conn:
        if "draft_status" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}draft_status VARCHAR(12)"
            ))
        if "draft_note" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}draft_note VARCHAR(400)"
            ))
        if "draft_message" not in cols:
            conn.execute(text(
                f"ALTER TABLE prospects ADD COLUMN {ine}draft_message TEXT"
            ))


def _migrate_job_event_id_nullable() -> None:
    """Drop the NOT NULL constraint on jobs.event_id so user-scoped jobs (the
    LinkedIn conversation import has no event) can use the Job poll machinery.
    Same posture as _migrate_user_unipile_account_id_nullable : SQLite already
    allows NULL via the updated Mapped[] annotation at create_all time, so the
    explicit ALTER is Postgres-only."""
    from sqlalchemy import inspect, text
    insp = inspect(ENGINE)
    if "jobs" not in insp.get_table_names():
        return
    if ENGINE.dialect.name != "postgresql":
        return
    cols = insp.get_columns("jobs")
    target = next((c for c in cols if c["name"] == "event_id"), None)
    if target is None or target.get("nullable") is True:
        return
    with ENGINE.begin() as conn:
        conn.execute(text(
            "ALTER TABLE jobs ALTER COLUMN event_id DROP NOT NULL"
        ))


# The User/Contact child tables whose rows must DIE with the parent, so the
# delete paths (admin.merge_users, demo cleanup, admin.cleanup-email-contacts)
# cascade in the DB instead of throwing a ForeignKeyViolation. Each entry is
# (child_table, fk_column, parent_table). Only orphan-delete children live here
# -- NOT rows a merge MOVES to a survivor (events/contacts/interactions/sessions
# are re-pointed by merge_users, never cascade-deleted).
_CASCADE_FKS: list[tuple[str, str, str]] = [
    ("contact_identities", "contact_id", "contacts"),
    ("contact_identities", "user_id", "users"),
    ("contact_facts", "contact_id", "contacts"),
    ("contact_facts", "user_id", "users"),
    ("outgoing_messages", "contact_id", "contacts"),
    ("outgoing_messages", "user_id", "users"),
    ("jobs", "user_id", "users"),
    ("connected_accounts", "user_id", "users"),
    ("email_accounts", "user_id", "users"),
]


def _migrate_email_pending_outreach() -> None:
    """Create the email_pending_outreach table on an EXISTING DB.

    create_all makes missing tables on a fresh DB, but the schema-rev sentinel
    skips create_all once the stored rev is current, so a brand-new table on a
    prod DB that is already at-rev would never appear. Adding this migration
    bumps len(migrations) (so the sentinel re-runs create_all) AND explicitly
    creates just this table -- checkfirst makes it idempotent and a no-op once
    it exists. Two replicas can race the CREATE; swallow the loser's error."""
    from . import models
    try:
        models.EmailPendingOutreach.__table__.create(ENGINE, checkfirst=True)
    except Exception as exc:  # noqa: BLE001 : replica race / already exists
        print(f"  [migrate] email_pending_outreach create: "
              f"{type(exc).__name__}: {exc}", flush=True)


def _migrate_fk_cascade() -> None:
    """Make the User/Contact child FKs ON DELETE CASCADE on Postgres.

    SQLite already picks up the ondelete="CASCADE" on the model columns at
    create_all time (and the app enables PRAGMA foreign_keys), so this explicit
    ALTER is Postgres-only : create_all never rewrites an EXISTING table's
    constraints, so a prod DB created before the cascade was added still carries
    NO CASCADE and every delete path 500s on a ForeignKeyViolation.

    For each (child, column, parent) we look the constraint up by its actual
    columns in pg_constraint (names differ : SQLAlchemy-created tables use
    <table>_<col>_fkey, the raw-SQL contact_identities table is auto-named), then
    DROP + re-ADD it with ON DELETE CASCADE. Idempotent : confdeltype 'c' means
    it is already cascading, so we skip. No-op when a table doesn't exist yet."""
    from sqlalchemy import inspect, text
    if ENGINE.dialect.name != "postgresql":
        return
    insp = inspect(ENGINE)
    have = set(insp.get_table_names())
    with ENGINE.begin() as conn:
        for child, col, parent in _CASCADE_FKS:
            if child not in have or parent not in have:
                continue
            # Find the single-column FK constraint on child.(col) -> parent.
            # confdeltype: 'c' = CASCADE, 'a' = NO ACTION (default), etc.
            row = conn.execute(text(
                "SELECT c.conname, c.confdeltype "
                "FROM pg_constraint c "
                "JOIN pg_class ch ON ch.oid = c.conrelid "
                "JOIN pg_class par ON par.oid = c.confrelid "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                "                    AND a.attnum = c.conkey[1] "
                "WHERE c.contype = 'f' "
                "  AND ch.relname = :child "
                "  AND par.relname = :parent "
                "  AND a.attname = :col "
                "  AND array_length(c.conkey, 1) = 1"
            ), {"child": child, "parent": parent, "col": col}).first()
            if row is None:
                # No matching FK (e.g. constraint dropped by an operator, or a
                # legacy table without it) : nothing to re-point.
                continue
            conname, deltype = row[0], row[1]
            if deltype == "c":
                continue  # already ON DELETE CASCADE
            conn.execute(text(
                f'ALTER TABLE {child} DROP CONSTRAINT "{conname}"'))
            conn.execute(text(
                f'ALTER TABLE {child} ADD CONSTRAINT "{conname}" '
                f'FOREIGN KEY ({col}) REFERENCES {parent}(id) ON DELETE CASCADE'))
