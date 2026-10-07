"""HTTP middleware for the portal.

``HostDispatchMiddleware`` reads the ``Host`` header on every request and, if
the host matches ``*.apps.<SITE_URL>``, records the resolved app slug on
``request.state.app_slug``. Downstream route handlers branch on this state to
serve child-app content (subdomain origin) versus portal content (root origin).

``AppHostGateMiddleware`` confines an app subdomain to the child-app surface
(the SDK, its JSON endpoints, public forms, and the app's own bundle) so the
portal's login / admin / management routes never answer on an origin the
untrusted app controls.

``ChildAppCSPMiddleware`` runs on the response path. For requests that
resolved to a child-app subdomain, it builds a per-app Content-Security-Policy
from the matching ``App.allowed_origins`` row and sets the header on the
response. Caddy intentionally does not set CSP for ``*.apps.<SITE_URL>``
anymore; the portal owns the header because the allowed external origins
vary per app.

Why a middleware rather than per-route checks: FastAPI / Starlette does not
support host-based route dispatch natively, and the ``request.state.app_slug``
attribute then becomes available to every handler in the app without any
opt-in plumbing. The same routes can serve different content depending on
which subdomain the request arrived on.
"""

from __future__ import annotations

import re
import secrets
from typing import Iterable, Optional

from sqlmodel import Session, select
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from portal.config import settings


# Kebab-case slug matcher: lowercase alphanumerics with internal single
# hyphens. Duplicated from ``portal.apps.SLUG_RE`` (the manifest validator's
# copy is the authority) rather than imported to avoid a circular import —
# ``portal.apps`` already imports a fair amount of the portal at startup and
# pulling middleware into its dependency graph is asking for trouble. This
# is defense-in-depth: any slug a request could legitimately resolve has
# already cleared the canonical regex at install time.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Matches a portal-origin ``/apps/<slug>/<anything>`` request path. The slug
# rule is the same as ``_SLUG_RE`` so any path that resolves to a real app
# at install-time will resolve here too. Used to apply per-app CSP in
# same-origin mode (where the child app is served at /apps/<slug>/<entry>
# rather than a dedicated subdomain) and to the portal-origin launcher
# wrapper in subdomain mode (also at /apps/<slug>/).
_APPS_PATH_RE = re.compile(r"^/apps/([a-z0-9]+(?:-[a-z0-9]+)*)(?:/|$)")


def _strip_port(host: str) -> str:
    """Drop the ``:port`` suffix from a Host header value.

    Accepts ``example.com:8000`` and returns ``example.com``. Preserves bare
    hostnames. We never receive IPv6 literals in the Host header here (the
    portal is fronted by Caddy, which always passes a hostname), so the naive
    ``rsplit(":", 1)`` is safe.
    """
    if ":" in host and not host.startswith("["):
        return host.rsplit(":", 1)[0]
    return host


def resolve_app_slug_from_host(host: str, site_url: str) -> Optional[str]:
    """Return the slug if ``host`` matches ``<slug>.apps.<site_url>``, else None.

    Host comparisons are case-insensitive (DNS is). The site URL must be a
    bare hostname (no scheme, no path) — Caddy / config validation should
    ensure that, but if not we still get a sane "no match" result here.
    """
    if not host or not site_url:
        return None
    host = _strip_port(host).lower().rstrip(".")
    base = f".apps.{site_url.lower().rstrip('.')}"
    if not host.endswith(base):
        return None
    slug = host[: -len(base)]
    if not _SLUG_RE.match(slug):
        # Empty slug, deeper subdomain like ``a.b.apps.example.com``, or any
        # malformed character — punctuation (``<script>``), leading/trailing
        # hyphens, double hyphens, etc. Uppercase letters in the Host header
        # are normalized to lowercase first (DNS is case-insensitive, so
        # rejecting them would be wrong). Manifest validation enforces the
        # same regex at install time, so any legitimate app's slug clears
        # this check — anything else is bogus input and should not propagate
        # to handlers as ``request.state.app_slug``.
        return None
    return slug


class HostDispatchMiddleware(BaseHTTPMiddleware):
    """Tag each request with ``request.state.app_slug``.

    For requests to ``<slug>.apps.<SITE_URL>``, sets ``app_slug`` to the
    resolved slug. For requests to any other host (the portal origin, health
    checks, etc.), sets ``app_slug`` to ``None``.
    """

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        slug = resolve_app_slug_from_host(host, settings.site_url)
        request.state.app_slug = slug
        return await call_next(request)


# Route prefix that serves a child app's own bundle on its subdomain. Never
# requested directly: ``AppHostGateMiddleware`` rewrites app-host GETs onto it.
APP_BUNDLE_ROUTE_PREFIX = "/__app_bundle__"

# The ONLY portal routes that answer on an app subdomain: the SDK, the JSON
# endpoints the SDK calls, and the public intake forms. Everything else is the
# app's own bundle.
_APP_HOST_PORTAL_PATHS = frozenset({
    "/portal-sdk.js",
    "/api/v1/user/me",
    "/api/v1/csrf-token",
    "/api/v1/session/exchange",
    "/api/v1/pdf/render",
    "/api/v1/email/send",
    "/api/v1/storage",
    "/api/v1/share/create",
})
_APP_HOST_PORTAL_PREFIXES = ("/api/v1/storage/",)
_APP_HOST_FORM_RE = re.compile(r"^/forms/[^/]+/?$")


def is_portal_path_on_app_host(path: str) -> bool:
    """True if ``path`` is a portal route that child-app subdomains may reach."""
    return (
        path in _APP_HOST_PORTAL_PATHS
        or path.startswith(_APP_HOST_PORTAL_PREFIXES)
        or bool(_APP_HOST_FORM_RE.match(path))
    )


class AppHostGateMiddleware:
    """Confine a child-app subdomain to the child-app surface.

    Every portal route is registered once and answers on any Host, so without
    this gate ``<slug>.apps.<SITE_URL>`` would also serve the portal's login,
    setup, admin, and app-management routes — on an origin the untrusted app's
    own JavaScript controls. An admin who opened the app (or signed in on its
    ``/login``) would hand that JavaScript a same-origin admin session.

    On an app host, a path in the allowlist above passes through unchanged. Any
    other GET/HEAD is rewritten onto ``APP_BUNDLE_ROUTE_PREFIX`` so it serves
    the app's bundle — which also stops portal routes like ``/sw.js`` and
    ``/manifest.webmanifest`` from shadowing the app's own files. Any other
    method gets a 404. Portal-origin requests pass through untouched.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because it has to rewrite the
    path before routing. Must sit inside ``HostDispatchMiddleware``, which sets
    the slug it reads.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        slug = (scope.get("state") or {}).get("app_slug")
        path = scope["path"]
        if not slug or is_portal_path_on_app_host(path):
            return await self.app(scope, receive, send)
        if scope["method"] not in ("GET", "HEAD"):
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            return await response(scope, receive, send)
        scope = dict(scope)
        scope["path"] = APP_BUNDLE_ROUTE_PREFIX + path
        raw_path = scope.get("raw_path")
        if raw_path is not None:
            scope["raw_path"] = APP_BUNDLE_ROUTE_PREFIX.encode() + raw_path
        return await self.app(scope, receive, send)


_MB = 1024 * 1024
# Request-body ceilings, sized to the largest legitimate payload on each path
# (each a little above the handler's own cap, so the handler's clearer error
# still wins for an honest client). Everything else — login, setup, forms,
# OAuth, admin forms — gets the 1 MB default.
_BUNDLE_UPLOAD_PATH_RE = re.compile(
    r"^(/admin/apps/upload|/api/v1/apps/upload|/admin/apps/[^/]+/replace|/api/v1/apps/[^/]+)$"
)
_DEFAULT_BODY_LIMIT = 1 * _MB


def body_limit_for(path: str) -> int:
    """Largest request body accepted on ``path``, in bytes."""
    if _BUNDLE_UPLOAD_PATH_RE.match(path) or path in ("/mcp", "/mcp/"):
        return 80 * _MB  # 50 MB zip (MAX_ZIP_BYTES), base64-inflated over MCP
    if path.startswith("/api/v1/storage/"):
        return 11 * _MB  # 10 MB object (MAX_OBJECT_BYTES)
    if path in ("/api/v1/pdf/render", "/api/v1/share/create", "/api/v1/email/send"):
        return 5 * _MB  # 2 MB HTML / 1 MB bodies, JSON-escaped
    if path == "/admin/settings":
        return 2 * _MB  # logo + favicon uploads (512 KB each)
    return _DEFAULT_BODY_LIMIT


class BodySizeLimitMiddleware:
    """Reject request bodies over the path's ceiling with a 413.

    Starlette parses multipart and urlencoded bodies — spooling them to disk
    or memory — before any auth dependency runs, so without a ceiling an
    anonymous client can push a body of any size at ``/login`` or a public
    form. A declared ``Content-Length`` over the limit is refused before the
    body is read; a chunked body is counted as it streams and cut off at the
    limit (the 413 raised from ``receive`` surfaces through the handler that
    was reading it). Caddy enforces a coarser cap in front of this.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = body_limit_for(scope["path"])
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                    return await response(scope, receive, send)
                break

        received = 0
        exceeded = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise HTTPException(413, "Request body too large")
            return message

        # FastAPI turns any error raised while it parses a body into a generic
        # 400, so once we've cut the body off, answer with the 413 ourselves.
        too_large = JSONResponse({"detail": "Request body too large"}, status_code=413)

        async def limited_send(message):
            if not exceeded:
                return await send(message)
            if message["type"] == "http.response.start":
                await send({"type": "http.response.start", "status": 413,
                            "headers": too_large.raw_headers})
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                await send({"type": "http.response.body", "body": too_large.body})

        return await self.app(scope, limited_receive, limited_send)


class APINoStoreMiddleware:
    """Mark every ``/api/v1/*`` response ``Cache-Control: no-store``.

    Storage GETs carry ETag / Last-Modified but no caching directive, so a
    browser may reuse them heuristically — and on a shared device the next
    person in the same app could be served the previous user's copy of the
    same key. Nothing under /api/v1 is safe to cache.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            return await self.app(scope, receive, send)

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_no_store)


# CSP for child-app subdomains. Matches the structure of the legacy Caddy
# header line that used to live in the ``*.apps.{$SITE_URL}`` block:
#
#   default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:
#   connect-src 'self' <approved external origins...>
#   frame-ancestors https://<SITE_URL>
#   base-uri 'self'
#   form-action 'self'
#
# The only piece that varies per app is ``connect-src``: same-origin XHR /
# fetch always allowed; external HTTPS endpoints opt-in via the manifest's
# ``permissions.network`` declaration and an admin's per-app approval.
def build_child_app_csp(
    allowed_origins: Iterable[str],
    *,
    frame_ancestors: str,
    frame_src: Optional[str] = None,
    strict: bool = False,
    nonce: Optional[str] = None,
) -> str:
    """Render the per-app CSP header value.

    ``allowed_origins`` is a list of normalized HTTPS origins (already
    validated by the manifest schema); malformed entries are skipped
    defensively rather than risk emitting an invalid CSP that the browser
    would silently drop. ``frame_ancestors`` is rendered verbatim — caller
    supplies either ``'self'`` (portal-origin launcher / same-origin app)
    or one or more ``https://...`` / ``http://...`` origins (subdomain app
    embedded by the portal shell).

    ``frame_src`` is emitted verbatim if non-empty. Required on the
    *launcher* response in per-app-origin mode: the launcher at
    ``/apps/<slug>/`` on the portal origin embeds an iframe pointing at
    ``<slug>.apps.<SITE_URL>`` (a different origin), so without an
    explicit ``frame-src`` directive the CSP falls back to ``default-src
    'self'`` and the browser refuses to load the iframe. Same-origin
    mode doesn't need it (the iframe target is same-origin, matched by
    the default).

    ``strict=True`` drops ``'unsafe-inline'`` / ``'unsafe-eval'`` and emits
    ``script-src``/``style-src`` with the given nonce (required when strict).
    Apps opt into this via the manifest's ``permissions.csp_strict``; the
    portal substitutes ``{{NONCE}}`` placeholders in served HTML so
    legitimate inline scripts/styles can carry the matching attribute.
    """
    origins = ["'self'"]
    for o in allowed_origins or []:
        if isinstance(o, str) and o.startswith("https://"):
            origins.append(o)
    connect = " ".join(origins)

    # Optional directive — rendered only when the caller supplies a value,
    # so existing CSP semantics for subdomain responses (no frame-src,
    # default-src governs) stay byte-for-byte unchanged.
    frame_src_directive = (
        f"frame-src {frame_src}; " if frame_src else ""
    )

    if strict:
        if not nonce:
            raise ValueError("strict CSP requires a nonce")
        # 'strict-dynamic' isn't included on purpose: we want apps to whitelist
        # their resources explicitly. data:/blob: stay allowed for images and
        # SDK-generated downloads (PDF blobs, etc.).
        script_src = f"'self' 'nonce-{nonce}'"
        style_src = f"'self' 'nonce-{nonce}'"
        img_src = "'self' data: blob:"
        return (
            "default-src 'self'; "
            f"script-src {script_src}; "
            f"style-src {style_src}; "
            f"img-src {img_src}; "
            "font-src 'self' data:; "
            f"connect-src {connect}; "
            f"{frame_src_directive}"
            f"frame-ancestors {frame_ancestors}; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "object-src 'none'"
        )

    return (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        f"connect-src {connect}; "
        f"{frame_src_directive}"
        f"frame-ancestors {frame_ancestors}; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


def _subdomain_frame_ancestors(site_url: str, http_only: bool) -> str:
    """frame-ancestors value for a child-app subdomain response.

    Under HTTP_ONLY the portal might be reached as http:// (local testing)
    or https:// (behind a TLS-terminating LB) — only one matches the real
    document URL at runtime, but listing both keeps the iframe wrapper
    working under either deployment shape. Under TLS-front Caddy (the
    default), only https:// applies.
    """
    if http_only:
        return f"http://{site_url} https://{site_url}"
    return f"https://{site_url}"


def _launcher_frame_src(
    slug: str, site_url: str, cookies_secure: bool, http_only: bool
) -> str:
    """frame-src value for the launcher embedding a child-app subdomain.

    Mirrors how ``portal.apps.serve_app_index`` constructs the iframe
    URL. Scoped to the specific slug (not a wildcard) so an HTML-injection
    bug in the launcher template can't pivot to embedding a sibling app's
    subdomain. Under HTTP_ONLY mode the iframe is reachable as either
    ``http://`` (local testing) or ``https://`` (behind a TLS-terminating
    LB), so both schemes are listed to keep the wrapper working in either
    deployment shape.
    """
    origin = f"{slug}.apps.{site_url}"
    if http_only:
        return f"http://{origin} https://{origin}"
    if cookies_secure:
        return f"https://{origin}"
    return f"http://{origin}"


class ChildAppCSPMiddleware(BaseHTTPMiddleware):
    """Stamp a per-app Content-Security-Policy on child-subdomain responses.

    Runs after ``HostDispatchMiddleware`` set ``request.state.app_slug``. If
    that slug is present, looks up the App row and writes a CSP header whose
    ``connect-src`` lists the admin-approved external origins for that app.
    Same-origin requests (the portal SDK, the app's own assets) are always
    allowed via ``'self'``. Requests to any other host (the portal shell at
    the bare ``SITE_URL``, the health endpoint) pass through untouched —
    Caddy still owns the portal-shell CSP.

    DB access is best-effort. If the lookup fails (transient error, dropped
    connection, etc.) we still emit a CSP with an empty external list, which
    is the safer side to fall on — the app gets same-origin-only behavior
    rather than no CSP at all.
    """

    def __init__(self, app: ASGIApp, *, engine):
        super().__init__(app)
        # Stash the engine so the middleware can open its own short-lived
        # Session without depending on FastAPI's request-scoped get_db. We
        # only need a single SELECT per child-app request.
        self._engine = engine

    async def dispatch(self, request: Request, call_next):
        # Two CSP contexts where per-app rules apply:
        #
        #  1. ``app_slug`` set by HostDispatchMiddleware — request arrived on
        #     ``<slug>.apps.<SITE_URL>``. The portal shell at the bare
        #     SITE_URL iframes this response, so ``frame-ancestors`` lists
        #     the portal origin.
        #
        #  2. ``/apps/<slug>/...`` on the portal origin — covers the
        #     launcher wrapper in both modes, AND the actual child app
        #     bundle in same-origin mode (``CHILD_APPS_SAME_ORIGIN=true``).
        #     The launcher embeds the entry file (or the subdomain iframe)
        #     same-origin, so ``frame-ancestors 'self'`` is the right rule.
        slug = getattr(request.state, "app_slug", None)
        on_subdomain = bool(slug)
        if not on_subdomain:
            m = _APPS_PATH_RE.match(request.url.path)
            if m:
                slug = m.group(1)

        # No candidate slug at all (the portal shell, /health, /static, …) —
        # nothing per-app to do; let the response through untouched.
        if not slug:
            return await call_next(request)

        # Pre-resolve the App row on the request path so file handlers can
        # read ``request.state.csp_nonce`` to substitute ``{{NONCE}}`` in
        # HTML before the response goes out. Strict CSP only applies on the
        # subdomain — the portal-origin launcher carries its own inline
        # scripts (base.html theme toggle, etc.) and would break under
        # strict mode. Same-origin mode keeps the permissive CSP for the
        # same reason.
        #
        # If the slug doesn't resolve to a real App row (bogus subdomain, a
        # ``/apps/<unknown>/`` probe), short-circuit: skip the per-app CSP
        # entirely and pass the response through. Real apps below are handled
        # byte-for-byte as before.
        allowed: list[str] = []
        csp_strict = False
        nonce: Optional[str] = None
        try:
            with Session(self._engine) as db:
                from portal.models import App

                app_row = db.exec(select(App).where(App.slug == slug)).first()
            if app_row is None:
                return await call_next(request)
            allowed = list(app_row.allowed_origins or [])
            # A ``/forms/*`` request on the subdomain is a PORTAL-rendered
            # page (base.html chrome with inline styles + the theme script),
            # not the app's own bundle — so keep it on the permissive CSP even
            # for a csp_strict app, or its inline styles/theme would be blocked
            # and the public form would render broken.
            is_form_page = request.url.path.startswith("/forms/")
            if (
                on_subdomain
                and not is_form_page
                and bool(getattr(app_row, "csp_strict", False))
            ):
                csp_strict = True
        except Exception:
            # DB hiccup — fall back to the legacy permissive CSP rather
            # than block the response. Apps stay functional; the worst
            # case is one request without strict CSP.
            allowed = []
            csp_strict = False

        if csp_strict:
            # token_urlsafe yields URL-safe base64; the CSP spec accepts any
            # base64 character set in the nonce-source. 16 bytes (~22 chars)
            # is well above the 128-bit-entropy bar.
            nonce = secrets.token_urlsafe(16)
            request.state.csp_nonce = nonce

        response = await call_next(request)

        # ``slug`` is guaranteed truthy here (bogus / unknown slugs returned
        # early above), so this response belongs to a real app — stamp its CSP.
        frame_src: Optional[str] = None
        if on_subdomain:
            frame_ancestors = _subdomain_frame_ancestors(
                settings.site_url, bool(settings.http_only)
            )
        else:
            frame_ancestors = "'self'"
            # The launcher (portal-origin ``/apps/<slug>/...``) in
            # per-app-origin mode embeds the child app via an iframe to
            # ``<slug>.apps.<SITE_URL>`` — a DIFFERENT origin. Without a
            # ``frame-src`` directive the browser falls back to
            # ``default-src 'self'`` and refuses to load the iframe with
            # a CSP violation, surfacing in DevTools as "content is
            # blocked" / "blocks some resources". Same-origin mode
            # doesn't need this because the iframe target is same-origin
            # and matches ``default-src 'self'`` already.
            if not bool(settings.child_apps_same_origin):
                frame_src = _launcher_frame_src(
                    slug,
                    settings.site_url,
                    bool(settings.cookies_secure),
                    bool(settings.http_only),
                )
        response.headers["Content-Security-Policy"] = build_child_app_csp(
            allowed,
            frame_ancestors=frame_ancestors,
            frame_src=frame_src,
            strict=csp_strict,
            nonce=nonce,
        )
        return response
