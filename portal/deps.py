import hashlib
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from portal.db import get_db
from portal.models import ApiToken, User


def current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Optional[User]:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return db.get(User, user_id)


def current_user_or_token(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[Optional[str], Header()] = None,
) -> Optional[User]:
    """Accept either a session cookie or `Authorization: Bearer <token>`."""
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
        if raw:
            token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            api_token = db.exec(
                select(ApiToken).where(ApiToken.token_hash == token_hash)
            ).first()
            if api_token is not None:
                api_token.last_used_at = datetime.now(timezone.utc)
                db.add(api_token)
                db.commit()
                return db.get(User, api_token.created_by)
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return db.get(User, user_id)


def require_user(user: Annotated[Optional[User], Depends(current_user)]) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user
