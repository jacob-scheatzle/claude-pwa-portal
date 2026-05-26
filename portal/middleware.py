"""HTTP middleware for the portal.

``HostDispatchMiddleware`` reads the ``Host`` header on every request and, if
the host matches ``*.apps.<SITE_URL>``, records the resolved app slug on
``request.state.app_slug``. Downstream route handlers branch on this state to
serve child-app content (subdomain origin) versus portal content (root origin).

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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
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
            f"frame-ancestors {frame_ancestors}; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "object-src 'none'"
        )

    return (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        f"connect-src {connect}; "
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

        # Pre-resolve the App row on the request path so file handlers can
        # read ``request.state.csp_nonce`` to substitute ``{{NONCE}}`` in
        # HTML before the response goes out. Strict CSP only applies on the
        # subdomain — the portal-origin launcher carries its own inline
        # scripts (base.html theme toggle, etc.) and would break under
        # strict mode. Same-origin mode keeps the permissive CSP for the
        # same reason.
        allowed: list[str] = []
        csp_strict = False
        nonce: Optional[str] = None
        if slug:
            try:
                with Session(self._engine) as db:
                    from portal.models import App

                    app_row = db.exec(select(App).where(App.slug == slug)).first()
                    if app_row is not None:
                        allowed = list(app_row.allowed_origins or [])
                        if on_subdomain and bool(getattr(app_row, "csp_strict", False)):
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

        if not slug:
            return response

        if on_subdomain:
            frame_ancestors = _subdomain_frame_ancestors(
                settings.site_url, bool(settings.http_only)
            )
        else:
            frame_ancestors = "'self'"
        response.headers["Content-Security-Policy"] = build_child_app_csp(
            allowed,
            frame_ancestors=frame_ancestors,
            strict=csp_strict,
            nonce=nonce,
        )
        return response
