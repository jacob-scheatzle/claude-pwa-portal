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


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_allowed_redirect_uri(uri: str) -> bool:
    """True if ``uri`` is an acceptable OAuth client redirect URI.

    Only ``https://`` or loopback ``http://`` with a real host, no userinfo, and
    no fragment. Anything else — notably ``javascript:`` / ``data:`` URIs, which
    the consent flow would otherwise hand the admin's browser as a link on the
    portal origin — is refused. Hosts are compared exactly, so
    ``http://localhost.evil.example`` is not loopback.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(uri).strip())
        host = parts.hostname
    except ValueError:
        return False
    if not host or parts.fragment or parts.username is not None or parts.password is not None:
        return False
    scheme = parts.scheme.lower()
    if scheme == "https":
        return True
    return scheme == "http" and host.lower() in _LOOPBACK_HOSTS


def redirect_uri_host(uri: str) -> str:
    """The host a redirect URI sends the browser to, for display."""
    from urllib.parse import urlsplit

    try:
        return urlsplit(str(uri).strip()).hostname or ""
    except ValueError:
        return ""


def client_network(ip: str) -> str:
    """The key per-client rate limits should use for ``ip``.

    An IPv4 address as-is; an IPv6 address as its /64 — the allocation one
    subscriber typically gets, so a client can't mint a fresh limit for every
    request by rotating addresses inside it. IPv4-mapped IPv6 folds back to
    the IPv4 address; anything unparseable is returned unchanged.
    """
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)

