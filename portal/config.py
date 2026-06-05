from pathlib import Path
from typing import Optional

from pydantic import field_validator
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

    # Where blob state lives — app bundles, per-user storage, branding assets,
    # and rendered share PDFs. "local" (the default) keeps everything on disk
    # under ``data_dir``: the existing Docker/Caddy product, unchanged. "s3"
    # stores blobs in an S3 bucket for the AWS (Fargate) deployment, where the
    # container filesystem is ephemeral. The database is selected separately
    # via ``database_url`` (SQLite locally, PostgreSQL/RDS on AWS). See
    # ``portal/storage_backend.py`` and ``aws/README.md``.
    storage_backend: str = "local"
    # S3 backend settings, used only when ``storage_backend == "s3"``.
    #   s3_bucket       — required in s3 mode; the bucket holding all blobs.
    #   s3_prefix       — optional key prefix so several portals can share a
    #                     bucket (e.g. "portal/"). No leading slash.
    #   s3_region       — optional; if unset, boto3's standard region chain
    #                     (AWS_REGION / instance metadata) applies.
    #   s3_endpoint_url — optional; point at MinIO / LocalStack to exercise the
    #                     S3 path locally without a real AWS account.
    s3_bucket: Optional[str] = None
    s3_prefix: str = ""
    s3_region: Optional[str] = None
    s3_endpoint_url: Optional[str] = None

    cookies_secure: bool = False
    # When True, the bundled Caddy serves plain HTTP only (no auto-HTTPS, no
    # Let's Encrypt, no on-demand TLS). Two intended use cases:
    #   1. A load balancer / reverse proxy in front of this stack terminates
    #      TLS and forwards plain HTTP. The operator should keep
    #      ``COOKIES_SECURE=true`` so session cookies stay Secure-flagged
    #      (client → LB is still HTTPS; uvicorn honors X-Forwarded-Proto).
    #   2. Local testing / strictly-internal deployments where TLS isn't
    #      desired. Set ``COOKIES_SECURE=false`` so the browser actually
    #      sends cookies over HTTP.
    # This flag is read by the Caddy container's entrypoint; the portal
    # process itself doesn't branch on it (all scheme decisions cascade
    # through ``cookies_secure``).
    http_only: bool = False
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

    # Controls the app-management MCP server at ``/mcp`` (admin-token authed).
    #   None (default) → AUTO: enabled when the optional ``mcp`` package is
    #       importable. The Docker image bundles it, so the container comes up
    #       with /mcp live; a bare ``pip install`` (no extra) stays off silently.
    #   True  → force on; logs a warning if ``mcp`` isn't installed.
    #   False → force off.
    # Install the dep with ``pip install 'pwa-portal[mcp]'`` (or the Docker
    # ``INSTALL_MCP`` build arg, which defaults on). See docs/mcp.md.
    mcp_enabled: Optional[bool] = None

    # SMTP (read here in step 5; admin UI will override in step 6)
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_use_tls: bool = True

    @field_validator("mcp_enabled", mode="before")
    @classmethod
    def _mcp_enabled_blank_is_auto(cls, v):
        # MCP_ENABLED="" (the .env.example default) means "auto" → None.
        # Pydantic can't coerce an empty string to a bool, so normalize first.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


settings = Settings()

if settings.secret_key == "change-me-before-running-in-production" and settings.site_url != "localhost":
    raise RuntimeError(
        "SECRET_KEY is the placeholder value; refusing to start with "
        f"site_url={settings.site_url!r}. Set SECRET_KEY in .env."
    )

if settings.storage_backend not in ("local", "s3"):
    raise RuntimeError(
        f"STORAGE_BACKEND={settings.storage_backend!r} is invalid; "
        "expected 'local' or 's3'."
    )

if settings.storage_backend == "s3" and not settings.s3_bucket:
    raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set.")


def ensure_data_dir() -> Path:
    p = Path(settings.data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p
