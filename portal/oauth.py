"""OAuth 2.1 Authorization Server for the ``/mcp`` connector.

Claude.ai's remote-MCP connector authenticates with OAuth — it cannot send the
static ``ApiToken`` bearer the way Claude Code / Desktop do. The portal runs a
small OAuth Authorization Server on its own origin using the ``mcp`` SDK's auth
framework:

  * the SDK provides the protocol endpoints — ``/authorize``, ``/token``,
    ``/register`` (dynamic client registration, RFC 7591), ``/revoke``, the
    authorization-server metadata, and the protected-resource metadata — plus
    mandatory PKCE (S256) verification on the token exchange;
  * this module supplies the storage-backed *provider* (the four ``OAuth*``
    tables) and a *consent* step that reuses the portal's own admin login.

Security model: an OAuth access token carries the **same privilege as an admin
API token**. ``/authorize`` parks the request and bounces the browser to the
portal consent page, which requires a signed-in **admin** who explicitly
approves; only then is an authorization code minted. Client secrets, codes, and
tokens are random (>=256 bits); codes and tokens are stored as SHA-256 hashes.
Static API tokens keep working on ``/mcp`` unchanged — this is additive.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, Request
from pydantic import AnyHttpUrl, AnyUrl
from sqlalchemy import delete, or_
from sqlmodel import Session, select
from starlette.responses import RedirectResponse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull
from mcp.shared.auth import OAuthToken as OAuthTokenResponse

from portal.audit import emit_security_line
from portal.config import settings
from portal.db import engine, get_db
from portal.deps import current_user
from portal.models import (
    OAuthClient,
    OAuthCode,
    OAuthPendingAuthorization,
    OAuthToken as OAuthTokenRow,
    User,
)
from portal.security import check_csrf
from portal.web import render

logger = logging.getLogger("uvicorn.error")

# Single scope — the connector gets full admin-equivalent MCP access or nothing.
SCOPE = "mcp"
# Authorization codes are single-use and short-lived (RFC 6749 §10.5).
CODE_TTL = timedelta(seconds=60)
# A parked /authorize request waiting on the admin to sign in + consent.
PENDING_TTL = timedelta(minutes=15)
# Issued token lifetimes. Access is short; the connector refreshes silently.
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)


# ----- helpers -----

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat a DB datetime (stored naive) as UTC so comparisons/timestamps are right."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _gen() -> str:
    return secrets.token_urlsafe(32)  # ~256 bits


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issuer_url() -> str:
    """Public OAuth issuer — the portal's own origin. Scheme follows cookies_secure."""
    scheme = "https" if settings.cookies_secure else "http"
    return f"{scheme}://{settings.site_url}"


def resource_url() -> str:
    return f"{issuer_url().rstrip('/')}/mcp"


def resource_metadata_url() -> str:
    """RFC 9728 metadata URL the /mcp 401 points clients to (path-suffixed form)."""
    return f"{issuer_url().rstrip('/')}/.well-known/oauth-protected-resource/mcp"


def _scopes(scope: str) -> list[str]:
    return scope.split() if scope else []


# ----- provider (implements mcp.server.auth OAuthAuthorizationServerProvider) -----

class PortalOAuthProvider:
    """Storage-backed OAuth provider. Opens its own DB sessions (the SDK calls
    these from Starlette routes, not inside a portal request session)."""

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        with Session(engine) as db:
            row = db.get(OAuthClient, client_id)
            if row is None:
                return None
            # client_info round-trips the full RFC 7591 object incl. the secret,
            # which the SDK's ClientAuthenticator compares with hmac at /token.
            return OAuthClientInformationFull.model_validate(row.client_info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with Session(engine) as db:
            data = client_info.model_dump(mode="json")
            row = db.get(OAuthClient, client_info.client_id)
            if row is None:
                db.add(OAuthClient(client_id=client_info.client_id, client_info=data))
            else:
                row.client_info = data
                db.add(row)
            db.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to the portal consent page.

        The redirect_uri + PKCE challenge have already been validated by the SDK
        authorize handler; we persist them and hand control to /oauth/consent,
        which authenticates the admin and (on approval) mints the code.
        """
        txn = _gen()
        with Session(engine) as db:
            db.add(
                OAuthPendingAuthorization(
                    txn=txn,
                    client_id=client.client_id,
                    redirect_uri=str(params.redirect_uri),
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    code_challenge=params.code_challenge,
                    scope=" ".join(params.scopes or [SCOPE]),
                    state=params.state,
                    resource=params.resource,
                    expires_at=_utcnow() + PENDING_TTL,
                )
            )
            db.commit()
        return f"{issuer_url().rstrip('/')}/oauth/consent?txn={txn}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        with Session(engine) as db:
            row = db.get(OAuthCode, _hash(authorization_code))
            if row is None or row.client_id != client.client_id:
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=_scopes(row.scope),
                expires_at=_as_aware(row.expires_at).timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
            )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthTokenResponse:
        # Single-use: consume the code row, then issue tokens. (The SDK already
        # verified PKCE, expiry, redirect_uri, and client_id before calling us.)
        with Session(engine) as db:
            row = db.get(OAuthCode, _hash(authorization_code.code))
            if row is None:
                raise TokenError("invalid_grant", "authorization code already used")
            user_id = row.user_id
            db.delete(row)
            db.commit()
        return self._issue(
            client.client_id, user_id, authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        with Session(engine) as db:
            row = db.exec(
                select(OAuthTokenRow).where(OAuthTokenRow.refresh_token_hash == _hash(refresh_token))
            ).first()
            if row is None or row.revoked_at is not None or row.client_id != client.client_id:
                return None
            exp = _as_aware(row.refresh_expires_at)
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=_scopes(row.scope),
                expires_at=int(exp.timestamp()) if exp else None,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthTokenResponse:
        # Rotate: revoke the presented refresh token's row, issue a fresh pair.
        with Session(engine) as db:
            row = db.exec(
                select(OAuthTokenRow).where(
                    OAuthTokenRow.refresh_token_hash == _hash(refresh_token.token)
                )
            ).first()
            if row is None or row.revoked_at is not None:
                raise TokenError("invalid_grant", "refresh token not found")
            user_id = row.user_id
            resource = row.resource
            row.revoked_at = _utcnow()
            db.add(row)
            db.commit()
        return self._issue(client.client_id, user_id, scopes or refresh_token.scopes, resource)

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        with Session(engine) as db:
            row = db.exec(
                select(OAuthTokenRow).where(OAuthTokenRow.access_token_hash == _hash(token))
            ).first()
            if row is None or row.revoked_at is not None:
                return None
            exp = _as_aware(row.access_expires_at)
            if exp and exp < _utcnow():
                return None
            return AccessToken(
                token=token,
                client_id=row.client_id,
                scopes=_scopes(row.scope),
                expires_at=int(exp.timestamp()) if exp else None,
                resource=row.resource,
            )

    async def revoke_token(self, token) -> None:
        # token is an AccessToken or RefreshToken; match either column.
        h = _hash(token.token)
        with Session(engine) as db:
            row = db.exec(
                select(OAuthTokenRow).where(
                    or_(
                        OAuthTokenRow.access_token_hash == h,
                        OAuthTokenRow.refresh_token_hash == h,
                    )
                )
            ).first()
            if row is not None and row.revoked_at is None:
                row.revoked_at = _utcnow()
                db.add(row)
                db.commit()

    # ----- internal -----

    def _issue(self, client_id: str, user_id: int, scopes: list[str], resource: Optional[str]) -> OAuthTokenResponse:
        access = _gen()
        refresh = _gen()
        now = _utcnow()
        scope_str = " ".join(scopes or [SCOPE])
        with Session(engine) as db:
            db.add(
                OAuthTokenRow(
                    access_token_hash=_hash(access),
                    refresh_token_hash=_hash(refresh),
                    client_id=client_id,
                    user_id=user_id,
                    scope=scope_str,
                    resource=resource,
                    access_expires_at=now + ACCESS_TTL,
                    refresh_expires_at=now + REFRESH_TTL,
                )
            )
            db.commit()
        return OAuthTokenResponse(
            access_token=access,
            token_type="Bearer",
            expires_in=int(ACCESS_TTL.total_seconds()),
            refresh_token=refresh,
            scope=scope_str,
        )


oauth_provider = PortalOAuthProvider()


# ----- /mcp bearer validation (called from the MCP ASGI auth wrapper) -----

def authenticate_oauth_token(db: Session, authorization: Optional[str]) -> Optional[User]:
    """Resolve an OAuth ``Authorization: Bearer`` access token to its admin User.

    Mirrors ``deps.authenticate_bearer`` for OAuth tokens: returns None for a
    missing / malformed / unknown / revoked / expired token. The caller enforces
    the admin role (same as the static-token path)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    raw = authorization[7:].strip()
    if not raw:
        return None
    row = db.exec(
        select(OAuthTokenRow).where(OAuthTokenRow.access_token_hash == _hash(raw))
    ).first()
    if row is None or row.revoked_at is not None:
        return None
    exp = _as_aware(row.access_expires_at)
    if exp and exp < _utcnow():
        return None
    return db.get(User, row.user_id)


def prune_oauth(db: Session) -> None:
    """Best-effort cleanup of expired pending requests, codes, and dead tokens.

    Called opportunistically at startup (db.init_db), like the other rolling
    tables. Cutoff is naive UTC to match how datetimes are stored on SQLite."""
    cutoff = _utcnow().replace(tzinfo=None)
    client_cutoff = (_utcnow() - timedelta(days=7)).replace(tzinfo=None)
    try:
        db.exec(delete(OAuthPendingAuthorization).where(OAuthPendingAuthorization.expires_at < cutoff))
        db.exec(delete(OAuthCode).where(OAuthCode.expires_at < cutoff))
        # Drop tokens whose refresh window has fully lapsed (access already dead).
        db.exec(delete(OAuthTokenRow).where(OAuthTokenRow.refresh_expires_at < cutoff))
        # Bound open dynamic-client registration: drop clients older than a week
        # that never produced a token. A completed connection always has a token
        # row, so this only sweeps abandoned or spam /register entries — live
        # connectors are kept regardless of age.
        used_clients = select(OAuthTokenRow.client_id).distinct()
        db.exec(
            delete(OAuthClient).where(
                OAuthClient.created_at < client_cutoff,
                OAuthClient.client_id.not_in(used_clients),
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


# ----- consent UI (portal-origin, admin-authenticated) -----

oauth_router = APIRouter()

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[Optional[User], Depends(current_user)]


def _login_redirect(txn: str) -> RedirectResponse:
    from urllib.parse import quote

    nxt = quote(f"/oauth/consent?txn={txn}", safe="")
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


def _client_redirect(request: Request, user, url: str):
    """Send the browser back to the OAuth client's redirect_uri.

    Returns a small interstitial that meta-refreshes to ``url`` rather than a
    302 off the consent form POST. The consent page's CSP carries
    ``form-action 'self'``, and WebKit/Safari enforces form-action against the
    *redirect target* of a form submission — which silently blocks a 302 to the
    external client (e.g. claude.ai), the cause of "Approve does nothing". The
    POST returns to 'self' (allowed); the meta-refresh then makes the
    cross-origin hop, which form-action does not govern. See oauth_redirect.html.
    """
    return render(request, "oauth_redirect.html", user=user, redirect_url=url)


def _client_name(db: Session, client_id: str) -> str:
    row = db.get(OAuthClient, client_id)
    if row and isinstance(row.client_info, dict):
        return (row.client_info.get("client_name") or "").strip() or "An MCP client"
    return "An MCP client"


@oauth_router.get("/oauth/consent")
def consent_form(txn: str, request: Request, db: DbDep, user: UserDep):
    if user is None:
        return _login_redirect(txn)
    pending = db.get(OAuthPendingAuthorization, txn)
    if pending is None or _as_aware(pending.expires_at) < _utcnow():
        return render(
            request, "oauth_error.html", user=user,
            message="This authorization request has expired. Start the connection again from Claude.",
        )
    if user.role != "admin":
        return render(
            request, "oauth_error.html", user=user,
            message="MCP access is admin-only. Sign in as an admin to connect this client.",
        )
    return render(
        request, "oauth_consent.html", user=user,
        txn=txn, client_name=_client_name(db, pending.client_id), scope=SCOPE,
    )


@oauth_router.post("/oauth/consent")
def consent_submit(
    request: Request,
    db: DbDep,
    user: UserDep,
    txn: Annotated[str, Form()] = "",
    decision: Annotated[str, Form()] = "deny",
    csrf: Annotated[str, Form(alias="_csrf")] = "",
):
    check_csrf(request, csrf)
    if user is None:
        return _login_redirect(txn)
    pending = db.get(OAuthPendingAuthorization, txn)
    if pending is None or _as_aware(pending.expires_at) < _utcnow():
        return render(
            request, "oauth_error.html", user=user,
            message="This authorization request has expired. Start the connection again from Claude.",
        )
    if user.role != "admin":
        return render(
            request, "oauth_error.html", user=user,
            message="MCP access is admin-only.",
        )

    redirect_uri = pending.redirect_uri
    state = pending.state
    client_id = pending.client_id  # capture before the row is deleted below

    if decision != "approve":
        db.delete(pending)
        db.commit()
        from portal.audit import record_event
        record_event(db, actor=user, action="oauth.deny", request=request,
                     target=f"client:{client_id}")
        params = {"error": "access_denied"}
        if state:
            params["state"] = state
        return _client_redirect(request, user, construct_redirect_uri(redirect_uri, **params))

    # Approve: mint a single-use authorization code bound to this admin.
    raw_code = _gen()
    db.add(
        OAuthCode(
            code_hash=_hash(raw_code),
            client_id=pending.client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            code_challenge=pending.code_challenge,
            scope=pending.scope or SCOPE,
            resource=pending.resource,
            expires_at=_utcnow() + CODE_TTL,
        )
    )
    db.delete(pending)
    db.commit()

    from portal.audit import record_event
    record_event(db, actor=user, action="oauth.authorize", request=request,
                 target=f"client:{client_id}")

    params = {"code": raw_code}
    if state:
        params["state"] = state
    return _client_redirect(request, user, construct_redirect_uri(redirect_uri, **params))


# ----- route assembly (called from main.py when MCP is enabled) -----

# The OAuth AS endpoints that carry credentials/codes — wrapped below so their
# auth failures feed fail2ban. Discovery/metadata GETs are deliberately excluded
# (a 4xx there isn't abuse and shouldn't ban anyone).
_AUDITED_PATHS = {"/authorize", "/token", "/register", "/revoke"}


def _ip_from_scope(scope) -> str:
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in (scope.get("headers") or [])
    }
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "-"
    client = scope.get("client")
    return client[0] if client else "-"


class _OAuthAuditASGI:
    """Wrap an OAuth AS endpoint so a 400/401 emits ``OAUTH_AUTH_FAILED`` to
    security.log. This gives the fail2ban login jail a signal to ban
    client-secret / code / PKCE guessing + bad-registration floods on the OAuth
    endpoints — the same way ``MCP_AUTH_FAILED`` covers bad bearer tokens on
    /mcp. Successful (2xx) and redirect (3xx) responses are passed through
    untouched, so legitimate authorize/token/refresh traffic is never logged.
    """

    def __init__(self, app, label: str):
        self._app = app
        self._label = label

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        seen = {"status": None}

        async def _send(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
            await send(message)

        await self._app(scope, receive, _send)
        if seen["status"] in (400, 401):
            try:
                emit_security_line(
                    "OAUTH_AUTH_FAILED", _ip_from_scope(scope),
                    endpoint=self._label, status=str(seen["status"]),
                )
            except Exception:
                pass


def build_oauth_routes() -> list:
    """Build the SDK's OAuth AS + protected-resource Starlette routes.

    Raises ValueError if the issuer isn't a valid OAuth issuer (the SDK requires
    HTTPS except for localhost); the caller skips OAuth wiring in that case but
    keeps the static-token /mcp path working.
    """
    issuer = AnyHttpUrl(issuer_url())
    routes = create_auth_routes(
        provider=oauth_provider,
        issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(resource_url()),
        authorization_servers=[issuer],
        scopes_supported=[SCOPE],
        resource_name="PWA Portal MCP",
    )
    # Wrap the credential-bearing endpoints so their auth failures reach
    # fail2ban (see _OAuthAuditASGI). Route.app is the compiled ASGI app the
    # route dispatches to; replacing it keeps routing + CORS intact.
    for route in routes:
        if getattr(route, "path", None) in _AUDITED_PATHS:
            route.app = _OAuthAuditASGI(route.app, route.path)
    return routes
