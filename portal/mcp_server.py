"""Portal-hosted MCP server.

Exposes a Model Context Protocol endpoint at ``/mcp`` on the portal origin so
Claude (Claude Code, Desktop, claude.ai connectors) connects with a URL + an
admin API token and:

  * **manages apps** — ``whoami``, ``list_apps``, ``get_app``, ``upload_app``,
    ``set_app_enabled`` (Phase A); and
  * **uses apps** — each enabled app's declared ``tools`` (Phase 2) appear as
    MCP tools named ``<slug>__<tool>`` and run via the declarative executor
    (``portal/app_tools.py``) over the portal's own trusted primitives.

Built on the **low-level** ``mcp.server.Server`` (not FastMCP) so the tool list
is computed dynamically from the database on every ``tools/list`` — app tools
appear/disappear as apps are uploaded, enabled, or disabled, with no restart and
no registry bookkeeping.

Optional: needs ``pip install 'pwa-portal[mcp]'`` (bundled in the Docker image).
``portal.main`` imports + serves this only when the toggle resolves on AND this
module imports cleanly (the ``from mcp...`` imports are the feature-detect).

Design notes (validated against mcp 1.27):
  * ``stateless`` + ``json_response`` → each POST is independent, plain JSON.
  * Auth is an ASGI wrapper in FRONT of the handler: it 404s app subdomains,
    validates the bearer (reusing ``deps.authenticate_bearer``), requires an
    admin, and stashes ``{id,email,role}`` on the ASGI ``scope`` for the tools.
  * ``main.py`` registers exact ``/mcp`` + ``/mcp/`` routes (not ``app.mount`` —
    the catch-all GET would shadow a bare ``POST /mcp``) and enters the session
    manager's ``run()`` in the app lifespan.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlmodel import Session, select
from starlette.responses import JSONResponse

from portal.app_tools import AppToolError, run_tool, tool_input_schema
from portal.apps import MAX_ZIP_BYTES, UploadError, install_bundle_from_path
from portal.audit import emit_security_line, record_event
from portal.config import settings
from portal.db import engine
from portal.deps import authenticate_bearer
from portal.middleware import resolve_app_slug_from_host
from portal.models import App, User
from portal.sessions import revoke_all_app_sessions_for_slug

logger = logging.getLogger("uvicorn.error")

# ASGI-scope key where the auth wrapper stashes the resolved admin identity.
_SCOPE_USER = "portal_mcp_user"
# Separator between an app slug and its tool name in the MCP tool name. App
# slugs are kebab-case (no underscores), so the first ``__`` cleanly splits
# ``<slug>__<tool>`` even when the tool name itself contains underscores.
_TOOL_SEP = "__"

_INSTRUCTIONS = (
    "Management + automation interface for a self-hosted PWA Portal — a "
    "single-tenant app host for one small business. Tools named whoami / "
    "list_apps / get_app / upload_app / set_app_enabled manage the portal's "
    "child apps (small PWAs packaged as a .zip with a portal.json manifest); "
    "call whoami first to confirm the connection and that the token is an admin. "
    "Tools named '<slug>__<tool>' are operations a specific app declared (e.g. "
    "render a document and email/share/store it) — call list_apps or get_app to "
    "see an app's tools and their parameters. Everything runs as the token's "
    "owner and is recorded in the portal audit log."
)


# ----- serialization helpers -----

def _app_summary(a: App) -> dict:
    return {
        "slug": a.slug,
        "name": a.name,
        "version": a.version,
        "enabled": bool(a.enabled),
        "description": a.description,
        "services": list(a.services or []),
        "allowed_services": list(a.allowed_services or []),
        "tools": [t.get("name") for t in (a.tools or [])],
        "display_order": a.display_order,
    }


def _app_detail(a: App) -> dict:
    d = _app_summary(a)
    d.update(
        {
            "entry": a.entry,
            "icon": a.icon,
            "csp_strict": bool(a.csp_strict),
            "requested_origins": list(a.requested_origins or []),
            "allowed_origins": list(a.allowed_origins or []),
            "tool_details": list(a.tools or []),
            "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
        }
    )
    return d


# ----- management tool declarations (static schemas) -----

_OBJ = {"type": "object", "additionalProperties": False}
_MGMT_TOOLS = [
    types.Tool(
        name="whoami",
        description="Return the authenticated portal user {id, email, role}. Use "
        "this to confirm the connection works and the token is an admin.",
        inputSchema={**_OBJ, "properties": {}},
    ),
    types.Tool(
        name="list_apps",
        description="List every child app on the portal (slug, name, version, "
        "enabled, services, and the names of any tools it exposes).",
        inputSchema={**_OBJ, "properties": {}},
    ),
    types.Tool(
        name="get_app",
        description="Full detail for one app by slug, including its declared "
        "tools, services, and network origins.",
        inputSchema={
            **_OBJ,
            "properties": {"slug": {"type": "string", "description": "App slug"}},
            "required": ["slug"],
        },
    ),
    types.Tool(
        name="upload_app",
        description="Install a packaged child-app .zip (base64-encoded). "
        "replace=false installs new (fails if the slug exists); replace=true "
        "updates in place, preserving per-user storage. Slug/name/version come "
        "from the bundle's portal.json.",
        inputSchema={
            **_OBJ,
            "properties": {
                "filename": {"type": "string", "description": "e.g. my-app-0.1.0.zip"},
                "zip_base64": {"type": "string", "description": "base64 of the .zip"},
                "replace": {"type": "boolean", "description": "update in place if slug exists"},
            },
            "required": ["filename", "zip_base64"],
        },
    ),
    types.Tool(
        name="set_app_enabled",
        description="Enable or disable an app. Disabling hides it from the "
        "dashboard and revokes open app sessions.",
        inputSchema={
            **_OBJ,
            "properties": {
                "slug": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["slug", "enabled"],
        },
    ),
]
_MGMT_NAMES = {t.name for t in _MGMT_TOOLS}


# ----- ASGI auth wrapper -----

def _client_ip(headers: dict, scope) -> str:
    """Best client IP for the audit / fail2ban line: first X-Forwarded-For hop
    (Caddy sets it), falling back to the ASGI peer address."""
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "-"
    client = scope.get("client")
    return client[0] if client else "-"


async def _send_json_error(scope, receive, send, status: int, message: str) -> None:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    await JSONResponse({"error": message}, status_code=status, headers=headers)(
        scope, receive, send
    )


class _AuthASGIApp:
    """MCP ASGI app enforcing portal-origin + admin-bearer before the handler.

    Registered as an exact Starlette ``Route`` (not a ``Mount``): a Mount only
    matches the ``/mcp/`` form, and the portal's catch-all ``GET
    /{full_path:path}`` would otherwise shadow a bare ``POST /mcp`` with a 405
    before Starlette's slash-redirect runs. A bare ASGI *function* would be
    misread by ``Route`` as a request/response endpoint, so this is a class.

    On success, stashes ``{id, email, role}`` on ``scope[_SCOPE_USER]`` for the
    tools and delegates to the MCP handler. Rejections are plain JSON HTTP
    errors (401/403/404) emitted before any MCP protocol exchange.
    """

    def __init__(self, handler):
        self._handler = handler

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._handler(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }

        # 1. Portal origin only — never reachable on an app subdomain.
        if resolve_app_slug_from_host(headers.get("host", ""), settings.site_url) is not None:
            await _send_json_error(scope, receive, send, 404, "Not found")
            return

        # 2. Admin bearer token — same validation path as the rest of the API.
        #    Failures are written to data/security.log (MCP_AUTH_FAILED) so the
        #    existing fail2ban jail bans bad-token / token-guessing floods.
        authz = headers.get("authorization")
        ip = _client_ip(headers, scope)
        with Session(engine) as db:
            user = authenticate_bearer(db, authz)
            if user is None:
                reason = "bad_token" if (authz and authz.lower().startswith("bearer ")) else "missing_token"
                emit_security_line("MCP_AUTH_FAILED", ip, reason=reason)
                await _send_json_error(
                    scope, receive, send, 401,
                    "Authorization: Bearer <token> required",
                )
                return
            if user.role != "admin":
                emit_security_line("MCP_AUTH_FAILED", ip, reason="not_admin", email=user.email)
                await _send_json_error(
                    scope, receive, send, 403,
                    "An admin API token is required for app management.",
                )
                return
            ident = {"id": user.id, "email": user.email, "role": user.role}

        scope[_SCOPE_USER] = ident
        await self._handler(scope, receive, send)


# ----- builder -----

def build_mcp_app():
    """Build the low-level MCP ``Server`` + the auth-wrapped ASGI app.

    Returns ``(session_manager, asgi_app)``. ``main.py`` registers ``asgi_app``
    at exact ``/mcp`` routes and enters ``session_manager.run()`` in the app
    lifespan (the streamable transport's task group must run for the app's
    lifetime).
    """
    server: Server = Server("pwa-portal", instructions=_INSTRUCTIONS)

    # --- request-context accessors (set by the auth wrapper on the ASGI scope) ---

    def _ctx_request():
        try:
            return server.request_context.request
        except Exception:
            return None

    def _ctx_user() -> dict:
        req = _ctx_request()
        ident = req.scope.get(_SCOPE_USER) if req is not None else None
        if not ident:
            raise ValueError("Not authenticated")
        return ident

    def _ctx_host() -> Optional[str]:
        req = _ctx_request()
        return req.headers.get("host") if req is not None else None

    # --- management handlers ---

    def _do_list_apps() -> dict:
        with Session(engine) as db:
            apps = db.exec(select(App).order_by(App.display_order, App.name)).all()
            return {"apps": [_app_summary(a) for a in apps], "count": len(apps)}

    def _do_get_app(args: dict) -> dict:
        slug = args.get("slug")
        with Session(engine) as db:
            app = db.exec(select(App).where(App.slug == slug)).first()
            if app is None:
                raise ValueError(f"No app with slug '{slug}'.")
            return _app_detail(app)

    async def _do_upload_app(args: dict) -> dict:
        ident = _ctx_user()
        filename = (args.get("filename") or "").strip()
        if not filename.lower().endswith(".zip"):
            raise ValueError("filename must end with .zip")
        try:
            data = base64.b64decode(args.get("zip_base64") or "", validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("zip_base64 is not valid base64")
        if not data:
            raise ValueError("decoded zip is empty")
        if len(data) > MAX_ZIP_BYTES:
            raise ValueError(f"zip exceeds {MAX_ZIP_BYTES // (1024 * 1024)}MB limit")

        fd, tmp_name = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_bytes(data)
        with Session(engine) as db:
            user = db.get(User, ident["id"])
            if user is None:
                tmp_path.unlink(missing_ok=True)
                raise ValueError("Authenticated user no longer exists.")
            try:
                result = await install_bundle_from_path(
                    db, user, tmp_path,
                    allow_replace=bool(args.get("replace", False)),
                    expected_slug=None,
                )
            except UploadError as e:
                raise ValueError(str(e))
            record_event(
                db, actor=user,
                action="app.replace" if result.replaced else "app.upload",
                request=_ctx_request(), target=f"app:{result.slug}",
                details={"name": result.name, "version": result.version, "via": "mcp"},
            )
            return {
                "slug": result.slug, "name": result.name,
                "version": result.version, "replaced": result.replaced,
            }

    def _do_set_enabled(args: dict) -> dict:
        ident = _ctx_user()
        slug = args.get("slug")
        enabled = bool(args.get("enabled"))
        with Session(engine) as db:
            app = db.exec(select(App).where(App.slug == slug)).first()
            if app is None:
                raise ValueError(f"No app with slug '{slug}'.")
            user = db.get(User, ident["id"])
            app.enabled = enabled
            db.add(app)
            db.commit()
            if not app.enabled:
                revoke_all_app_sessions_for_slug(db, app.slug)
            record_event(
                db, actor=user, action="app.toggle", request=_ctx_request(),
                target=f"app:{app.slug}",
                details={"enabled": bool(app.enabled), "via": "mcp"},
            )
            return {"slug": app.slug, "enabled": bool(app.enabled)}

    # --- app-declared tool dispatch ---

    async def _do_app_tool(name: str, args: dict) -> dict:
        ident = _ctx_user()
        host = _ctx_host()
        slug, _, tool_name = name.partition(_TOOL_SEP)
        with Session(engine) as db:
            app = db.exec(
                select(App).where(App.slug == slug, App.enabled == True)  # noqa: E712
            ).first()
            decl = None
            if app is not None:
                decl = next(
                    (t for t in (app.tools or []) if t.get("name") == tool_name), None
                )
        if decl is None:
            raise ValueError(f"Unknown tool: {name}")

        # The executor opens its own DB session, so it's safe to run in a worker
        # thread (it does blocking PDF render / SMTP / disk work).
        try:
            result = await anyio.to_thread.run_sync(
                lambda: run_tool(
                    slug=slug, tool=decl, args=args,
                    user_id=ident["id"], host=host,
                )
            )
        except AppToolError as e:
            raise ValueError(str(e))

        # Best-effort audit: record that the business acted via this app's tool.
        try:
            with Session(engine) as db:
                user = db.get(User, ident["id"])
                record_event(
                    db, actor=user, action="app.tool", request=_ctx_request(),
                    target=f"app:{slug}",
                    details={"tool": tool_name, "delivered": result.get("delivered"), "via": "mcp"},
                )
        except Exception:
            logger.exception("MCP app-tool audit failed for %s", name)
        return result

    # --- MCP protocol handlers ---

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        tools = list(_MGMT_TOOLS)
        with Session(engine) as db:
            apps = db.exec(
                select(App).where(App.enabled == True)  # noqa: E712
                .order_by(App.display_order, App.name)
            ).all()
            for app in apps:
                for decl in (app.tools or []):
                    tools.append(
                        types.Tool(
                            name=f"{app.slug}{_TOOL_SEP}{decl['name']}",
                            description=f"[{app.name}] {decl.get('description', '')}".strip(),
                            inputSchema=tool_input_schema(decl),
                        )
                    )
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> dict:
        if name == "whoami":
            return _ctx_user()
        if name == "list_apps":
            return _do_list_apps()
        if name == "get_app":
            return _do_get_app(arguments)
        if name == "upload_app":
            return await _do_upload_app(arguments)
        if name == "set_app_enabled":
            return _do_set_enabled(arguments)
        if _TOOL_SEP in name:
            return await _do_app_tool(name, arguments)
        raise ValueError(f"Unknown tool: {name}")

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        # Caddy fronts the portal and only routes configured hosts, the portal
        # does its own Host dispatch, and the wrapper above requires a secret
        # bearer + 404s app subdomains. Disable the SDK's redundant rebinding
        # check rather than carry a brittle host:port allowlist across envs.
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    asgi_app = _AuthASGIApp(manager.handle_request)
    return manager, asgi_app
