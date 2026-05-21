import time
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from portal import admin as admin_module
from portal import api as api_module
from portal import apps as apps_module
from portal.config import settings
from portal.db import get_db, init_db
from portal.deps import current_user, require_user
from portal.models import App, Setting, User
from portal.security import (
    check_csrf,
    hash_password,
    validate_password,
    verify_password,
)
from portal.settings_store import get_setting, set_setting
from portal.web import STATIC_DIR, flash, render


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="PWA Portal", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.cookies_secure,
    max_age=settings.session_max_age,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(apps_module.router)
app.include_router(api_module.router)
app.include_router(admin_module.router)


email_adapter = TypeAdapter(EmailStr)

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[Optional[User], Depends(current_user)]
RequireUserDep = Annotated[User, Depends(require_user)]


def admin_exists(db: Session) -> bool:
    # Cached short-circuit: once setup_submit writes setup_complete=true the
    # answer is permanent, so we can skip the table scan on every / and /login.
    cached = get_setting(db, "setup_complete")
    if cached == "true":
        return True
    return db.exec(select(User).where(User.role == "admin")).first() is not None


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


# ----- Login rate limit (per-process, lost on restart) -----
#
# Rolling-window throttle keyed by (client IP, normalized email). Five failed
# POSTs in ten minutes triggers a 429. Cleaned lazily on each check so the
# dict can't grow without bound. Not a substitute for a real distributed
# limiter; deliberately omits slowapi to keep dependencies minimal.
_LOGIN_FAIL_WINDOW_SECONDS = 600
_LOGIN_FAIL_LIMIT = 5
_login_failures: dict[tuple[str, str], list[float]] = {}


def _login_key(request: Request, email: str) -> tuple[str, str]:
    ip = request.client.host if request.client else "unknown"
    return (ip, email.strip().lower())


def _prune_login_failures(now: float) -> None:
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    stale: list[tuple[str, str]] = []
    for key, hits in _login_failures.items():
        fresh = [t for t in hits if t > cutoff]
        if fresh:
            _login_failures[key] = fresh
        else:
            stale.append(key)
    for key in stale:
        _login_failures.pop(key, None)


def _login_blocked(key: tuple[str, str]) -> bool:
    now = time.monotonic()
    _prune_login_failures(now)
    hits = _login_failures.get(key, [])
    return len(hits) >= _LOGIN_FAIL_LIMIT


def _record_login_failure(key: tuple[str, str]) -> None:
    now = time.monotonic()
    _prune_login_failures(now)
    _login_failures.setdefault(key, []).append(now)


def _clear_login_failures(key: tuple[str, str]) -> None:
    _login_failures.pop(key, None)


# ----- Dashboard -----

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: DbDep, user: UserDep):
    if not admin_exists(db):
        return RedirectResponse("/setup", status_code=303)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    visible = db.exec(
        select(App).where(App.enabled == True).order_by(App.name)  # noqa: E712
    ).all()
    return render(request, "dashboard.html", user=user, apps=visible)


# ----- PWA endpoints -----

@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/portal-sdk.js", include_in_schema=False)
def portal_sdk():
    return FileResponse(
        STATIC_DIR / "portal-sdk.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ----- First-run wizard -----

@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: DbDep):
    if admin_exists(db):
        return RedirectResponse("/", status_code=303)
    return render(request, "setup.html", site_url_default=settings.site_url)


@app.post("/setup")
def setup_submit(
    request: Request,
    db: DbDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    site_url: Annotated[str, Form()],
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    if admin_exists(db):
        return RedirectResponse("/", status_code=303)

    errors: list[str] = []
    try:
        validated_email = str(email_adapter.validate_python(email)).lower()
    except ValidationError:
        errors.append("Please enter a valid email address.")
        validated_email = email

    errors.extend(validate_password(password))
    if password != password_confirm:
        errors.append("Passwords do not match.")
    if not site_url.strip():
        errors.append("Site URL is required.")

    if errors:
        return render(
            request, "setup.html",
            errors=errors, email=email, site_url=site_url,
            status_code=400,
        )

    user = User(
        email=validated_email,
        password_hash=hash_password(password),
        role="admin",
    )
    db.add(user)
    existing = db.get(Setting, "site_url")
    if existing:
        existing.value = site_url.strip()
    else:
        db.add(Setting(key="site_url", value=site_url.strip()))
    # Cache flag so admin_exists() can short-circuit the table scan on / and /login.
    set_setting(db, "setup_complete", "true")
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


# ----- Login / logout -----

@app.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    db: DbDep,
    user: UserDep,
    next: str = "/",
):
    if not admin_exists(db):
        return RedirectResponse("/setup", status_code=303)
    next_url = _safe_next(next)
    if user is not None:
        return RedirectResponse(next_url, status_code=303)
    return render(request, "login.html", next=next_url)


@app.post("/login")
def login_submit(
    request: Request,
    db: DbDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    key = _login_key(request, email)
    if _login_blocked(key):
        return render(
            request, "login.html",
            error="Too many failed attempts. Try again in a few minutes.",
            email=email, next=_safe_next(next),
            status_code=429,
        )
    normalized = email.strip().lower()
    user = db.exec(select(User).where(User.email == normalized)).first()
    if user is None or not verify_password(password, user.password_hash):
        _record_login_failure(key)
        return render(
            request, "login.html",
            error="Invalid email or password.", email=email, next=_safe_next(next),
            status_code=401,
        )
    _clear_login_failures(key)
    request.session["user_id"] = user.id
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/logout")
def logout(
    request: Request,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ----- Self-serve profile -----

@app.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, user: RequireUserDep):
    return render(request, "profile.html", user=user)


@app.post("/profile/change-password")
def profile_change_password(
    request: Request,
    db: DbDep,
    user: RequireUserDep,
    old_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    new_password_confirm: Annotated[str, Form()],
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    errors: list[str] = []
    if not verify_password(old_password, user.password_hash):
        errors.append("Current password is incorrect.")
    errors.extend(validate_password(new_password))
    if new_password != new_password_confirm:
        errors.append("New passwords do not match.")
    if errors:
        return render(
            request, "profile.html",
            user=user, errors=errors,
            status_code=400,
        )
    user.password_hash = hash_password(new_password)
    db.add(user)
    db.commit()
    flash(request, "Password updated.")
    return RedirectResponse("/profile", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
