"""Alembic environment for surplus.

Uses the app's own SQLAlchemy metadata (importing backend.models registers every
table onto backend.db.Base.metadata) and the app's DATABASE_URL, so migrations
run against the same schema the app defines. Falls back to alembic.ini's url in
dev when DATABASE_URL is unset.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Registering the models populates Base.metadata with every table.
from backend.db import Base
from backend import models  # noqa: F401
try:
    from backend import models_monitoring  # noqa: F401
except Exception:  # noqa: BLE001 -- optional extra tables
    pass

config = context.config

# Prefer the app's DATABASE_URL (Railway/prod/staging); normalize the legacy
# postgres:// scheme that SQLAlchemy 2 rejects.
_db_url = (os.environ.get("DATABASE_URL") or "").strip()
if _db_url:
    if _db_url.startswith("postgres://"):
        _db_url = "postgresql://" + _db_url[len("postgres://"):]
    config.set_main_option("sqlalchemy.url", _db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic manages ONLY the tables the models define. The deployed DB also carries
# tables that are NOT in main's models -- the designed-but-unmerged accounts
# layer (accounts, companies, teams, walls, *_memberships, company_*) and raw-SQL
# infra tables (schema_meta = the legacy schema-rev sentinel, scheduler_claims,
# alembic_version). Without this filter, autogenerate would try to DROP them.
_ALWAYS_UNMANAGED = {"alembic_version", "schema_meta", "scheduler_claims"}


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        if name in _ALWAYS_UNMANAGED:
            return False
        # A table reflected FROM the DB that the models don't define is not ours
        # to migrate -- leave it alone (never auto-drop).
        if reflected and name not in target_metadata.tables:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
