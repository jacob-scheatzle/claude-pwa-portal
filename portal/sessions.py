"""Server-side session records.

The cookie carries only an opaque ``session_id``; the authoritative session
state (active vs revoked, last_seen_at) lives in the ``UserSession`` table so
logout, password change, or admin action can invalidate a session even while
its signed cookie is still within max_age.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from portal.models import User, UserSession

# Bump last_seen_at lazily: writing on every authenticated request would mean
# a DB commit per request, which is wasteful for an audit-style timestamp.
_LAST_SEEN_REFRESH = timedelta(seconds=60)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_session(db: Session, user: User) -> str:
    """Insert a new UserSession row and return its opaque id.

    Caller is responsible for committing the surrounding transaction (we
    flush so the row is visible to subsequent queries in the same session,
    but leave the commit to the caller — typical callers commit other rows
    in the same request).
    """
    sid = secrets.token_urlsafe(32)
    row = UserSession(id=sid, user_id=user.id)
    db.add(row)
    db.commit()
    return sid


def revoke_session(db: Session, session_id: Optional[str]) -> None:
    """Mark a session row revoked. No-op if missing or already revoked."""
    if not session_id:
        return
    row = db.get(UserSession, session_id)
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = _utcnow()
    db.add(row)
    db.commit()


def revoke_all_for_user(db: Session, user_id: int) -> int:
    """Revoke every active session for a user. Returns count revoked."""
    rows = db.exec(
        select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    now = _utcnow()
    count = 0
    for row in rows:
        row.revoked_at = now
        db.add(row)
        count += 1
    if count:
        db.commit()
    return count


def get_active_session(db: Session, session_id: Optional[str]) -> Optional[UserSession]:
    """Return the row iff it exists and is not revoked."""
    if not session_id:
        return None
    row = db.get(UserSession, session_id)
    if row is None or row.revoked_at is not None:
        return None
    return row


def touch_session(db: Session, session: UserSession) -> None:
    """Bump last_seen_at if the previous write is older than _LAST_SEEN_REFRESH.

    Treats a naive last_seen_at as UTC (defensive — SQLite drops tzinfo on
    round-trip) so the comparison is meaningful.
    """
    now = _utcnow()
    last = session.last_seen_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last) > _LAST_SEEN_REFRESH:
        session.last_seen_at = now
        db.add(session)
        db.commit()
