import secrets

import bcrypt
from fastapi import HTTPException, Request

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_BYTES = 72  # bcrypt's hard limit


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LEN:
        errors.append(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        errors.append(f"Password must be {MAX_PASSWORD_BYTES} bytes or fewer.")
    return errors


def csrf_token(request: Request) -> str:
    tok = request.session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["_csrf"] = tok
    return tok


def check_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("_csrf")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(403, "CSRF check failed")
