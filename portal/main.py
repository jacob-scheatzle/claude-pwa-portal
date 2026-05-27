import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from portal import admin as admin_module
from portal import api as api_module
from portal import apps as apps_module
from portal.access import accessible_app_ids_for
from portal.audit import emit_security_line, record_anonymous, record_event
from portal.config import settings
from portal.db import engine, get_db, init_db
from portal.deps import current_user, require_user
from portal.middleware import ChildAppCSPMiddleware, HostDispatchMiddleware
from portal.models import App, Setting, User
from portal.security import (
    check_csrf,
    hash_password,
    validate_password,
    verify_password,
    verify_password_dummy,
)
from portal.sessions import (
    create_session,
    revoke_all_app_sessions_for_user,
    revoke_all_for_user,
    revoke_session,
)
from portal.settings_store import get_setting, set_setting
from portal.web import STATIC_DIR, flash, render


# uvicorn's "error" logger handles operational/startup output and has a stderr
# handler attached by default. Using it ensures startup advisories show up in
# `docker compose logs portal` without us having to bolt on a root-logger
# handler ourselves. (Python's root logger is unconfigured under uvicorn's
# default dictConfig, so a custom logger name silently no-ops.)
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Surface configuration footguns at startup. The combination below is
    # legitimate when a TLS-terminating proxy sits in front (the LB tells
    # uvicorn the scheme via X-Forwarded-Proto), but on a direct-to-Caddy
    # HTTP deployment it leaves Secure-flagged cookies that the browser
    # refuses to send — login appears to work but the next request looks
    # unauthenticated and the per-app iframe URL is generated with
    # ``https://`` and fails to load. Cheap to log; saves a debugging
    # session for whoever sets HTTP_ONLY=true without flipping
    # COOKIES_SECURE.
    if settings.http_only and settings.cookies_secure:
        logger.warning(
            "HTTP_ONLY=true with COOKIES_SECURE=true: assuming a "
            "TLS-terminating proxy sits in front of this stack. If browsers "
            "reach the portal directly over plain HTTP, Secure-flagged "
            "cookies will not be sent and per-app iframes will fail to "
            "load. Set COOKIES_SECURE=false if there is no proxy doing TLS."
        )
    yield


# FastAPI's auto-generated docs (``/docs`` and ``/redoc``) and the
# OpenAPI schema (``/openapi.json``) are turned off in production: this is a
# self-hosted single-tenant portal, the API is documented for app authors in
# ``docs/api-reference.md``, and the live endpoints aren't a public API
# surface we want to advertise to scanners. Leaving them on after the first
# real-VPS deploy showed scanners hitting /docs and /openapi.json within
# minutes of TLS coming up.
app = FastAPI(
    title="PWA Portal",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.cookies_secure,
    max_age=settings.session_max_age,
)
# Middleware order matters here. Starlette's add_middleware prepends to the
# user-middleware list, so the call made LAST becomes the OUTERMOST wrapper
# and runs FIRST on each request. We need HostDispatch to set
# ``request.state.app_slug`` from the Host header BEFORE ChildAppCSP reads
# it (the strict-CSP nonce is stamped onto ``request.state`` pre-handler so
# templates can substitute ``{{NONCE}}`` during render).
#
# Therefore: add ChildAppCSP first (innermost), then HostDispatch (outermost).
# On request: HostDispatch → ChildAppCSP → handler. On response: handler →
# ChildAppCSP (stamps the per-app Content-Security-Policy header) → HostDispatch.
#
# Per-app CSP lives in the portal, not Caddy, because the allowed external
# ``connect-src`` origins are per-app data sourced from ``App.allowed_origins``.
app.add_middleware(ChildAppCSPMiddleware, engine=engine)
app.add_middleware(HostDispatchMiddleware)
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


# Whitelist for the post-login ``?next=`` URL: a relative path made of
# URL-safe characters. The earlier (``startswith("/") and not "//")``) check
# accepted ``/\evil.com`` — browsers normalize ``\`` to ``/``, turning that
# into a protocol-relative redirect to evil.com mid-login. Forbidding the
# backslash (and every other byte outside the allowlist) closes that.
_SAFE_NEXT_RE = re.compile(r"^/(?:[A-Za-z0-9._~/?&=#%+\-]*)?$")


def _safe_next(value: str) -> str:
    return value if _SAFE_NEXT_RE.match(value) and not value.startswith("//") else "/"


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
    # On an app subdomain, ``/`` is the child app's index.html, not the
    # portal dashboard. Dispatch to the child-app serve path which handles
    # the no-cookie redirect back to the portal launcher.
    if getattr(request.state, "app_slug", None):
        return apps_module.serve_subdomain_request(request, db, path="")

    if not admin_exists(db):
        return RedirectResponse("/setup", status_code=303)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    visible = db.exec(
        select(App)
        .where(App.enabled == True)  # noqa: E712
        .order_by(App.display_order, App.name)
    ).all()
    # Filter to apps the user is allowed to launch. Admins see everything;
    # for non-admins the helper consults the UserAppAccess m2m.
    if user.role != "admin":
        allowed_ids = accessible_app_ids_for(db, user)
        visible = [a for a in visible if a.id in allowed_ids]
    return render(
        request,
        "dashboard.html",
        user=user,
        apps=visible,
        same_origin_mode=bool(settings.child_apps_same_origin),
        site_url=settings.site_url,
    )


# ----- PWA endpoints -----

@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest(db: DbDep):
    """Render the PWA manifest from current branding settings.

    Was a static file in earlier versions; now generated so the
    business name, theme color, and uploaded favicon (added in v0.6.0)
    flow into the iOS / Android install experience.

    When a custom favicon is configured we emit a single icon entry
    with ``sizes: "any"`` and ``purpose: "any maskable"``. Browsers
    handle the scaling — that's simpler than asking admins to upload
    five separate sized PNGs, and keeps SVG uploads working with the
    same code path. When no favicon is configured we fall back to the
    bundled emerald default at three explicit sizes so older browsers
    that don't understand ``sizes: "any"`` still find a usable icon.
    """
    from portal.branding import get_branding

    brand = get_branding(db)
    if brand["favicon_url"]:
        icon_entry = {
            "src": brand["favicon_url"],
            "sizes": "any",
            "purpose": "any maskable",
        }
        if brand["favicon_mime"]:
            icon_entry["type"] = brand["favicon_mime"]
        icons = [icon_entry]
    else:
        icons = [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ]

    return JSONResponse(
        {
            "name": brand["business_name"],
            # iOS clips short_name aggressively; 12 chars keeps the
            # branded label readable on the home screen instead of
            # truncating to an ellipsis mid-word.
            "short_name": brand["business_name"][:12],
            "description": "Your business app portal.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#fafaf9",
            "theme_color": brand["accent_color"],
            "icons": icons,
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


# Conventional ``/favicon.ico`` — many tools (RSS readers, link previews,
# scanners) request this path verbatim even though base.html advertises
# the actual icon via ``<link rel="icon">``. Redirect to the configured
# branding favicon when set; serve the bundled PNG default otherwise so
# the request stops 404'ing in the access log.
@app.get("/favicon.ico", include_in_schema=False)
def favicon(db: DbDep):
    from portal.branding import get_branding

    brand = get_branding(db)
    if brand["favicon_url"]:
        return RedirectResponse(brand["favicon_url"], status_code=302)
    return FileResponse(
        STATIC_DIR / "icons" / "favicon.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


# ``robots.txt`` disallows every crawler — the portal is a private
# small-business tool, not something that should be indexed in search
# engines or harvested for LLM training. Both anonymous and pseudonymous
# crawlers honor this file (Google, OpenAI's GPTBot, Anthropic's
# ClaudeBot, Perplexity, Bytedance, Amazon, etc. all explicitly support
# the ``User-agent: <bot>`` syntax). Crawlers that ignore robots.txt
# would also ignore a portal-side block list, so the wildcard ``*``
# disallow plus the named-bot explicit blocks is what we can do
# without sliding into request-fingerprinting territory.
_ROBOTS_TXT = (
    "# This is a private business portal. Please don't index or train on it.\n"
    "\n"
    "User-agent: *\n"
    "Disallow: /\n"
    "\n"
    "# Named blocks below: some crawlers honor a specific User-agent line\n"
    "# more reliably than the wildcard. Listed alphabetically so this stays\n"
    "# easy to audit and extend.\n"
    "\n"
    "User-agent: AhrefsBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: Amazonbot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: anthropic-ai\n"
    "Disallow: /\n"
    "\n"
    "User-agent: Applebot-Extended\n"
    "Disallow: /\n"
    "\n"
    "User-agent: Bytespider\n"
    "Disallow: /\n"
    "\n"
    "User-agent: CCBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: ChatGPT-User\n"
    "Disallow: /\n"
    "\n"
    "User-agent: ClaudeBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: cohere-ai\n"
    "Disallow: /\n"
    "\n"
    "User-agent: DataForSeoBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: Diffbot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: FacebookBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: Google-Extended\n"
    "Disallow: /\n"
    "\n"
    "User-agent: GPTBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: ImagesiftBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: meta-externalagent\n"
    "Disallow: /\n"
    "\n"
    "User-agent: OAI-SearchBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: PerplexityBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: SemrushBot\n"
    "Disallow: /\n"
    "\n"
    "User-agent: YouBot\n"
    "Disallow: /\n"
)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(
        _ROBOTS_TXT,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
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


# Uploaded logo served by name. Anyone (signed-in or not) can fetch the
# active logo — same trust level as /favicon.ico. We validate the filename
# against the same whitelist the upload handler enforces so a stale or
# malicious DB value can't traverse out of the branding directory.
@app.get("/branding/{name}", include_in_schema=False)
def branding_logo(name: str):
    from portal.branding import _safe_logo_name, branding_dir

    if not _safe_logo_name(name):
        raise HTTPException(404)
    target = (branding_dir() / name).resolve()
    try:
        target.relative_to(branding_dir())
    except ValueError:
        raise HTTPException(404)
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=300"})


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

    # create_session needs user.id, which only exists after the commit above.
    sid = create_session(db, user)
    request.session["session_id"] = sid
    record_event(
        db, actor=user, action="setup.complete", request=request,
        target=f"user:{user.email}",
    )
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
    from portal.health import record_login_attempt

    client_ip = request.client.host if request.client else "unknown"
    key = _login_key(request, email)
    if _login_blocked(key):
        record_login_attempt(
            db, ip=client_ip, email=email, success=False, reason="rate_limited",
        )
        record_anonymous(
            db, action="login.failure", request=request,
            target=f"email:{email.strip().lower()}", actor_email=email,
            details={"reason": "rate_limited"},
        )
        emit_security_line(
            "LOGIN_RATE_LIMITED", client_ip,
            email=email.strip().lower(), reason="rate_limited",
        )
        return render(
            request, "login.html",
            error="Too many failed attempts. Try again in a few minutes.",
            email=email, next=_safe_next(next),
            status_code=429,
        )
    normalized = email.strip().lower()
    user = db.exec(select(User).where(User.email == normalized)).first()
    if user is None:
        # Burn the same bcrypt time a real-user lookup would, so an attacker
        # can't enumerate registered emails by timing the response.
        verify_password_dummy(password)
        bad_password = True
    else:
        bad_password = not verify_password(password, user.password_hash)
    if user is None or bad_password:
        _record_login_failure(key)
        record_login_attempt(
            db, ip=client_ip, email=email, success=False, reason="bad_credentials",
        )
        record_anonymous(
            db, action="login.failure", request=request,
            target=f"email:{normalized}", actor_email=email,
            details={"reason": "bad_credentials"},
        )
        emit_security_line(
            "FAILED_LOGIN", client_ip,
            email=normalized, reason="bad_credentials",
        )
        return render(
            request, "login.html",
            error="Invalid email or password.", email=email, next=_safe_next(next),
            status_code=401,
        )
    _clear_login_failures(key)
    record_login_attempt(
        db, ip=client_ip, email=email, success=True, reason="ok",
    )
    record_event(
        db, actor=user, action="login.success", request=request,
        target=f"user:{user.email}",
    )
    # Rotate the session on login to defeat session fixation: a pre-planted
    # session id (MITM before TLS terminated, leaked link, etc.) must not
    # survive the auth boundary. Clearing the dict changes its signed value,
    # so Starlette emits a fresh Set-Cookie. _csrf is regenerated lazily by
    # the next render call.
    request.session.clear()
    sid = create_session(db, user)
    request.session["session_id"] = sid
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/logout")
def logout(
    request: Request,
    db: DbDep,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    # Revoke the server-side row BEFORE clearing the cookie so a stolen
    # pre-logout cookie can't continue to authenticate.
    sid = request.session.get("session_id")
    # Resolve the user behind this session BEFORE revoking, so we can cascade
    # the revocation to every open child-app session for the same user.
    from portal.sessions import get_active_session
    session_row = get_active_session(db, sid)
    actor: Optional[User] = None
    if session_row is not None:
        actor = db.get(User, session_row.user_id)
        revoke_all_app_sessions_for_user(db, session_row.user_id)
    revoke_session(db, sid)
    request.session.clear()
    record_event(
        db, actor=actor, action="login.logout", request=request,
        target=f"user:{actor.email}" if actor else "",
    )
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
    # Rotate the session after a successful password change: revoke every
    # active portal session AND every open child-app session for this user
    # (logging out other devices that may have been hijacked under the old
    # password) and mint a fresh portal session for the current browser so
    # the user stays signed in here. Child-app sessions can be re-minted by
    # re-launching from the dashboard.
    revoke_all_for_user(db, user.id)
    revoke_all_app_sessions_for_user(db, user.id)
    new_sid = create_session(db, user)
    request.session.clear()
    request.session["session_id"] = new_sid
    record_event(
        db, actor=user, action="profile.password.change", request=request,
        target=f"user:{user.email}",
    )
    flash(request, "Password updated.")
    return RedirectResponse("/profile", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}


# ----- Public share URLs -----
#
# /s/<token> is the only public, unauthenticated content surface on the
# portal origin. The token is high-entropy (token_urlsafe(24) = ~192 bits)
# and lookup is constant-time relative to the user-supplied input, so
# probing for valid tokens is infeasible in practice. Storage shares
# stream from the creator's namespace; PDF shares serve a pre-rendered
# file from data/shares/. Both return 404 for any non-serveable state
# (unknown / revoked / expired / view-capped / missing file).

@app.get("/s/{token}", include_in_schema=False)
def share_view(token: str, db: DbDep):
    import re

    from fastapi.responses import FileResponse as _FileResponse
    from portal.shares import lookup_active, record_view, shares_dir
    from portal.models import App as _App

    # The token URL-safe alphabet is [A-Za-z0-9_-]. Anything else is a
    # malformed link; reject without hitting the DB.
    if not re.match(r"^[A-Za-z0-9_-]+$", token) or len(token) > 64:
        raise HTTPException(404)

    row = lookup_active(db, token)
    if row is None:
        raise HTTPException(404)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", row.filename or "shared") or "shared"

    if row.kind == "pdf":
        path_name = (row.payload or {}).get("path") or ""
        if not path_name:
            raise HTTPException(404)
        base = shares_dir()
        target = (base / path_name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(404)
        if not target.is_file():
            raise HTTPException(404)
        # Atomic claim against ``max_views``. If we lost the race to a
        # concurrent viewer that pushed the count over the cap, treat this
        # like any other saturated/expired share — 404.
        if not record_view(db, row):
            raise HTTPException(404)
        return _FileResponse(
            target,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
        )

    if row.kind == "storage":
        # Resolve the storage object out of the creator's namespace. The
        # share is bound to (app, creator) at mint time, so revoking the
        # creator's access to the app doesn't break the link — that's
        # arguably right (the link is independent capability) but a
        # design choice worth being explicit about.
        app_row = db.get(_App, row.app_id)
        if app_row is None or not app_row.enabled:
            raise HTTPException(404)
        key = (row.payload or {}).get("key") or ""
        if not key:
            raise HTTPException(404)
        # Re-validate the key — defense in depth in case the row was
        # written by a future version with looser rules.
        from portal.api import _ns_dir, _validate_key

        try:
            safe_key = _validate_key(key)
        except HTTPException:
            raise HTTPException(404)
        ns = _ns_dir(app_row.slug, row.created_by)
        target = (ns / safe_key).resolve()
        try:
            target.relative_to(ns)
        except ValueError:
            raise HTTPException(404)
        if not target.is_file():
            raise HTTPException(404)
        if not record_view(db, row):
            raise HTTPException(404)
        # Storage shares serve user-controlled bytes from the portal origin,
        # where the session cookie lives. If we let the browser sniff this
        # as text/html (or any renderable type) and render it inline, a
        # malicious child app could upload ``xss.html`` to storage, mint a
        # share link, phish an admin into clicking it, and run JS in the
        # admin's session — same-origin token mint, settings change, etc.
        # The matching SDK endpoint at /api/v1/storage/{key} already forces
        # ``attachment`` for the same reason; this branch had regressed it.
        # PDF shares (above) are safe to serve inline because they're
        # pre-rendered by WeasyPrint on the server and always
        # application/pdf.
        return FileResponse(
            target,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    raise HTTPException(404)


# ----- App subdomain catch-all -----
#
# Registered LAST so every other portal-origin route gets a chance to match
# first. On the portal origin (``request.state.app_slug is None``) this hands
# back a 404 — the route is effectively a no-op for portal-origin traffic.
# On an app subdomain it serves the child app's static bundle out of
# ``data/apps/<slug>/`` (or the shared SDK at ``/portal-sdk.js``).

@app.get("/{full_path:path}", include_in_schema=False)
def app_subdomain_catch_all(full_path: str, request: Request, db: DbDep):
    if not getattr(request.state, "app_slug", None):
        raise HTTPException(status_code=404)
    return apps_module.serve_subdomain_request(request, db, path=full_path)
