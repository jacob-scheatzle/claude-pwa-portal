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
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone
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
from portal.models import App, ScheduledRun, User
from portal.scheduler import FREQUENCIES, compute_next_run, fire_schedule
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
    "owner and is recorded in the portal audit log. The list_schedules / "
    "create_schedule / set_schedule_enabled / delete_schedule / run_schedule "
    "tools run an app's tool automatically on a daily/weekly/monthly cadence. "
    "Before building or changing an app, call authoring_guide for the manifest "
    "schema, the tool DSL (params, render templates, deliver kinds, array line "
    "items), and public intake forms."
)

# Self-contained app-authoring guide returned by the ``authoring_guide`` tool so
# an MCP-connected Claude can build cutting-edge apps WITHOUT the local
# pwa-portal-app skill or the repo checked out. Kept in sync with SKILL.md /
# docs/mcp.md / docs/app-authoring.md.
_AUTHORING_GUIDE = """# Authoring a PWA Portal app

An app is a small folder of files. Over MCP the reliable way to install it is
`create_app`: pass `files` as a map of {filename: text content} (include
`portal.json` and the entry HTML) and the portal assembles + installs the bundle
— no zip, no base64. The icon is optional: pass `icon_base64`, include an `.svg`
icon in `files`, or let one be generated. `upload_app` is the alternative when
you already have a packaged `.zip` (base64 into `zip_base64`), but large bundles
can be truncated over MCP — prefer the web UI for those. `replace=true` updates
in place and preserves per-user storage. The bundle root holds at least:

- `portal.json` — the manifest (below)
- `index.html` — the entry page (HTML/CSS/JS; load the SDK with
  `<script src="/portal-sdk.js"></script>`)
- `icon.png` — a 192x192 icon

## portal.json

```json
{
  "slug": "my-app",                          // kebab-case, 2-40 chars, unique
  "name": "My App",
  "version": "1.0.0",
  "description": "One line.",
  "icon": "icon.png",
  "entry": "index.html",
  "services": ["pdf", "email", "storage"],   // SDK + tool services this app uses
  "permissions": { "network": [] },          // external https origins the UI fetches
  "tools": [ ... ],                          // optional MCP tools (below)
  "forms": [ ... ]                           // optional public intake forms (below)
}
```

## Tools — what Claude runs over MCP

A tool is a *declaration*, not code: the portal renders an HTML template you
supply to a PDF, then shares / downloads / emails / stores it. The app's own
code never runs server-side. Each enabled app's tools appear as `<slug>__<tool>`.

```json
{
  "name": "create_invoice",                  // snake_case, unique in the app
  "description": "What it does (shown to Claude).",
  "params": [
    {"name": "customer", "type": "string", "required": true},
    {"name": "items", "type": "array", "required": true,
     "fields": [
       {"name": "description", "type": "string", "required": true},
       {"name": "qty", "type": "number", "required": true},
       {"name": "rate", "type": "number", "required": true}
     ]},
    {"name": "tax_rate", "type": "number", "required": false}
  ],
  "render": {
    "html": "<table>{% set ns = namespace(t=0) %}{% for it in items %}<tr><td>{{ it.description }}</td><td>${{ '%.2f'|format(it.qty * it.rate) }}</td></tr>{% set ns.t = ns.t + it.qty * it.rate %}{% endfor %}</table><p>Total: ${{ '%.2f'|format(ns.t) }}</p>",
    "filename": "invoice.pdf",
    "branded": true
  },
  "deliver": {"kind": "share", "ttl_days": 60}
}
```

### params
- `type` is `string` | `number` | `boolean`, or `array` for a list of objects.
- An `array` param adds `fields: [{name, type, required, description}]` — the
  shape of each element (scalars only; no nested arrays). Great for line items.

### render.html — a sandboxed, autoescaping Jinja template
- `{{ param }}` is substituted HTML-**escaped**, so a value can't break the doc.
- Supported: `{% for it in items %}`, `{% if x %}`, arithmetic
  (`it.qty * it.rate`), `{{ '%.2f'|format(n) }}`, running totals with
  `{% set ns = namespace(t=0) %}` + `{% set ns.t = ns.t + ... %}`, and
  `{{ x | default(0, true) }}` for optional numbers.
- No external resources are fetched — embed images/fonts as `data:` URIs.
- `branded: true` prepends the portal's business-name/logo header.

### deliver.kind
- `share` -> public link `{url, expires_at}` (`ttl_days`, max 90)
- `download` -> `{filename, pdf_base64}`
- `store` -> save the PDF in storage at `key` (may use `{{ param }}`) -> `{key, size}`
- `email` -> send the rendered HTML to `to` (templated) with `subject` -> `{count}`
  (sends HTML as the body, not a PDF attachment)

### services a tool needs (must appear in the manifest's `services`)
- `share` / `download` -> `pdf`
- `store` -> `pdf` + `storage`
- `email` -> `email`
The manifest is rejected at upload if a tool uses an undeclared service.

## Forms — public intake (optional)

Declare `forms` to collect input from people who are NOT signed in (a customer
quote request, a job intake). Each form is served on the app's own origin
(`<slug>.apps.<SITE_URL>/forms/<form>`); every submission is stored for the owner
(Admin -> Submissions) and included in the data export. Forms are declarative —
no app code runs.

```json
"forms": [
  {
    "name": "quote_request",                 // snake_case, unique, used in the URL
    "title": "Request a quote",
    "description": "Tell us about your project.",
    "fields": [
      {"name": "full_name", "label": "Your name", "type": "text", "required": true},
      {"name": "email", "label": "Email", "type": "email", "required": true},
      {"name": "details", "label": "Details", "type": "textarea"}
    ],
    "notify_email": "owner@example.com",      // optional: emailed on each submission
    "success_message": "Thanks — we'll be in touch."
  }
]
```

Field `type` is `text` | `email` | `tel` | `number` | `textarea`. Forms need no
`services`. The public endpoint is rate-limited per IP and has a spam honeypot.

## Notes
- Tool calls run as the connected admin user, in that user's per-app storage,
  send email as the business, and share the SDK's per-user PDF/email rate limits.
- Recurring runs: any tool can be scheduled to run daily/weekly/monthly with the
  `create_schedule` tool (or Admin -> Schedules) — great for periodic reports.
- The `invoice-gen`, `quote-builder`, and `work-order` example apps in the repo
  ship full, working line-item tools to copy from.
"""


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


# ----- app bundle assembly from text files (the create_app tool) -----

def generate_default_icon_svg(name: str) -> str:
    """A generated fallback icon following the skill's icon rules: a 192×192
    square in the portal accent (#059669) with the app's initial in white.

    The skill prefers a hand-drawn pictogram, but the MCP create_app path has no
    way to draw one server-side — this keeps a freshly-created app from falling
    back to the generic portal placeholder. A real pictogram can be uploaded
    later. SVG (a supported icon format) so no image library is needed.
    """
    letter = (name.strip()[:1] or "A").upper()
    letter = (
        letter.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" '
        'viewBox="0 0 192 192" role="img">'
        '<rect width="192" height="192" rx="36" fill="#059669"/>'
        '<text x="96" y="104" text-anchor="middle" dominant-baseline="central" '
        'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
        f'font-size="108" font-weight="700" fill="#ffffff">{letter}</text>'
        "</svg>"
    )


def build_app_zip(files: Any, icon_base64: Optional[str] = None) -> bytes:
    """Assemble an installable child-app .zip from text source files.

    ``files`` maps filename → text content and must include ``portal.json`` (and
    the entry file it names). The icon is resolved in order: ``icon_base64`` (a
    binary icon), else an ``.svg`` icon already present in ``files``, else a
    generated fallback (see ``generate_default_icon_svg``) — and ``portal.json``'s
    ``icon`` field is updated to match. Returns the zip bytes; raises
    ``ValueError`` on malformed input. Kept module-level so it's unit-testable
    independent of the MCP request context.
    """
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty object of {filename: text content}")
    clean: dict[str, str] = {}
    for fname, content in files.items():
        if not isinstance(fname, str) or not isinstance(content, str):
            raise ValueError("each file must be a string filename mapped to string content")
        parts = Path(fname).parts
        if fname.startswith("/") or "\\" in fname or ".." in parts or not parts:
            raise ValueError(f"unsafe filename in files: {fname!r}")
        clean[fname] = content
    if "portal.json" not in clean:
        raise ValueError("files must include portal.json")
    try:
        manifest = json.loads(clean["portal.json"])
    except Exception as e:
        raise ValueError(f"portal.json is not valid JSON: {e}")
    if not isinstance(manifest, dict):
        raise ValueError("portal.json must be a JSON object")

    icon_name = (manifest.get("icon") or "").strip()
    icon_bytes: Optional[bytes] = None
    if icon_base64:
        try:
            icon_bytes = base64.b64decode("".join(icon_base64.split()), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("icon_base64 is not valid base64")
        if not icon_name:
            icon_name = "icon.png"
        clean.pop(icon_name, None)  # a supplied binary icon wins over any text version
        manifest["icon"] = icon_name
    elif icon_name and icon_name in clean and icon_name.lower().endswith(".svg"):
        pass  # an SVG icon was provided inline as a text file
    else:
        icon_name = "icon.svg"
        manifest["icon"] = icon_name
        clean[icon_name] = generate_default_icon_svg(
            str(manifest.get("name") or manifest.get("slug") or "App")
        )

    clean["portal.json"] = json.dumps(manifest, indent=2)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, content in clean.items():
            z.writestr(fname, content)
        if icon_bytes is not None:
            z.writestr(icon_name, icon_bytes)
    data = buf.getvalue()
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError(f"assembled bundle exceeds {MAX_ZIP_BYTES // (1024 * 1024)}MB limit")
    return data


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
        name="authoring_guide",
        description="Return the full guide for building a portal app and declaring "
        "tools — manifest schema, the tool DSL (params incl. array line items, "
        "render templates, deliver kinds), and a worked example. Read this before "
        "creating or modifying an app, especially without the local skill.",
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
        "from the bundle's portal.json. Call authoring_guide first for the "
        "manifest schema and tool DSL. NOTE: zip_base64 must be the actual file "
        "bytes base64-encoded (e.g. `base64 -w0 app.zip`) — it cannot be "
        "hand-generated, and large bundles can be truncated over MCP, so prefer "
        "the web UI (Admin → Apps → Upload) for anything but a small app.",
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
        name="create_app",
        description="Create or update a child app from its source files as TEXT — "
        "the reliable way to ship an app over MCP (no zip, no base64 except an "
        "optional icon). 'files' maps each filename to its text content and MUST "
        "include portal.json and the entry file it names (e.g. index.html). The "
        "portal assembles the bundle and installs it. Provide a 192x192 icon via "
        "icon_base64 (PNG/JPEG/WebP) or include an .svg icon in 'files'; if "
        "neither is given a default icon is generated. replace=true updates an "
        "existing slug in place (preserves per-user storage). Call authoring_guide "
        "first for the manifest schema and tool DSL.",
        inputSchema={
            **_OBJ,
            "properties": {
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "filename -> text content; must include portal.json + the entry HTML",
                },
                "icon_base64": {
                    "type": "string",
                    "description": "optional base64 of a 192x192 PNG/JPEG/WebP icon; omit to auto-generate",
                },
                "replace": {"type": "boolean", "description": "update in place if the slug exists"},
            },
            "required": ["files"],
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
    types.Tool(
        name="list_schedules",
        description="List recurring scheduled tool runs (id, app, tool, cadence, "
        "enabled, args, last/next run).",
        inputSchema={**_OBJ, "properties": {}},
    ),
    types.Tool(
        name="create_schedule",
        description="Schedule one of an app's tools to run automatically on a "
        "cadence; its output is delivered through the tool's own deliver action "
        "(email / store / share). The run acts as the authenticated user. Cadence "
        "is daily / weekly / monthly at a UTC hour:minute (day_of_week 0=Mon..6=Sun "
        "for weekly; day_of_month 1-28 for monthly). 'args' are the tool's "
        "parameters — same shape as calling the tool directly.",
        inputSchema={
            **_OBJ,
            "properties": {
                "app_slug": {"type": "string"},
                "tool_name": {"type": "string"},
                "args": {"type": "object", "description": "tool parameters"},
                "frequency": {"type": "string", "enum": list(FREQUENCIES)},
                "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                "day_of_week": {"type": "integer", "minimum": 0, "maximum": 6},
                "day_of_month": {"type": "integer", "minimum": 1, "maximum": 28},
                "label": {"type": "string"},
            },
            "required": ["app_slug", "tool_name", "frequency"],
        },
    ),
    types.Tool(
        name="set_schedule_enabled",
        description="Enable or pause a schedule by id.",
        inputSchema={
            **_OBJ,
            "properties": {"id": {"type": "integer"}, "enabled": {"type": "boolean"}},
            "required": ["id", "enabled"],
        },
    ),
    types.Tool(
        name="delete_schedule",
        description="Delete a schedule by id.",
        inputSchema={**_OBJ, "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    ),
    types.Tool(
        name="run_schedule",
        description="Run a schedule's tool immediately. Does not change its cadence.",
        inputSchema={**_OBJ, "properties": {"id": {"type": "integer"}}, "required": ["id"]},
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
    headers = None
    if status == 401:
        # Point OAuth-capable clients (the Claude.ai connector) at the
        # protected-resource metadata so they can discover the authorization
        # server and start the OAuth flow. Falls back to a bare Bearer challenge
        # if OAuth isn't available.
        challenge = "Bearer"
        try:
            from portal.oauth import resource_metadata_url

            challenge = f'Bearer resource_metadata="{resource_metadata_url()}"'
        except Exception:
            pass
        headers = {"WWW-Authenticate": challenge}
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
                # Not a static API token — try an OAuth access token (the
                # Claude.ai connector path). Lazy import so an OAuth-module hiccup
                # degrades to static-token-only rather than breaking /mcp.
                try:
                    from portal.oauth import authenticate_oauth_token

                    user = authenticate_oauth_token(db, authz)
                except Exception:
                    user = None
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
        # Tolerate line-wrapped base64 (some clients/transports insert newlines,
        # which validate=True would otherwise reject as "not valid base64").
        raw_b64 = "".join((args.get("zip_base64") or "").split())
        if not raw_b64:
            raise ValueError("zip_base64 is required")
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(
                "zip_base64 is not valid base64. Encode the actual file bytes "
                "(e.g. `base64 -w0 app.zip`) — it can't be hand-generated."
            )
        if not data:
            raise ValueError("decoded zip is empty")
        if len(data) > MAX_ZIP_BYTES:
            raise ValueError(f"zip exceeds {MAX_ZIP_BYTES // (1024 * 1024)}MB limit")
        # The base64 decoded but the bytes aren't a ZIP — almost always means the
        # payload was truncated or altered in transit (common for large bundles
        # over the MCP tool channel). Distinguish this from "not valid base64".
        if data[:2] != b"PK":
            raise ValueError(
                f"zip_base64 decoded to {len(data)} bytes that aren't a ZIP "
                f"(starts with {data[:4]!r}) — it was likely truncated or altered "
                "in transit. For anything but a tiny app, upload via the web UI "
                "(Admin → Apps → Upload) or the HTTP API instead of MCP base64."
            )

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

    async def _do_create_app(args: dict) -> dict:
        ident = _ctx_user()
        # Assemble the bundle from text files (+ optional / generated icon). Bad
        # input raises ValueError, surfaced to the caller as a tool error.
        data = build_app_zip(args.get("files"), args.get("icon_base64"))
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
                details={"name": result.name, "version": result.version, "via": "mcp/create_app"},
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

    # --- schedule handlers ---

    def _sched_summary(s: ScheduledRun) -> dict:
        return {
            "id": s.id, "app_slug": s.app_slug, "tool_name": s.tool_name,
            "frequency": s.frequency, "hour": s.hour, "minute": s.minute,
            "day_of_week": s.day_of_week, "day_of_month": s.day_of_month,
            "enabled": bool(s.enabled), "label": s.label, "args": dict(s.args or {}),
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "last_status": s.last_status, "last_result": s.last_result,
        }

    def _do_list_schedules() -> dict:
        with Session(engine) as db:
            rows = db.exec(select(ScheduledRun).order_by(ScheduledRun.next_run_at)).all()
            return {"schedules": [_sched_summary(s) for s in rows], "count": len(rows)}

    def _clamp_int(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default

    def _do_create_schedule(args: dict) -> dict:
        ident = _ctx_user()
        slug = (args.get("app_slug") or "").strip()
        tool_name = (args.get("tool_name") or "").strip()
        frequency = (args.get("frequency") or "daily").strip()
        if frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {sorted(FREQUENCIES)}")
        tool_args = args.get("args") or {}
        if not isinstance(tool_args, dict):
            raise ValueError("'args' must be an object")
        with Session(engine) as db:
            app = db.exec(select(App).where(App.slug == slug)).first()
            if app is None:
                raise ValueError(f"No app with slug '{slug}'.")
            decl = next((t for t in (app.tools or []) if t.get("name") == tool_name), None)
            if decl is None:
                raise ValueError(f"App '{slug}' has no tool '{tool_name}'.")
            hour = _clamp_int(args.get("hour"), 0, 23, 8)
            minute = _clamp_int(args.get("minute"), 0, 59, 0)
            dow = _clamp_int(args.get("day_of_week"), 0, 6, 0)
            dom = _clamp_int(args.get("day_of_month"), 1, 28, 1)
            now = datetime.now(timezone.utc)
            sched = ScheduledRun(
                app_slug=slug, tool_name=tool_name, args=tool_args,
                user_id=ident["id"], created_by=ident["id"],
                label=(args.get("label") or "")[:120], frequency=frequency,
                hour=hour, minute=minute, day_of_week=dow, day_of_month=dom,
                enabled=True,
                next_run_at=compute_next_run(
                    frequency=frequency, hour=hour, minute=minute,
                    day_of_week=dow, day_of_month=dom, after=now,
                ),
            )
            db.add(sched)
            db.commit()
            db.refresh(sched)
            record_event(
                db, actor=db.get(User, ident["id"]), action="schedule.create",
                request=_ctx_request(), target=f"schedule:{slug}/{tool_name}",
                details={"frequency": frequency, "via": "mcp"},
            )
            return _sched_summary(sched)

    def _do_set_schedule_enabled(args: dict) -> dict:
        ident = _ctx_user()
        sid = args.get("id")
        enabled = bool(args.get("enabled"))
        with Session(engine) as db:
            sched = db.get(ScheduledRun, sid)
            if sched is None:
                raise ValueError(f"No schedule with id {sid}.")
            sched.enabled = enabled
            if enabled:
                sched.next_run_at = compute_next_run(
                    frequency=sched.frequency, hour=sched.hour, minute=sched.minute,
                    day_of_week=sched.day_of_week, day_of_month=sched.day_of_month,
                    after=datetime.now(timezone.utc),
                )
            db.add(sched)
            db.commit()
            record_event(
                db, actor=db.get(User, ident["id"]), action="schedule.toggle",
                request=_ctx_request(),
                target=f"schedule:{sched.app_slug}/{sched.tool_name}",
                details={"enabled": enabled, "via": "mcp"},
            )
            return _sched_summary(sched)

    def _do_delete_schedule(args: dict) -> dict:
        ident = _ctx_user()
        sid = args.get("id")
        with Session(engine) as db:
            sched = db.get(ScheduledRun, sid)
            if sched is None:
                raise ValueError(f"No schedule with id {sid}.")
            target = f"schedule:{sched.app_slug}/{sched.tool_name}"
            db.delete(sched)
            db.commit()
            record_event(
                db, actor=db.get(User, ident["id"]), action="schedule.delete",
                request=_ctx_request(), target=target, details={"via": "mcp"},
            )
            return {"deleted": sid}

    async def _do_run_schedule(args: dict) -> dict:
        ident = _ctx_user()
        sid = args.get("id")
        # fire_schedule opens its own session and runs blocking tool work; offload.
        out = await anyio.to_thread.run_sync(lambda: fire_schedule(sid, advance=False))
        if not out.get("found"):
            raise ValueError(f"No schedule with id {sid}.")
        try:
            with Session(engine) as db:
                record_event(
                    db, actor=db.get(User, ident["id"]), action="schedule.run_now",
                    request=_ctx_request(), target=f"schedule:{sid}",
                    details={"status": out.get("status"), "via": "mcp"},
                )
        except Exception:
            logger.exception("MCP run_schedule audit failed (id=%s)", sid)
        return {"id": sid, "status": out.get("status"), "result": out.get("result")}

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
                    tname = decl.get("name")
                    if not tname:
                        continue  # malformed/legacy row — skip it, don't break the whole list
                    tools.append(
                        types.Tool(
                            name=f"{app.slug}{_TOOL_SEP}{tname}",
                            description=f"[{app.name}] {decl.get('description', '')}".strip(),
                            inputSchema=tool_input_schema(decl),
                        )
                    )
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name == "whoami":
            return _ctx_user()
        if name == "authoring_guide":
            return [types.TextContent(type="text", text=_AUTHORING_GUIDE)]
        if name == "list_apps":
            return _do_list_apps()
        if name == "get_app":
            return _do_get_app(arguments)
        if name == "upload_app":
            return await _do_upload_app(arguments)
        if name == "create_app":
            return await _do_create_app(arguments)
        if name == "set_app_enabled":
            return _do_set_enabled(arguments)
        if name == "list_schedules":
            return _do_list_schedules()
        if name == "create_schedule":
            return _do_create_schedule(arguments)
        if name == "set_schedule_enabled":
            return _do_set_schedule_enabled(arguments)
        if name == "delete_schedule":
            return _do_delete_schedule(arguments)
        if name == "run_schedule":
            return await _do_run_schedule(arguments)
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
