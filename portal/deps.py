import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from portal.db import get_db
from portal.models import ApiToken, User
from portal.sessions import (
    get_active_app_session,
    get_active_session,
    touch_app_session,
    touch_session,
)

# Cookie name for the per-app-subdomain session. Lives on
# ``<slug>.apps.<SITE_URL>`` and is distinct from the portal's session cookie.
APP_SESSION_COOKIE = "app_session"

# Only persist a token's last_used_at when this much time has elapsed since the
# previous write, to avoid a commit on every authenticated API request.
_TOKEN_LAST_USED_REFRESH = timedelta(seconds=60)


def current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Optional[User]:
    # Authoritative auth state lives in the UserSession table; the cookie is
    # just a pointer. Legacy cookies that only carry "user_id" (no
    # "session_id") are intentionally invalid — those users re-login.
    session_id = request.session.get("session_id")
    session_row = get_active_session(db, session_id)
    if session_row is None:
        return None
    touch_session(db, session_row)
    # Record how this request authenticated so downstream code (e.g. the
    # storage app-slug resolver) can distinguish cookie vs bearer auth.
    request.state.auth_method = "cookie"
    return db.get(User, session_row.user_id)


def current_user_or_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[Optional[str], Header()] = None,
) -> Optional[User]:
    """Accept any of: app-subdomain AppSession cookie, portal UserSession
    cookie, or ``Authorization: Bearer <token>``.

    Sets ``request.state.auth_method`` to one of:

    - ``"app_session"`` — request arrived on ``<slug>.apps.<SITE_URL>`` and
      carried a valid ``app_session`` cookie matching that slug. The portal's
      own ``session_id`` cookie isn't sent on this origin, so the UserSession
      path can't fire here.
    - ``"token"`` — ``Authorization: Bearer …`` resolved to a known ApiToken.
    - ``"cookie"`` — the portal-origin UserSession cookie authenticated.

    An invalid bearer that falls through to cookie auth produces ``"cookie"``
    — callers MUST NOT trust the raw header.

    Auth-method precedence:

    1. On an app subdomain (``request.state.app_slug`` set by
       ``HostDispatchMiddleware``), the AppSession cookie is authoritative.
       If absent or wrong-slug, we return ``None`` without falling back to
       UserSession — the portal cookie isn't even sent here, and silently
       falling back to bearer auth could mask a misconfigured client.
    2. Else, bearer header (token clients).
    3. Else, portal UserSession cookie.
    """
    # 1. App-subdomain context — AppSession cookie is the only valid auth.
    app_slug = getattr(request.state, "app_slug", None)
    if app_slug:
        sid = request.cookies.get(APP_SESSION_COOKIE)
        app_session = get_active_app_session(db, sid)
        if app_session is not None and app_session.slug == app_slug:
            user = db.get(User, app_session.user_id)
            if user is not None:
                touch_app_session(db, app_session)
                request.state.auth_method = "app_session"
                return user
        return None

    # 2. Bearer token (portal-origin programmatic clients).
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
        if raw:
            token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            api_token = db.exec(
                select(ApiToken).where(ApiToken.token_hash == token_hash)
            ).first()
            if api_token is not None:
                now = datetime.now(timezone.utc)
                last = api_token.last_used_at
                # Treat naive last_used_at as UTC for the comparison.
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last is None or (now - last) > _TOKEN_LAST_USED_REFRESH:
                    api_token.last_used_at = now
                    db.add(api_token)
                    db.commit()
                request.state.auth_method = "token"
                return db.get(User, api_token.created_by)
            # Invalid bearer: deliberately fall through to cookie auth for
            # back-compat, but DO NOT set auth_method="token" — the bearer
            # was bogus, so any "I'm a token client" privileges (e.g. naming
            # an arbitrary app via X-Portal-App) must not apply.

    # 3. Portal-origin UserSession cookie.
    session_id = request.session.get("session_id")
    session_row = get_active_session(db, session_id)
    if session_row is None:
        return None
    touch_session(db, session_row)
    request.state.auth_method = "cookie"
    return db.get(User, session_row.user_id)


def require_user(user: Annotated[Optional[User], Depends(current_user)]) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user
