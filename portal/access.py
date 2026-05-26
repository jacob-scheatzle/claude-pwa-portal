"""Per-user app access helpers.

Admins implicitly have access to every app (no rows stored). Non-admin users
need a ``UserAppAccess`` row for the (user, app) pair to launch / see the
app.

Rows are populated at creation time:

- New user is created → grant rows for every existing app if the
  ``default_user_app_access`` Setting is ``"all"``; create no rows if
  ``"none"``.
- New app is uploaded → grant rows for every existing non-admin user under
  the same rule.

The setting only affects newly-created entities. Admins reshape an existing
user's access from ``/admin/users/<id>/apps``.
"""
from __future__ import annotations

from typing import Iterable

from sqlmodel import Session, select

from portal.models import App, User, UserAppAccess
from portal.settings_store import get_setting, set_setting

DEFAULT_ACCESS_KEY = "default_user_app_access"
_VALID_DEFAULTS = {"all", "none"}


def get_default_access(db: Session) -> str:
    """Return ``"all"`` or ``"none"`` — the policy for new users + apps.

    Defaults to ``"all"`` on a fresh database so the v0.1.x → v0.2 upgrade
    keeps today's behavior: every user sees every app until the admin opts
    in to a stricter policy via the Settings page.
    """
    value = get_setting(db, DEFAULT_ACCESS_KEY)
    if value not in _VALID_DEFAULTS:
        return "all"
    return value


def set_default_access(db: Session, value: str) -> None:
    if value not in _VALID_DEFAULTS:
        raise ValueError(f"default_user_app_access must be one of {_VALID_DEFAULTS}")
    set_setting(db, DEFAULT_ACCESS_KEY, value)


def user_can_access_app(db: Session, user: User, app: App) -> bool:
    """Return True iff ``user`` is allowed to launch ``app``.

    Admins always have access. Non-admins need an explicit
    ``UserAppAccess`` row.
    """
    if user.role == "admin":
        return True
    if user.id is None or app.id is None:
        return False
    row = db.exec(
        select(UserAppAccess).where(
            UserAppAccess.user_id == user.id,
            UserAppAccess.app_id == app.id,
        )
    ).first()
    return row is not None


def accessible_app_ids_for(db: Session, user: User) -> set[int]:
    """Set of app ids the user is allowed to launch.

    Admins return the universe (every enabled app). Non-admins return only
    apps with a matching ``UserAppAccess`` row.
    """
    if user.role == "admin":
        return {
            a.id for a in db.exec(select(App).where(App.enabled == True)).all()  # noqa: E712
            if a.id is not None
        }
    if user.id is None:
        return set()
    rows = db.exec(
        select(UserAppAccess.app_id).where(UserAppAccess.user_id == user.id)
    ).all()
    # `db.exec(select(col))` yields scalar ints directly in SQLModel >=0.0.16.
    return {row for row in rows if row is not None}


def grant_default_access_for_new_user(db: Session, user: User) -> None:
    """Populate UserAppAccess rows for ``user`` against every existing app.

    No-op when the configured default is ``"none"`` or when ``user`` is an
    admin. Caller is responsible for ``db.commit()`` — this only stages the
    rows so it composes with the surrounding user-create transaction.
    """
    if user.role == "admin":
        return
    if get_default_access(db) != "all":
        return
    if user.id is None:
        # User hasn't been flushed yet; nothing to FK against. Caller must
        # call commit-then-refresh before invoking this.
        raise ValueError("grant_default_access_for_new_user: user.id is None — flush before calling")
    apps = db.exec(select(App)).all()
    for app in apps:
        if app.id is None:
            continue
        db.add(UserAppAccess(user_id=user.id, app_id=app.id))


def grant_default_access_for_new_app(db: Session, app: App) -> None:
    """Populate UserAppAccess rows for every non-admin user against ``app``.

    No-op when the configured default is ``"none"``. Caller commits.
    """
    if get_default_access(db) != "all":
        return
    if app.id is None:
        raise ValueError("grant_default_access_for_new_app: app.id is None — flush before calling")
    users = db.exec(select(User).where(User.role == "user")).all()
    for user in users:
        if user.id is None:
            continue
        db.add(UserAppAccess(user_id=user.id, app_id=app.id))


def replace_user_app_access(
    db: Session, user: User, allowed_app_ids: Iterable[int]
) -> None:
    """Set ``user``'s access to exactly the apps in ``allowed_app_ids``.

    No-op for admins (their access is implicit). Caller commits.
    """
    if user.role == "admin" or user.id is None:
        return
    desired = {aid for aid in allowed_app_ids if aid is not None}
    existing_rows = db.exec(
        select(UserAppAccess).where(UserAppAccess.user_id == user.id)
    ).all()
    existing = {row.app_id: row for row in existing_rows}
    # Delete rows the admin removed.
    for app_id, row in existing.items():
        if app_id not in desired:
            db.delete(row)
    # Add rows the admin added.
    for app_id in desired - set(existing.keys()):
        db.add(UserAppAccess(user_id=user.id, app_id=app_id))


def delete_access_for_user(db: Session, user_id: int) -> None:
    """Remove every UserAppAccess row referencing ``user_id``.

    Called by users_delete before the User row itself is deleted, since SQLite
    foreign keys are advisory unless PRAGMA foreign_keys=ON is set and we
    don't rely on that.
    """
    rows = db.exec(
        select(UserAppAccess).where(UserAppAccess.user_id == user_id)
    ).all()
    for row in rows:
        db.delete(row)


def delete_access_for_app(db: Session, app_id: int) -> None:
    """Remove every UserAppAccess row referencing ``app_id``. See above."""
    rows = db.exec(
        select(UserAppAccess).where(UserAppAccess.app_id == app_id)
    ).all()
    for row in rows:
        db.delete(row)
