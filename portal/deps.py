import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from portal.db import get_db
from portal.models import ApiToken, User

# Only persist a token's last_used_at when this much time has elapsed since the
# previous write, to avoid a commit on every authenticated API request.
_TOKEN_LAST_USED_REFRESH = timedelta(seconds=60)


def current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Optional[User]:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    # Record how this request authenticated so downstream code (e.g. the
    # storage app-slug resolver) can distinguish cookie vs bearer auth.
    request.state.auth_method = "cookie"
    return db.get(User, user_id)


def current_user_or_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[Optional[str], Header()] = None,
) -> Optional[User]:
    """Accept either a session cookie or `Authorization: Bearer <token>`.

    Sets ``request.state.auth_method`` to ``"token"`` when the bearer header
    resolved to a valid ApiToken, or ``"cookie"`` when the session cookie
    authenticated the request. An invalid bearer that falls through to cookie
    auth produces ``"cookie"`` — callers MUST NOT trust the raw header.
    """
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
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    request.state.auth_method = "cookie"
    return db.get(User, user_id)


def require_user(user: Annotated[Optional[User], Depends(current_user)]) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user
