from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from portal.models import User
from portal.security import csrf_token

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def render(
    request: Request,
    name: str,
    *,
    user: Optional[User] = None,
    status_code: int = 200,
    **context: Any,
):
    flashes = request.session.pop("flashes", [])
    # Branding is read fresh on every render so a settings save takes effect
    # without a process restart. One cheap SELECT per render — the alternative
    # (module-scope cache + invalidation hook) would buy back microseconds at
    # the cost of an extra moving part. Open our own short-lived Session so
    # render() doesn't need a DbDep on every call site.
    from portal.branding import get_branding
    from portal.db import engine

    try:
        with Session(engine) as db:
            branding = get_branding(db)
    except Exception:
        # During first-run setup or a transient DB blip, fall back to defaults
        # rather than crash the response we're trying to render.
        from portal.branding import DEFAULT_ACCENT_COLOR, DEFAULT_BUSINESS_NAME

        branding = {
            "business_name": DEFAULT_BUSINESS_NAME,
            "accent_color": DEFAULT_ACCENT_COLOR,
            "logo_url": None,
        }

    # The one-click backup only works on the local SQLite + filesystem
    # deployment (matches the guard in admin.backup_download); on S3/Postgres
    # the nav link is hidden rather than 400-ing on click.
    from portal.config import settings

    backup_available = settings.storage_backend == "local" and (
        settings.database_url.startswith("sqlite")
    )

    return templates.TemplateResponse(
        request,
        name,
        {
            **context,
            "user": user,
            "flashes": flashes,
            "csrf_token": csrf_token(request),
            "branding": branding,
            # ``current_path`` drives the active-nav highlight in base.html.
            "current_path": request.url.path,
            "backup_available": backup_available,
        },
        status_code=status_code,
    )


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault("flashes", []).append(
        {"message": message, "level": level}
    )
