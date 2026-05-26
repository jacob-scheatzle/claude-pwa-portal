"""HTTP API child apps consume via /portal-sdk.js, plus programmatic endpoints
for the Claude skill. Auth accepts either a session cookie or
`Authorization: Bearer <token>`."""
from __future__ import annotations

import io
import mimetypes
import re
import time
from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from threading import Lock
from typing import Annotated, Optional

from fastapi import (
    APIRouter, Depends, File, Header, HTTPException, Request, Response, UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlmodel import Session, select

from portal.apps import UploadError, install_bundle
from portal.config import settings
from portal.db import get_db
from portal.deps import APP_SESSION_COOKIE, current_user_or_token
from portal.models import App, AppLaunchToken, User
from portal.sessions import create_app_session
from portal.settings_store import get_setting, smtp_config
from portal.smtp import send_message

# Re-export for callers that still import from portal.api (back-compat shim).
_smtp_send = send_message

router = APIRouter(prefix="/api/v1")

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[Optional[User], Depends(current_user_or_token)]
AppHeader = Annotated[Optional[str], Header(alias="X-Portal-App")]


def _require_user(user: Optional[User]) -> User:
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


def _require_admin(user: Optional[User]) -> User:
    me = _require_user(user)
    if me.role != "admin":
        raise HTTPException(403, "Admin role required")
    return me


def _require_csrf_for_cookie(request: Request, x_csrf: Optional[str]) -> None:
    """Skip for bearer tokens; require X-CSRF-Token for cookie sessions.

    Cookie-auth callers are subject to CSRF because a malicious cross-origin
    page can ride the browser's session cookie. Bearer-token callers
    (the Claude skill, server-to-server) carry the token explicitly and so
    are not subject to CSRF — they skip this check entirely.

    Note: this is defense-in-depth on top of SameSite=Lax. It does NOT
    protect against a malicious same-origin child app at /apps/evil/,
    which can read the CSRF token via /api/v1/csrf-token just like any
    other same-origin script. The structural fix for that is separate
    origins per child app.
    """
    auth_method = getattr(request.state, "auth_method", None)
    if auth_method == "token":
        return
    from portal.security import check_csrf_header
    check_csrf_header(request, x_csrf)


def _require_app(db: Session, slug: Optional[str]) -> App:
    if not slug:
        raise HTTPException(400, "X-Portal-App header missing")
    app_row = db.exec(
        select(App).where(App.slug == slug, App.enabled == True)  # noqa: E712
    ).first()
    if app_row is None:
        raise HTTPException(400, f"App '{slug}' not found or disabled")
    return app_row


_REFERER_APP_RE = re.compile(r"^https?://[^/]+/apps/([^/]+)/")


def _resolve_app_slug(
    request: Request,
    user: User,
    x_portal_app: Optional[str],
    db: Session,
) -> App:
    """Pick the authoritative app slug for a storage request.

    Bearer-token clients (the Claude skill, CI, etc.) may name any app via
    ``X-Portal-App``. Browser/cookie clients can't be trusted with that
    header — a malicious child app at ``/apps/evil/`` could set it to
    ``finance-app`` and read another app's namespace. For cookie auth we
    extract the slug from the Origin/Referer URL path instead.

    The decision is driven by ``request.state.auth_method`` (set by
    ``current_user_or_token``), NOT by the raw Authorization header. That
    matters because a malicious page can send any ``Authorization: Bearer …``
    string — when the token is invalid the dep falls back to cookie auth, and
    we must apply the cookie-side Referer check in that case.
    """
    auth_method = getattr(request.state, "auth_method", None)
    if auth_method == "token":
        slug = x_portal_app  # token clients can name any app
        if not slug:
            raise HTTPException(
                400, "X-Portal-App header required for token auth"
            )
    elif auth_method == "cookie":
        ref = request.headers.get("referer", "") or request.headers.get("origin", "")
        m = _REFERER_APP_RE.match(ref)
        if not m:
            raise HTTPException(
                400,
                "Storage requires browser context under /apps/<slug>/ or a bearer "
                "token with X-Portal-App",
            )
        slug = m.group(1)
    else:
        # No authenticated session and no valid token — callers should have
        # been rejected by _require_user first, but be defensive.
        raise HTTPException(401, "Authentication required")
    return _require_app(db, slug)


# ----- /user -----

@router.get("/user/me")
def user_me(user: UserDep):
    me = _require_user(user)
    return {"id": me.id, "email": me.email, "role": me.role}


@router.get("/csrf-token")
def get_csrf_token(request: Request, user: UserDep):
    """Return the current cookie session's CSRF token.

    Cookie auth required. Bearer-token clients don't need a CSRF token
    (they're not subject to CSRF) — they get a 400 if they call this.
    """
    _require_user(user)
    auth_method = getattr(request.state, "auth_method", None)
    if auth_method != "cookie":
        raise HTTPException(400, "CSRF token only relevant for cookie auth")
    from portal.security import csrf_token as _csrf_token
    return {"csrf_token": _csrf_token(request)}


# ----- /internal/cert-ask (Caddy on_demand_tls hook) -----

# Pre-built once at import time so we don't recompile the pattern on every
# Caddy probe. ``re.escape`` keeps the site URL safe even when it contains
# regex metacharacters (a dotted hostname always does — ``.`` matches any
# char if unescaped). The slug character class matches the slug rules
# enforced by ``portal.apps``: lowercase letters, digits, and hyphens.
_CERT_ASK_RE = re.compile(
    rf"^([a-z0-9-]+)\.apps\.{re.escape(settings.site_url.lower())}$"
)


@router.get("/internal/cert-ask", include_in_schema=False)
def cert_ask(domain: str, db: DbDep):
    """Approve TLS-certificate issuance for known app subdomains.

    Called by Caddy's ``on_demand_tls.ask`` hook BEFORE Caddy attempts to
    fetch a Let's Encrypt certificate for a host that matches the wildcard
    ``*.apps.<SITE_URL>`` block. Returns 200 if the host should be served,
    404 otherwise — Caddy treats any non-2xx as "don't issue a cert."

    The guardrail matters because Let's Encrypt enforces a per-registered-
    domain rate limit (50 certs/week as of writing). Without this hook a
    stranger could send requests for ``<random>.apps.<SITE_URL>`` and burn
    the quota, locking the operator out of issuing certs for real apps.

    Accepts:
      * ``<slug>.apps.<SITE_URL>`` for any enabled ``App.slug``  -> 200
      * The portal hostname itself                               -> 200
        (defensive; Caddy may probe-ask for the main host under some
        configurations even though the main host has its own site block
        and wouldn't actually use on_demand)
      * Anything else                                            -> 404

    No authentication: Caddy reaches the portal over the internal Docker
    network. External callers can't learn anything they couldn't already
    learn by attempting ``https://<slug>.apps.<SITE_URL>/`` directly.
    """
    host = (domain or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(404)

    # Strip an optional :port suffix — Caddy normally sends a bare hostname
    # but be defensive in case a future version adds one.
    if ":" in host and not host.startswith("["):
        host = host.rsplit(":", 1)[0]

    site = settings.site_url.lower().rstrip(".")
    if host == site:
        return {"ok": True}

    m = _CERT_ASK_RE.match(host)
    if not m:
        raise HTTPException(404)
    slug = m.group(1)

    app_row = db.exec(
        select(App).where(App.slug == slug, App.enabled == True)  # noqa: E712
    ).first()
    if app_row is None:
        raise HTTPException(404)
    return {"ok": True}


# ----- /session/exchange (app subdomain) -----

class SessionExchangeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@router.post("/session/exchange")
def session_exchange(
    body: SessionExchangeRequest,
    request: Request,
    response: Response,
    db: DbDep,
):
    """Trade a single-use launch token for an AppSession cookie.

    Only meaningful on an app subdomain (the middleware sets
    ``request.state.app_slug`` based on the Host header). Validation:

    - the request must be on an app subdomain (otherwise 400)
    - the token must exist, be unconsumed, unexpired, and bound to that
      subdomain's slug (otherwise 401 with a uniform error message — we
      deliberately don't distinguish "wrong slug" from "expired" so probing
      tokens reveals as little as possible)
    - the linked user must still exist (otherwise 401)

    On success, marks the token consumed, mints an AppSession, sets the
    ``app_session`` cookie scoped to the current subdomain, and returns
    ``{"ok": true}``.
    """
    slug = getattr(request.state, "app_slug", None)
    if not slug:
        raise HTTPException(400, "Not on an app subdomain")

    launch = db.get(AppLaunchToken, body.token)
    now = datetime.now(timezone.utc)

    def _bad():
        # Uniform reject to avoid leaking which validation step failed.
        raise HTTPException(401, "Invalid or expired launch token")

    if launch is None or launch.consumed_at is not None:
        _bad()
    expires_at = launch.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is None or expires_at < now:
        _bad()
    if launch.slug != slug:
        _bad()

    user = db.get(User, launch.user_id)
    if user is None:
        _bad()

    launch.consumed_at = now
    db.add(launch)
    db.commit()

    sid = create_app_session(db, launch.user_id, slug)

    # Cookie is scoped to the exact subdomain by ``Domain`` so it never leaks
    # to other app subdomains or to the portal origin. ``SameSite=Lax`` is
    # sufficient because the iframe load + subsequent fetches are same-site
    # (same registrable domain as the portal) and the only state-changing
    # path here (the POST itself) carries the launch token, which is
    # single-use and time-bound.
    host = request.headers.get("host", "")
    cookie_domain = host.split(":", 1)[0] or f"{slug}.apps.{settings.site_url}"
    response.set_cookie(
        APP_SESSION_COOKIE,
        sid,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
        path="/",
        domain=cookie_domain,
        max_age=settings.session_max_age,
    )
    return {"ok": True}


# ----- /pdf -----

class PdfRequest(BaseModel):
    html: str = Field(min_length=1)
    filename: str = "document.pdf"


def _no_external_fetcher(url, timeout=10, ssl_context=None):
    """url_fetcher that blocks every scheme except data: URIs.

    Stops WeasyPrint from making outbound HTTP/file/etc. requests on behalf
    of caller-controlled HTML (SSRF / local file exfiltration). Inline assets
    via ``data:`` are still allowed.
    """
    if not url.startswith("data:"):
        raise ValueError(
            f"External resource fetching is disabled (got {url[:60]!r})"
        )
    from weasyprint.urls import default_url_fetcher
    return default_url_fetcher(url, timeout=timeout, ssl_context=ssl_context)


@router.post("/pdf/render")
def pdf_render(
    req: PdfRequest,
    request: Request,
    user: UserDep,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    try:
        from weasyprint import HTML  # lazy: avoid hard import at startup
    except ImportError:
        raise HTTPException(503, "PDF service unavailable: WeasyPrint not installed")
    except OSError as e:
        raise HTTPException(503, f"PDF service unavailable: {e}")

    buf = io.BytesIO()
    try:
        HTML(string=req.html, url_fetcher=_no_external_fetcher).write_pdf(buf)
    except Exception as e:
        raise HTTPException(500, f"PDF render failed: {e}")
    buf.seek(0)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", req.filename) or "document.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ----- /email -----

class EmailRequest(BaseModel):
    to: list[EmailStr] | EmailStr
    subject: str = Field(default="", max_length=200)
    text: Optional[str] = None
    html: Optional[str] = None


# Per-user, in-memory, rolling-hour send counter. NOTE: this is per-process,
# so it only protects a single-instance deployment. Multi-worker setups would
# need a shared store (Redis/DB) to enforce the same cap globally.
_EMAIL_RATE_WINDOW_SECONDS = 3600
_EMAIL_RATE_LIMIT_PER_HOUR = 30
_email_send_log: dict[int, deque] = {}
_email_rate_lock = Lock()


def _check_email_rate(user_id: int) -> None:
    """Raise 429 if ``user_id`` has exceeded the rolling-hour send limit."""
    now = time.monotonic()
    cutoff = now - _EMAIL_RATE_WINDOW_SECONDS
    with _email_rate_lock:
        dq = _email_send_log.get(user_id)
        if dq is None:
            dq = deque()
            _email_send_log[user_id] = dq
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _EMAIL_RATE_LIMIT_PER_HOUR:
            raise HTTPException(
                429,
                f"Email send rate limit exceeded "
                f"({_EMAIL_RATE_LIMIT_PER_HOUR}/hour per user, per process).",
            )
        dq.append(now)


def _recipient_domain_allowlist(db: Session) -> Optional[set[str]]:
    """Parse the ``email_recipient_domains`` Setting into a lowercase set.

    Returns None when unset/empty (i.e. no restriction).
    """
    raw = get_setting(db, "email_recipient_domains", None)
    if not raw:
        return None
    domains = {d.strip().lower() for d in raw.split(",") if d.strip()}
    return domains or None


def _enforce_recipient_allowlist(to_list: list[str], allowed: Optional[set[str]]) -> None:
    if not allowed:
        return
    for addr in to_list:
        _, _, domain = str(addr).rpartition("@")
        if domain.lower() not in allowed:
            raise HTTPException(
                400,
                f"Recipient domain not allowed: {addr}",
            )


@router.post("/email/send")
def email_send(
    req: EmailRequest,
    request: Request,
    user: UserDep,
    db: DbDep,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    me = _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    cfg = smtp_config(db)
    if not cfg["host"]:
        raise HTTPException(503, "Email service unavailable: SMTP not configured")
    if not req.text and not req.html:
        raise HTTPException(400, "Provide at least one of `text` or `html`")

    to_list = req.to if isinstance(req.to, list) else [req.to]
    _enforce_recipient_allowlist(to_list, _recipient_domain_allowlist(db))
    _check_email_rate(me.id)

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"] or cfg["username"] or me.email
    msg["To"] = ", ".join(str(t) for t in to_list)
    msg["Subject"] = req.subject

    if req.text and req.html:
        msg.set_content(req.text)
        msg.add_alternative(req.html, subtype="html")
    elif req.html:
        msg.set_content(req.html, subtype="html")
    else:
        msg.set_content(req.text or "")

    try:
        send_message(msg, cfg)
    except Exception as e:
        raise HTTPException(502, f"Email send failed: {e}")
    return {"status": "sent", "count": len(to_list)}


# ----- /storage -----

MAX_OBJECT_BYTES = 10 * 1024 * 1024
MAX_NAMESPACE_BYTES = 100 * 1024 * 1024
KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _validate_key(key: str) -> str:
    if not key or len(key) > 200:
        raise HTTPException(400, "key length 1..200 required")
    if not KEY_RE.match(key):
        raise HTTPException(400, "key may only contain A-Z a-z 0-9 . _ - and /")
    parts = key.split("/")
    if any(p in ("", "..", ".") for p in parts):
        raise HTTPException(400, "key may not contain empty, '.', or '..' segments")
    return key


def _ns_dir(app_slug: str, user_id: int) -> Path:
    base = Path(settings.data_dir).resolve() / "storage" / app_slug / str(user_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ns_usage(ns: Path) -> int:
    return sum(p.stat().st_size for p in ns.rglob("*") if p.is_file())


@router.get("/storage")
def storage_list(
    request: Request, user: UserDep, db: DbDep, x_portal_app: AppHeader = None,
):
    me = _require_user(user)
    app_row = _resolve_app_slug(request, me, x_portal_app, db)
    ns = _ns_dir(app_row.slug, me.id)
    items = []
    usage = 0
    for p in ns.rglob("*"):
        if p.is_file():
            size = p.stat().st_size
            items.append({"key": p.relative_to(ns).as_posix(), "size": size})
            usage += size
    return {"items": items, "usage": usage, "limit": MAX_NAMESPACE_BYTES}


@router.get("/storage/{key:path}")
def storage_get(
    key: str, request: Request, user: UserDep, db: DbDep,
    x_portal_app: AppHeader = None,
):
    me = _require_user(user)
    app_row = _resolve_app_slug(request, me, x_portal_app, db)
    safe = _validate_key(key)
    ns = _ns_dir(app_row.slug, me.id)
    target = (ns / safe).resolve()
    try:
        target.relative_to(ns)
    except ValueError:
        raise HTTPException(404)
    if not target.is_file():
        raise HTTPException(404)
    mt, _ = mimetypes.guess_type(str(target))
    # Force download rather than inline rendering. A malicious child app
    # could otherwise stash an `evil.html` then trick the user into clicking
    # /api/v1/storage/evil.html — served same-origin as text/html, that's an
    # XSS pivot into the portal. Content-Disposition: attachment neutralizes
    # it. Sanitize the basename so the filename can't break out of the header.
    basename = Path(safe).name or "download"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", basename) or "download"
    return FileResponse(
        target,
        media_type=mt or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.put("/storage/{key:path}")
async def storage_put(
    key: str,
    request: Request,
    user: UserDep,
    db: DbDep,
    x_portal_app: AppHeader = None,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    me = _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    app_row = _resolve_app_slug(request, me, x_portal_app, db)
    safe = _validate_key(key)
    ns = _ns_dir(app_row.slug, me.id)
    target = (ns / safe).resolve()
    try:
        target.relative_to(ns)
    except ValueError:
        raise HTTPException(400, "invalid key")

    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with open(target, "wb") as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_OBJECT_BYTES:
                    raise HTTPException(
                        413,
                        f"object exceeds {MAX_OBJECT_BYTES // (1024 * 1024)}MB limit",
                    )
                f.write(chunk)
    except Exception:
        # Disk error, oversized body, network drop — any path that leaves
        # a partial file behind should clean up before re-raising.
        target.unlink(missing_ok=True)
        raise

    if _ns_usage(ns) > MAX_NAMESPACE_BYTES:
        target.unlink(missing_ok=True)
        raise HTTPException(
            507,
            f"storage namespace exceeds {MAX_NAMESPACE_BYTES // (1024 * 1024)}MB limit",
        )

    return {
        "key": key,
        "size": written,
        "content_type": request.headers.get("Content-Type", "application/octet-stream"),
    }


@router.delete("/storage/{key:path}")
def storage_delete(
    key: str, request: Request, user: UserDep, db: DbDep,
    x_portal_app: AppHeader = None,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    me = _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    app_row = _resolve_app_slug(request, me, x_portal_app, db)
    safe = _validate_key(key)
    ns = _ns_dir(app_row.slug, me.id)
    target = (ns / safe).resolve()
    try:
        target.relative_to(ns)
    except ValueError:
        raise HTTPException(404)
    if not target.is_file():
        raise HTTPException(404)
    target.unlink()
    return {"deleted": key}


# ----- /apps (programmatic upload for the Claude skill) -----

@router.post("/apps/upload")
async def apps_upload(
    request: Request,
    user: UserDep,
    db: DbDep,
    bundle: UploadFile = File(...),
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    admin = _require_admin(user)
    _require_csrf_for_cookie(request, x_csrf)
    try:
        result = await install_bundle(db, admin, bundle)
    except UploadError as e:
        raise HTTPException(400, str(e))
    return {"slug": result.slug, "name": result.name, "version": result.version}
