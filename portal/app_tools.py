"""Declarative executor for app-declared MCP tools (Phase 2).

An app declares ``tools`` in its ``portal.json`` (validated by
``portal.apps.PortalAppTool``); the MCP server (``portal/mcp_server.py``)
surfaces each enabled app's tools and dispatches calls here. A tool is a
*declaration*, never code — this module runs it by composing the portal's OWN
trusted primitives:

    render an HTML template (sandboxed, autoescaping Jinja) → PDF
        → deliver: share link | base64 download | email | per-user storage

The app's uploaded code is never executed server-side, so the per-app-origin
trust model is preserved. Tool calls run as the acting (admin) MCP user, in
that user's per-(app, user) storage namespace, and are subject to the same
per-user PDF/email rate limits, recipient allowlist, and per-app service gate
as the SDK.

``run_tool`` is synchronous and opens its own DB session so the MCP layer can
offload it to a worker thread (``anyio.to_thread.run_sync``) without sharing a
Session across threads.
"""
from __future__ import annotations

import io
import math
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from sqlmodel import Session, select

from portal.db import engine
from portal.models import App, User


class AppToolError(Exception):
    """Raised when a tool call can't be completed (bad args, gated service,
    render/delivery failure). The MCP layer turns this into a tool error with
    this message — keep messages user-facing and free of internals."""


# Two sandboxed Jinja environments: HTML output is autoescaped (params can't
# inject markup into the rendered document); plain-text fields (email to /
# subject, storage key, filename) are NOT autoescaped — escaping an email
# address or storage key would corrupt it. ``StrictUndefined`` surfaces typos
# (an undeclared ``{{ param }}``) as a clear error rather than silently blank.
_HTML_ENV = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
_TEXT_ENV = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)

# Bound a single tool call's blast radius. An array param can't exceed this many
# elements (also emitted as JSON-Schema ``maxItems``), and an email-deliver tool
# can't fan out past this many recipients — caps render cost and mail volume for
# an admin-driven (but possibly prompt-influenced) MCP call.
MAX_TOOL_ARRAY_ITEMS = 500
MAX_EMAIL_RECIPIENTS = 50


def _param_json_schema(p: dict) -> dict:
    """JSON-Schema for one declared param — a scalar, or an array of objects."""
    ptype = p.get("type", "string")
    if ptype == "array":
        iprops: dict[str, Any] = {}
        ireq: list[str] = []
        for f in p.get("fields", []):
            fs: dict[str, Any] = {"type": f["type"]}
            if f.get("description"):
                fs["description"] = f["description"]
            iprops[f["name"]] = fs
            if f.get("required"):
                ireq.append(f["name"])
        item: dict[str, Any] = {
            "type": "object", "properties": iprops, "additionalProperties": False,
        }
        if ireq:
            item["required"] = ireq
        schema: dict[str, Any] = {"type": "array", "items": item, "maxItems": MAX_TOOL_ARRAY_ITEMS}
    else:
        schema = {"type": ptype}
    if p.get("description"):
        schema["description"] = p["description"]
    return schema


def tool_input_schema(tool: dict) -> dict:
    """Build a JSON-Schema ``inputSchema`` for an MCP tool from its declared
    params. Used by the MCP server when listing tools."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in tool.get("params", []):
        props[p["name"]] = _param_json_schema(p)
        if p.get("required"):
            required.append(p["name"])
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _coerce_scalar(value: Any, ptype: str, where: str) -> Any:
    try:
        if ptype == "number":
            # bool is an int subclass — reject it so True isn't read as 1.
            if isinstance(value, bool):
                raise ValueError
            num = float(value)
            if not math.isfinite(num):  # block NaN / Infinity poisoning totals
                raise AppToolError(f"{where} must be a finite number")
            return num
        if ptype == "boolean" and not isinstance(value, bool):
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if ptype == "string":
            return str(value)
    except (TypeError, ValueError):
        raise AppToolError(f"{where} must be a {ptype}")
    return value


def _build_context(tool: dict, args: dict) -> dict:
    """Validate ``args`` against the tool's declared params and coerce types.

    Missing required params raise; missing optional params default to a blank
    string (or an empty list for arrays) so templates render cleanly. An array
    param becomes a list of dicts the template iterates with ``{% for %}``;
    each element's declared fields are coerced and missing optional fields are
    zero-filled so arithmetic like ``item.qty * item.rate`` never blows up.
    """
    ctx: dict[str, Any] = {}
    for p in tool.get("params", []):
        name = p["name"]
        ptype = p.get("type", "string")
        present = name in args and args[name] is not None
        if not present:
            if p.get("required"):
                raise AppToolError(f"missing required parameter '{name}'")
            ctx[name] = [] if ptype == "array" else ""
            continue
        value = args[name]
        if ptype == "array":
            if not isinstance(value, list):
                raise AppToolError(f"parameter '{name}' must be a list")
            if len(value) > MAX_TOOL_ARRAY_ITEMS:
                raise AppToolError(
                    f"parameter '{name}' has too many items "
                    f"({len(value)} > {MAX_TOOL_ARRAY_ITEMS})"
                )
            field_types = {f["name"]: f.get("type", "string") for f in p.get("fields", [])}
            rows: list[dict] = []
            for el in value:
                if not isinstance(el, dict):
                    raise AppToolError(f"each item in '{name}' must be an object")
                row: dict[str, Any] = {}
                for fname, ftype in field_types.items():
                    fv = el.get(fname)
                    if fv is None:
                        row[fname] = "" if ftype == "string" else (0 if ftype == "number" else False)
                    else:
                        row[fname] = _coerce_scalar(fv, ftype, f"field '{fname}' in '{name}'")
                rows.append(row)
            ctx[name] = rows
        else:
            ctx[name] = _coerce_scalar(value, ptype, f"parameter '{name}'")
    return ctx


def _render_html(template: str, ctx: dict) -> str:
    try:
        return _HTML_ENV.from_string(template).render(**ctx)
    except Exception as e:  # undefined var, template syntax, sandbox violation
        raise AppToolError(f"template render failed: {e}")


def _render_text(template: Optional[str], ctx: dict) -> str:
    if not template:
        return ""
    try:
        out = _TEXT_ENV.from_string(template).render(**ctx)
    except Exception as e:
        raise AppToolError(f"template render failed: {e}")
    # Strip CR/LF so a param can't inject extra email headers or break a key.
    return out.replace("\r", " ").replace("\n", " ").strip()


def _maybe_brand(db: Session, html: str, branded: bool) -> str:
    if not branded:
        return html
    from portal.branding import (
        get_branding,
        get_logo_data_uri,
        inject_pdf_header,
        render_pdf_header,
    )

    brand = get_branding(db)
    header = render_pdf_header(
        brand["business_name"], brand["accent_color"], get_logo_data_uri(db)
    )
    return inject_pdf_header(html, header)


def _render_pdf_bytes(html: str) -> bytes:
    """Render trusted HTML to a PDF, blocking external fetches (reuses the SDK's
    URL fetcher so a template can't SSRF or read local files)."""
    try:
        from weasyprint import HTML
    except ImportError:
        raise AppToolError("PDF service unavailable: WeasyPrint not installed")
    except OSError:
        raise AppToolError("PDF service unavailable")
    from portal.api import _no_external_fetcher

    buf = io.BytesIO()
    try:
        HTML(string=html, url_fetcher=_no_external_fetcher).write_pdf(buf)
    except Exception:
        raise AppToolError("PDF render failed")
    return buf.getvalue()


def run_tool(
    *,
    slug: str,
    tool: dict,
    args: dict,
    user_id: int,
    host: Optional[str] = None,
) -> dict:
    """Execute one declared tool and return a JSON-able result dict.

    Opens its own DB session (safe to call in a worker thread). Raises
    ``AppToolError`` on any failure; the MCP layer maps that to a tool error.
    """
    import base64

    from portal.api import (
        MAX_NAMESPACE_BYTES,
        MAX_OBJECT_BYTES,
        _check_email_rate,
        _check_pdf_rate,
        _enforce_recipient_allowlist,
        _ns_dir,
        _ns_usage,
        _recipient_domain_allowlist,
        _validate_key,
        namespace_lock,
    )

    render = tool.get("render") or {}
    deliver = tool.get("deliver") or {}
    kind = deliver.get("kind")

    with Session(engine) as db:
        app_row = db.exec(select(App).where(App.slug == slug)).first()
        if app_row is None or not app_row.enabled:
            raise AppToolError(f"App '{slug}' not found or disabled")
        user = db.get(User, user_id)
        if user is None:
            raise AppToolError("Acting user no longer exists")

        # Per-app service gate: a tool may only use services the admin has left
        # enabled for this app (the manifest already cross-checked that every
        # service is declared). ``allowed_services`` is the approved subset.
        allowed = set(app_row.allowed_services or [])
        if kind == "email":
            needed = {"email"}  # renders HTML → email body; no PDF produced
        elif kind == "store":
            needed = {"pdf", "storage"}  # render PDF, then save it
        else:
            needed = {"pdf"}  # share, download
        missing = needed - allowed
        if missing:
            raise AppToolError(
                f"app '{slug}' is not authorized to use service(s) "
                f"{sorted(missing)}; ask an admin to enable them under /admin/apps"
            )

        ctx = _build_context(tool, args)
        html = _maybe_brand(db, _render_html(render.get("html", ""), ctx), bool(render.get("branded")))
        filename = _render_text(render.get("filename"), ctx) or "document.pdf"

        try:
            if kind == "share":
                _check_pdf_rate(user.id)
                from portal.shares import create_pdf_share, share_url

                ttl_days = deliver.get("ttl_days")
                row = create_pdf_share(
                    db,
                    app_row=app_row,
                    user=user,
                    html=html,
                    filename=filename,
                    ttl_seconds=(ttl_days * 86400 if ttl_days else None),
                    max_views=None,
                )
                return {
                    "delivered": "share",
                    "url": share_url(row.token, host),
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                }

            if kind == "download":
                _check_pdf_rate(user.id)
                pdf = _render_pdf_bytes(html)
                if len(pdf) > MAX_OBJECT_BYTES:
                    raise AppToolError(
                        f"PDF exceeds {MAX_OBJECT_BYTES // (1024 * 1024)}MB limit"
                    )
                return {
                    "delivered": "download",
                    "filename": filename,
                    "content_type": "application/pdf",
                    "pdf_base64": base64.b64encode(pdf).decode("ascii"),
                }

            if kind == "store":
                _check_pdf_rate(user.id)
                key = _render_text(deliver.get("key"), ctx)
                safe_key = _validate_key(key)
                pdf = _render_pdf_bytes(html)
                if len(pdf) > MAX_OBJECT_BYTES:
                    raise AppToolError(
                        f"object exceeds {MAX_OBJECT_BYTES // (1024 * 1024)}MB limit"
                    )
                ns = _ns_dir(app_row.slug, user.id)
                target = (ns / safe_key).resolve()
                try:
                    target.relative_to(ns)
                except ValueError:
                    raise AppToolError("invalid storage key")
                # Cross-process lock over the namespace (shared with the SDK's
                # storage PUT) so two concurrent near-cap writes can't both pass
                # the usage check and then both self-delete.
                with namespace_lock(app_row.slug, user.id):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(pdf)
                    if _ns_usage(ns) > MAX_NAMESPACE_BYTES:
                        target.unlink(missing_ok=True)
                        raise AppToolError(
                            f"storage namespace exceeds {MAX_NAMESPACE_BYTES // (1024 * 1024)}MB limit"
                        )
                return {"delivered": "store", "key": safe_key, "size": len(pdf)}

            if kind == "email":
                from portal.health import record_email_send
                from portal.settings_store import smtp_config
                from portal.smtp import send_message

                cfg = smtp_config(db)
                if not cfg["host"]:
                    raise AppToolError("Email service unavailable: SMTP not configured")
                to_raw = _render_text(deliver.get("to"), ctx)
                to_list = [a.strip() for a in to_raw.split(",") if a.strip()]
                if not to_list:
                    raise AppToolError("no recipient resolved for email delivery")
                if len(to_list) > MAX_EMAIL_RECIPIENTS:
                    raise AppToolError(
                        f"too many recipients ({len(to_list)} > {MAX_EMAIL_RECIPIENTS})"
                    )
                _enforce_recipient_allowlist(to_list, _recipient_domain_allowlist(db))
                _check_email_rate(user.id)
                subject = _render_text(deliver.get("subject"), ctx)

                msg = EmailMessage()
                msg["From"] = cfg["from_addr"] or cfg["username"] or user.email
                msg["To"] = ", ".join(to_list)
                msg["Subject"] = subject
                msg.set_content("This message requires an HTML-capable mail client.")
                msg.add_alternative(html, subtype="html")
                try:
                    send_message(msg, cfg)
                except Exception:
                    raise AppToolError("Email send failed")
                record_email_send(
                    db,
                    user_id=user.id,
                    app_slug=app_row.slug,
                    recipient=to_list[0],
                    recipient_count=len(to_list),
                    subject=subject,
                    status="sent",
                )
                return {"delivered": "email", "count": len(to_list)}

            raise AppToolError(f"unknown deliver kind '{kind}'")
        except HTTPException as e:
            # Reused SDK helpers (rate limits, recipient allowlist, key
            # validation) signal via HTTPException; surface their message.
            raise AppToolError(str(getattr(e, "detail", e)))
