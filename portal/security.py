import secrets
from typing import Optional

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


# Pre-computed bcrypt hash of a value no one will ever guess. Used to burn the
# same ~100ms in the unknown-email login path that a real email + wrong-password
# attempt costs, so an attacker can't enumerate registered users via timing.
# Generated once at import time; the cost is amortized across the process.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"this-hash-is-never-matched-against-a-real-password",
    bcrypt.gensalt(),
).decode("utf-8")


def verify_password_dummy(password: str) -> None:
    """Run bcrypt against a dummy hash and discard the result.

    Call this when the looked-up user doesn't exist so the response time
    matches a real (user-found, wrong-password) check. Otherwise an attacker
    can probe which emails are registered just by timing the login form.
    """
    try:
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_PASSWORD_HASH.encode("utf-8"))
    except (ValueError, TypeError):
        pass


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


def check_csrf_header(request: Request, x_csrf: Optional[str]) -> None:
    """CSRF check for JSON/fetch endpoints that read the token from a header
    (X-CSRF-Token) rather than a form field. Logic mirrors ``check_csrf``."""
    expected = request.session.get("_csrf")
    if not expected or not x_csrf or not secrets.compare_digest(expected, x_csrf):
        raise HTTPException(403, "CSRF check failed")
