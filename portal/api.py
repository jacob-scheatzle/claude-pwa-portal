"""HTTP API child apps consume via /portal-sdk.js, plus programmatic endpoints
for the Claude skill. Auth accepts either a session cookie or
`Authorization: Bearer <token>`."""
from __future__ import annotations

import io
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Optional

from fastapi import (
    APIRouter, Depends, File, Header, HTTPException, Request, UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlmodel import Session, select

from portal.apps import UploadError, install_bundle
from portal.config import settings
from portal.db import get_db
from portal.deps import current_user_or_token
from portal.models import App, User
from portal.settings_store import smtp_config

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


def _require_app(db: Session, slug: Optional[str]) -> App:
    if not slug:
        raise HTTPException(400, "X-Portal-App header missing")
    app_row = db.exec(
        select(App).where(App.slug == slug, App.enabled == True)  # noqa: E712
    ).first()
    if app_row is None:
        raise HTTPException(400, f"App '{slug}' not found or disabled")
    return app_row


# ----- /user -----

@router.get("/user/me")
def user_me(user: UserDep):
    me = _require_user(user)
    return {"id": me.id, "email": me.email, "role": me.role}


# ----- /pdf -----

class PdfRequest(BaseModel):
    html: str = Field(min_length=1)
    filename: str = "document.pdf"


@router.post("/pdf/render")
def pdf_render(req: PdfRequest, user: UserDep):
    _require_user(user)
    try:
        from weasyprint import HTML  # lazy: avoid hard import at startup
    except ImportError:
        raise HTTPException(503, "PDF service unavailable: WeasyPrint not installed")
    except OSError as e:
        raise HTTPException(503, f"PDF service unavailable: {e}")

    buf = io.BytesIO()
    try:
        HTML(string=req.html).write_pdf(buf)
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


def _smtp_send(msg: EmailMessage, cfg: dict) -> None:
    host = cfg["host"]
    port = cfg["port"]
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        if cfg["use_tls"]:
            server.starttls()
    try:
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"] or "")
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


@router.post("/email/send")
def email_send(req: EmailRequest, user: UserDep, db: DbDep):
    me = _require_user(user)
    cfg = smtp_config(db)
    if not cfg["host"]:
        raise HTTPException(503, "Email service unavailable: SMTP not configured")
    if not req.text and not req.html:
        raise HTTPException(400, "Provide at least one of `text` or `html`")

    to_list = req.to if isinstance(req.to, list) else [req.to]
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
        _smtp_send(msg, cfg)
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
def storage_list(user: UserDep, db: DbDep, x_portal_app: AppHeader = None):
    me = _require_user(user)
    app_row = _require_app(db, x_portal_app)
    ns = _ns_dir(app_row.slug, me.id)
    items = []
    for p in ns.rglob("*"):
        if p.is_file():
            items.append({"key": p.relative_to(ns).as_posix(), "size": p.stat().st_size})
    return {"items": items, "usage": sum(i["size"] for i in items), "limit": MAX_NAMESPACE_BYTES}


@router.get("/storage/{key:path}")
def storage_get(
    key: str, user: UserDep, db: DbDep, x_portal_app: AppHeader = None
):
    me = _require_user(user)
    app_row = _require_app(db, x_portal_app)
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
    return FileResponse(target, media_type=mt or "application/octet-stream")


@router.put("/storage/{key:path}")
async def storage_put(
    key: str,
    request: Request,
    user: UserDep,
    db: DbDep,
    x_portal_app: AppHeader = None,
):
    me = _require_user(user)
    app_row = _require_app(db, x_portal_app)
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
    except HTTPException:
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
    key: str, user: UserDep, db: DbDep, x_portal_app: AppHeader = None
):
    me = _require_user(user)
    app_row = _require_app(db, x_portal_app)
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
    user: UserDep,
    db: DbDep,
    bundle: UploadFile = File(...),
):
    admin = _require_admin(user)
    try:
        result = await install_bundle(db, admin, bundle)
    except UploadError as e:
        raise HTTPException(400, str(e))
    return {"slug": result.slug, "name": result.name, "version": result.version}
