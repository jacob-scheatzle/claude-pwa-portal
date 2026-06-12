"""Operational visibility for the admin health dashboard.

Two append-only tables (``LoginAttempt``, ``EmailSendLog``) capture rolling
history of two flows admins typically debug:

- ``LoginAttempt`` — every /login POST, success or failure, with the
  client IP and submitted email. Helps spot brute-force probes and
  user-side typos without leaving uvicorn-log territory.
- ``EmailSendLog`` — every successful /api/v1/email/send call, with the
  first recipient, subject, and originating app slug. Useful for "did
  that quote actually go out?" debugging where SMTP logs are too low-level.

Both tables are opportunistically pruned to ``MAX_ROWS`` on startup so they
can't grow without bound. Failures (5xx) aren't written to EmailSendLog —
they're already surfaced via the SMTP test status + uvicorn logs and would
add noise to the success view.

This module also exposes ``smtp_last_test_*`` settings written by the
admin SMTP-test handler, plus filesystem-size helpers used directly by
the /admin/health page renderer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import delete
from sqlmodel import Session, select

from portal.models import EmailSendLog, LoginAttempt

# Keep this much history per table. ~200 entries comfortably covers a
# week or two of normal traffic on a single-tenant SMB portal; the
# dashboard shows the most recent 20.
MAX_ROWS = 200


def record_login_attempt(
    db: Session,
    *,
    ip: str,
    email: str,
    success: bool,
    reason: str = "",
) -> None:
    """Append one row to ``LoginAttempt``. Best-effort — never raises."""
    try:
        db.add(
            LoginAttempt(
                ip=(ip or "")[:64],
                email=(email or "").strip().lower()[:200],
                success=bool(success),
                reason=reason[:40],
            )
        )
        db.commit()
    except Exception:
        # Logging is observability, not correctness — a failure here must
        # never break the login flow itself. Roll back so the session is
        # left clean for the next operation.
        try:
            db.rollback()
        except Exception:
            pass


def record_email_send(
    db: Session,
    *,
    user_id: Optional[int],
    app_slug: str,
    recipient: str,
    recipient_count: int,
    subject: str,
    status: str = "sent",
) -> None:
    """Append one row to ``EmailSendLog``. Best-effort — never raises."""
    try:
        db.add(
            EmailSendLog(
                user_id=user_id,
                app_slug=(app_slug or "")[:60],
                recipient=(recipient or "")[:200],
                recipient_count=max(1, int(recipient_count or 1)),
                subject=(subject or "")[:120],
                status=(status or "sent")[:20],
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def prune_logs(db: Session) -> None:
    """Trim each log table to the most recent ``MAX_ROWS`` rows.

    Called opportunistically from ``init_db`` on every boot. Cheap on a
    single-tenant SQLite DB; the cap keeps the table bounded under any
    real-world traffic without a separate cron.
    """
    for model in (LoginAttempt, EmailSendLog):
        # Find the id of the row at the cutoff (Nth most recent), then
        # delete everything older. Done as two SELECT-then-DELETE round
        # trips rather than a single subquery to keep the SQL portable
        # across SQLite quirks with LIMIT-in-subquery.
        ids = db.exec(
            select(model.id).order_by(model.id.desc()).offset(MAX_ROWS).limit(1)
        ).first()
        if ids is None:
            continue
        cutoff_id = ids
        try:
            db.exec(delete(model).where(model.id <= cutoff_id))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


def recent_login_attempts(db: Session, limit: int = 20) -> list[LoginAttempt]:
    return list(db.exec(
        select(LoginAttempt).order_by(LoginAttempt.at.desc()).limit(limit)
    ).all())


def recent_email_sends(db: Session, limit: int = 20) -> list[EmailSendLog]:
    return list(db.exec(
        select(EmailSendLog).order_by(EmailSendLog.at.desc()).limit(limit)
    ).all())


# ----- Storage + DB stats -----
#
# These power the admin health dashboard. They go through the configured
# storage backend (get_storage()) and inspect the DB by its URL, so they
# report real numbers on BOTH deployments: local SQLite + filesystem (the
# Docker/Caddy product) and Postgres + S3 (AWS). The previous filesystem-only
# implementation silently read 0 everywhere on the AWS pairing.


def db_size_bytes(db_path: Path) -> int:
    """Total bytes on disk for the SQLite DB (including WAL / SHM siblings).

    Returns 0 if the main file is missing; siblings missing is fine
    (SQLite recreates them on next open). Callers display this verbatim;
    the WAL can grow large under write load and a single ``.db`` reading
    would understate the operator-visible cost.

    SQLite only — for Postgres use ``db_size_label`` instead, which can't
    return a byte count from a file stat.
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix) if suffix else db_path
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def db_size_label(db: Session, db_path: Path) -> str:
    """Human-readable DB size string, correct for both backends.

    SQLite: sum the .db (+ WAL/SHM) file sizes via ``db_size_bytes`` and format.
    Postgres: ask the server with ``pg_database_size(current_database())`` —
    there's no file to stat from the app container. Returns "unknown" if the
    query fails (e.g. permissions) rather than a misleading 0 B.
    """
    from portal.config import settings as _settings

    if _settings.database_url.startswith("sqlite"):
        return fmt_bytes(db_size_bytes(db_path))
    # Postgres (or any non-sqlite backend reachable via the same session).
    # Use the SQLAlchemy ``execute`` API for raw text (SQLModel's ``exec`` is
    # typed for select() statements); ``.scalar()`` pulls the single value.
    try:
        from sqlalchemy import text

        n = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
        return fmt_bytes(int(n)) if n is not None else "unknown"
    except Exception:
        return "unknown"


def storage_usage_by_app(storage_root: Path | None = None) -> dict[str, int]:
    """Return total bytes of per-user storage per app slug.

    Reads through the configured storage backend (local filesystem or S3) so the
    numbers are correct on both deployments. Keys returned by the backend look
    like ``storage/<slug>/<user_id>/<rest>``; we bucket bytes by ``<slug>``.

    Returns an empty dict when nothing has been stored yet. The ``storage_root``
    argument is accepted for backward-compatibility but ignored — the backend
    owns the layout now.
    """
    from portal.storage_backend import get_storage

    out: dict[str, int] = {}
    for obj in get_storage().list("storage"):
        # obj.key == "storage/<slug>/<user_id>/<...>"; the slug is segment [1].
        parts = obj.key.split("/")
        if len(parts) < 2 or parts[0] != "storage":
            continue
        slug = parts[1]
        out[slug] = out.get(slug, 0) + obj.size
    return out


def fmt_bytes(n: int) -> str:
    """Human-friendly size string. Always 2 significant digits."""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n_kib = n / 1024
        if n_kib < 1024:
            return f"{n_kib:.1f} {unit}"
        n = n_kib  # type: ignore[assignment]
    return f"{n_kib:.1f} PB"


def smtp_last_test(db: Session) -> dict[str, Optional[str]]:
    """Return (timestamp, status, error) tuple for the most recent SMTP test."""
    from portal.settings_store import get_setting

    return {
        "at": get_setting(db, "smtp_last_test_at"),
        "status": get_setting(db, "smtp_last_test_status"),
        "error": get_setting(db, "smtp_last_test_error"),
    }


def record_smtp_test(db: Session, success: bool, error: Optional[str] = None) -> None:
    """Persist the result of an admin-triggered SMTP test for the dashboard."""
    from portal.settings_store import set_setting

    set_setting(
        db,
        "smtp_last_test_at",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    set_setting(db, "smtp_last_test_status", "ok" if success else "failed")
    set_setting(db, "smtp_last_test_error", (error or "")[:200] if not success else None)
    db.commit()
