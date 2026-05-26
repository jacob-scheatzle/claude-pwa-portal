"""HTTP middleware for the portal.

``HostDispatchMiddleware`` reads the ``Host`` header on every request and, if
the host matches ``*.apps.<SITE_URL>``, records the resolved app slug on
``request.state.app_slug``. Downstream route handlers branch on this state to
serve child-app content (subdomain origin) versus portal content (root origin).

Why a middleware rather than per-route checks: FastAPI / Starlette does not
support host-based route dispatch natively, and the ``request.state.app_slug``
attribute then becomes available to every handler in the app without any
opt-in plumbing. The same routes can serve different content depending on
which subdomain the request arrived on.
"""

from __future__ import annotations

import re
from typing import Optional

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
