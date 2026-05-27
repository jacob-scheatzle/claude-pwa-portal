"""Public, tokenized share URLs for storage objects and on-demand PDFs.

Two kinds:

- ``storage`` — points at a key in the creator's per-app storage namespace.
  The /s/<token> handler streams the file from disk on each request, so
  updates to the underlying object are reflected live (until revoked).
- ``pdf`` — the portal renders caller-supplied HTML to PDF at create
  time and stores the resulting file under ``data/shares/<token>.pdf``.
  The /s/<token> handler just serves the file. The render is one-shot:
  changing the source after creating the link has no effect.

Lifecycle:

- Tokens are 32-char URL-safe random (token_urlsafe(24)) — practically
  unguessable.
- TTL is bounded between ``MIN_TTL_SECONDS`` and ``MAX_TTL_SECONDS`` at
  create time. Defaults to 7 days; admins can configure neither (yet).
- ``max_views`` is optional; 0 means unlimited. Successful /s/<token>
  hits increment a counter; the cap is enforced before serving.
- Admin revoke sets ``revoked_at``; the public handler refuses thereafter.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import update
from sqlmodel import Session

from portal.config import settings
from portal.models import App, ShareLink, User

# Reasonable bounds. A share that's too short isn't useful; too long
# starts becoming a leak risk if the recipient mishandles the link.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MIN_TTL_SECONDS = 60                  # 1 minute floor for sanity
MAX_TTL_SECONDS = 90 * 24 * 3600      # 90 days ceiling

# Cap views at a sane upper bound — anything above this is probably an
# operator mistake (a shared link they meant to be one-off becoming
# unintentionally durable).
MAX_VIEW_LIMIT = 1000

# PDFs created via the ``pdf`` kind get capped at this many bytes after
# render. Matches the storage object cap so /s/ doesn't become a way to
# bypass per-namespace quotas.
MAX_PDF_BYTES = 10 * 1024 * 1024


def shares_dir() -> Path:
    """Resolve (and create) the directory holding rendered share PDFs."""
    p = Path(settings.data_dir).resolve() / "shares"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _clamp_ttl(ttl_seconds: Optional[int]) -> int:
    if ttl_seconds is None or ttl_seconds <= 0:
        return DEFAULT_TTL_SECONDS
    if ttl_seconds < MIN_TTL_SECONDS:
        return MIN_TTL_SECONDS
    if ttl_seconds > MAX_TTL_SECONDS:
        return MAX_TTL_SECONDS
    return ttl_seconds


def _clamp_views(max_views: Optional[int]) -> int:
    if max_views is None or max_views <= 0:
        return 0  # unlimited
    return min(max_views, MAX_VIEW_LIMIT)


def create_storage_share(
    db: Session,
    *,
    app_row: App,
    user: User,
    key: str,
    filename: str = "",
    ttl_seconds: Optional[int] = None,
    max_views: Optional[int] = None,
) -> ShareLink:
    """Persist a share row pointing at a per-user storage key.

    The caller has already validated the key against the storage rules
    (length, charset, no traversal). We re-store it verbatim — the public
    handler re-validates before reading from disk.
    """
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=_clamp_ttl(ttl_seconds)
    )
    row = ShareLink(
        token=token,
        app_id=app_row.id or 0,
        created_by=user.id or 0,
        kind="storage",
        payload={"key": key},
        filename=(filename or Path(key).name)[:80],
        expires_at=expires_at,
        max_views=_clamp_views(max_views),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_pdf_share(
    db: Session,
    *,
    app_row: App,
    user: User,
    html: str,
    filename: str = "shared.pdf",
    ttl_seconds: Optional[int] = None,
    max_views: Optional[int] = None,
) -> ShareLink:
    """Render the HTML to PDF and persist a share row pointing at it.

    Uses the same WeasyPrint configuration as ``/api/v1/pdf/render`` (no
    external fetcher). The output file lives at
    ``data/shares/<token>.pdf``; tokens are random so they don't collide.
    Caller responsibility to gate against the "pdf" service permission.
    """
    from portal.api import _no_external_fetcher

    try:
        from weasyprint import HTML
    except ImportError:
        raise RuntimeError("PDF service unavailable: WeasyPrint not installed")

    token = secrets.token_urlsafe(24)
    target = shares_dir() / f"{token}.pdf"

    # Render to bytes first so we can enforce the size cap before
    # committing anything to disk. WeasyPrint can balloon a small HTML
    # input into a many-MB PDF if pathological CSS is used.
    import io

    buf = io.BytesIO()
    HTML(string=html, url_fetcher=_no_external_fetcher).write_pdf(buf)
    body = buf.getvalue()
    if len(body) > MAX_PDF_BYTES:
        raise RuntimeError(
            f"Rendered PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)}MB share cap"
        )
    target.write_bytes(body)

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=_clamp_ttl(ttl_seconds)
    )
    row = ShareLink(
        token=token,
        app_id=app_row.id or 0,
        created_by=user.id or 0,
        kind="pdf",
        payload={"path": f"{token}.pdf"},
        filename=(filename or "shared.pdf")[:80],
        expires_at=expires_at,
        max_views=_clamp_views(max_views),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def share_url(token: str, request_host: Optional[str] = None) -> str:
    """Build the public URL the SDK hands back to the app.

    Always points at the portal origin (the bare ``SITE_URL``), not the
    app subdomain — share recipients are external and the launcher
    chrome would just confuse them. Scheme follows ``cookies_secure``
    (https in production, http in dev).
    """
    scheme = "https" if settings.cookies_secure else "http"
    # If we know the request host (port included), prefer it so dev
    # links on lvh.me:8000 stay reachable rather than redirecting to
    # the bare hostname.
    if request_host and ":" in request_host and not request_host.startswith("["):
        # Strip subdomain — share is portal-origin. The request might
        # have arrived on <slug>.apps.<site> when called from a child
        # app via the SDK; we want the bare site for the public link.
        port = ":" + request_host.rsplit(":", 1)[1]
    else:
        port = ""
    return f"{scheme}://{settings.site_url}{port}/s/{token}"


def lookup_active(db: Session, token: str) -> Optional[ShareLink]:
    """Find a share by token if it's still serveable.

    Returns None for unknown / revoked / expired / view-capped tokens.
    Doesn't increment the view counter — callers do that after they've
    decided to actually serve the response (so a 404 on the underlying
    file doesn't burn a view).
    """
    from sqlmodel import select

    row = db.exec(select(ShareLink).where(ShareLink.token == token)).first()
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is None or expires_at < now:
        return None
    if row.max_views and row.view_count >= row.max_views:
        return None
    return row


def record_view(db: Session, row: ShareLink) -> bool:
    """Atomically increment view_count, refusing if it would exceed max_views.

    Returns True if the increment was applied, False if the share has hit
    its cap concurrently (caller should treat this as a 404 to match the
    behavior of ``lookup_active`` finding a saturated share).

    The previous read-modify-write let N concurrent /s/<token> hits all
    pass the ``view_count < max_views`` check in lookup_active and then
    each separately increment, exceeding the cap by ~N. Pushing the cap
    test into the WHERE clause makes the bound exact under SQLite's
    statement-level locking.
    """
    if row.max_views and row.max_views > 0:
        result = db.exec(
            update(ShareLink)
            .where(ShareLink.id == row.id)
            .where(ShareLink.view_count < row.max_views)
            .values(view_count=ShareLink.view_count + 1)
        )
        db.commit()
        applied = bool(getattr(result, "rowcount", 0))
    else:
        # No cap — a plain increment is fine.
        db.exec(
            update(ShareLink)
            .where(ShareLink.id == row.id)
            .values(view_count=ShareLink.view_count + 1)
        )
        db.commit()
        applied = True
    if applied:
        # Keep the passed-in row consistent with the DB for any caller that
        # reads view_count after this returns.
        row.view_count += 1
    return applied


def revoke(db: Session, row: ShareLink) -> None:
    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()


def delete_share_files(token_filenames: list[str]) -> None:
    """Remove on-disk PDFs for share rows that have been deleted from the DB."""
    base = shares_dir()
    for name in token_filenames:
        if not name:
            continue
        # Defensive: only delete files inside the shares dir.
        target = (base / name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            continue
        try:
            if target.is_file():
                target.unlink()
        except OSError:
            pass
