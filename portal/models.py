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
    services: list[str] = Field(default_factory=list, sa_column=Column(JSON))
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
