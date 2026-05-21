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

    # SMTP (read here in step 5; admin UI will override in step 6)
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_use_tls: bool = True


settings = Settings()


def ensure_data_dir() -> Path:
    p = Path(settings.data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p
