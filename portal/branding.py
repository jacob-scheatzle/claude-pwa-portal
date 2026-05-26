"""Helpers for portal branding (business name, accent color, uploaded logo).

Three Setting keys drive the look of the portal shell and the optional PDF
header:

- ``branding_business_name`` — shown in the topbar, login page, dashboard,
  and (if requested) the PDF header. Empty → default ``PWA Portal``.
- ``branding_accent_color`` — a six-digit hex color (``#rrggbb``) that
  overrides ``--accent`` in base.html. Empty / malformed → default emerald.
- ``branding_logo_path`` — filename of an uploaded image inside
  ``data/branding/``. Served at ``/branding/<name>`` and inlined into PDF
  headers as a ``data:`` URI. Empty → fall back to the brand-mark letter.

The branding dict is auto-injected into every template render by
``portal.web.render`` and read on PDF render when the SDK passes
``branded: true``. Admin-only mutation happens through ``/admin/settings``.
"""
from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Optional, TypedDict

from sqlmodel import Session

from portal.config import settings
from portal.settings_store import get_setting

DEFAULT_BUSINESS_NAME = "PWA Portal"
DEFAULT_ACCENT_COLOR = "#059669"

# Six-digit hex only. The CSS variable is interpolated raw into a <style>
# block, so anything outside this charset would let an admin inject CSS via
# the Settings page (which they own anyway, but defense in depth is cheap).
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Logo filename whitelist. Mirrors the apps zip-extraction rules: no path
# separators, no leading-dot weirdness, only A-Z a-z 0-9 . _ -.
_SAFE_LOGO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]{2,5}$")

# Logos are inlined into PDFs and shipped on every dashboard / login render,
# so they need to stay small. Hard cap at 512 KiB — well above what a sane
# logo needs, well below what would bloat each page response.
MAX_LOGO_BYTES = 512 * 1024

ALLOWED_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


class Branding(TypedDict):
    business_name: str
    accent_color: str
    logo_url: Optional[str]


def branding_dir() -> Path:
    """Resolve (and create) the on-disk directory that holds uploaded logos."""
    p = Path(settings.data_dir).resolve() / "branding"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_logo_name(name: str) -> bool:
    if not name or len(name) > 100:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_SAFE_LOGO_NAME_RE.match(name))


def get_branding(db: Session) -> Branding:
    """Read all three branding keys at once, applying defaults + validation."""
    name = (get_setting(db, "branding_business_name") or "").strip()
    if not name:
        name = DEFAULT_BUSINESS_NAME

    accent = (get_setting(db, "branding_accent_color") or "").strip()
    if not _HEX_COLOR_RE.match(accent):
        accent = DEFAULT_ACCENT_COLOR

    logo_name = (get_setting(db, "branding_logo_path") or "").strip()
    logo_url: Optional[str] = None
    if logo_name and _safe_logo_name(logo_name):
        if (branding_dir() / logo_name).is_file():
            logo_url = f"/branding/{logo_name}"

    return Branding(
        business_name=name,
        accent_color=accent,
        logo_url=logo_url,
    )


def get_logo_data_uri(db: Session) -> Optional[str]:
    """Return the uploaded logo as a ``data:`` URI for PDF embedding.

    WeasyPrint runs with a strict URL fetcher that blocks every scheme but
    ``data:`` (see ``portal.api._no_external_fetcher``). Logos are read here
    once, base64-encoded, and handed back to the caller for inlining.
    """
    logo_name = (get_setting(db, "branding_logo_path") or "").strip()
    if not logo_name or not _safe_logo_name(logo_name):
        return None
    path = branding_dir() / logo_name
    if not path.is_file():
        return None
    mt, _ = mimetypes.guess_type(str(path))
    if not mt or not mt.startswith("image/"):
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return f"data:{mt};base64,{base64.b64encode(data).decode('ascii')}"


# ``<body...>`` opener with optional attributes. Used to inject the PDF
# branding header right after the body tag rather than at the top of the
# document where it would land before any <head>/<style> the app provided.
_BODY_OPEN_RE = re.compile(r"(<body\b[^>]*>)", re.IGNORECASE)


def render_pdf_header(business_name: str, accent_color: str, logo_data_uri: Optional[str]) -> str:
    """Build the HTML snippet prepended to a branded PDF.

    Inline styles only — WeasyPrint accepts them and the host document may
    not have a stylesheet we can extend. The colored bottom border is the
    visual hook tying the header to the portal's accent.
    """
    logo_html = ""
    if logo_data_uri:
        logo_html = (
            f'<img src="{logo_data_uri}" alt="" '
            'style="height:42px; max-width:160px; object-fit:contain; margin-right:14px;">'
        )
    # Escape the business name minimally — it came from the admin's Settings
    # input which has no HTML-context escaping of its own.
    safe_name = (
        business_name
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    safe_color = accent_color if _HEX_COLOR_RE.match(accent_color) else DEFAULT_ACCENT_COLOR
    return (
        '<div style="'
        'display:flex; align-items:center; '
        f'border-bottom:3px solid {safe_color}; '
        'padding:0 0 12px 0; margin:0 0 18px 0;'
        '">'
        f'{logo_html}'
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif; '
        'font-size:18px; font-weight:700; color:#111;">'
        f'{safe_name}'
        '</div></div>'
    )


def inject_pdf_header(html: str, header_html: str) -> str:
    """Splice ``header_html`` into a caller-supplied HTML document.

    If a ``<body>`` opener is present, inject right after it so the header
    appears at the top of the rendered output. Otherwise, prepend the header
    directly — the app passed a body fragment, not a full document.
    """
    m = _BODY_OPEN_RE.search(html)
    if m:
        idx = m.end()
        return html[:idx] + header_html + html[idx:]
    return header_html + html
