from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="user")  # "admin" or "user"
    created_at: datetime = Field(default_factory=_utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class App(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    name: str
    description: Optional[str] = None
    version: str
    icon: Optional[str] = None  # relative path inside the app dir
    entry: str = "index.html"
    # What the manifest declared. Persisted as the source of truth for "what
    # this app says it needs" — never changes after install except by re-upload.
    services: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # The subset of ``services`` an admin has approved. SDK calls to
    # ``portal.pdf``, ``portal.email``, ``portal.storage`` 403 when the
    # corresponding service is absent from this list. Like ``allowed_origins``,
    # new uploads auto-approve everything the manifest declared (the admin
    # already trusted the bundle by uploading it) and admin revocations
    # persist across re-uploads. Empty list means the app loses access to
    # every gated service; an admin can revoke and re-grant freely.
    #
    # Back-compat: pre-feature rows have an empty list here. The enforcement
    # path treats an empty ``allowed_services`` on an app whose manifest
    # also declared no services as "no gating" (legacy behavior) — so
    # existing apps that didn't declare anything in ``services`` keep
    # working without admin action.
    allowed_services: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # When True, the app opted into a strict Content-Security-Policy:
    # ``'unsafe-inline'`` / ``'unsafe-eval'`` are dropped, and a per-response
    # nonce is generated and substituted into the served HTML in place of the
    # literal token ``{{NONCE}}``. Only takes effect on the app subdomain
    # (per-app-origin mode); under ``CHILD_APPS_SAME_ORIGIN=true`` the portal
    # launcher's own inline scripts share the origin, so the flag is
    # silently ignored to keep the launcher working.
    csp_strict: bool = Field(default=False)
    # External HTTPS origins this app's manifest declared it needs to reach
    # (e.g. ["https://api.open-meteo.com"]). Refreshed on every upload /
    # replace; the manifest is the source of truth for what was requested.
    requested_origins: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # The subset of ``requested_origins`` an admin has approved. Drives the
    # ``connect-src`` directive in the per-app CSP. New uploads auto-approve
    # everything the manifest declared (the admin uploaded it); revocations
    # made through the admin UI persist across re-uploads of the same slug.
    allowed_origins: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = Field(default=True)
    # Admin-controlled sort key for the user-facing dashboard tile grid AND
    # the /admin/apps table. Lower values render first. New apps default to
    # 0; the migration backfills existing rows in spaced increments (10, 20,
    # …) by name so the visual order doesn't change on upgrade and there's
    # headroom for re-inserts without a full renumber. Admins reshuffle via
    # the up/down chips on /admin/apps, which renumber the whole list.
    display_order: int = Field(default=0, index=True)
    uploaded_by: Optional[int] = Field(default=None, foreign_key="user.id")
    uploaded_at: datetime = Field(default_factory=_utcnow)


class ApiToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=80)
    token_hash: str = Field(index=True)
    prefix: str  # first 8 chars of the raw token, shown for identification
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: Optional[datetime] = None


class UserSession(SQLModel, table=True):
    # Server-side session record. The cookie payload only carries the opaque
    # ``id`` (a random token); auth state lives here so logout / password
    # change can revoke a session even though its signed cookie is still
    # within its max_age window. Name avoids colliding with sqlmodel.Session.
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)
    revoked_at: Optional[datetime] = None


class AppLaunchToken(SQLModel, table=True):
    # Single-use, short-lived token minted on the portal origin and consumed
    # on the app subdomain to bootstrap an AppSession. The token's ``slug`` is
    # locked at mint time — the subdomain handler refuses to mint an
    # AppSession unless the token's slug matches the host-derived slug.
    token: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    slug: str = Field(index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    consumed_at: Optional[datetime] = None


class AppSession(SQLModel, table=True):
    # Per-(user, app-slug) session minted by the exchange endpoint on the app
    # subdomain. Lifetime is independent of UserSession; cascade revocation is
    # explicit in logout / password-change handlers, so no parent FK linkage
    # is stored (design spec option (b)).
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    slug: str = Field(index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)
    revoked_at: Optional[datetime] = None


class LoginAttempt(SQLModel, table=True):
    # Rolling history of /login POSTs, shown on the admin health dashboard.
    # Inserted on both success and failure. ``email`` is the form-submitted
    # value lowercased; it may not correspond to a real User row (the whole
    # point of logging failures is catching typos and brute-force probes).
    # The table is pruned to the most recent ~500 rows by an opportunistic
    # cleanup in init_db so it can't grow without bound.
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_utcnow, index=True)
    ip: str = Field(default="")
    email: str = Field(default="")
    success: bool = Field(default=False)
    # Short tag for the failure mode: "bad_credentials", "rate_limited",
    # "ok" (on success). Free-form to keep the schema simple; the dashboard
    # treats unknown values as a passthrough display.
    reason: str = Field(default="")


class EmailSendLog(SQLModel, table=True):
    # Rolling history of successful /api/v1/email/send calls, shown on the
    # admin health dashboard. Failures bubble up as 5xx and aren't logged
    # here (they have no stable recipient/subject to display); the dashboard
    # surfaces them via uvicorn logs / SMTP test status. Same opportunistic
    # pruning as ``LoginAttempt`` keeps the table bounded.
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_utcnow, index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    app_slug: str = Field(default="")
    # First recipient verbatim (for display) + total count (so a bulk send
    # of 12 doesn't show as just the first address).
    recipient: str = Field(default="")
    recipient_count: int = Field(default=1)
    subject: str = Field(default="")
    status: str = Field(default="sent")  # "sent" or "failed"


class ShareLink(SQLModel, table=True):
    # A public, tokenized URL that lets a non-user (a customer, vendor,
    # etc.) view a single resource without signing in. Two kinds:
    #
    #   - ``storage`` — references an object in the creator's per-app
    #     storage namespace. Served by streaming the file at request time.
    #     ``payload`` carries ``{"key": "<storage key>"}``.
    #   - ``pdf`` — rendered at create time from caller-supplied HTML;
    #     the resulting PDF is stored on disk under ``data/shares/<token>.pdf``.
    #     ``payload`` carries ``{"path": "<filename inside data/shares>"}``.
    #
    # Tokens are 32-char URL-safe random; the table is indexed on token so
    # the public /s/<token> lookup is one SELECT. Revocation sets
    # ``revoked_at`` (server still serves the row from disk for audit but
    # /s/<token> refuses). Expiry is a hard cap, max_views is an optional
    # additional cap (0 means unlimited).
    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    app_id: int = Field(foreign_key="app.id", index=True)
    created_by: int = Field(foreign_key="user.id")
    kind: str = Field(default="storage")  # "storage" or "pdf"
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    filename: str = Field(default="")  # served as Content-Disposition filename
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    max_views: int = Field(default=0)  # 0 = unlimited
    view_count: int = Field(default=0)
    revoked_at: Optional[datetime] = None


class UserAppAccess(SQLModel, table=True):
    # Per-user grant for a specific app. Presence of the row = user can launch
    # the app from the dashboard / API; absence = denied. Admins bypass this
    # check entirely (role == "admin" implies access to everything), so no
    # rows are stored for admins.
    #
    # Rows are populated automatically when a user is created OR an app is
    # uploaded, controlled by the ``default_user_app_access`` Setting key
    # ("all" or "none"). The default-access setting is consulted at creation
    # time only — flipping it later doesn't retroactively reshape existing
    # grants. Admins reshape per-user/per-app from /admin/users/<id>/apps.
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    app_id: int = Field(foreign_key="app.id", primary_key=True)
    granted_at: datetime = Field(default_factory=_utcnow)
