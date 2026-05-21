"""Read/write helpers for the Setting key/value table, plus SMTP resolution."""
from __future__ import annotations

from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer
from sqlmodel import Session

from portal.config import settings
from portal.models import Setting

# Marker prefix for encrypted-at-rest secrets in the Setting table.
# Values written via set_secret() use this prefix; values without it are
# treated as legacy plaintext for backward compatibility.
_SECRET_PREFIX = "enc:v1:"


def _serializer() -> URLSafeSerializer:
    # Bound to settings.secret_key so rotating the key invalidates old ciphertexts;
    # legacy plaintext keeps working regardless of key rotation.
    return URLSafeSerializer(settings.secret_key, salt="settings_store.secret.v1")


def get_setting(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    s = db.get(Setting, key)
    return s.value if s is not None else default


def set_setting(db: Session, key: str, value: Optional[str]) -> None:
    """Upsert a setting. Empty/None value clears the row so the env fallback wins."""
    s = db.get(Setting, key)
    if value is None or value == "":
        if s is not None:
            db.delete(s)
        return
    if s is None:
        db.add(Setting(key=key, value=value))
    else:
        s.value = value


def get_secret(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a secret value.

    If the stored value carries the ``enc:v1:`` prefix it is decrypted with the
    settings.secret_key-bound serializer. If it lacks the prefix it is treated
    as legacy plaintext and returned unchanged — this lets pre-existing SMTP
    passwords keep working after upgrade. Rows are rewritten encrypted the
    next time set_secret() runs for that key.
    """
    raw = get_setting(db, key, None)
    if raw is None:
        return default
    if not raw.startswith(_SECRET_PREFIX):
        # Legacy plaintext passthrough — encrypted on next set_secret().
        return raw
    payload = raw[len(_SECRET_PREFIX):]
    try:
        return _serializer().loads(payload)
    except BadSignature:
        # Tampered or wrong key — refuse to return garbage; behave as if unset.
        return default


def set_secret(db: Session, key: str, value: Optional[str]) -> None:
    """Upsert an encrypted secret. Empty/None clears the row."""
    if value is None or value == "":
        set_setting(db, key, None)
        return
    token = _serializer().dumps(value)
    set_setting(db, key, f"{_SECRET_PREFIX}{token}")


def smtp_config(db: Session) -> dict:
    """SMTP config: DB-first, then .env. Always returns the same shape.

    smtp_password is read via get_secret() so DB-stored values are decrypted
    transparently; env-fallback values are passed through as plaintext.
    """
    def get(key: str, env_default):
        v = get_setting(db, key)
        return v if v else env_default

    host = get("smtp_host", settings.smtp_host)
    try:
        port = int(get("smtp_port", str(settings.smtp_port)) or "0")
    except (ValueError, TypeError):
        port = 587
    use_tls_raw = get("smtp_use_tls", "true" if settings.smtp_use_tls else "false") or "true"

    password = get_secret(db, "smtp_password")
    if not password:
        password = settings.smtp_password

    return {
        "host": host,
        "port": port,
        "username": get("smtp_username", settings.smtp_username),
        "password": password,
        "from_addr": get("smtp_from", settings.smtp_from),
        "use_tls": use_tls_raw.lower() == "true",
    }
