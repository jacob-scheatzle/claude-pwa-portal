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
from sqlalchemy import update
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
    """Skip for bearer tokens; require X-CSRF-Token for any cookie session.

    Two cookie-session flavors are covered:

    - ``cookie`` — the portal's own ``UserSession`` cookie (portal origin).
    - ``app_session`` — the per-app ``AppSession`` cookie (subdomain origin).

    Both are HttpOnly, ``SameSite=Lax``, and rideable by the browser without
    user intent. Bearer-token callers (the Claude skill, server-to-server)
    carry the token explicitly and so are not subject to CSRF — they skip
    this check entirely.

    Defense-in-depth note: this layer sits on top of ``SameSite=Lax`` and the
    cross-origin boundary. Even though a malicious page on another origin
    can't fetch the CSRF token (they can't read responses from the portal /
    app subdomain), requiring the token still defends against subtle
    bypasses (e.g. a misconfigured CORS policy in the future). On the
    subdomain, the SDK fetches ``/api/v1/csrf-token`` same-origin and sends
    ``X-CSRF-Token`` automatically — the gate is transparent to app authors.
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


def _maybe_resolve_app(
    request: Request,
    user: User,
    x_portal_app: Optional[str],
    db: Session,
) -> Optional[App]:
    """Best-effort App resolution for service-gating on non-storage endpoints.

    Returns the App row when a request arrives in an app context (subdomain,
    portal-origin /apps/<slug>/ Referer, or bearer-token with X-Portal-App).
    Returns ``None`` when no app context is detectable — for example a token
    client calling ``/api/v1/pdf/render`` directly without an X-Portal-App
    header. Callers use the None return to mean "no app to gate against,
    allow."

    Storage explicitly requires an app context; for that, callers use
    ``_resolve_app_slug`` (below), which raises 400 on no-context.
    """
    host_slug = getattr(request.state, "app_slug", None)
    if host_slug:
        return _require_app(db, host_slug)

    auth_method = getattr(request.state, "auth_method", None)
    if auth_method == "token":
        if not x_portal_app:
            return None  # programmatic call without a named app
        return _require_app(db, x_portal_app)

    if auth_method in ("cookie", "app_session"):
        ref = request.headers.get("referer", "") or request.headers.get("origin", "")
        m = _REFERER_APP_RE.match(ref)
        if not m:
            return None
        return _require_app(db, m.group(1))

    return None


def _require_service(app_row: Optional[App], service: str) -> None:
    """403 if ``service`` isn't in this app's admin-approved list.

    Back-compat: an app whose manifest declared NO services at all gets a
    pass — pre-feature apps and apps that never opted in keep working. The
    moment an app declares ``services: [...]`` in its manifest, only the
    declared + admin-approved subset is callable; the admin can revoke
    individual services from /admin/apps.

    ``app_row`` may be None — a token client without X-Portal-App, for
    instance. In that case there's no app to gate against, so we allow.
    """
    if app_row is None:
        return
    declared = set(app_row.services or [])
    if not declared:
        return  # legacy / undeclared — no gate
    allowed = set(app_row.allowed_services or [])
    if service not in allowed:
        raise HTTPException(
            403,
            f"App '{app_row.slug}' is not authorized to use the '{service}' "
            f"service. Ask an admin to enable it under /admin/apps.",
        )


def _resolve_app_slug(
    request: Request,
    user: User,
    x_portal_app: Optional[str],
    db: Session,
) -> App:
    """Pick the authoritative app slug for a storage request.

    Precedence (most-trusted first):

    1. **App subdomain (Host-derived).** When ``request.state.app_slug`` is set
       by ``HostDispatchMiddleware``, the slug came from the browser-set Host
       header on ``<slug>.apps.<SITE_URL>`` and the AppSession cookie was
       already verified to be scoped to that same slug. No client header or
       Referer can override this.
    2. **Bearer-token clients.** A valid ``Authorization: Bearer …`` (the
       Claude skill, CI, server-to-server) names the target app via
       ``X-Portal-App``. Token clients are out-of-band: they have legitimate
       reason to act on any slug their user has access to.
    3. **Legacy portal-origin cookie auth.** When child apps run same-origin
       (``CHILD_APPS_SAME_ORIGIN=true``), the slug is derived from the
       Origin/Referer URL path — same as before Phase D.

    The decision is driven by ``request.state.auth_method`` (set by
    ``current_user_or_token``), NOT by the raw Authorization header. That
    matters because a malicious page can send any ``Authorization: Bearer …``
    string — when the token is invalid the dep falls back to cookie auth, and
    we must apply the cookie-side Referer check in that case.
    """
    # 1. Host-derived slug from the app subdomain wins outright. The auth
    # method here is "app_session" (the matching AppSession cookie was
    # already validated by current_user_or_token before we got here), so the
    # slug from the Host header is doubly checked: by the middleware, and
    # implicitly by the cookie-row's slug match.
    host_slug = getattr(request.state, "app_slug", None)
    if host_slug:
        return _require_app(db, host_slug)

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

    Cookie auth required — either the portal's ``UserSession`` cookie
    (``auth_method == "cookie"``) or an app-subdomain ``AppSession`` cookie
    (``auth_method == "app_session"``). Each origin has its own independent
    Starlette session cookie, so the CSRF token returned is scoped to the
    caller's origin and only valid for state-changing calls back to the
    same origin.

    Bearer-token clients don't need a CSRF token (they're not subject to
    CSRF) — they get a 400 if they call this.
    """
    _require_user(user)
    auth_method = getattr(request.state, "auth_method", None)
    if auth_method not in ("cookie", "app_session"):
        raise HTTPException(400, "CSRF token only relevant for cookie auth")
    from portal.security import csrf_token as _csrf_token
    return {"csrf_token": _csrf_token(request)}


# ----- /internal/cert-ask (Caddy on_demand_tls hook) -----

# Pre-built once at import time so we don't recompile the pattern on every
# Caddy probe. ``re.escape`` keeps the site URL safe even when it contains
# regex metacharacters (a dotted hostname always does — ``.`` matches any
# char if unescaped). The slug character class matches the slug rules
# enforced by ``portal.apps``: lowercase letters, digits, and hyphens.
#
# IMPORTANT: ``settings.site_url`` is the env-loaded value (config.py
# Pydantic Settings). This is intentional — Caddy reads ``SITE_URL`` from
# the same env at container startup to build its site blocks, so the
# portal's routing must match Caddy's. Letting an admin override site_url
# at runtime via a DB Setting would create a split-brain where Caddy
# accepts certs for one hostname and the portal validates against another.
# If site_url ever needs to change, restart the stack with new env.
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

    now = datetime.now(timezone.utc)

    def _bad():
        # Uniform reject to avoid leaking which validation step failed.
        raise HTTPException(401, "Invalid or expired launch token")

    # Atomic claim: mark the row consumed in a single UPDATE conditioned on
    # ``consumed_at IS NULL``. SQL guarantees only one concurrent request
    # gets ``rowcount == 1``; any racing duplicate sees ``rowcount == 0`` and
    # is rejected. Doing the check-then-write in Python would let two
    # threads both pass the ``consumed_at is None`` check before either
    # commits, double-spending a single-use token.
    result = db.exec(
        update(AppLaunchToken)
        .where(AppLaunchToken.token == body.token)
        .where(AppLaunchToken.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    db.commit()
    if result.rowcount != 1:
        _bad()

    # We now own the claim — read back the row to validate the remaining
    # fields (slug match, expiry, user existence). Even if these checks
    # fail, the token is already consumed, which is fine: a single-use
    # token that fails validation is just spent without minting a session.
    launch = db.get(AppLaunchToken, body.token)
    if launch is None:
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

    sid = create_app_session(db, launch.user_id, slug)

    # Host-only cookie: omit ``Domain`` so the browser scopes the cookie to
    # the exact subdomain the response came from (e.g. ``foo.apps.example
    # .com``). With ``Domain`` set, the cookie is ALSO sent to deeper
    # subdomains like ``x.foo.apps.example.com`` — strictly looser than what
    # we want and a needless attack surface. ``SameSite=Lax`` is sufficient
    # because the iframe load + subsequent fetches are same-site (same
    # registrable domain as the portal) and the only state-changing path
    # here (the POST itself) carries the launch token, which is single-use
    # and time-bound.
    response.set_cookie(
        APP_SESSION_COOKIE,
        sid,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_max_age,
    )
    return {"ok": True}


# ----- /pdf -----

class PdfRequest(BaseModel):
    html: str = Field(min_length=1)
    filename: str = "document.pdf"
    # When True, the portal injects a branding header (business name + logo
    # + accent border) into the rendered PDF before running WeasyPrint. The
    # injection is a string splice — see ``portal.branding.inject_pdf_header``
    # — so the app's HTML is otherwise untouched. Opt-in so existing apps
    # that fit content precisely aren't reflowed by the new header.
    branded: bool = False


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
    db: DbDep,
    x_portal_app: AppHeader = None,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    me = _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    _require_service(_maybe_resolve_app(request, me, x_portal_app, db), "pdf")
    try:
        from weasyprint import HTML  # lazy: avoid hard import at startup
    except ImportError:
        raise HTTPException(503, "PDF service unavailable: WeasyPrint not installed")
    except OSError as e:
        raise HTTPException(503, f"PDF service unavailable: {e}")

    html_to_render = req.html
    if req.branded:
        # Read branding fresh (cheap) so an admin's just-saved logo / accent
        # is reflected in the very next PDF.
        from portal.branding import (
            get_branding,
            get_logo_data_uri,
            inject_pdf_header,
            render_pdf_header,
        )

        brand = get_branding(db)
        header_html = render_pdf_header(
            brand["business_name"],
            brand["accent_color"],
            get_logo_data_uri(db),
        )
        html_to_render = inject_pdf_header(req.html, header_html)

    buf = io.BytesIO()
    try:
        HTML(string=html_to_render, url_fetcher=_no_external_fetcher).write_pdf(buf)
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
    x_portal_app: AppHeader = None,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    me = _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    _require_service(_maybe_resolve_app(request, me, x_portal_app, db), "email")
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

    # Record to the rolling EmailSendLog so /admin/health can show recent
    # outbound mail. Resolve the source app from the request context so the
    # dashboard can attribute the send to the right app. Best-effort: an
    # observability failure must never reject a successful send.
    from portal.health import record_email_send

    app_row = _maybe_resolve_app(request, me, None, db)
    record_email_send(
        db,
        user_id=me.id,
        app_slug=app_row.slug if app_row is not None else "",
        recipient=str(to_list[0]) if to_list else "",
        recipient_count=len(to_list),
        subject=req.subject,
        status="sent",
    )
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
    _require_service(app_row, "storage")
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
    _require_service(app_row, "storage")
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
    _require_service(app_row, "storage")
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
    _require_service(app_row, "storage")
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


# ----- /share -----

class ShareCreateRequest(BaseModel):
    kind: str = Field(default="storage")  # "storage" or "pdf"
    # storage kind:
    key: Optional[str] = None
    # pdf kind:
    html: Optional[str] = None
    # both:
    filename: Optional[str] = Field(default=None, max_length=80)
    ttl_seconds: Optional[int] = None
    max_views: Optional[int] = Field(default=None, ge=0, le=10000)


@router.post("/share/create")
def share_create(
    body: ShareCreateRequest,
    request: Request,
    user: UserDep,
    db: DbDep,
    x_portal_app: AppHeader = None,
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    """Mint a public share URL for either a stored object or a fresh render.

    Requires app context (subdomain, /apps/<slug>/ Referer, or bearer
    + X-Portal-App). The share inherits the creator's identity for storage
    reads — the public /s/<token> handler reads from the creator's
    per-(app, user) namespace, NOT the requester's.
    """
    me = _require_user(user)
    _require_csrf_for_cookie(request, x_csrf)
    app_row = _resolve_app_slug(request, me, x_portal_app, db)

    kind = (body.kind or "storage").strip().lower()
    if kind not in ("storage", "pdf"):
        raise HTTPException(400, "kind must be 'storage' or 'pdf'")

    from portal.shares import (
        create_pdf_share,
        create_storage_share,
        share_url,
    )

    if kind == "storage":
        _require_service(app_row, "storage")
        if not body.key:
            raise HTTPException(400, "storage shares require a 'key'")
        safe_key = _validate_key(body.key)
        ns = _ns_dir(app_row.slug, me.id)
        target = (ns / safe_key).resolve()
        try:
            target.relative_to(ns)
        except ValueError:
            raise HTTPException(404, "key not found")
        if not target.is_file():
            raise HTTPException(404, "key not found")
        row = create_storage_share(
            db,
            app_row=app_row,
            user=me,
            key=safe_key,
            filename=body.filename or "",
            ttl_seconds=body.ttl_seconds,
            max_views=body.max_views,
        )
    else:
        # pdf kind
        _require_service(app_row, "pdf")
        if not body.html:
            raise HTTPException(400, "pdf shares require 'html'")
        try:
            row = create_pdf_share(
                db,
                app_row=app_row,
                user=me,
                html=body.html,
                filename=body.filename or "shared.pdf",
                ttl_seconds=body.ttl_seconds,
                max_views=body.max_views,
            )
        except RuntimeError as e:
            raise HTTPException(500, str(e))

    return {
        "token": row.token,
        "url": share_url(row.token, request.headers.get("host")),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "kind": row.kind,
        "max_views": row.max_views,
    }


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
