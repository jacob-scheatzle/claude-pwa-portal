from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    site_url: str = "localhost"
    secret_key: str = "change-me-before-running-in-production"
    database_url: str = "sqlite:///./data/portal.db"
    data_dir: str = "./data"
    cookies_secure: bool = False
    session_max_age: int = 60 * 60 * 24 * 14  # 14 days

    # When True (the current default, preserving today's behavior), child apps
    # are served same-origin with the portal at ``/apps/<slug>/``. When False,
    # child apps are served from per-app subdomains at ``<slug>.apps.<SITE_URL>``
    # with their own isolated AppSession cookie — see
    # docs/per-app-origin-design.md. Requires a wildcard DNS A record at
    # ``*.apps.<SITE_URL>`` pointing at this VPS when False.
    # Default = False: each child app runs on its own subdomain
    # (<slug>.apps.<SITE_URL>) for browser-origin isolation. Set to True only
    # if you can't or don't want to configure wildcard DNS — the portal falls
    # back to serving apps at <SITE_URL>/apps/<slug>/ (same origin as the
    # portal shell, less safe; admins see a warning banner).
    child_apps_same_origin: Optional[bool] = False

    # SMTP (read here in step 5; admin UI will override in step 6)
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_use_tls: bool = True


settings = Settings()

if settings.secret_key == "change-me-before-running-in-production" and settings.site_url != "localhost":
    raise RuntimeError(
        "SECRET_KEY is the placeholder value; refusing to start with "
        f"site_url={settings.site_url!r}. Set SECRET_KEY in .env."
    )


def ensure_data_dir() -> Path:
    p = Path(settings.data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p
