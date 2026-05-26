"""Admin pages: settings, API tokens, staff user management, backups."""
from __future__ import annotations

import hashlib
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Optional

import anyio
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from portal.access import (
    delete_access_for_user,
    get_default_access,
    grant_default_access_for_new_user,
    replace_user_app_access,
    set_default_access,
)
from portal.config import settings
from portal.db import get_db
from portal.deps import require_admin
from portal.models import ApiToken, App, User, UserAppAccess
from portal.security import check_csrf, hash_password, validate_password
from portal.settings_store import set_secret, set_setting, smtp_config
from portal.smtp import send_message
from portal.web import flash, render

router = APIRouter()

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[User, Depends(require_admin)]

email_adapter = TypeAdapter(EmailStr)


# ----- Settings -----

@router.get("/admin/settings")
def settings_form(request: Request, db: DbDep, admin: AdminDep):
    cfg = smtp_config(db)
    # site_url is environment-only: Caddy reads SITE_URL at startup for the
    # wildcard subdomain config, so a DB-side change would silently diverge
    # from the routing layer. Surface the env value read-only and let admins
    # know it requires a restart.
    return render(
        request, "admin_settings.html",
        user=admin,
        site_url=settings.site_url,
        smtp_host=cfg["host"] or "",
        smtp_port=str(cfg["port"] or 587),
        smtp_username=cfg["username"] or "",
        smtp_password_set=bool(cfg["password"]),
        smtp_from=cfg["from_addr"] or "",
        smtp_use_tls=cfg["use_tls"],
        default_user_app_access=get_default_access(db),
    )


@router.post("/admin/settings")
def settings_save(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    smtp_host: Annotated[str, Form()] = "",
    smtp_port: Annotated[str, Form()] = "",
    smtp_username: Annotated[str, Form()] = "",
    smtp_password: Annotated[str, Form()] = "",
    smtp_from: Annotated[str, Form()] = "",
    smtp_use_tls: Annotated[Optional[str], Form()] = None,
    default_user_app_access: Annotated[str, Form()] = "all",
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    # site_url is intentionally NOT writable here — see settings_form above.
    set_setting(db, "smtp_host", smtp_host.strip())
    set_setting(db, "smtp_port", smtp_port.strip())
    set_setting(db, "smtp_username", smtp_username.strip())
    if smtp_password:
        set_secret(db, "smtp_password", smtp_password)
    set_setting(db, "smtp_from", smtp_from.strip())
    set_setting(db, "smtp_use_tls", "true" if smtp_use_tls == "on" else "false")
    # The default-access toggle gates the auto-grant logic in user/app create.
    # Existing access rows are untouched; flipping it only affects future
    # creations, which keeps the change reversible.
    if default_user_app_access in ("all", "none"):
        set_default_access(db, default_user_app_access)
    db.commit()
    flash(request, "Settings saved.")
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/smtp/test")
def settings_smtp_test(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
        send_message(msg, cfg)
    except Exception as e:
        flash(request, f"SMTP test failed: {e}", level="error")
        return RedirectResponse("/admin/settings", status_code=303)
    flash(request, f"Test email sent to {admin.email}.")
    return RedirectResponse("/admin/settings", status_code=303)


# ----- API tokens -----

@router.get("/admin/tokens")
def tokens_list(request: Request, db: DbDep, admin: AdminDep):
    tokens = db.exec(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    # Fallback for legacy session-stored values (e.g. from a prior redirect flow).
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
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
    # Render directly so the raw token survives only this response — no session round-trip.
    tokens = db.exec(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    return render(
        request, "admin_tokens.html",
        user=admin, tokens=tokens,
        last_token=raw, last_token_name=name,
    )


@router.post("/admin/tokens/{token_id}/delete")
def tokens_delete(
    request: Request, db: DbDep, admin: AdminDep,
    token_id: int,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
    return db.exec(
        select(func.count()).select_from(User).where(User.role == "admin")
    ).one()


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
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
    new_user = User(email=validated_email, password_hash=hash_password(password), role=role)
    db.add(new_user)
    # Flush to assign new_user.id before staging the access rows that FK to it.
    db.flush()
    grant_default_access_for_new_user(db, new_user)
    db.commit()
    flash(request, f"Created {validated_email} ({role}).")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/role")
def users_set_role(
    request: Request, db: DbDep, admin: AdminDep,
    user_id: int,
    role: Annotated[str, Form()],
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
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
    # SQLite FKs are advisory here, so drop the access rows explicitly before
    # the User row goes away to keep the table consistent.
    if target.id is not None:
        delete_access_for_user(db, target.id)
    db.delete(target)
    db.commit()
    flash(request, f"Deleted {email}.")
    return RedirectResponse("/admin/users", status_code=303)


# ----- Per-user app access -----

@router.get("/admin/users/{user_id}/apps")
def users_apps_form(
    request: Request, db: DbDep, admin: AdminDep, user_id: int
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    all_apps = db.exec(select(App).order_by(App.name)).all()
    granted_ids = set(
        db.exec(
            select(UserAppAccess.app_id).where(UserAppAccess.user_id == user_id)
        ).all()
    )
    return render(
        request, "admin_user_apps.html",
        user=admin, target=target, apps=all_apps, granted_ids=granted_ids,
    )


@router.post("/admin/users/{user_id}/apps")
async def users_apps_save(
    request: Request, db: DbDep, admin: AdminDep, user_id: int,
):
    # The form submits as ``app_ids`` checkboxes; FastAPI's Form() can't
    # easily collect a repeated field name without colliding with the
    # ``csrf`` kw, so we parse the raw form ourselves.
    form = await request.form()
    csrf = form.get("_csrf", "")
    check_csrf(request, csrf if isinstance(csrf, str) else "")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if target.role == "admin":
        flash(request, "Admins have access to every app by role.", level="error")
        return RedirectResponse(f"/admin/users/{user_id}/apps", status_code=303)
    raw_ids = form.getlist("app_ids")
    allowed: list[int] = []
    for v in raw_ids:
        try:
            allowed.append(int(v))
        except (TypeError, ValueError):
            continue
    replace_user_app_access(db, target, allowed)
    db.commit()
    flash(request, f"Updated app access for {target.email}.")
    return RedirectResponse(f"/admin/users/{user_id}/apps", status_code=303)


# ----- Backups -----
#
# One-click downloadable backup of the entire data directory:
#
#   - portal.db        — a sqlite3 online-backup snapshot (consistent even
#                        if writes are in flight)
#   - apps/            — extracted child-app bundles
#   - storage/         — per-app, per-user key/value storage
#
# What's NOT included: the .env file (lives outside the container's data
# dir; contains SECRET_KEY) and Caddy's auto-renewing certificate cache
# (regenerable on demand). To fully restore on a new host the operator
# needs both this tarball AND the SECRET_KEY from the original .env, since
# the SMTP password row inside portal.db is Fernet-encrypted with a key
# derived from SECRET_KEY.

def _snapshot_sqlite(src: Path, dest: Path) -> None:
    """Online backup of ``src`` to ``dest`` using SQLite's standard API.

    ``sqlite3.Connection.backup()`` produces a consistent snapshot even
    while the source DB is being written to — preferable to a raw file
    copy, which would race the WAL.
    """
    src_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _build_backup(data_dir: Path) -> tuple[Path, Path, str]:
    """Build a gzipped tarball of the portal data directory.

    Returns ``(backup_file_path, tmp_dir_to_cleanup, filename)``. The
    caller is responsible for scheduling ``shutil.rmtree(tmp_dir)`` as a
    background task once the response finishes streaming.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="portal-backup-"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    filename = f"pwa-portal-backup-{timestamp}.tar.gz"
    backup_path = tmp_dir / filename
    try:
        db_snapshot = tmp_dir / "portal.db"
        db_file = data_dir / "portal.db"
        if db_file.exists():
            _snapshot_sqlite(db_file, db_snapshot)
        with tarfile.open(backup_path, "w:gz") as tar:
            if db_snapshot.exists():
                tar.add(db_snapshot, arcname="portal.db")
            apps_dir = data_dir / "apps"
            if apps_dir.exists() and apps_dir.is_dir():
                tar.add(apps_dir, arcname="apps")
            storage_dir = data_dir / "storage"
            if storage_dir.exists() and storage_dir.is_dir():
                tar.add(storage_dir, arcname="storage")
        return backup_path, tmp_dir, filename
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


@router.get("/admin/backup")
def backup_form(request: Request, db: DbDep, admin: AdminDep):
    return render(
        request, "admin_backup.html", user=admin, site_url=settings.site_url,
    )


@router.post("/admin/backup/download")
async def backup_download(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    data_dir = Path(settings.data_dir).resolve()
    # Run the tar/snapshot work on a worker thread so a large data dir
    # doesn't block the event loop while the response is being prepared.
    backup_path, tmp_dir, filename = await anyio.to_thread.run_sync(
        _build_backup, data_dir
    )
    # The BackgroundTask runs AFTER the file is fully streamed to the client,
    # then removes the entire scratch directory (including the tarball).
    cleanup = BackgroundTask(shutil.rmtree, str(tmp_dir), ignore_errors=True)
    return FileResponse(
        backup_path,
        filename=filename,
        media_type="application/gzip",
        background=cleanup,
    )
