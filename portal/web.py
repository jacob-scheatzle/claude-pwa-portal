from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates

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
    return templates.TemplateResponse(
        request,
        name,
        {
            **context,
            "user": user,
            "flashes": flashes,
            "csrf_token": csrf_token(request),
        },
        status_code=status_code,
    )


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault("flashes", []).append(
        {"message": message, "level": level}
    )
