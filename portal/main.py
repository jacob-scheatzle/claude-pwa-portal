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
from portal.deps import current_user
from portal.models import App, Setting, User
from portal.security import hash_password, validate_password, verify_password
from portal.web import STATIC_DIR, render


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


def admin_exists(db: Session) -> bool:
    return db.exec(select(User).where(User.role == "admin")).first() is not None


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


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
):
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
):
    normalized = email.strip().lower()
    user = db.exec(select(User).where(User.email == normalized)).first()
    if user is None or not verify_password(password, user.password_hash):
        return render(
            request, "login.html",
            error="Invalid email or password.", email=email, next=_safe_next(next),
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
