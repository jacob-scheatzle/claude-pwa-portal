"""Admin pages: settings, API tokens, staff user management."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlmodel import Session, select

from portal.api import _smtp_send
from portal.config import settings
from portal.db import get_db
from portal.deps import require_admin
from portal.models import ApiToken, User
from portal.security import hash_password, validate_password
from portal.settings_store import get_setting, set_setting, smtp_config
from portal.web import flash, render

router = APIRouter()

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[User, Depends(require_admin)]

email_adapter = TypeAdapter(EmailStr)


# ----- Settings -----

@router.get("/admin/settings")
def settings_form(request: Request, db: DbDep, admin: AdminDep):
    cfg = smtp_config(db)
    site_url = get_setting(db, "site_url", settings.site_url) or settings.site_url
    return render(
        request, "admin_settings.html",
        user=admin,
        site_url=site_url,
        smtp_host=cfg["host"] or "",
        smtp_port=str(cfg["port"] or 587),
        smtp_username=cfg["username"] or "",
        smtp_password_set=bool(cfg["password"]),
        smtp_from=cfg["from_addr"] or "",
        smtp_use_tls=cfg["use_tls"],
    )


@router.post("/admin/settings")
def settings_save(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    site_url: Annotated[str, Form()],
    smtp_host: Annotated[str, Form()] = "",
    smtp_port: Annotated[str, Form()] = "",
    smtp_username: Annotated[str, Form()] = "",
    smtp_password: Annotated[str, Form()] = "",
    smtp_from: Annotated[str, Form()] = "",
    smtp_use_tls: Annotated[Optional[str], Form()] = None,
):
    set_setting(db, "site_url", site_url.strip())
    set_setting(db, "smtp_host", smtp_host.strip())
    set_setting(db, "smtp_port", smtp_port.strip())
    set_setting(db, "smtp_username", smtp_username.strip())
    if smtp_password:
        set_setting(db, "smtp_password", smtp_password)
    set_setting(db, "smtp_from", smtp_from.strip())
    set_setting(db, "smtp_use_tls", "true" if smtp_use_tls == "on" else "false")
    db.commit()
    flash(request, "Settings saved.")
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/smtp/test")
def settings_smtp_test(request: Request, db: DbDep, admin: AdminDep):
    cfg = smtp_config(db)
    if not cfg["host"]:
        flash(request, "SMTP not configured.", level="error")
        return RedirectResponse("/admin/settings", status_code=303)
    msg = EmailMessage()
    msg["From"] = cfg["from_addr"] or cfg["username"] or admin.email
    msg["To"] = admin.email
    msg["Subject"] = "PWA Portal — SMTP test"
    msg.set_content(
        "If you received this, your portal's SMTP settings are working.\n\n"
        f"Sent at {datetime.now(timezone.utc).isoformat()}\n"
    )
    try:
        _smtp_send(msg, cfg)
    except Exception as e:
        flash(request, f"SMTP test failed: {e}", level="error")
        return RedirectResponse("/admin/settings", status_code=303)
    flash(request, f"Test email sent to {admin.email}.")
    return RedirectResponse("/admin/settings", status_code=303)


# ----- API tokens -----

@router.get("/admin/tokens")
def tokens_list(request: Request, db: DbDep, admin: AdminDep):
    tokens = db.exec(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    last_token = request.session.pop("_last_token", None)
    last_token_name = request.session.pop("_last_token_name", None)
    return render(
        request, "admin_tokens.html",
        user=admin, tokens=tokens,
        last_token=last_token, last_token_name=last_token_name,
    )


@router.post("/admin/tokens")
def tokens_create(
    request: Request, db: DbDep, admin: AdminDep,
    name: Annotated[str, Form()],
):
    name = name.strip()
    if not name:
        flash(request, "Token name is required.", level="error")
        return RedirectResponse("/admin/tokens", status_code=303)
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    db.add(ApiToken(
        name=name, token_hash=token_hash, prefix=raw[:8], created_by=admin.id,
    ))
    db.commit()
    request.session["_last_token"] = raw
    request.session["_last_token_name"] = name
    return RedirectResponse("/admin/tokens", status_code=303)


@router.post("/admin/tokens/{token_id}/delete")
def tokens_delete(
    request: Request, db: DbDep, admin: AdminDep,
    token_id: int,
):
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(404)
    name = token.name
    db.delete(token)
    db.commit()
    flash(request, f"Token '{name}' revoked.")
    return RedirectResponse("/admin/tokens", status_code=303)


# ----- Users -----

def _count_admins(db: Session) -> int:
    return len(db.exec(select(User).where(User.role == "admin")).all())


@router.get("/admin/users")
def users_list(request: Request, db: DbDep, admin: AdminDep):
    users = db.exec(select(User).order_by(User.created_at.desc())).all()
    return render(
        request, "admin_users.html",
        user=admin, users=users, admin_count=_count_admins(db),
    )


@router.post("/admin/users")
def users_create(
    request: Request, db: DbDep, admin: AdminDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "user",
):
    if role not in ("admin", "user"):
        flash(request, "Invalid role.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    try:
        validated_email = str(email_adapter.validate_python(email)).lower()
    except ValidationError:
        flash(request, "Invalid email address.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    errors = validate_password(password)
    if errors:
        flash(request, "; ".join(errors), level="error")
        return RedirectResponse("/admin/users", status_code=303)
    existing = db.exec(select(User).where(User.email == validated_email)).first()
    if existing is not None:
        flash(request, f"User {validated_email} already exists.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    db.add(User(email=validated_email, password_hash=hash_password(password), role=role))
    db.commit()
    flash(request, f"Created {validated_email} ({role}).")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/role")
def users_set_role(
    request: Request, db: DbDep, admin: AdminDep,
    user_id: int,
    role: Annotated[str, Form()],
):
    if role not in ("admin", "user"):
        flash(request, "Invalid role.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if target.role == "admin" and role == "user" and _count_admins(db) <= 1:
        flash(request, "Can't demote the last admin.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    target.role = role
    db.add(target)
    db.commit()
    flash(request, f"{target.email} role set to {role}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reset-password")
def users_reset_password(
    request: Request, db: DbDep, admin: AdminDep,
    user_id: int,
    password: Annotated[str, Form()],
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    errors = validate_password(password)
    if errors:
        flash(request, "; ".join(errors), level="error")
        return RedirectResponse("/admin/users", status_code=303)
    target.password_hash = hash_password(password)
    db.add(target)
    db.commit()
    flash(request, f"Password reset for {target.email}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def users_delete(
    request: Request, db: DbDep, admin: AdminDep,
    user_id: int,
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if target.id == admin.id:
        flash(request, "Can't delete your own account.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.role == "admin" and _count_admins(db) <= 1:
        flash(request, "Can't delete the last admin.", level="error")
        return RedirectResponse("/admin/users", status_code=303)
    email = target.email
    db.delete(target)
    db.commit()
    flash(request, f"Deleted {email}.")
    return RedirectResponse("/admin/users", status_code=303)
