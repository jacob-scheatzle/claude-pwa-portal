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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

import anyio
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import delete
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from portal.access import (
    delete_access_for_app,
    grant_default_access_for_new_app,
    user_can_access_app,
)
from portal.audit import record_event
from portal.config import settings
from portal.db import get_db
from portal.deps import (
    APP_SESSION_COOKIE,
    current_user,
    current_user_or_token,
    require_admin,
    require_user,
)
from portal.models import (
    App,
    AppLaunchToken,
    FormSubmission,
    ScheduledRun,
    ShareLink,
    User,
)
from portal.security import check_csrf
from portal.sessions import revoke_all_app_sessions_for_slug
from portal.storage_backend import get_storage
from portal.web import STATIC_DIR, flash, render

# How long a launch token is honored. The token is single-use and is consumed
# almost immediately on the subdomain handoff; 60 s leaves slack for slow
# devices / browser load while keeping the replay window narrow.
_LAUNCH_TOKEN_TTL = timedelta(seconds=60)

router = APIRouter()

# ----- Manifest schema -----

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ALLOWED_SERVICES = {"pdf", "email", "storage"}

# Icon file extensions an app may declare. Raster formats render harmlessly as
# images; ``.svg`` is permitted but the dashboard-icon endpoint forces it to be
# served as a non-rendering attachment (stored-XSS guard) — see ``app_icon``.
ALLOWED_ICON_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".gif", ".svg"}

# Validates an HTTPS origin: scheme://hostname[:port], no path / query /
# fragment. Hostname segments follow standard DNS label rules; uppercase is
# accepted in the manifest and normalized to lowercase before storage so
# Caddy's lowercased Host matches at request time. We deliberately require
# HTTPS — child apps loaded over HTTPS (the only mode the portal supports
# in production) would generate mixed-content warnings if they fetched
# plain HTTP anyway, and allowing ``http://`` would be a footgun.
_ORIGIN_RE = re.compile(
    r"^https://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
    r"(?::[0-9]{1,5})?$"
)
# Hard cap on how many origins a single manifest can declare. Protects the
# CSP header from getting absurdly long and protects the admin UI from
# becoming unreadable. Real apps need a handful.
MAX_REQUESTED_ORIGINS = 12


class PortalAppPermissions(BaseModel):
    # Declarative list of external HTTPS origins the app's ``fetch()`` /
    # ``XMLHttpRequest`` calls need to reach. The portal applies these to
    # the ``connect-src`` directive of the per-app Content-Security-Policy.
    # Same-origin requests (to the app's own subdomain or the SDK) are
    # always allowed and don't need to be listed.
    network: list[str] = Field(default_factory=list)
    # Opt into a strict per-app Content-Security-Policy: drops
    # ``'unsafe-inline'`` and ``'unsafe-eval'``; the portal substitutes the
    # literal token ``{{NONCE}}`` in served HTML with a per-response nonce
    # so legitimate inline scripts/styles can still run. Only takes effect
    # under per-app-origin mode (the default). See SKILL.md for the author
    # contract.
    csp_strict: bool = False

    @field_validator("network")
    @classmethod
    def _origins_valid(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_REQUESTED_ORIGINS:
            raise ValueError(
                f"too many network origins ({len(v)} > {MAX_REQUESTED_ORIGINS})"
            )
        out: list[str] = []
        seen: set[str] = set()
        for raw in v:
            if not isinstance(raw, str):
                raise ValueError("network origins must be strings")
            norm = raw.strip().lower().rstrip("/")
            if not _ORIGIN_RE.match(norm):
                raise ValueError(
                    f"invalid origin '{raw}'; expected https://hostname[:port] "
                    "with no path"
                )
            if norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
        return out


# ----- Declarative app tools (Phase 2) -----
#
# An app can declare ``tools`` in its manifest: named, parameterized operations
# the portal exposes over its MCP server (``portal/mcp_server.py``) and runs
# server-side with the declarative executor (``portal/app_tools.py``). The
# executor only ever calls the portal's OWN trusted primitives — render an HTML
# template to PDF, then share / email / store / return it — so the app's
# uploaded code never runs server-side and the per-app-origin trust model holds.

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_TOOLS = 20
MAX_TOOL_PARAMS = 20
MAX_TOOL_FIELDS = 12
MAX_TOOL_TEMPLATE_BYTES = 100 * 1024
TOOL_PARAM_TYPES = {"string", "number", "boolean"}        # scalar + array-element types
TOOL_PARAM_TYPES_TOP = TOOL_PARAM_TYPES | {"array"}        # a top-level param may also be an array of objects
TOOL_DELIVER_KINDS = {"share", "download", "email", "store"}


class PortalToolField(BaseModel):
    """One field of an array param's element object. Scalars only — an array
    param holds a flat list of objects (e.g. invoice line items), not nested
    arrays."""
    name: str = Field(min_length=1, max_length=40)
    type: str = "string"
    required: bool = False
    description: str = Field(default="", max_length=200)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError(
                f"field name '{v}' must be lowercase snake_case (a-z, 0-9, _)"
            )
        return v

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in TOOL_PARAM_TYPES:
            raise ValueError(
                f"field type '{v}' must be one of {sorted(TOOL_PARAM_TYPES)}"
            )
        return v


class PortalToolParam(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    type: str = "string"
    required: bool = False
    description: str = Field(default="", max_length=200)
    # Only meaningful when ``type == "array"``: the object shape of each list
    # element. The template iterates with ``{% for item in <name> %}``.
    fields: list[PortalToolField] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError(
                f"param name '{v}' must be lowercase snake_case (a-z, 0-9, _)"
            )
        return v

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in TOOL_PARAM_TYPES_TOP:
            raise ValueError(
                f"param type '{v}' must be one of {sorted(TOOL_PARAM_TYPES_TOP)}"
            )
        return v

    @model_validator(mode="after")
    def _array_fields_ok(self) -> "PortalToolParam":
        if self.type == "array":
            if not self.fields:
                raise ValueError(
                    f"array param '{self.name}' requires a non-empty 'fields' list"
                )
            if len(self.fields) > MAX_TOOL_FIELDS:
                raise ValueError(
                    f"array param '{self.name}' has too many fields "
                    f"({len(self.fields)} > {MAX_TOOL_FIELDS})"
                )
            names = [f.name for f in self.fields]
            dups = sorted({n for n in names if names.count(n) > 1})
            if dups:
                raise ValueError(
                    f"duplicate field names in array param '{self.name}': {dups}"
                )
        elif self.fields:
            raise ValueError(
                f"param '{self.name}' is type '{self.type}' but declares 'fields' "
                "(only 'array' params may)"
            )
        return self


class PortalToolRender(BaseModel):
    # Inline HTML template rendered server-side with the tool's params via a
    # sandboxed, autoescaping Jinja environment (see portal/app_tools.py).
    # ``{{ param }}`` placeholders are substituted with escaped values; no
    # external resources are fetched (same WeasyPrint URL fetcher as the SDK).
    html: str = Field(min_length=1, max_length=MAX_TOOL_TEMPLATE_BYTES)
    filename: str = Field(default="document.pdf", max_length=80)
    branded: bool = False


class PortalToolDeliver(BaseModel):
    # What to do with the rendered document. ``to``/``subject``/``key`` may
    # contain ``{{ param }}`` placeholders, substituted at run time.
    kind: str
    to: Optional[str] = Field(default=None, max_length=400)       # email
    subject: Optional[str] = Field(default=None, max_length=200)  # email
    key: Optional[str] = Field(default=None, max_length=200)      # store
    ttl_days: Optional[int] = Field(default=None, ge=1, le=90)    # share

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v: str) -> str:
        if v not in TOOL_DELIVER_KINDS:
            raise ValueError(
                f"deliver.kind must be one of {sorted(TOOL_DELIVER_KINDS)}"
            )
        return v

    @model_validator(mode="after")
    def _kind_requirements(self) -> "PortalToolDeliver":
        if self.kind == "email" and not (self.to and self.to.strip()):
            raise ValueError("deliver.kind 'email' requires 'to'")
        if self.kind == "store" and not (self.key and self.key.strip()):
            raise ValueError("deliver.kind 'store' requires 'key'")
        return self


class PortalAppTool(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=300)
    params: list[PortalToolParam] = Field(default_factory=list)
    render: PortalToolRender
    deliver: PortalToolDeliver

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError(
                f"tool name '{v}' must be lowercase snake_case (a-z, 0-9, _)"
            )
        return v

    @field_validator("params")
    @classmethod
    def _params_ok(cls, v: list) -> list:
        if len(v) > MAX_TOOL_PARAMS:
            raise ValueError(f"too many params ({len(v)} > {MAX_TOOL_PARAMS})")
        names = [p.name for p in v]
        dups = sorted({n for n in names if names.count(n) > 1})
        if dups:
            raise ValueError(f"duplicate param names: {dups}")
        return v

    def required_services(self) -> set[str]:
        """Portal services this tool's recipe needs — drives the manifest
        cross-check below and execution-time gating. ``share``/``download``
        render a PDF (``pdf``); ``store`` renders then saves it (``pdf`` +
        ``storage``); ``email`` sends the rendered HTML as the message body,
        so it needs only ``email`` — no PDF is produced."""
        if self.deliver.kind == "email":
            return {"email"}
        if self.deliver.kind == "store":
            return {"pdf", "storage"}
        return {"pdf"}  # share, download


# ----- Declarative intake forms -----
#
# An app can declare ``forms`` in its manifest: public, no-sign-in intake forms
# served at ``/forms/<slug>/<form>``. Each submission is stored as a
# ``FormSubmission`` row for the business owner to read (/admin/submissions) and
# export. Like tools, this is purely declarative — the portal renders the form
# and records the submission; no uploaded code runs server-side.

FORM_FIELD_TYPES = {"text", "email", "tel", "number", "textarea"}
MAX_FORMS = 10
MAX_FORM_FIELDS = 25


class PortalAppFormField(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    type: str = "text"
    required: bool = False
    placeholder: Optional[str] = Field(default=None, max_length=120)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError(
                f"form field name '{v}' must be lowercase snake_case (a-z, 0-9, _)"
            )
        return v

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in FORM_FIELD_TYPES:
            raise ValueError(
                f"unknown field type '{v}'; allowed: {sorted(FORM_FIELD_TYPES)}"
            )
        return v


class PortalAppForm(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    fields: list[PortalAppFormField] = Field(default_factory=list)
    # Where to email a notification when someone submits (optional). Validated
    # for header safety so a manifest value can't inject extra recipients/headers.
    notify_email: Optional[str] = Field(default=None, max_length=200)
    success_message: Optional[str] = Field(default=None, max_length=300)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError(
                f"form name '{v}' must be lowercase snake_case (a-z, 0-9, _)"
            )
        return v

    @field_validator("notify_email")
    @classmethod
    def _notify_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        if "@" not in v or any(c in v for c in "\r\n,;"):
            raise ValueError("notify_email must be a single valid email address")
        return v

    @field_validator("fields")
    @classmethod
    def _fields_ok(cls, v: list) -> list:
        if not v:
            raise ValueError("a form must declare at least one field")
        if len(v) > MAX_FORM_FIELDS:
            raise ValueError(f"too many form fields ({len(v)} > {MAX_FORM_FIELDS})")
        names = [f.name for f in v]
        dups = sorted({n for n in names if names.count(n) > 1})
        if dups:
            raise ValueError(f"duplicate form field names: {dups}")
        return v


class PortalAppManifest(BaseModel):
    slug: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=60)
    version: str = Field(min_length=1, max_length=20)
    description: Optional[str] = Field(default=None, max_length=200)
    icon: Optional[str] = None
    entry: str = "index.html"
    services: list[str] = Field(default_factory=list)
    permissions: PortalAppPermissions = Field(default_factory=PortalAppPermissions)
    # Reserved for future portal/app compatibility checks; accepted but unused today.
    min_portal_version: Optional[str] = None
    # Phase 2: declarative MCP tools (see PortalAppTool). Optional; empty for
    # apps that don't expose tools.
    tools: list[PortalAppTool] = Field(default_factory=list)
    # Public intake forms (see PortalAppForm). Optional; served at
    # /forms/<slug>/<form>.
    forms: list[PortalAppForm] = Field(default_factory=list)

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

    @field_validator("icon")
    @classmethod
    def _icon_ext(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if Path(v).suffix.lower() not in ALLOWED_ICON_EXTS:
            raise ValueError(
                f"icon must be one of {sorted(ALLOWED_ICON_EXTS)}"
            )
        return v

    @field_validator("tools")
    @classmethod
    def _tools_unique(cls, v: list) -> list:
        if len(v) > MAX_TOOLS:
            raise ValueError(f"too many tools ({len(v)} > {MAX_TOOLS})")
        names = [t.name for t in v]
        dups = sorted({n for n in names if names.count(n) > 1})
        if dups:
            raise ValueError(f"duplicate tool names: {dups}")
        return v

    @field_validator("forms")
    @classmethod
    def _forms_unique(cls, v: list) -> list:
        if len(v) > MAX_FORMS:
            raise ValueError(f"too many forms ({len(v)} > {MAX_FORMS})")
        names = [f.name for f in v]
        dups = sorted({n for n in names if names.count(n) > 1})
        if dups:
            raise ValueError(f"duplicate form names: {dups}")
        return v

    @model_validator(mode="after")
    def _tools_declare_services(self) -> "PortalAppManifest":
        # A tool may only use portal services the manifest also declares in
        # ``services`` — so every capability a tool exercises is visible to the
        # admin and revocable through the existing per-app service gate. (An app
        # with tools therefore can't rely on the empty-services "legacy, no
        # gating" shortcut.)
        declared = set(self.services)
        for t in self.tools:
            missing = t.required_services() - declared
            if missing:
                raise ValueError(
                    f"tool '{t.name}' uses service(s) {sorted(missing)} that the "
                    f"manifest's 'services' does not declare"
                )
        return self


# ----- Zip safety -----

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_FILES = 1000
MAX_COMPRESS_RATIO = 50
# Ceiling on HTML files we'll read into memory for per-request ``{{NONCE}}``
# substitution under csp_strict. Files larger than this stream as a normal
# FileResponse — the strict CSP will reject the un-nonced inline script and
# the symptom surfaces in DevTools so the app author knows to split the HTML.
_MAX_NONCE_SUB_BYTES = 256 * 1024


class UploadError(Exception):
    pass


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
        # Track normalized file paths so two entries that collapse to the same
        # on-disk target (e.g. ``a/b.txt`` vs ``a/./b.txt``, or a case-only
        # difference on a case-insensitive filesystem) are rejected rather than
        # silently overwriting each other at extraction time — which would make
        # the served bundle differ from what was validated/reviewed.
        seen: set[str] = set()
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
            norm = os.path.normpath(name).lower()
            if norm in seen:
                raise UploadError(f"Duplicate entry in zip: {name}")
            seen.add(norm)
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


def _build_bundle_zip(slug: str, version: str) -> tuple[Optional[Path], Optional[Path]]:
    """Re-zip an installed app's files into a downloadable bundle.

    Blocking (run via ``to_thread``). The original uploaded ``.zip`` is never
    retained — only the extracted tree under ``apps/<slug>/`` survives — so the
    download is rebuilt from storage on demand. This is backend-agnostic (works
    for both the local and S3 stores) and always reflects what is actually
    installed. Returns ``(zip_path, tmp_dir)``; the caller schedules
    ``shutil.rmtree(tmp_dir)`` once the response has streamed. Returns
    ``(None, None)`` when no bundle files exist for the slug.

    Each blob is read individually, so peak memory is one file — the validated
    bundle is capped at ``MAX_UNCOMPRESSED_BYTES`` / ``MAX_FILES`` at upload.
    """
    storage = get_storage()
    prefix = f"apps/{slug}"
    objects = storage.list(prefix)
    if not objects:
        return None, None
    base = prefix + "/"
    tmp_dir = Path(tempfile.mkdtemp(prefix="portal-bundle-"))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{slug}-{version}").strip("-") or slug
    zip_path = tmp_dir / f"{safe}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for obj in objects:
                arcname = obj.key[len(base):] if obj.key.startswith(base) else obj.key
                if not arcname:
                    continue
                data = storage.read_or_none(obj.key)
                if data is None:
                    # A blob vanished between list and read (concurrent
                    # replace/delete) — skip it rather than abort the archive.
                    continue
                zf.writestr(arcname, data)
        return zip_path, tmp_dir
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ----- Deps -----

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[Optional[User], Depends(current_user)]
TokenUserDep = Annotated[Optional[User], Depends(current_user_or_token)]
AdminDep = Annotated[User, Depends(require_admin)]
RequireUserDep = Annotated[User, Depends(require_user)]


def _apps_redirect(slug: Optional[str] = None) -> RedirectResponse:
    """303 back to the apps admin list.

    When ``slug`` is given, the redirect carries an ``#app-<slug>`` fragment so
    the browser lands back on that row instead of snapping to the top of the
    page after a per-row action (replace, toggle, reorder, services, network).
    Slugs match ``SLUG_RE`` so they're safe to drop into a URL fragment / id.
    """
    target = f"/admin/apps#app-{slug}" if slug else "/admin/apps"
    return RedirectResponse(target, status_code=303)


# ----- Admin routes -----

@router.get("/admin/apps")
def admin_apps_list(request: Request, db: DbDep, admin: AdminDep):
    # Sorted by the admin-controlled display_order so this list matches the
    # tile order on the user-facing dashboard. The up/down chips in the
    # template reshuffle display_order in spaced increments.
    apps = db.exec(select(App).order_by(App.display_order, App.name)).all()
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
    return await install_bundle_from_path(
        db, uploader, tmp_path,
        allow_replace=allow_replace, expected_slug=expected_slug,
    )


async def install_bundle_from_path(
    db: Session,
    uploader: User,
    tmp_path: Path,
    *,
    allow_replace: bool = False,
    expected_slug: Optional[str] = None,
) -> InstallResult:
    """Core install: validate + extract + register a zip already at ``tmp_path``.

    Shared by ``install_bundle`` (the HTTP UploadFile path) and the MCP
    ``upload_app`` tool (the base64 path). Runs the blocking zip/file work on a
    worker thread so the event loop isn't held on large uploads; DB writes stay
    on the main thread. Always removes ``tmp_path`` before returning, on success
    or failure.
    """
    extract_dir: Optional[Path] = None
    try:
        manifest = await anyio.to_thread.run_sync(_prepare_bundle, tmp_path)

        if expected_slug is not None and manifest.slug != expected_slug:
            raise UploadError(
                f"Bundle slug '{manifest.slug}' does not match URL slug "
                f"'{expected_slug}'."
            )

        existing = db.exec(select(App).where(App.slug == manifest.slug)).first()
        storage = get_storage()
        bundle_prefix = f"apps/{manifest.slug}"

        if existing is not None and not allow_replace:
            raise UploadError(
                f"An app with slug '{manifest.slug}' already exists. "
                "Use the replace endpoint (--replace in the skill CLI) to update it."
            )

        # Extract the validated zip to a scratch dir, then persist the tree
        # through the storage backend (atomic dir-swap on the local backend;
        # upload + stale-key reconcile on S3). The scratch dir is removed in the
        # finally below alongside tmp_path.
        extract_dir = Path(await anyio.to_thread.run_sync(tempfile.mkdtemp))
        await anyio.to_thread.run_sync(_extract_into, tmp_path, extract_dir)

        if existing is None:
            # Orphan guard: blobs under the prefix with no DB row mean a prior
            # half-completed delete; refuse rather than silently merging.
            if storage.list(bundle_prefix):
                raise UploadError(
                    f"App data already exists for slug '{manifest.slug}'. "
                    "Remove it and try again."
                )
            await anyio.to_thread.run_sync(storage.replace_tree, bundle_prefix, extract_dir)

            requested = list(manifest.permissions.network)
            declared_services = list(manifest.services)
            app_row = App(
                slug=manifest.slug,
                name=manifest.name,
                description=manifest.description,
                version=manifest.version,
                icon=manifest.icon,
                entry=manifest.entry,
                services=declared_services,
                # Auto-approve every service the manifest declared on a fresh
                # install — same rationale as ``allowed_origins`` below.
                allowed_services=list(declared_services),
                csp_strict=bool(manifest.permissions.csp_strict),
                requested_origins=requested,
                # Auto-approve everything the manifest declared on a fresh
                # install. The admin already trusted the bundle enough to
                # upload it; making them re-approve in a separate UI step
                # would be friction for no security gain. Revocations remain
                # one click away on the apps admin page.
                allowed_origins=list(requested),
                tools=[t.model_dump() for t in manifest.tools],
                forms=[f.model_dump() for f in manifest.forms],
                enabled=True,
                uploaded_by=uploader.id,
            )
            try:
                db.add(app_row)
                db.flush()  # so app_row.id is assigned before we FK to it below
                # Auto-grant access for every existing non-admin user under the
                # configured default-access policy. Replacements (below)
                # deliberately don't touch existing grants — admins reshape
                # access manually.
                grant_default_access_for_new_app(db, app_row)
                db.commit()
            except Exception:
                # The bundle is already persisted under ``apps/<slug>``. If the
                # DB step fails (constraint violation, FK error, etc.), we'd
                # otherwise leave orphaned blobs that block the next upload via
                # the orphan guard above. Drop them so the next attempt proceeds.
                db.rollback()
                storage.delete_prefix(bundle_prefix)
                raise
            return InstallResult(
                slug=app_row.slug,
                name=app_row.name,
                version=app_row.version,
                replaced=False,
            )

        # Replace path: the validated tree is already extracted at
        # ``extract_dir``; swap it in through the backend (atomic dir-swap
        # locally, upload + stale-key reconcile on S3). Per-user storage under
        # ``storage/<slug>/`` is a separate prefix and is left untouched.
        await anyio.to_thread.run_sync(storage.replace_tree, bundle_prefix, extract_dir)

        # On replace, preserve any origins the admin explicitly revoked
        # (present in the previous requested list but absent from
        # allowed_origins) so an in-place update of the same slug doesn't
        # silently re-grant network access the admin deliberately turned off.
        # Newly-declared origins are auto-approved on the same logic as
        # fresh installs.
        new_requested = list(manifest.permissions.network)
        prev_requested = list(existing.requested_origins or [])
        prev_allowed = set(existing.allowed_origins or [])
        revoked = set(prev_requested) - prev_allowed
        new_allowed = [o for o in new_requested if o not in revoked]
        # Same preserve-revocations logic for service scopes: any service the
        # admin had explicitly turned off (declared previously but absent
        # from allowed_services) stays off after the replace. Services newly
        # declared in this upload are auto-approved.
        new_services = list(manifest.services)
        prev_services_declared = list(existing.services or [])
        prev_services_allowed = set(existing.allowed_services or [])
        services_revoked = set(prev_services_declared) - prev_services_allowed
        new_allowed_services = [s for s in new_services if s not in services_revoked]
        existing.name = manifest.name
        existing.description = manifest.description
        existing.version = manifest.version
        existing.icon = manifest.icon
        existing.entry = manifest.entry
        existing.services = new_services
        existing.allowed_services = new_allowed_services
        existing.csp_strict = bool(manifest.permissions.csp_strict)
        existing.requested_origins = new_requested
        existing.allowed_origins = new_allowed
        existing.tools = [t.model_dump() for t in manifest.tools]
        existing.forms = [f.model_dump() for f in manifest.forms]
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
        if extract_dir is not None:
            shutil.rmtree(extract_dir, ignore_errors=True)


# ----- Pending-upload stash (upload-area "is this an update?" flow) -----
#
# When an admin uploads a bundle through the apps page whose slug already
# exists, we don't hard-error any more — we stash the already-streamed,
# already-validated zip and show a confirmation page ("update the existing
# app?"). Confirming installs it as a replace. Stashing avoids forcing the
# admin to re-pick the file on the second step (a file input can't be
# pre-filled). The portal runs single-process (single uvicorn / single ECS
# task — the scheduler and rate limiters already rely on this), so an
# in-memory map keyed by an unguessable token is safe; pending uploads are
# ephemeral and simply lost on restart (the admin re-uploads). The temp file
# lives on local scratch disk, same as a normal in-flight upload.

@dataclass
class _PendingUpload:
    tmp_path: Path
    slug: str
    name: str
    new_version: str
    existing_name: str
    existing_version: str
    created_at: datetime


_PENDING_UPLOADS: dict[str, _PendingUpload] = {}
_PENDING_UPLOAD_TTL = timedelta(minutes=30)


def _prune_pending_uploads() -> None:
    """Drop expired stashed uploads and remove their temp files.

    Called opportunistically on the upload routes so an abandoned confirm
    (admin navigates away without choosing) can't leak temp files forever.
    """
    now = datetime.now(timezone.utc)
    for token, pending in list(_PENDING_UPLOADS.items()):
        if now - pending.created_at > _PENDING_UPLOAD_TTL:
            _PENDING_UPLOADS.pop(token, None)
            try:
                os.unlink(pending.tmp_path)
            except OSError:
                pass


def _discard_pending(token: Optional[str]) -> None:
    """Drop one stashed upload by token and remove its temp file (no-op if absent)."""
    if not token:
        return
    pending = _PENDING_UPLOADS.pop(token, None)
    if pending is not None:
        try:
            os.unlink(pending.tmp_path)
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
    _prune_pending_uploads()

    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        return render(
            request, "admin_apps_upload.html",
            user=admin, error="Please upload a .zip file.", status_code=400,
        )

    # Stream + validate up front so we can read the manifest's slug and decide
    # between a fresh install and an "is this an update?" prompt before touching
    # the filesystem or DB. ``tmp_path`` stays None if streaming itself failed
    # (``_stream_to_temp`` cleans up its own partial in that case).
    tmp_path: Optional[Path] = None
    try:
        tmp_path = await _stream_to_temp(bundle, MAX_ZIP_BYTES)
        manifest = await anyio.to_thread.run_sync(_prepare_bundle, tmp_path)
    except UploadError as e:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return render(
            request, "admin_apps_upload.html",
            user=admin, error=str(e), status_code=400,
        )

    existing = db.exec(select(App).where(App.slug == manifest.slug)).first()
    if existing is not None:
        # Slug collision → offer an update instead of erroring. Stash the
        # validated zip; the confirm route reuses it so the file isn't re-picked.
        # Drop any earlier stash from this admin's session first, so repeated or
        # abandoned collision uploads can't pile up temp files (bounded to one
        # per session).
        _discard_pending(request.session.get("pending_upload"))
        token = secrets.token_urlsafe(24)
        _PENDING_UPLOADS[token] = _PendingUpload(
            tmp_path=tmp_path,
            slug=manifest.slug,
            name=manifest.name,
            new_version=manifest.version,
            existing_name=existing.name,
            existing_version=existing.version,
            created_at=datetime.now(timezone.utc),
        )
        # Post/Redirect/Get: stash the token in the (signed) session and redirect
        # to a GET confirm page so a browser Reload re-fetches that page instead
        # of re-POSTing — and re-streaming — the whole upload. Keeping the token
        # in the session rather than the URL keeps it out of logs / history.
        request.session["pending_upload"] = token
        return RedirectResponse("/admin/apps/upload/confirm", status_code=303)

    # Fresh install: hand the validated temp file to the core, which extracts,
    # registers, and unlinks ``tmp_path`` (success or failure).
    try:
        result = await install_bundle_from_path(db, admin, tmp_path)
    except UploadError as e:
        return render(
            request, "admin_apps_upload.html",
            user=admin, error=str(e), status_code=400,
        )
    record_event(
        db, actor=admin, action="app.upload", request=request,
        target=f"app:{result.slug}",
        details={"name": result.name, "version": result.version},
    )
    flash(request, f"Uploaded ‘{result.name}’ (slug: {result.slug})")
    return _apps_redirect(result.slug)


@router.get("/admin/apps/upload/confirm")
def admin_apps_upload_confirm_form(request: Request, admin: AdminDep):
    """Render the 'is this an update?' page for the session's stashed upload.

    Reached via the PRG redirect from a colliding upload; safe to reload.
    """
    _prune_pending_uploads()
    token = request.session.get("pending_upload")
    pending = _PENDING_UPLOADS.get(token) if token else None
    if pending is None:
        request.session.pop("pending_upload", None)
        flash(
            request,
            "That upload is no longer available — please choose the file again.",
            "error",
        )
        return RedirectResponse("/admin/apps/upload", status_code=303)
    return render(
        request, "admin_apps_update_confirm.html",
        user=admin,
        token=token,
        slug=pending.slug,
        new_name=pending.name,
        new_version=pending.new_version,
        existing_name=pending.existing_name,
        existing_version=pending.existing_version,
    )


@router.post("/admin/apps/upload/confirm")
async def admin_apps_upload_confirm(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    token: str = Form(default=""),
    csrf: str = Form(default="", alias="_csrf"),
):
    """Apply a stashed colliding upload as an in-place update."""
    check_csrf(request, csrf)
    _prune_pending_uploads()
    # The confirm form carries the token; fall back to the session copy.
    token = token or request.session.get("pending_upload", "")
    request.session.pop("pending_upload", None)
    pending = _PENDING_UPLOADS.pop(token, None)
    if pending is None:
        flash(
            request,
            "That upload is no longer available — please choose the file again.",
            "error",
        )
        return RedirectResponse("/admin/apps/upload", status_code=303)
    try:
        # install_bundle_from_path unlinks pending.tmp_path in its finally.
        result = await install_bundle_from_path(
            db, admin, pending.tmp_path,
            allow_replace=True, expected_slug=pending.slug,
        )
    except UploadError as e:
        return render(
            request, "admin_apps_upload.html",
            user=admin, error=str(e), status_code=400,
        )
    record_event(
        db, actor=admin, action="app.replace", request=request,
        target=f"app:{result.slug}",
        details={"name": result.name, "version": result.version, "via": "upload_update"},
    )
    flash(request, f"Updated ‘{result.name}’ to v{result.version} (slug: {result.slug})")
    return _apps_redirect(result.slug)


@router.post("/admin/apps/upload/cancel")
def admin_apps_upload_cancel(
    request: Request,
    db: DbDep,
    admin: AdminDep,
    token: str = Form(default=""),
    csrf: str = Form(default="", alias="_csrf"),
):
    """Discard a stashed colliding upload and its temp file."""
    check_csrf(request, csrf)
    token = token or request.session.get("pending_upload", "")
    request.session.pop("pending_upload", None)
    _discard_pending(token)
    return RedirectResponse("/admin/apps/upload", status_code=303)


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
        # Re-render the list with the error inline in this row's replace panel
        # (and the panel reopened) instead of bouncing to the standalone upload
        # page — the admin stays in context next to the app they were updating.
        apps = db.exec(select(App).order_by(App.display_order, App.name)).all()
        return render(
            request, "admin_apps.html",
            user=admin, apps=apps,
            replace_error_slug=slug, replace_error=str(e),
            status_code=400,
        )
    record_event(
        db, actor=admin, action="app.replace", request=request,
        target=f"app:{result.slug}",
        details={"name": result.name, "version": result.version, "via": "admin_ui"},
    )
    flash(request, f"Replaced ‘{result.name}’ (slug: {result.slug})")
    return _apps_redirect(result.slug)


@router.post("/admin/apps/{slug}/download")
async def admin_apps_download(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: str = Form(default="", alias="_csrf"),
):
    """Download an installed app's bundle as a ``.zip``.

    Rebuilt on demand from the extracted tree in storage (the original upload
    isn't retained), so the archive always matches what's currently installed
    and re-installs cleanly via the upload/replace flow.

    A POST (not GET) so it's CSRF-gated like every other per-row action — the
    handler does real work (deflates the whole bundle) and writes an audit row,
    which a cross-site prefetch of a GET could otherwise trigger.
    """
    check_csrf(request, csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404)
    # Build off the event loop — reads every blob and deflates into a temp zip.
    zip_path, tmp_dir = await anyio.to_thread.run_sync(
        _build_bundle_zip, app_row.slug, app_row.version
    )
    if zip_path is None:
        raise HTTPException(404, "No bundle files on disk for this app.")
    record_event(
        db, actor=admin, action="app.download", request=request,
        target=f"app:{app_row.slug}",
        details={"version": app_row.version},
    )
    # The cleanup runs after the file finishes streaming to the client.
    cleanup = BackgroundTask(shutil.rmtree, str(tmp_dir), ignore_errors=True)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        background=cleanup,
    )


@router.put("/api/v1/apps/{slug}")
async def api_apps_replace(
    slug: str,
    request: Request,
    db: DbDep,
    user: TokenUserDep,
    bundle: UploadFile = File(...),
    x_csrf: Annotated[Optional[str], Header(alias="X-CSRF-Token")] = None,
):
    if user is None:
        raise HTTPException(401, "Sign in required")
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    # Cross-module import: both routers are siblings under the same app; a
    # dedicated shared module for one helper would be over-engineering.
    from portal.api import _require_csrf_for_cookie
    _require_csrf_for_cookie(request, x_csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404, f"App '{slug}' not found")
    try:
        result = await install_bundle(
            db, user, bundle, allow_replace=True, expected_slug=slug,
        )
    except UploadError as e:
        raise HTTPException(400, str(e))
    record_event(
        db, actor=user, action="app.replace", request=request,
        target=f"app:{result.slug}",
        details={"name": result.name, "version": result.version, "via": "api"},
    )
    return {
        "slug": result.slug,
        "name": result.name,
        "version": result.version,
        "replaced": True,
    }


def _parse_extra_origins(raw: str) -> tuple[list[str], list[str]]:
    """Split a free-text 'extras' textarea into (valid, invalid) origins.

    Each non-blank line is one origin candidate. Validation reuses the same
    regex the manifest validator applies — we never want the admin-added
    list to diverge in format from the manifest-declared list.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        cand = line.strip().lower().rstrip("/")
        if not cand:
            continue
        if not _ORIGIN_RE.match(cand):
            invalid.append(line.strip())
            continue
        if cand in seen:
            continue
        seen.add(cand)
        valid.append(cand)
    return valid, invalid


@router.post("/admin/apps/{slug}/network")
def admin_apps_network_update(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: str = Form(default="", alias="_csrf"),
    allowed_requested: list[str] = Form(default_factory=list),
    extras: str = Form(default=""),
):
    check_csrf(request, csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404)

    # Sanitize: the checkbox set must be a subset of what the manifest
    # actually requested. A malicious client posting an arbitrary value
    # under ``allowed_requested`` would otherwise insert it into the
    # allowed list bypassing the extras-format validation below.
    requested = set(app_row.requested_origins or [])
    checked = [o for o in allowed_requested if o in requested]

    extra_valid, extra_invalid = _parse_extra_origins(extras)
    if extra_invalid:
        flash(
            request,
            "Some extra origins were invalid and were ignored: "
            + ", ".join(extra_invalid[:5])
            + (f" (+{len(extra_invalid) - 5} more)" if len(extra_invalid) > 5 else ""),
        )

    # Order: checked-requested first, then admin-added extras, both
    # deduped while preserving order so the admin UI doesn't shuffle
    # entries between page loads.
    seen: set[str] = set()
    new_allowed: list[str] = []
    for o in checked + extra_valid:
        if o in seen:
            continue
        seen.add(o)
        new_allowed.append(o)

    app_row.allowed_origins = new_allowed
    db.add(app_row)
    db.commit()
    record_event(
        db, actor=admin, action="app.network.update", request=request,
        target=f"app:{app_row.slug}",
        details={"allowed": new_allowed},
    )
    flash(request, f"Network access updated for {app_row.name}.")
    return _apps_redirect(app_row.slug)


@router.post("/admin/apps/{slug}/services")
def admin_apps_services_update(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    csrf: str = Form(default="", alias="_csrf"),
    allowed_services: list[str] = Form(default_factory=list),
):
    """Update the admin-approved subset of services for an app.

    The checkbox set MUST be a subset of the manifest's declared services —
    a malicious POST that tries to grant a service the app never declared
    is silently filtered. To grant a new service, the app's manifest must
    declare it and the bundle must be re-uploaded.
    """
    check_csrf(request, csrf)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None:
        raise HTTPException(404)
    declared = set(app_row.services or [])
    # Filter to the declared set AND the known-allowed services so a
    # malformed form post can't inject e.g. ``"shell"``.
    picked = [s for s in allowed_services if s in declared and s in ALLOWED_SERVICES]
    # Dedupe while preserving order so the admin UI doesn't shuffle entries.
    seen: set[str] = set()
    final: list[str] = []
    for s in picked:
        if s in seen:
            continue
        seen.add(s)
        final.append(s)
    app_row.allowed_services = final
    db.add(app_row)
    db.commit()
    record_event(
        db, actor=admin, action="app.services.update", request=request,
        target=f"app:{app_row.slug}",
        details={"allowed_services": final},
    )
    flash(request, f"Service access updated for {app_row.name}.")
    return _apps_redirect(app_row.slug)


@router.post("/admin/apps/{slug}/move")
def admin_apps_move(
    slug: str,
    request: Request,
    db: DbDep,
    admin: AdminDep,
    direction: Annotated[str, Form()] = "",
    csrf: str = Form(default="", alias="_csrf"),
):
    """Nudge an app one slot up or down in the dashboard tile order.

    On every move we fetch the full ordered list, swap the moved app with
    its neighbour, and renumber the whole list in spaced increments. A full
    renumber is O(N) writes but N is the number of apps installed in this
    portal — a handful for a typical small business — so we trade a tiny
    bit of churn for never having to reason about colliding display_order
    values, gaps, or off-by-one slot math.

    No-ops silently if the app is already at the boundary; the up chip is
    rendered ``disabled`` on the first row and the down chip on the last,
    but a stale tab POSTing anyway shouldn't 500.
    """
    check_csrf(request, csrf)
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")

    apps = db.exec(select(App).order_by(App.display_order, App.name)).all()
    idx = next((i for i, a in enumerate(apps) if a.slug == slug), None)
    if idx is None:
        raise HTTPException(404)

    target_idx = idx - 1 if direction == "up" else idx + 1
    if target_idx < 0 or target_idx >= len(apps):
        return _apps_redirect(slug)

    apps[idx], apps[target_idx] = apps[target_idx], apps[idx]
    for i, app in enumerate(apps):
        app.display_order = (i + 1) * 10
        db.add(app)
    db.commit()
    record_event(
        db, actor=admin, action="app.move", request=request,
        target=f"app:{slug}",
        details={"direction": direction},
    )
    return _apps_redirect(slug)


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
    # When disabling, close out any open AppSessions so a user already in
    # the app's iframe stops authenticating against the SDK at the moment
    # of the admin action. The per-request access gate already 404s on a
    # disabled app, so this is defense in depth / cleaner audit trail.
    if not app_row.enabled:
        revoke_all_app_sessions_for_slug(db, app_row.slug)
    record_event(
        db, actor=admin, action="app.toggle", request=request,
        target=f"app:{app_row.slug}",
        details={"enabled": bool(app_row.enabled)},
    )
    flash(request, f"{app_row.name} {state}")
    return _apps_redirect(app_row.slug)


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

    # Drop the app's bundle blobs (apps/<slug>/...) through the storage backend.
    # Per-user storage under storage/<slug>/ is deliberately left in place —
    # reinstalling the same slug keeps its data.
    get_storage().delete_prefix(f"apps/{app_row.slug}")

    # Drop the app's scratch lock dir (data/locks/<slug>/) so it doesn't linger.
    # Per-user storage under data/storage/<slug>/ is deliberately left in place —
    # reinstalling the same slug keeps its data.
    locks_root = Path(settings.data_dir).resolve() / "locks"
    lock_dir = (locks_root / app_row.slug).resolve()
    try:
        lock_dir.relative_to(locks_root)
    except ValueError:
        lock_dir = None
    if lock_dir is not None and lock_dir.exists():
        shutil.rmtree(lock_dir, ignore_errors=True)

    name = app_row.name
    slug_for_audit = app_row.slug
    if app_row.id is not None:
        delete_access_for_app(db, app_row.id)
    # Cascade-delete the app's dependent rows so nothing keeps referencing a
    # deleted app. A surviving ScheduledRun would otherwise keep firing every
    # scheduler tick against a slug that no longer exists. ShareLink rows also
    # have rendered PDFs on disk — remove those blobs before dropping the rows.
    # NOTE: per-user storage under storage/<slug>/ is intentionally retained
    # (reinstalling the same slug keeps its data), matching the bundle comment.
    from portal.shares import delete_share_files

    if app_row.id is not None:
        pdf_files = [
            (r.payload or {}).get("path")
            for r in db.exec(
                select(ShareLink).where(
                    ShareLink.app_id == app_row.id, ShareLink.kind == "pdf"
                )
            ).all()
        ]
        delete_share_files([p for p in pdf_files if p])
        db.exec(delete(ShareLink).where(ShareLink.app_id == app_row.id))
    db.exec(delete(ScheduledRun).where(ScheduledRun.app_slug == app_row.slug))
    db.exec(delete(FormSubmission).where(FormSubmission.app_slug == app_row.slug))
    # Close any open AppSessions before the App row goes away so the
    # session table doesn't carry rows that FK at a deleted app slug.
    revoke_all_app_sessions_for_slug(db, app_row.slug)
    db.delete(app_row)
    db.commit()
    record_event(
        db, actor=admin, action="app.delete", request=request,
        target=f"app:{slug_for_audit}",
        details={"name": name},
    )
    flash(request, f"Deleted {name}")
    return RedirectResponse("/admin/apps", status_code=303)


# ----- Dashboard tile icons (portal origin) -----
#
# Why this exists as a dedicated endpoint: in per-app-origin mode the
# obvious URL ``/apps/<slug>/icon.png`` would 308-redirect to the app's
# subdomain (see ``serve_app_file`` below), where the lack of an
# AppSession cookie bounces the request back to the portal-origin
# launcher (HTML, not an image) — broken-image glyph on every tile.
#
# Routing dashboard icons through their own portal-origin endpoint
# sidesteps the redirect entirely. Same per-user access gate as the
# launcher; same uniform 404 response for "doesn't exist" /
# "exists but blocked" / "exists but no icon configured" so a curious
# user can't enumerate slugs they don't have access to.

@router.get("/app-icons/{slug}", include_in_schema=False)
def app_icon(
    slug: str,
    db: DbDep,
    user: UserDep,
):
    if user is None:
        raise HTTPException(404)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None or not app_row.enabled or not app_row.icon:
        raise HTTPException(404)
    if not user_can_access_app(db, user, app_row):
        raise HTTPException(404)

    # Short cache: icons rarely change but DO change on app replace, so
    # five minutes is the floor where a re-uploaded icon shows up quickly
    # without re-fetching on every dashboard render. Starlette's FileResponse
    # adds ETag/Last-Modified automatically (local backend), so the browser
    # 304s on the conditional revalidation after max-age expires.
    headers = {"Cache-Control": "public, max-age=300"}
    # SVGs can carry inline <script>; an app author's icon would otherwise be
    # stored XSS on the portal origin if the browser rendered it as a top-level
    # document. Force ``Content-Disposition: attachment`` so a direct navigation
    # downloads the file instead of rendering+executing it. The dashboard tile
    # uses ``<img src>``, which ignores Content-Disposition AND runs SVGs in the
    # script-disabled image context — so tiles still display the icon safely.
    # Raster icons need no special handling.
    if Path(app_row.icon).suffix.lower() == ".svg":
        headers["Content-Disposition"] = "attachment"
    try:
        return get_storage().file_response(
            f"apps/{slug}/{app_row.icon}",
            headers=headers,
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)


# ----- Child app serving -----

@router.get("/apps/{slug}", include_in_schema=False)
def app_root_redirect(slug: str):
    return RedirectResponse(f"/apps/{slug}/", status_code=308)


@router.get("/apps/{slug}/")
def serve_app_index(
    slug: str,
    request: Request,
    db: DbDep,
    user: UserDep,
):
    """Entry point for opening a child app from the portal origin.

    Always renders the iframe-launcher template so the portal chrome
    (back-to-Apps link, app name + version) is visible regardless of which
    origin mode is in effect. What varies is the iframe ``src``:

    - ``CHILD_APPS_SAME_ORIGIN`` truthy (legacy): iframe loads the app's
      entry file from the SAME origin at ``/apps/<slug>/<entry>``. The
      portal's session cookie is shared with the iframe; there's no
      browser-enforced isolation between the portal and the child app.
      The launcher chrome is the only navigation affordance back to the
      dashboard from inside the app.
    - ``CHILD_APPS_SAME_ORIGIN`` falsy (default): mint a single-use launch
      token and point the iframe at
      ``<slug>.apps.<SITE_URL>/#token=<token>``. The token's slug is
      bound at mint time; the subdomain's exchange endpoint refuses to
      mint an AppSession unless the host-derived slug matches.
    """
    if user is None:
        return RedirectResponse(f"/login?next=/apps/{slug}/", status_code=303)

    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None or not app_row.enabled:
        raise HTTPException(404)

    # Per-user access gate. Admins bypass; non-admins need a UserAppAccess
    # row. 404 (not 403) so an attacker probing app slugs can't distinguish
    # "exists but blocked" from "doesn't exist".
    if not user_can_access_app(db, user, app_row):
        raise HTTPException(404)

    if settings.child_apps_same_origin:
        # Iframe targets the app's entry file directly on the portal origin.
        # Caddy's /apps/* CSP allows ``frame-ancestors 'self'`` so the
        # launcher (also on /apps/<slug>/) can embed it.
        iframe_src = f"/apps/{slug}/{app_row.entry}"
    else:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        db.add(
            AppLaunchToken(
                token=token,
                user_id=user.id,
                slug=slug,
                created_at=now,
                expires_at=now + _LAUNCH_TOKEN_TTL,
            )
        )
        db.commit()

        scheme = "https" if settings.cookies_secure else "http"
        # Preserve the port the portal is reached on, so a dev hitting
        # http://lvh.me:8000/apps/x/ ends up loading the iframe at
        # http://x.apps.lvh.me:8000/#token=... rather than the bare hostname.
        host = request.headers.get("host", settings.site_url)
        port = ""
        if ":" in host and not host.startswith("["):
            port = ":" + host.rsplit(":", 1)[1]
        iframe_src = f"{scheme}://{slug}.apps.{settings.site_url}{port}/#token={token}"
    return render(
        request,
        "app_launcher.html",
        user=user,
        app=app_row,
        iframe_src=iframe_src,
        site_url=settings.site_url,
    )


@router.get("/apps/{slug}/{path:path}")
def serve_app_file(
    slug: str, path: str, request: Request, db: DbDep, user: UserDep
):
    # In per-app-origin mode, the portal origin must NEVER serve a child app's
    # static files directly — doing so defeats the isolation by handing the
    # bundle back same-origin with the portal. A typed URL or stale bookmark
    # to /apps/<slug>/<path> is redirected to the subdomain, where the
    # no-cookie path will bounce the browser to /apps/<slug>/ to mint a launch
    # token. Token isn't included here; the subdomain handler handles that.
    if not settings.child_apps_same_origin:
        # Validate the slug against the kebab-case grammar BEFORE interpolating
        # it into the Location header. Without this, a path like
        # ``/apps/attacker.com%23/x`` decodes to ``slug = "attacker.com#"`` and
        # Starlette's RedirectResponse keeps the ``#`` literal in the URL —
        # browsers then parse Location as ``host=attacker.com,
        # fragment=.apps.example.com/x`` and navigate to attacker.com.
        # ``%5C`` (backslash) gives the same effect because Chrome/Firefox
        # normalize ``\`` to ``/``. Reject anything outside the slug alphabet.
        if not SLUG_RE.match(slug):
            raise HTTPException(404)
        scheme = "https" if settings.cookies_secure else "http"
        host = request.headers.get("host", settings.site_url)
        port = ""
        if ":" in host and not host.startswith("["):
            port = ":" + host.rsplit(":", 1)[1]
        query = request.url.query
        suffix = f"?{query}" if query else ""
        target = (
            f"{scheme}://{slug}.apps.{settings.site_url}{port}/{path}{suffix}"
        )
        return RedirectResponse(target, status_code=308)
    return _serve_app_file(slug, db, user, path=path)


def _serve_app_file(
    slug: str, db: Session, user: Optional[User], path: str
):
    if user is None:
        return RedirectResponse(f"/login?next=/apps/{slug}/", status_code=303)
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None or not app_row.enabled:
        raise HTTPException(404)

    # Per-user access gate: a previously-granted user whose access was
    # revoked while a tab was still open shouldn't be able to keep loading
    # the bundle. 404 to avoid leaking the slug's existence.
    if not user_can_access_app(db, user, app_row):
        raise HTTPException(404)

    if path in ("", "/"):
        path = app_row.entry
    try:
        return get_storage().file_response(f"apps/{slug}/{path}")
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)


# ----- App-subdomain serving (the new per-app origin) -----

def _serve_app_subdomain_path(
    request: Request, db: Session, slug: str, path: str
) -> FileResponse | RedirectResponse | Response:
    """Serve a static file from ``data/apps/<slug>/<path>``.

    Requires an active ``AppSession`` cookie for this subdomain (checked by
    the caller via current_app_session_user). The bare ``portal-sdk.js`` is
    served from the portal's static dir so child apps fetching it from their
    own origin get the same code that the portal serves at root.

    When the resolved app opted into strict CSP (manifest's
    ``permissions.csp_strict``) and the served file is HTML, the literal
    token ``{{NONCE}}`` is substituted with the per-response nonce stashed
    on ``request.state.csp_nonce`` by ``ChildAppCSPMiddleware``. App
    authors put ``nonce="{{NONCE}}"`` on every inline ``<script>`` /
    ``<style>`` they want to keep running.
    """
    app_row = db.exec(select(App).where(App.slug == slug)).first()
    if app_row is None or not app_row.enabled:
        raise HTTPException(404)

    # The SDK is shared across all child apps. Serve it from the portal's
    # static dir on every subdomain so apps just do <script src="/portal-sdk.js">.
    if path == "portal-sdk.js":
        return FileResponse(
            STATIC_DIR / "portal-sdk.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    if path in ("", "/"):
        rel = app_row.entry
    else:
        rel = path
    key = f"apps/{slug}/{rel}"
    storage = get_storage()

    # Nonce-substitute HTML responses for csp_strict apps. Only files under
    # ``_MAX_NONCE_SUB_BYTES`` get the read-into-memory + replace treatment;
    # an app that ships a multi-megabyte index.html (under the zip cap but
    # well past anything reasonable) would otherwise amplify per-request
    # memory N× under concurrency. Larger HTML falls through to a normal serve
    # — the strict CSP will then reject the un-nonced inline script and the
    # app author will see the symptom in DevTools, which is the correct
    # signal to split the HTML or drop inline scripts.
    if bool(getattr(app_row, "csp_strict", False)) and Path(rel).suffix.lower() in (".html", ".htm"):
        nonce = getattr(request.state, "csp_nonce", None)
        if nonce:
            try:
                size = storage.size(key)
            except ValueError:
                raise HTTPException(404)
            if size is None:
                raise HTTPException(404)
            if size <= _MAX_NONCE_SUB_BYTES:
                try:
                    body = storage.read(key).decode("utf-8")
                except (FileNotFoundError, UnicodeDecodeError):
                    # Not UTF-8 text (or vanished) — fall through to a normal
                    # serve; the browser will complain about an inline script
                    # lacking a nonce, the correct outcome for a mis-declared app.
                    body = None
                if body is not None:
                    return Response(
                        body.replace("{{NONCE}}", nonce),
                        media_type="text/html; charset=utf-8",
                    )
            # else: too big to nonce — fall through to a normal serve.

    try:
        return storage.file_response(key)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)


def _launcher_url(request: Request, slug: str) -> str:
    """Server-build the portal-origin launcher URL for this slug.

    Preserves whatever port the request arrived on (matters for dev where
    uvicorn binds an arbitrary port; in prod the port is implicit in the
    scheme).
    """
    scheme = "https" if settings.cookies_secure else "http"
    host = request.headers.get("host", settings.site_url)
    port = ""
    if ":" in host and not host.startswith("["):
        port = ":" + host.rsplit(":", 1)[1]
    return f"{scheme}://{settings.site_url}{port}/apps/{slug}/"


def _is_document_navigation(request: Request) -> bool:
    """True when this looks like a browser navigating to a page.

    Used to decide whether a cookie-less deep path on an app subdomain is
    worth bouncing to the launcher (a real navigation) or should just 404 (a
    subresource fetch, or a scanner).

    ``Sec-Fetch-Dest`` is the reliable signal: every current browser sends it
    on every request, and it's a forbidden header name, so page JavaScript
    can't forge it. When it's present we trust it exactly — ``document`` is a
    top-level navigation, ``script``/``style``/``image``/``empty`` are not.

    Only when the header is absent entirely (older browsers, curl, a
    non-browser client) do we fall back to sniffing ``Accept`` for
    ``text/html``. That fallback is guessable by anything that sets its own
    headers, which is fine — the branch it picks decides UX, not access. Both
    outcomes are unauthenticated and neither reveals whether the app exists.
    """
    dest = request.headers.get("sec-fetch-dest")
    if dest:
        return dest == "document"
    return "text/html" in request.headers.get("accept", "")


def _launch_redirect_response(request: Request, slug: str) -> RedirectResponse:
    """303 to the portal-origin launcher.

    Used as a fallback for non-bootstrap paths that need to mint a fresh
    launch token (currently the legacy ``/apps/<slug>/<file>`` redirects in
    per-app-origin mode). For first-hit subdomain loads the bootstrap HTML
    handles the token exchange in-place — see ``_launch_bootstrap_html``.
    """
    return RedirectResponse(_launcher_url(request, slug), status_code=303)


def _launch_bootstrap_html(request: Request, slug: str) -> Response:
    """HTML page served on the subdomain when there's no AppSession cookie.

    The per-app-origin handshake parks a single-use launch token in the
    URL fragment (``#token=<...>``) of the iframe src constructed by the
    portal-origin launcher. Fragments don't survive HTTP round trips, so
    the FIRST GET to the subdomain root arrives without the token — only
    client-side JS can see it. This page is that client-side step:

      1. Read ``#token=<token>`` from ``window.location.hash``
      2. POST it to ``/api/v1/session/exchange`` (same-origin → the
         ``Set-Cookie: app_session=...`` response actually sticks)
      3. ``window.location.replace(window.location.pathname)`` — drops
         the fragment and reloads, which now serves the real app HTML
         because the AppSession cookie is set

    On any failure (no token, expired token, network error), shows a
    clear message pointing users at the launcher chrome's ← Apps link.
    The iframe sandbox doesn't grant ``allow-top-navigation``, so
    auto-bouncing the top window isn't an option — the recovery has to
    be user-driven from outside the iframe.

    This replaces the prior ``303 → launcher`` behaviour, which couldn't
    actually complete the handshake: the launcher would just mint a
    fresh token and render another iframe, looping forever inside
    nested iframes without ever exchanging the token.
    """
    # ``_launcher_url`` is still built so callers that diagnose this page in
    # logs can see it, but the inline JS below doesn't actually navigate
    # back — the iframe sandbox doesn't allow top-navigation, so the user
    # recovers via the launcher chrome's ← Apps link. Keep this call so the
    # validate-launcher-url-format invariant is exercised on every hit.
    _launcher_url(request, slug)

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Loading…</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, \"Inter\", \"Segoe UI\",
    Roboto, sans-serif;
  padding: 2.5rem 1.5rem;
  color: #57534e;
  background: #fafaf9;
  text-align: center;
  margin: 0;
}}
.err {{ color: #b91c1c; }}
.err a {{ color: inherit; }}
@media (prefers-color-scheme: dark) {{
  body {{ color: #a8a29e; background: #0c0a09; }}
  .err {{ color: #f87171; }}
}}
</style>
</head>
<body>
<p id=\"msg\">Loading…</p>
<p id=\"err\" class=\"err\" hidden>
  Couldn’t start the app. Use the <strong>← Apps</strong> link above
  to return to the dashboard and try again.
</p>
<script>
(function () {{
  function fail() {{
    var msg = document.getElementById(\"msg\");
    var err = document.getElementById(\"err\");
    if (msg) msg.hidden = true;
    if (err) err.hidden = false;
  }}
  var m = window.location.hash.match(/(?:^#|&)token=([^&]+)/);
  if (!m) {{ fail(); return; }}
  var token;
  try {{ token = decodeURIComponent(m[1]); }}
  catch (_) {{ fail(); return; }}
  fetch(\"/api/v1/session/exchange\", {{
    method: \"POST\",
    headers: {{ \"Content-Type\": \"application/json\" }},
    credentials: \"same-origin\",
    body: JSON.stringify({{ token: token }})
  }}).then(function (res) {{
    if (!res.ok) throw new Error(\"HTTP \" + res.status);
    // Cookie is set. Reload without the fragment to fetch the real app HTML.
    window.location.replace(window.location.pathname || \"/\");
  }}).catch(fail);
}})();
</script>
</body>
</html>
"""
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def serve_subdomain_request(
    request: Request,
    db: Session,
    path: str,
) -> FileResponse | RedirectResponse:
    """Dispatch a subdomain GET ``/`` or ``/<path>`` request.

    Used by the main app's catch-all (and by the explicit ``/`` handler when
    a request arrived on an app subdomain). The slug comes from
    ``request.state.app_slug`` (set by HostDispatchMiddleware); we never trust
    a path-derived slug here. Without an AppSession cookie matching this
    subdomain, we bounce back to the portal-origin launcher to mint one.
    """
    slug = getattr(request.state, "app_slug", None)
    if not slug:
        raise HTTPException(404)

    # ``portal-sdk.js`` is served pre-auth: it's the bootstrap script that
    # runs the launch-token exchange. Without this exception, the SDK could
    # never load on the first hit because no AppSession exists yet.
    if path != "portal-sdk.js":
        sid = request.cookies.get(APP_SESSION_COOKIE)
        from portal.sessions import get_active_app_session, touch_app_session
        session_row = get_active_app_session(db, sid)
        if session_row is None or session_row.slug != slug:
            # No AppSession cookie. The handshake from the portal-origin
            # launcher parked a single-use token in the URL fragment
            # (#token=...) of the iframe src; fragments aren't sent in
            # HTTP requests, so the server can't see it. Serve a small
            # HTML page that reads the fragment client-side, calls
            # /api/v1/session/exchange, and reloads — which fetches the
            # real app HTML with the now-set cookie. See
            # _launch_bootstrap_html for the full flow.
            #
            # Deep paths without a cookie split by request kind. A top-level
            # navigation (a bookmarked or typed deep link) still gets the 303
            # to the launcher, which re-mints a token and lands the user in
            # the app. Everything else — script/style/image/fetch
            # subresources — gets a flat 404 instead, for two reasons:
            #
            #   1. The 303 was never useful to them. The browser follows it,
            #      receives the launcher's HTML, and hands HTML to a caller
            #      expecting JS/CSS/JSON — a MIME or parse error that reads
            #      as an app bug rather than "your session expired".
            #   2. It stops handing an unauthenticated client a redirect into
            #      the portal origin to follow.
            if path in ("", "/"):
                return _launch_bootstrap_html(request, slug)
            if _is_document_navigation(request):
                return _launch_redirect_response(request, slug)
            raise HTTPException(404)
        # Re-check per-user access on every static-file hit so a revocation
        # propagates immediately to live tabs without waiting for the
        # AppSession to expire. The session-row lookup above already gave us
        # the user_id; fetch the user + app once and gate. Admins still
        # bypass.
        user_row = db.get(User, session_row.user_id)
        app_row = db.exec(select(App).where(App.slug == slug)).first()
        if (
            user_row is None
            or app_row is None
            or not app_row.enabled
            or not user_can_access_app(db, user_row, app_row)
        ):
            # Bounce back to the launcher; on the portal origin, serve_app_index
            # will either deny (404) or mint a fresh token if access was restored.
            return _launch_redirect_response(request, slug)
        touch_app_session(db, session_row)
        request.state.auth_method = "app_session"

    return _serve_app_subdomain_path(request, db, slug, path)
