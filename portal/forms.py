"""Public intake forms.

An app declares ``forms`` in its manifest (see ``portal.apps.PortalAppForm``);
the portal serves each on the **app's own origin** — consistent with per-app
isolation, since a form belongs to one app:

  * per-app-subdomain mode (default): ``<slug>.apps.<SITE_URL>/forms/<form>``
    (the slug is the subdomain; the path is just the form name);
  * same-origin mode (``CHILD_APPS_SAME_ORIGIN=true``):
    ``<SITE_URL>/forms/<slug>/<form>`` (no subdomains exist).

The portal-origin ``/forms/<slug>/<form>`` URL still works: in subdomain mode a
GET there 307-redirects to the app subdomain (the canonical link), so any link
already shared keeps working.

A GET renders the form; a POST records a ``FormSubmission`` for the business
owner and optionally emails a notification. This is the only **public write**
surface, so it is locked down: no session / CSRF (anonymous by design), a hidden
honeypot, a per-(IP, app, form) rate limit, per-field size caps, and recording
only declared fields. Submitted values are autoescaped on render, and the notify
recipient comes from the trusted manifest — never the submission.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from email.message import EmailMessage
from threading import Lock
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from portal.config import settings
from portal.db import get_db
from portal.models import App, FormSubmission
from portal.settings_store import smtp_config
from portal.smtp import send_message
from portal.web import render

logger = logging.getLogger("portal.forms")
router = APIRouter()

DbDep = Annotated[Session, Depends(get_db)]

# Anti-abuse knobs for the public submit surface.
_RATE_WINDOW_SECONDS = 600
_RATE_LIMIT = 10              # submissions per (ip, slug, form) per window
_FIELD_MAXLEN = 2000          # single-line fields
_TEXTAREA_MAXLEN = 5000
# Hidden field a real browser leaves empty; bots that fill every input trip it.
HONEYPOT_FIELD = "company_url"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_rate: dict[tuple, deque] = {}
_rate_lock = Lock()


def public_form_url(slug: str, form_name: str, request: Optional[Request] = None) -> str:
    """Canonical public URL for a form — on the app's own origin.

    Subdomain mode: ``<scheme>://<slug>.apps.<SITE_URL>[:port]/forms/<form>``.
    Same-origin mode: ``<scheme>://<SITE_URL>[:port]/forms/<slug>/<form>``.
    Scheme follows ``cookies_secure``; the request's port is preserved so dev
    links on ``lvh.me:8000`` stay reachable.
    """
    scheme = "https" if settings.cookies_secure else "http"
    port = ""
    if request is not None:
        host = request.headers.get("host", "")
        if ":" in host and not host.startswith("["):
            port = ":" + host.rsplit(":", 1)[1]
    if settings.child_apps_same_origin:
        return f"{scheme}://{settings.site_url}{port}/forms/{slug}/{form_name}"
    return f"{scheme}://{slug}.apps.{settings.site_url}{port}/forms/{form_name}"


def _rate_ok(ip: str, slug: str, form_name: str) -> bool:
    """True if this (ip, app, form) is under the per-window submission cap."""
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SECONDS
    key = (ip, slug, form_name)
    with _rate_lock:
        dq = _rate.get(key)
        if dq is None:
            dq = deque()
            _rate[key] = dq
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            return False
        dq.append(now)
        if len(_rate) > 4096:
            for k in [k for k, d in _rate.items() if not d or d[-1] < cutoff]:
                _rate.pop(k, None)
        return True


def _find_form(app_row: App, form_name: str) -> Optional[dict]:
    for f in (app_row.forms or []):
        if f.get("name") == form_name:
            return f
    return None


def _load(db: Session, slug: str, form_name: str):
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None or not app_row.enabled:
        return None, None
    return app_row, _find_form(app_row, form_name)


def _post_action(request: Request, slug: str, form_name: str) -> str:
    """Where the rendered form POSTs back to — same origin it's served on.

    On the app subdomain the slug is implicit in the host, so the form posts to
    ``/forms/<form>``; on the portal origin (same-origin mode) it carries the slug.
    """
    if getattr(request.state, "app_slug", None):
        return f"/forms/{form_name}"
    return f"/forms/{slug}/{form_name}"


def _render_form(
    request: Request, app_row: App, decl: dict, slug: str, form_name: str,
    *, values: Optional[dict] = None, errors: Optional[list] = None, status_code: int = 200,
):
    return render(
        request, "public_form.html",
        app_row=app_row, form=decl, honeypot=HONEYPOT_FIELD,
        values=values or {}, errors=errors or [],
        post_action=_post_action(request, slug, form_name),
        status_code=status_code,
    )


async def _handle_submit(request: Request, db: Session, slug: str, form_name: str):
    app_row, decl = _load(db, slug, form_name)
    if app_row is None or decl is None:
        raise HTTPException(404)

    form = await request.form()

    # Honeypot: silently accept (don't tell a bot it was caught) but store nothing.
    if (form.get(HONEYPOT_FIELD) or "").strip():
        return render(request, "public_form_done.html", app_row=app_row, form=decl)

    ip = request.client.host if request.client else ""
    fields = decl.get("fields", [])

    if not _rate_ok(ip, slug, form_name):
        values = {f["name"]: (form.get(f["name"]) or "").strip() for f in fields}
        return _render_form(
            request, app_row, decl, slug, form_name, values=values,
            errors=["Too many submissions from here — please try again later."],
            status_code=429,
        )

    values: dict = {}
    errors: list[str] = []
    for fld in fields:
        name = fld["name"]
        ftype = fld.get("type", "text")
        raw = (form.get(name) or "").strip()
        maxlen = _TEXTAREA_MAXLEN if ftype == "textarea" else _FIELD_MAXLEN
        if len(raw) > maxlen:
            raw = raw[:maxlen]
        if fld.get("required") and not raw:
            errors.append(f"{fld['label']} is required.")
        elif raw and ftype == "email" and not _EMAIL_RE.match(raw):
            errors.append(f"{fld['label']} must be a valid email address.")
        elif raw and ftype == "number":
            try:
                float(raw)
            except ValueError:
                errors.append(f"{fld['label']} must be a number.")
        values[name] = raw

    if errors:
        return _render_form(
            request, app_row, decl, slug, form_name,
            values=values, errors=errors, status_code=400,
        )

    db.add(FormSubmission(
        app_slug=slug, form_name=form_name, data=values, source_ip=ip[:64],
    ))
    db.commit()

    notify = (decl.get("notify_email") or "").strip()
    if notify:
        _notify(db, app_row, decl, values, notify)

    return render(request, "public_form_done.html", app_row=app_row, form=decl)


# ----- subdomain routes (canonical in per-app-origin mode) -----

@router.get("/forms/{form_name}", include_in_schema=False)
def form_page_sub(form_name: str, request: Request, db: DbDep):
    slug = getattr(request.state, "app_slug", None)
    if not slug:
        # Portal origin: a bare /forms/<x> has no app context. 404 (the
        # two-segment /forms/<slug>/<form> route handles the portal origin).
        raise HTTPException(404)
    app_row, decl = _load(db, slug, form_name)
    if app_row is None or decl is None:
        raise HTTPException(404)
    return _render_form(request, app_row, decl, slug, form_name)


@router.post("/forms/{form_name}", include_in_schema=False)
async def form_submit_sub(form_name: str, request: Request, db: DbDep):
    slug = getattr(request.state, "app_slug", None)
    if not slug:
        raise HTTPException(404)
    return await _handle_submit(request, db, slug, form_name)


# ----- portal-origin routes (canonical in same-origin mode; redirect otherwise) -----

@router.get("/forms/{slug}/{form_name}", include_in_schema=False)
def form_page_portal(slug: str, form_name: str, request: Request, db: DbDep):
    if getattr(request.state, "app_slug", None):
        raise HTTPException(404)  # two-segment URLs don't apply on a subdomain
    if not settings.child_apps_same_origin:
        # Subdomain mode: the form lives on the app's own origin. Redirect any
        # portal-origin link to the canonical subdomain URL.
        return RedirectResponse(public_form_url(slug, form_name, request), status_code=307)
    app_row, decl = _load(db, slug, form_name)
    if app_row is None or decl is None:
        raise HTTPException(404)
    return _render_form(request, app_row, decl, slug, form_name)


@router.post("/forms/{slug}/{form_name}", include_in_schema=False)
async def form_submit_portal(slug: str, form_name: str, request: Request, db: DbDep):
    if getattr(request.state, "app_slug", None):
        raise HTTPException(404)
    # Canonical in same-origin mode; also a harmless fallback if a subdomain
    # form's POST ever lands here.
    return await _handle_submit(request, db, slug, form_name)


def _notify(db: Session, app_row: App, decl: dict, values: dict, to_addr: str) -> None:
    """Best-effort 'new submission' email to the owner. Never fails the submit."""
    try:
        cfg = smtp_config(db)
        if not cfg.get("host"):
            return  # SMTP not configured — the submission is already stored
        title = decl.get("title") or decl.get("name")
        lines = [f"{f['label']}: {values.get(f['name'], '')}" for f in decl.get("fields", [])]
        msg = EmailMessage()
        # Recipient + headers come from the trusted manifest / config, never the
        # submission, so there's no header-injection surface from user input.
        msg["From"] = cfg.get("from_addr") or cfg.get("username") or to_addr
        msg["To"] = to_addr
        msg["Subject"] = f"[{app_row.name}] New submission: {title}"
        msg.set_content(
            f"A new submission to “{title}” on {app_row.name}:\n\n" + "\n".join(lines)
        )
        send_message(msg, cfg)
    except Exception:
        logger.exception(
            "form notify email failed for %s/%s", app_row.slug, decl.get("name")
        )
