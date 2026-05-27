"""Admin pages: settings, API tokens, staff user management, backups."""
from __future__ import annotations

import hashlib
import mimetypes
import re
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
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, func
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from portal.access import (
    delete_access_for_user,
    get_default_access,
    grant_default_access_for_new_user,
    replace_user_app_access,
    set_default_access,
)
from portal.audit import record_event, recent_events
from portal.branding import (
    ALLOWED_FAVICON_TYPES,
    ALLOWED_LOGO_TYPES,
    DEFAULT_ACCENT_COLOR,
    MAX_LOGO_BYTES,
    branding_dir,
    get_branding,
)
from portal.config import settings
from portal.db import get_db
from portal.deps import require_admin
from portal.models import ApiToken, App, AppLaunchToken, User, UserAppAccess
from portal.security import check_csrf, hash_password, validate_password
from portal.sessions import revoke_all_app_sessions_for_user, revoke_all_for_user
from portal.settings_store import get_setting, set_secret, set_setting, smtp_config
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
    brand = get_branding(db)
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
        branding_business_name=brand["business_name"],
        branding_accent_color=brand["accent_color"],
        branding_logo_url=brand["logo_url"],
        branding_favicon_url=brand["favicon_url"],
        default_accent_color=DEFAULT_ACCENT_COLOR,
        max_logo_kb=MAX_LOGO_BYTES // 1024,
    )


# Six-digit hex only, matching ``portal.branding._HEX_COLOR_RE``. Duplicated
# here so the form handler can validate the submitted value without crossing
# into branding internals.
_ACCENT_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Character class of disallowed characters — applied via .sub("", stem) to
# strip anything outside the safe whitelist. The previously-anchored
# ``^[A-Za-z0-9_-]+$`` form was wrong: with re.sub, an anchored full-match
# regex either matches the whole string (replaced by "") or doesn't match at
# all, so a clean stem like "logo" got nuked and a bad stem like "foo!bar"
# passed through untouched. The serve-side ``_safe_logo_name`` whitelist
# then 404'd the orphaned file, so logo/favicon uploads with most non-trivial
# filenames silently failed to display.
_LOGO_BASENAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _store_logo(upload: UploadFile) -> Optional[str]:
    """Validate, persist, and return the filename of an uploaded logo.

    Returns the stored filename on success. Returns None when the upload was
    empty (no file selected). Raises HTTPException with a user-facing
    message for any validation failure so the caller can surface it as a
    flash without parsing the exception.
    """
    if upload is None or not (upload.filename or "").strip():
        return None
    # Read the body once into memory — the cap is 512 KiB, so this is fine.
    # Read +1 byte to detect overruns deterministically rather than trust
    # the multipart parser's size accounting.
    blob = upload.file.read(MAX_LOGO_BYTES + 1)
    if not blob:
        return None
    if len(blob) > MAX_LOGO_BYTES:
        raise HTTPException(
            413,
            f"Logo exceeds {MAX_LOGO_BYTES // 1024} KB limit",
        )

    # Trust the declared content-type, but only if it's in the allowlist.
    # For SVG we additionally do a cheap sniff for opening <svg> to catch
    # files that lied about their type (an HTML disguised as SVG could pivot
    # XSS via the /branding/<name> route's image/svg+xml content-type).
    ct = (upload.content_type or "").lower().split(";")[0].strip()
    if ct not in ALLOWED_LOGO_TYPES:
        # Fall back to extension-based guess for clients that don't set type.
        guessed, _ = mimetypes.guess_type(upload.filename or "")
        if guessed and guessed.lower() in ALLOWED_LOGO_TYPES:
            ct = guessed.lower()
        else:
            raise HTTPException(
                400,
                "Logo must be PNG, JPEG, SVG, or WebP",
            )

    if ct == "image/svg+xml":
        head = blob.lstrip()[:200].lower()
        if b"<svg" not in head:
            raise HTTPException(400, "Logo does not look like a valid SVG")

    ext = ALLOWED_LOGO_TYPES[ct]
    # Derive a basename from the upload filename, sanitized to the safe
    # whitelist. Append a short random suffix to avoid CDN-cache collisions
    # when an admin replaces a logo with a new file of the same name.
    stem = Path(upload.filename or "logo").stem
    safe_stem = _LOGO_BASENAME_RE.sub("", stem.replace(" ", "-"))[:40] or "logo"
    final_name = f"{safe_stem}-{secrets.token_hex(4)}{ext}"

    target = branding_dir() / final_name
    target.write_bytes(blob)
    return final_name


def _clear_existing_logo(db: Session) -> None:
    """Delete the on-disk logo (if any) and clear the Setting row.

    Safe to call when no logo is configured (no-op). Doesn't raise on
    filesystem errors — the DB pointer is the source of truth and the file
    becomes orphaned, which a future settings page load will tolerate.
    """
    existing = (get_setting(db, "branding_logo_path") or "").strip()
    if existing:
        from portal.branding import _safe_logo_name

        if _safe_logo_name(existing):
            path = branding_dir() / existing
            try:
                path.unlink()
            except OSError:
                pass
    set_setting(db, "branding_logo_path", None)


def _store_favicon(upload: UploadFile) -> Optional[str]:
    """Validate, persist, and return the filename of an uploaded favicon.

    Parallel to ``_store_logo`` but accepts ``.ico`` in addition to the
    logo-format set. Same 512 KiB cap, same SVG sniff, same sanitized
    basename + random suffix to avoid CDN-cache collisions on replace.
    Empty upload returns ``None``; validation failures raise HTTPException
    so the caller can surface them as flash messages.
    """
    if upload is None or not (upload.filename or "").strip():
        return None
    blob = upload.file.read(MAX_LOGO_BYTES + 1)
    if not blob:
        return None
    if len(blob) > MAX_LOGO_BYTES:
        raise HTTPException(
            413,
            f"Favicon exceeds {MAX_LOGO_BYTES // 1024} KB limit",
        )

    ct = (upload.content_type or "").lower().split(";")[0].strip()
    if ct not in ALLOWED_FAVICON_TYPES:
        guessed, _ = mimetypes.guess_type(upload.filename or "")
        if guessed and guessed.lower() in ALLOWED_FAVICON_TYPES:
            ct = guessed.lower()
        else:
            raise HTTPException(
                400,
                "Favicon must be PNG, JPEG, SVG, WebP, or ICO",
            )

    if ct == "image/svg+xml":
        head = blob.lstrip()[:200].lower()
        if b"<svg" not in head:
            raise HTTPException(400, "Favicon does not look like a valid SVG")

    ext = ALLOWED_FAVICON_TYPES[ct]
    stem = Path(upload.filename or "favicon").stem
    safe_stem = _LOGO_BASENAME_RE.sub("", stem.replace(" ", "-"))[:40] or "favicon"
    final_name = f"{safe_stem}-{secrets.token_hex(4)}{ext}"

    target = branding_dir() / final_name
    target.write_bytes(blob)
    return final_name


def _clear_existing_favicon(db: Session) -> None:
    """Delete the on-disk favicon (if any) and clear the Setting row.

    Same lenient semantics as ``_clear_existing_logo``: no-op if nothing's
    configured, swallows filesystem errors (orphaned file is tolerable),
    and uses the shared ``_safe_logo_name`` whitelist to guard the unlink.
    """
    existing = (get_setting(db, "branding_favicon_path") or "").strip()
    if existing:
        from portal.branding import _safe_logo_name

        if _safe_logo_name(existing):
            path = branding_dir() / existing
            try:
                path.unlink()
            except OSError:
                pass
    set_setting(db, "branding_favicon_path", None)


@router.post("/admin/settings")
async def settings_save(
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
    branding_business_name: Annotated[str, Form()] = "",
    branding_accent_color: Annotated[str, Form()] = "",
    branding_logo: Optional[UploadFile] = File(default=None),
    branding_logo_clear: Annotated[Optional[str], Form()] = None,
    branding_favicon: Optional[UploadFile] = File(default=None),
    branding_favicon_clear: Annotated[Optional[str], Form()] = None,
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

    # Branding: name + accent are direct settings; logo flows through
    # _store_logo which validates type/size and writes to data/branding/.
    business_name = (branding_business_name or "").strip()[:60]
    set_setting(db, "branding_business_name", business_name)

    accent = (branding_accent_color or "").strip()
    if accent and not _ACCENT_HEX_RE.match(accent):
        # Reject without rolling back the other settings — they're already
        # staged. The flash tells the admin the accent didn't take.
        flash(
            request,
            "Accent color must be a #rrggbb hex value; previous value kept.",
            level="error",
        )
    else:
        # Empty clears back to the default emerald.
        set_setting(db, "branding_accent_color", accent or None)

    if branding_logo_clear == "on":
        _clear_existing_logo(db)
    elif branding_logo is not None:
        try:
            stored = _store_logo(branding_logo)
        except HTTPException as e:
            flash(request, str(e.detail), level="error")
        else:
            if stored is not None:
                _clear_existing_logo(db)
                set_setting(db, "branding_logo_path", stored)

    # Favicon: same control flow as the logo above. Cleared if the
    # ``branding_favicon_clear`` checkbox is on; otherwise a fresh upload
    # replaces the previously-stored file (if any). Validation errors
    # flash without rolling back the other settings that already staged.
    if branding_favicon_clear == "on":
        _clear_existing_favicon(db)
    elif branding_favicon is not None:
        try:
            stored = _store_favicon(branding_favicon)
        except HTTPException as e:
            flash(request, str(e.detail), level="error")
        else:
            if stored is not None:
                _clear_existing_favicon(db)
                set_setting(db, "branding_favicon_path", stored)

    db.commit()
    # Capture the high-signal subset of what changed in details — full diffs
    # would be too noisy for the audit table given how many settings ride
    # this single endpoint. The interesting forensic question is "was SMTP
    # reconfigured / branding swapped / default access flipped" rather than
    # "what was every byte of every form field".
    record_event(
        db, actor=admin, action="settings.save", request=request,
        target="settings",
        details={
            "smtp_host_set": bool(smtp_host.strip()),
            "smtp_password_changed": bool(smtp_password),
            "branding_name_set": bool(business_name),
            "branding_accent_set": bool(accent),
            "branding_logo_cleared": branding_logo_clear == "on",
            "branding_favicon_cleared": branding_favicon_clear == "on",
            "default_user_app_access": default_user_app_access,
        },
    )
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
    from portal.health import record_smtp_test

    try:
        send_message(msg, cfg)
    except Exception as e:
        record_smtp_test(db, success=False, error=str(e))
        record_event(
            db, actor=admin, action="settings.smtp.test", request=request,
            target="settings",
            details={"success": False, "error": str(e)[:200]},
        )
        flash(request, f"SMTP test failed: {e}", level="error")
        return RedirectResponse("/admin/settings", status_code=303)
    record_smtp_test(db, success=True)
    record_event(
        db, actor=admin, action="settings.smtp.test", request=request,
        target="settings",
        details={"success": True, "recipient": admin.email},
    )
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
    record_event(
        db, actor=admin, action="token.create", request=request,
        target=f"token:{name}",
        details={"prefix": raw[:8]},
    )
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
    prefix = token.prefix
    db.delete(token)
    db.commit()
    record_event(
        db, actor=admin, action="token.revoke", request=request,
        target=f"token:{name}",
        details={"prefix": prefix},
    )
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
    record_event(
        db, actor=admin, action="user.create", request=request,
        target=f"user:{validated_email}",
        details={"role": role},
    )
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
    old_role = target.role
    target.role = role
    db.add(target)
    db.commit()
    record_event(
        db, actor=admin, action="user.role.change", request=request,
        target=f"user:{target.email}",
        details={"from": old_role, "to": role},
    )
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
    record_event(
        db, actor=admin, action="user.reset_password", request=request,
        target=f"user:{target.email}",
    )
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
    role = target.role
    # SQLite FKs are advisory here, so drop the access rows explicitly before
    # the User row goes away to keep the table consistent. We also have to
    # cascade tokens and sessions: SQLite reuses INTEGER PRIMARY KEY rowids
    # when the highest-id row is deleted, so any lingering ApiToken,
    # UserSession, AppSession, or AppLaunchToken whose user_id points at the
    # deleted row would silently re-authenticate as a future new user that
    # inherits the same id.
    if target.id is not None:
        delete_access_for_user(db, target.id)
        revoke_all_for_user(db, target.id)
        revoke_all_app_sessions_for_user(db, target.id)
        db.exec(delete(ApiToken).where(ApiToken.created_by == target.id))
        db.exec(delete(AppLaunchToken).where(AppLaunchToken.user_id == target.id))
    db.delete(target)
    db.commit()
    record_event(
        db, actor=admin, action="user.delete", request=request,
        target=f"user:{email}",
        details={"role": role},
    )
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
    record_event(
        db, actor=admin, action="user.apps.update", request=request,
        target=f"user:{target.email}",
        details={"app_ids": allowed},
    )
    flash(request, f"Updated app access for {target.email}.")
    return RedirectResponse(f"/admin/users/{user_id}/apps", status_code=303)


# ----- Health dashboard -----

@router.get("/admin/health")
def health_dashboard(request: Request, db: DbDep, admin: AdminDep):
    """Operational snapshot for admins.

    Reads are all cheap (one SELECT per table, two filesystem walks scoped
    to ``data/``). The page is not cached — admins viewing it expect live
    numbers, and the SMB-scale data sets stay small enough that re-walking
    on each load is fine.
    """
    from sqlalchemy.engine.url import make_url

    from portal.config import settings as _settings
    from portal.health import (
        db_size_bytes,
        fmt_bytes,
        recent_email_sends,
        recent_login_attempts,
        smtp_last_test,
        storage_usage_by_app,
    )

    data_dir = Path(_settings.data_dir).resolve()
    # Best-effort parse of the configured DB URL. We only support sqlite
    # in this deployment, but keep the code defensive in case someone
    # points DATABASE_URL elsewhere — the helper returns 0 on missing.
    url = make_url(_settings.database_url)
    db_path = Path(url.database) if url.database else (data_dir / "portal.db")
    if not db_path.is_absolute():
        # Relative DB paths resolve against the working directory the
        # portal was started in (which is the repo root in dev, /app in
        # the container). Anchor to that explicitly.
        db_path = Path.cwd() / db_path

    storage_root = data_dir / "storage"
    usage = storage_usage_by_app(storage_root)
    # Join app metadata so the dashboard can show name + status alongside
    # raw byte counts.
    all_apps = db.exec(select(App).order_by(App.name)).all()
    storage_rows = []
    for app_row in all_apps:
        bytes_used = usage.get(app_row.slug, 0)
        storage_rows.append({
            "slug": app_row.slug,
            "name": app_row.name,
            "enabled": app_row.enabled,
            "bytes": bytes_used,
            "human": fmt_bytes(bytes_used),
        })
    # Plus any storage dirs whose App row was deleted but whose data
    # lingered (storage_* doesn't currently delete on app delete).
    known_slugs = {row["slug"] for row in storage_rows}
    for slug, bytes_used in usage.items():
        if slug not in known_slugs:
            storage_rows.append({
                "slug": slug,
                "name": "(orphaned)",
                "enabled": False,
                "bytes": bytes_used,
                "human": fmt_bytes(bytes_used),
            })
    storage_rows.sort(key=lambda r: r["bytes"], reverse=True)
    total_storage = sum(r["bytes"] for r in storage_rows)

    return render(
        request, "admin_health.html",
        user=admin,
        db_path=str(db_path),
        db_size_human=fmt_bytes(db_size_bytes(db_path)),
        storage_rows=storage_rows,
        total_storage_human=fmt_bytes(total_storage),
        smtp_last=smtp_last_test(db),
        recent_logins=recent_login_attempts(db, limit=20),
        recent_emails=recent_email_sends(db, limit=20),
    )


# ----- Audit log -----

@router.get("/admin/audit")
def audit_log(request: Request, db: DbDep, admin: AdminDep):
    """Forensic view of every state-changing action.

    Renders the most recent 200 events newest-first. Each row shows who
    did what, when, where from (IP), and an optional details dict. The
    underlying table is bounded by ``portal.audit.MAX_ROWS`` so this query
    is always cheap.
    """
    events = recent_events(db, limit=200)
    return render(
        request, "admin_audit.html",
        user=admin, events=events,
    )


# ----- Public share links -----

@router.get("/admin/shares")
def shares_list(request: Request, db: DbDep, admin: AdminDep):
    """Surface every share link with metadata + a revoke action.

    Shows both active and inactive (revoked, expired, view-exhausted)
    entries — the dashboard is for audit, not just management, and an
    admin investigating "did we send out a link?" needs to see expired
    rows too.
    """
    from portal.models import ShareLink as _ShareLink

    rows = db.exec(
        select(_ShareLink).order_by(_ShareLink.created_at.desc()).limit(200)
    ).all()
    # Join app + creator info for display. Two cheap dicts so we don't
    # N+1 the template.
    app_map = {a.id: a for a in db.exec(select(App)).all()}
    user_map = {u.id: u for u in db.exec(select(User)).all()}
    now = datetime.now(timezone.utc)

    items = []
    for r in rows:
        app_row = app_map.get(r.app_id)
        user_row = user_map.get(r.created_by)
        expires_at = r.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if r.revoked_at is not None:
            state = "revoked"
        elif expires_at is None or expires_at < now:
            state = "expired"
        elif r.max_views and r.view_count >= r.max_views:
            state = "exhausted"
        else:
            state = "active"
        items.append({
            "row": r,
            "app_name": app_row.name if app_row else "(deleted app)",
            "app_slug": app_row.slug if app_row else "",
            "creator": user_row.email if user_row else "(deleted user)",
            "state": state,
        })
    return render(
        request, "admin_shares.html", user=admin, items=items,
        site_url=settings.site_url,
    )


@router.post("/admin/shares/{share_id}/revoke")
def shares_revoke(
    share_id: int, request: Request, db: DbDep, admin: AdminDep,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    from portal.models import ShareLink as _ShareLink
    from portal.shares import revoke as _revoke

    row = db.get(_ShareLink, share_id)
    if row is None:
        raise HTTPException(404)
    if row.revoked_at is not None:
        flash(request, "Already revoked.")
        return RedirectResponse("/admin/shares", status_code=303)
    _revoke(db, row)
    record_event(
        db, actor=admin, action="share.revoke", request=request,
        target=f"share:{row.id}",
        details={"kind": row.kind, "app_id": row.app_id},
    )
    flash(request, "Share link revoked.")
    return RedirectResponse("/admin/shares", status_code=303)


@router.post("/admin/shares/{share_id}/delete")
def shares_delete(
    share_id: int, request: Request, db: DbDep, admin: AdminDep,
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    from portal.models import ShareLink as _ShareLink
    from portal.shares import delete_share_files

    row = db.get(_ShareLink, share_id)
    if row is None:
        raise HTTPException(404)
    share_id_audit = row.id
    share_kind = row.kind
    share_app = row.app_id
    # If it's a PDF kind, remove the on-disk file as well.
    if row.kind == "pdf":
        delete_share_files([(row.payload or {}).get("path", "")])
    db.delete(row)
    db.commit()
    record_event(
        db, actor=admin, action="share.delete", request=request,
        target=f"share:{share_id_audit}",
        details={"kind": share_kind, "app_id": share_app},
    )
    flash(request, "Share link deleted.")
    return RedirectResponse("/admin/shares", status_code=303)


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
    record_event(
        db, actor=admin, action="backup.download", request=request,
        target=f"backup:{filename}",
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
