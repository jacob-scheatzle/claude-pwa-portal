"""Read/write helpers for the Setting key/value table, plus SMTP resolution."""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session

from portal.config import settings
from portal.models import Setting

logger = logging.getLogger("portal.settings_store")

# Set once we've already warned about a Fernet decrypt failure so a rotated
# SECRET_KEY doesn't flood the log on every read of an unreadable secret.
_warned_decrypt_failure = False

# Marker prefix for AEAD-encrypted-at-rest secrets in the Setting table.
# v2 = Fernet (AES-128-CBC + HMAC-SHA256), key derived from settings.secret_key.
# v1 = legacy itsdangerous.URLSafeSerializer payloads (signed-but-not-encrypted);
#      read-side migration path only — never written by this module.
_FERNET_PREFIX = "enc:v2:"
_LEGACY_V1_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    """Build a Fernet bound to settings.secret_key.

    Fernet requires a 32-byte urlsafe-base64-encoded key, so we derive one
    deterministically by SHA-256-ing the configured secret_key. Rotating the
    SECRET_KEY therefore invalidates previously-encrypted values — the same
    coupling that already exists for session cookies, so this is acceptable.
    """
    key_bytes = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


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

    Values with the ``enc:v2:`` prefix are Fernet-decrypted. Values with the
    legacy ``enc:v1:`` prefix (itsdangerous-signed-but-not-encrypted) are
    best-effort decoded as a migration aid; the next set_secret() call rewrites
    them under v2. Values with neither prefix are treated as legacy plaintext
    and returned unchanged.
    """
    stored = get_setting(db, key)
    if stored is None:
        return default

    if stored.startswith(_FERNET_PREFIX):
        try:
            token = stored[len(_FERNET_PREFIX):].encode("ascii")
            return _fernet().decrypt(token).decode("utf-8")
        except InvalidToken:
            # Key was rotated or storage was tampered with. Don't expose
            # garbage; treat as missing so callers fall back to env config.
            # Warn once so a rotated SECRET_KEY (the common cause) is visible
            # to the operator without spamming the log on every read.
            global _warned_decrypt_failure
            if not _warned_decrypt_failure:
                _warned_decrypt_failure = True
                logger.warning(
                    "Could not decrypt a stored secret (e.g. %r) — SECRET_KEY "
                    "was likely rotated. Falling back to env config; re-save the "
                    "value in the admin UI to re-encrypt it under the new key.",
                    key,
                )
            return default

    if stored.startswith(_LEGACY_V1_PREFIX):
        # The v1 scheme used itsdangerous.URLSafeSerializer, which signs but
        # does not encrypt. The payload before the `.` is base64-urlsafe JSON.
        # We accept it without verifying the HMAC (we don't know the original
        # salt was 'settings_store.secret.v1' for all installs, and v1 was a
        # security bug anyway). Best-effort decode; on any failure, behave as
        # if unset so callers fall back to env config.
        try:
            import json as _json

            payload = stored[len(_LEGACY_V1_PREFIX):].split(".", 1)[0]
            padded = payload + "=" * (-len(payload) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            # URLSafeSerializer JSON-encodes its argument, so a string becomes
            # a JSON string literal. Decode it if so; otherwise return as-is.
            return _json.loads(raw) if raw.startswith('"') else raw
        except Exception:
            return default

    # Legacy plaintext passthrough — will be re-encrypted on next set_secret().
    return stored


def set_secret(db: Session, key: str, value: Optional[str]) -> None:
    """Upsert an encrypted secret using Fernet (AEAD). Empty/None clears the row."""
    if value is None or value == "":
        set_setting(db, key, None)
        return
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    set_setting(db, key, _FERNET_PREFIX + token)


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
