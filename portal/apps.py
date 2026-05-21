"""Child PWA bundle: upload, validation, extraction, serving."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlmodel import Session, select

from portal.config import settings
from portal.db import get_db
from portal.deps import current_user, current_user_or_token, require_admin
from portal.models import App, User
from portal.security import check_csrf
from portal.web import flash, render

router = APIRouter()

# ----- Manifest schema -----

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ALLOWED_SERVICES = {"pdf", "email", "storage"}


class PortalAppManifest(BaseModel):
    slug: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=60)
    version: str = Field(min_length=1, max_length=20)
    description: Optional[str] = Field(default=None, max_length=200)
    icon: Optional[str] = None
    entry: str = "index.html"
    services: list[str] = Field(default_factory=list)
    # Reserved for future portal/app compatibility checks; accepted but unused today.
    min_portal_version: Optional[str] = None

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must be lowercase kebab-case (a-z, 0-9, hyphens)")
        return v

    @field_validator("services")
    @classmethod
    def _services_known(cls, v: list[str]) -> list[str]:
        unknown = [s for s in v if s not in ALLOWED_SERVICES]
        if unknown:
            raise ValueError(
                f"unknown service(s): {unknown}; allowed: {sorted(ALLOWED_SERVICES)}"
            )
        return v

    @field_validator("entry", "icon")
    @classmethod
    def _safe_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v.startswith("/") or v.startswith("\\") or ".." in Path(v).parts:
            raise ValueError("paths must be relative and may not contain '..'")
        return v


# ----- Zip safety -----

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_FILES = 1000
MAX_COMPRESS_RATIO = 50


class UploadError(Exception):
    pass


def _apps_root() -> Path:
    p = Path(settings.data_dir).resolve() / "apps"
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _stream_to_temp(upload: UploadFile, max_bytes: int) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_name = tmp.name
    written = 0
    try:
        while True:
            chunk = await upload.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise UploadError(
                    f"Upload exceeds {max_bytes // (1024 * 1024)}MB limit"
                )
            tmp.write(chunk)
        tmp.close()
        return Path(tmp_name)
    except BaseException:
        # Close first so the unlink can succeed on platforms that hold a lock
        # on open files, then remove the partial file. Avoids the prior
        # close-after-unlink ordering where a finally close could double-act.
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _validate_zip(path: Path) -> None:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UploadError(f"Not a valid zip file: {e}")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise UploadError(f"Too many files in zip ({len(infos)} > {MAX_FILES})")
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename
            # Explicit backslash rejection: Windows separators don't survive
            # Path(name).parts on POSIX, so a substring check is required.
            if "\\" in name:
                raise UploadError(f"Unsafe path in zip (backslash): {name}")
            if name.startswith("/"):
                raise UploadError(f"Unsafe path in zip: {name}")
            parts = Path(name).parts
            if ".." in parts or any(p.startswith("\\") for p in parts):
                raise UploadError(f"Unsafe path in zip: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                # POSIX-created symlink entry.
                raise UploadError(f"Symlink not allowed: {name}")
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise UploadError(
                    f"Uncompressed size exceeds {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB"
                )
            if info.compress_size > 0 and info.file_size > 100 * 1024:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESS_RATIO:
                    raise UploadError(f"Suspicious compression ratio in {name}")


def _read_manifest(zip_path: Path) -> PortalAppManifest:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                with zf.open("portal.json") as f:
                    raw = json.load(f)
            except KeyError:
                raise UploadError("portal.json missing from zip root")
            except json.JSONDecodeError as e:
                raise UploadError(f"portal.json is not valid JSON: {e}")
    except zipfile.BadZipFile as e:
        raise UploadError(f"Not a valid zip file: {e}")
    try:
        return PortalAppManifest(**raw)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "root"
            msgs.append(f"{loc}: {err['msg']}")
        raise UploadError("portal.json validation failed: " + "; ".join(msgs))


def _check_required_files(zip_path: Path, manifest: PortalAppManifest) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if manifest.entry not in names:
            raise UploadError(f"entry file '{manifest.entry}' not found in zip")
        if manifest.icon and manifest.icon not in names:
            raise UploadError(f"icon file '{manifest.icon}' not found in zip")


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                raise UploadError(f"path escape during extract: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _prepare_bundle(tmp_path: Path) -> PortalAppManifest:
    """Blocking validation + manifest parse (run via to_thread)."""
    _validate_zip(tmp_path)
    manifest = _read_manifest(tmp_path)
    _check_required_files(tmp_path, manifest)
    return manifest


def _extract_into(tmp_path: Path, dest: Path) -> None:
    """Blocking extraction (run via to_thread)."""
    _safe_extract(tmp_path, dest)


def _atomic_replace(dest: Path, new_dir: Path) -> None:
    """Swap `new_dir` into `dest`, moving the old `dest` aside and removing it.

    Both paths must live under the same parent directory so the renames are
    atomic on POSIX.
    """
    suffix = secrets.token_hex(6)
    old_dir = dest.with_name(dest.name + f".old-{suffix}")
    if dest.exists():
        os.rename(dest, old_dir)
    try:
        os.rename(new_dir, dest)
    except OSError:
        # Roll back: try to restore old_dir if we moved it.
        if old_dir.exists() and not dest.exists():
            try:
                os.rename(old_dir, dest)
            except OSError:
                pass
        raise
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)


# ----- Deps -----

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[Optional[User], Depends(current_user)]
TokenUserDep = Annotated[Optional[User], Depends(current_user_or_token)]
AdminDep = Annotated[User, Depends(require_admin)]


# ----- Admin routes -----

@router.get("/admin/apps")
def admin_apps_list(request: Request, db: DbDep, admin: AdminDep):
    apps = db.exec(select(App).order_by(App.uploaded_at.desc())).all()
    return render(request, "admin_apps.html", user=admin, apps=apps)


@router.get("/admin/apps/upload")
def admin_apps_upload_form(request: Request, admin: AdminDep):
    return render(request, "admin_apps_upload.html", user=admin)


@dataclass
class InstallResult:
    slug: str
    name: str
    version: str
    replaced: bool = False


async def install_bundle(
    db: Session,
    uploader: User,
    bundle: UploadFile,
    *,
    allow_replace: bool = False,
    expected_slug: Optional[str] = None,
) -> InstallResult:
    """Validate, extract, and register a child-app zip.

    When ``allow_replace`` is False (default) and the slug already exists, an
    ``UploadError`` is raised. When True, the on-disk app directory is swapped
    atomically and the matching DB row is updated in place; per-user storage
    under ``data/storage/<slug>/`` is left untouched.

    When ``expected_slug`` is set, the manifest's slug must match it; otherwise
    an ``UploadError`` is raised before any filesystem or DB mutation. Used by
    the replace endpoints to prevent a cross-slug overwrite where a bundle
    whose manifest names slug "B" is uploaded to the URL for slug "A".

    Raises ``UploadError`` on any failure.
    """
    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise UploadError("Please upload a .zip file.")

    tmp_path = await _stream_to_temp(bundle, MAX_ZIP_BYTES)
    try:
        # Blocking zip/file work moves to a worker thread so we don't block the
        # event loop on large uploads. DB writes stay on the main thread.
        manifest = await anyio.to_thread.run_sync(_prepare_bundle, tmp_path)

        if expected_slug is not None and manifest.slug != expected_slug:
            raise UploadError(
                f"Bundle slug '{manifest.slug}' does not match URL slug "
                f"'{expected_slug}'."
            )

        existing = db.exec(select(App).where(App.slug == manifest.slug)).first()
        apps_root = _apps_root()
        dest = apps_root / manifest.slug

        if existing is not None and not allow_replace:
            raise UploadError(
                f"An app with slug '{manifest.slug}' already exists. "
                "Use the replace endpoint (--replace in the skill CLI) to update it."
            )

        if existing is None:
            if dest.exists():
                raise UploadError(
                    f"App directory already exists on disk at {dest}. "
                    "Remove it manually and try again."
                )
            try:
                await anyio.to_thread.run_sync(_extract_into, tmp_path, dest)
            except UploadError:
                shutil.rmtree(dest, ignore_errors=True)
                raise

            app_row = App(
                slug=manifest.slug,
                name=manifest.name,
                description=manifest.description,
                version=manifest.version,
                icon=manifest.icon,
                entry=manifest.entry,
                services=manifest.services,
                enabled=True,
                uploaded_by=uploader.id,
            )
            db.add(app_row)
            db.commit()
            return InstallResult(
                slug=app_row.slug,
                name=app_row.name,
                version=app_row.version,
                replaced=False,
            )

        # Replace path: extract to a sibling temp dir, then atomically swap.
        staging_name = f".{manifest.slug}.new-{secrets.token_hex(6)}"
        staging = apps_root / staging_name
        try:
            await anyio.to_thread.run_sync(_extract_into, tmp_path, staging)
        except UploadError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        try:
            await anyio.to_thread.run_sync(_atomic_replace, dest, staging)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        existing.name = manifest.name
        existing.description = manifest.description
        existing.version = manifest.version
        existing.icon = manifest.icon
        existing.entry = manifest.entry
        existing.services = manifest.services
        existing.uploaded_by = uploader.id
        existing.uploaded_at = datetime.now(timezone.utc)
        db.add(existing)
        db.commit()
        return InstallResult(
            slug=existing.slug,
            name=existing.name,
            version=existing.version,
            replaced=True,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.post("/admin/apps/upload")
async def admin_apps_upload(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    bundle: UploadFile = File(...),
    csrf: str = Form(default="", alias="_csrf"),
):
    check_csrf(request, csrf)
    try:
        result = await install_bundle(db, admin, bundle)
    except UploadError as e:
        return render(
            request, "admin_apps_upload.html",
            user=admin, error=str(e), status_code=400,
        )
    flash(request, f"Uploaded ‘{result.name}’ (slug: {result.slug})")
    return RedirectResponse("/admin/apps", status_code=303)


@router.post("/admin/apps/{slug}/replace")
async def admin_apps_replace(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    bundle: UploadFile = File(...),
    csrf: str = Form(default="", alias="_csrf"),
):
    check_csrf(request, csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404)
    try:
        result = await install_bundle(
            db, admin, bundle, allow_replace=True, expected_slug=slug,
        )
    except UploadError as e:
        return render(
            request, "admin_apps_upload.html",
            user=admin, error=str(e), status_code=400,
        )
    flash(request, f"Replaced ‘{result.name}’ (slug: {result.slug})")
    return RedirectResponse("/admin/apps", status_code=303)


@router.put("/api/v1/apps/{slug}")
async def api_apps_replace(
    slug: str,
    db: DbDep,
    user: TokenUserDep,
    bundle: UploadFile = File(...),
):
    if user is None:
        raise HTTPException(401, "Sign in required")
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404, f"App '{slug}' not found")
    try:
        result = await install_bundle(
            db, user, bundle, allow_replace=True, expected_slug=slug,
        )
    except UploadError as e:
        raise HTTPException(400, str(e))
    return {
        "slug": result.slug,
        "name": result.name,
        "version": result.version,
        "replaced": True,
    }


@router.post("/admin/apps/{slug}/toggle")
def admin_apps_toggle(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: str = Form(default="", alias="_csrf"),
):
    check_csrf(request, csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404)
    app_row.enabled = not app_row.enabled
    db.add(app_row)
    db.commit()
    state = "enabled" if app_row.enabled else "disabled"
    flash(request, f"{app_row.name} {state}")
    return RedirectResponse("/admin/apps", status_code=303)


@router.post("/admin/apps/{slug}/delete")
def admin_apps_delete(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: str = Form(default="", alias="_csrf"),
):
    check_csrf(request, csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404)

    apps_root = _apps_root()
    dest = (apps_root / app_row.slug).resolve()
    try:
        dest.relative_to(apps_root)
    except ValueError:
        raise HTTPException(500, "App path escape — refusing delete")

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    name = app_row.name
    db.delete(app_row)
    db.commit()
    flash(request, f"Deleted {name}")
    return RedirectResponse("/admin/apps", status_code=303)


# ----- Child app serving -----

@router.get("/apps/{slug}", include_in_schema=False)
def app_root_redirect(slug: str):
    return RedirectResponse(f"/apps/{slug}/", status_code=308)


@router.get("/apps/{slug}/")
def serve_app_index(slug: str, request: Request, db: DbDep, user: UserDep):
    return _serve_app_file(slug, db, user, path="")


@router.get("/apps/{slug}/{path:path}")
def serve_app_file(
    slug: str, path: str, request: Request, db: DbDep, user: UserDep
):
    return _serve_app_file(slug, db, user, path=path)


def _serve_app_file(
    slug: str, db: Session, user: Optional[User], path: str
):
    if user is None:
        return RedirectResponse(f"/login?next=/apps/{slug}/", status_code=303)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None or not app_row.enabled:
        raise HTTPException(404)

    apps_root = _apps_root()
    app_dir = (apps_root / slug).resolve()
    try:
        app_dir.relative_to(apps_root)
    except ValueError:
        raise HTTPException(404)

    if path in ("", "/"):
        path = app_row.entry

    target = (app_dir / path).resolve()
    try:
        target.relative_to(app_dir)
    except ValueError:
        raise HTTPException(404)
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)
