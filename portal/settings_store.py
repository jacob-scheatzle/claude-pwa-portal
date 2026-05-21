"""Read/write helpers for the Setting key/value table, plus SMTP resolution."""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from portal.config import settings
from portal.models import Setting


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


def smtp_config(db: Session) -> dict:
    """SMTP config: DB-first, then .env. Always returns the same shape."""
    def get(key: str, env_default):
        v = get_setting(db, key)
        return v if v else env_default

    host = get("smtp_host", settings.smtp_host)
    try:
        port = int(get("smtp_port", str(settings.smtp_port)) or "0")
    except (ValueError, TypeError):
        port = 587
    use_tls_raw = get("smtp_use_tls", "true" if settings.smtp_use_tls else "false") or "true"

    return {
        "host": host,
        "port": port,
        "username": get("smtp_username", settings.smtp_username),
        "password": get("smtp_password", settings.smtp_password),
        "from_addr": get("smtp_from", settings.smtp_from),
        "use_tls": use_tls_raw.lower() == "true",
    }
