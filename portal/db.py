import os
from pathlib import Path

from sqlmodel import Session, create_engine

from portal.config import ensure_data_dir, settings

ensure_data_dir()


def _engine_kwargs() -> dict:
    """SQLAlchemy engine kwargs tuned per database.

    SQLite (local/default) needs ``check_same_thread=False`` because FastAPI
    runs sync handlers in a threadpool. PostgreSQL (RDS, AWS deploy) gets a
    small connection pool with ``pool_pre_ping`` so a connection RDS dropped
    while idle is detected and replaced instead of erroring on the next
    request; ``pool_recycle`` proactively retires long-lived connections.
    Sized for a single Fargate task (desired_count=1).
    """
    if settings.database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": 1800,
    }


engine = create_engine(settings.database_url, echo=False, **_engine_kwargs())


def init_db() -> None:
    """Run Alembic migrations to bring the schema to head.

    For pre-Alembic databases (existing dev/prod DBs created via the old
    ``SQLModel.metadata.create_all`` path) we detect the missing
    ``alembic_version`` table and ``stamp`` instead of ``upgrade`` — the schema
    is already at head, we just need to record the revision.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    # Import models so SQLModel.metadata is fully populated (alembic's env.py
    # also does this, but importing here keeps the contract explicit and avoids
    # depending on import ordering).
    from portal import models  # noqa: F401

    repo_root = Path(__file__).resolve().parent.parent
    alembic_dir = Path(os.environ.get("ALEMBIC_DIR", str(repo_root / "alembic")))
    alembic_ini = Path(os.environ.get("ALEMBIC_INI", str(repo_root / "alembic.ini")))

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)

    insp = inspect(engine)
    has_user_table = insp.has_table("user")
    has_alembic_table = insp.has_table("alembic_version")

    if has_user_table and not has_alembic_table:
        # Pre-Alembic database — assume it's at the head schema and just record
        # the version so future migrations can pick up from here.
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")

    # Opportunistic maintenance: clear out launch tokens older than a day,
    # and trim the LoginAttempt/EmailSendLog rolling history to its cap.
    # Wrapped in a broad try so a transient DB hiccup during this cleanup
    # can never block app startup.
    try:
        from portal.audit import prune as prune_audit_log
        from portal.health import prune_logs
        from portal.sessions import purge_expired_launch_tokens

        with Session(engine) as db:
            purge_expired_launch_tokens(db)
            prune_logs(db)
            prune_audit_log(db)
    except Exception:
        pass


def get_db():
    with Session(engine) as session:
        yield session
