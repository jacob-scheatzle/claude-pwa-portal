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


# ----- Filesystem stats -----

def db_size_bytes(db_path: Path) -> int:
    """Total bytes on disk for the SQLite DB (including WAL / SHM siblings).

    Returns 0 if the main file is missing; siblings missing is fine
    (SQLite recreates them on next open). Callers display this verbatim;
    the WAL can grow large under write load and a single ``.db`` reading
    would understate the operator-visible cost.
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


def storage_usage_by_app(storage_root: Path) -> dict[str, int]:
    """Walk ``data/storage/<slug>/`` and return total bytes per app slug.

    Returns an empty dict when the root doesn't exist (no apps have
    written storage yet). Symlinks aren't followed — storage_put rejects
    them at write time, so any present would be operator-introduced.
    """
    out: dict[str, int] = {}
    if not storage_root.is_dir():
        return out
    for app_dir in storage_root.iterdir():
        if not app_dir.is_dir() or app_dir.is_symlink():
            continue
        total = 0
        for f in app_dir.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                continue
        out[app_dir.name] = total
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
