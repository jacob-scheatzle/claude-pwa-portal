import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the repo root importable so `from portal.config import settings` works
# whether alembic is invoked from the repo root or programmatically from
# portal.db.init_db().
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from portal.config import settings  # noqa: E402
from portal import models  # noqa: E402, F401  (register tables on SQLModel.metadata)
from sqlmodel import SQLModel  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Inject the DB URL from app settings so alembic.ini doesn't have to know it
# and so dev/prod/test all use the same source of truth.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# ``disable_existing_loggers=False`` is critical: ``fileConfig`` defaults to
# True, which would mark every logger not declared in alembic.ini as
# disabled — including uvicorn's "uvicorn", "uvicorn.error", and
# "uvicorn.access". Because ``init_db()`` runs Alembic on every portal
# boot, leaving the default in place silently swallows "Application
# startup complete" and every per-request access log line, plus any
# portal-side warning that uses one of the uvicorn loggers as its sink.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata for 'autogenerate' support.
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # Batch mode only matters for SQLite (it can't ALTER columns in place,
        # so Alembic rebuilds the table). On PostgreSQL it forces needless
        # table rebuilds, so scope it to SQLite.
        render_as_batch=(url or "").startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
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
            # SQLite-only — see run_migrations_offline for the rationale.
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
