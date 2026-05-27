"""Append-only audit log.

Every state-changing admin action — and every login attempt — is recorded
in the ``AuditEvent`` table. The point is forensic: if you've granted a
contractor admin access briefly, this is the table that tells you
*exactly* what they touched between login and logout.

Design rules:

- ``record_event`` is a best-effort fire-and-forget. It stages and commits
  on its own session, swallowing errors so a logging failure can never
  break the underlying request. Call it AFTER the primary action's
  commit so the row reflects what actually persisted.
- Action names are dot-namespaced verbs (``app.upload``, ``user.role.change``,
  ``login.failure`` …). Free-form on purpose so adding a new handler
  doesn't require a schema migration.
- ``target`` is a short human-readable resource identifier
  (``app:my-slug``, ``user:alice@x.com``, ``token:claude-skill``). The
  /admin/audit table renders it verbatim.
- ``details`` is a small JSON dict carrying the change diff or failure
  reason. Keep it compact — admins read these rows visually; long blobs
  make the table unreadable.

Pruning is opportunistic at startup, like the other rolling-history
tables. ``MAX_ROWS`` is large enough that an active small business won't
lose a quarter's worth of history under normal use, and small enough
that the table fits comfortably in SQLite without a separate cron.

In addition to the DB row, security-relevant events (failed logins,
rate-limited login attempts) are mirrored to a plain-text file at
``<data_dir>/security.log`` in a fail2ban-friendly format. The DB row is
the source of truth for human review at /admin/audit; the text log
exists so external tooling (fail2ban, fluent-bit, vector, etc.) can tail
a stable file path on the host without having to query SQLite.
"""
from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request
from sqlalchemy import delete
from sqlmodel import Session, select

from portal.config import settings
from portal.models import AuditEvent, User

# Forensic retention target. 5000 events covers months of normal use for
# a single-business portal; the dashboard surfaces the most recent 200.
# Adjust if you're operating with many more daily admins than the typical
# SMB single-tenant deployment.
MAX_ROWS = 5000

# ----- fail2ban-friendly text log -----
#
# A second sink for security-relevant events. The format is deliberately
# space-separated, single-line, no JSON, no escaping — fail2ban regexes
# stay short and uvicorn's access-log format changing (or being replaced
# by something else upstream) can't break the parse.
#
# Format:
#
#   <ISO8601 timestamp Z> <EVENT_NAME> ip=<addr> [key=value ...]
#
# Examples:
#
#   2026-05-27T14:35:18Z FAILED_LOGIN ip=45.88.138.44 email=admin@example.com reason=bad_credentials
#   2026-05-27T14:36:02Z FAILED_LOGIN ip=45.88.138.44 email=admin@example.com reason=rate_limited
#
# A RotatingFileHandler caps the on-disk footprint at 5 × 1MB ≈ 5MB. That
# fits comfortably alongside the SQLite DB in the bind-mounted ``data/``
# dir; operators who want longer retention can swap in logrotate.
_security_logger: Optional[logging.Logger] = None


def _security_log() -> Optional[logging.Logger]:
    """Lazily build the ``portal.security`` logger.

    Returns ``None`` (and silently no-ops) if the data dir isn't writable.
    Initialization is wrapped in a broad try so a missing path or
    permission error can never block startup; the DB-side audit row
    still lands either way.
    """
    global _security_logger
    if _security_logger is not None:
        return _security_logger
    try:
        log_path = Path(settings.data_dir) / "security.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=5,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg = logging.getLogger("portal.security")
        lg.setLevel(logging.INFO)
        # Replace any existing handlers (in case this is re-entered after
        # a reload) so we don't end up with duplicate writes.
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.addHandler(handler)
        # Don't propagate to root — the line is for the security file
        # only, not the regular uvicorn / stderr stream.
        lg.propagate = False
        _security_logger = lg
    except Exception:
        return None
    return _security_logger


def _sanitize_value(v: object) -> str:
    """Make a value safe to drop into the ``key=value`` line."""
    s = str(v if v is not None else "")
    # Collapse whitespace so the line stays single-token-parseable.
    s = s.replace("\n", " ").replace("\r", " ").strip()
    s = "_".join(s.split())
    return s[:120]


def emit_security_line(event: str, ip: str, **fields: object) -> None:
    """Append one line to ``data/security.log``. Best-effort — never raises.

    ``event`` should be a short UPPERCASE token (FAILED_LOGIN,
    LOGIN_RATE_LIMITED) so fail2ban filters can anchor on it. ``ip``
    becomes the ``ip=<addr>`` field; ``-`` is rendered when empty so the
    field is always present and the regex stays simple. Remaining kwargs
    are appended as ``key=value`` pairs in insertion order.
    """
    lg = _security_log()
    if lg is None:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [ts, (event or "")[:40], f"ip={(ip or '-')[:64]}"]
    for k, v in fields.items():
        val = _sanitize_value(v)
        if val:
            parts.append(f"{k}={val}")
    try:
        lg.info(" ".join(parts))
    except Exception:
        pass


def _client_ip(request: Optional[Request]) -> str:
    """Pull the client IP out of a Request, honoring proxy headers.

    ``request.client.host`` is already populated from X-Forwarded-For when
    uvicorn runs with ``--proxy-headers --forwarded-allow-ips=*`` (set in
    the container CMD). Returns an empty string for cases where the
    handler didn't receive a Request (system-driven events).
    """
    if request is None or request.client is None:
        return ""
    return (request.client.host or "")[:64]


def record_event(
    db: Session,
    *,
    actor: Optional[User],
    action: str,
    request: Optional[Request] = None,
    target: str = "",
    details: Optional[dict] = None,
) -> None:
    """Append one row to ``AuditEvent``. Best-effort — never raises.

    Call AFTER the primary commit so the audit row reflects what actually
    persisted. The function uses its own commit so it stays
    self-contained; on any DB error it rolls back and silently returns.
    """
    try:
        db.add(
            AuditEvent(
                actor_user_id=actor.id if actor is not None else None,
                actor_email=(actor.email if actor is not None else "")[:200],
                action=(action or "")[:60],
                target=(target or "")[:120],
                ip=_client_ip(request),
                details=details or {},
            )
        )
        db.commit()
    except Exception:
        # Logging is observability, not correctness — a failure here must
        # never break the surrounding request. Roll back so the session
        # is left clean for any follow-on work the caller does.
        try:
            db.rollback()
        except Exception:
            pass


def record_anonymous(
    db: Session,
    *,
    action: str,
    request: Optional[Request] = None,
    target: str = "",
    details: Optional[dict] = None,
    actor_email: str = "",
) -> None:
    """Record an event with no logged-in user (failed login, setup wizard).

    Same best-effort semantics as ``record_event``; the ``actor_email``
    argument lets the failed-login path persist the submitted email so
    audit reviewers can see who was being probed.
    """
    try:
        db.add(
            AuditEvent(
                actor_user_id=None,
                actor_email=(actor_email or "").strip().lower()[:200],
                action=(action or "")[:60],
                target=(target or "")[:120],
                ip=_client_ip(request),
                details=details or {},
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def recent_events(db: Session, limit: int = 200) -> list[AuditEvent]:
    """Most recent ``limit`` events, newest first."""
    return list(db.exec(
        select(AuditEvent).order_by(AuditEvent.at.desc()).limit(limit)
    ).all())


def prune(db: Session) -> None:
    """Trim the table to the most recent ``MAX_ROWS`` rows.

    Called from ``init_db`` on every boot. Same SELECT-then-DELETE shape
    as ``portal.health.prune_logs`` to keep the SQL portable.
    """
    cutoff = db.exec(
        select(AuditEvent.id).order_by(AuditEvent.id.desc()).offset(MAX_ROWS).limit(1)
    ).first()
    if cutoff is None:
        return
    try:
        db.exec(delete(AuditEvent).where(AuditEvent.id <= cutoff))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
