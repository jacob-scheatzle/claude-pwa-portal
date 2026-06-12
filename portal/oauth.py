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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import AnyHttpUrl, AnyUrl
from sqlalchemy import delete, or_, update
from sqlmodel import Session, select
from starlette.responses import JSONResponse, RedirectResponse

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
from portal.middleware import resolve_app_slug_from_host
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


# Client secrets live inside the OAuthClient.client_info JSON. The SDK's
# ClientAuthenticator compares the presented secret against the cleartext one in
# that object (constant-time), so we can't hash it — but we can encrypt it at
# rest with the same Fernet machinery used for the SMTP password
# (portal.settings_store). We mark the encrypted value with this prefix so
# get_client only attempts decryption on values we actually wrote, and any
# legacy plaintext secret keeps working until the client re-registers.
_SECRET_ENC_PREFIX = "enc:v2:"


def _encrypt_client_secret(secret: Optional[str]) -> Optional[str]:
    if not secret or secret.startswith(_SECRET_ENC_PREFIX):
        return secret
    from portal.settings_store import _fernet

    token = _fernet().encrypt(secret.encode("utf-8")).decode("ascii")
    return _SECRET_ENC_PREFIX + token


def _decrypt_client_secret(stored: Optional[str]) -> Optional[str]:
    if not stored or not stored.startswith(_SECRET_ENC_PREFIX):
        return stored  # legacy plaintext (or absent) — pass through unchanged
    from cryptography.fernet import InvalidToken

    from portal.settings_store import _fernet

    try:
        return _fernet().decrypt(stored[len(_SECRET_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        # SECRET_KEY rotated or row tampered with — treat as no usable secret so
        # the compare fails closed rather than matching encrypted garbage.
        return None


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
            # which the SDK's ClientAuthenticator compares (constant-time) at
            # /token — so decrypt the at-rest secret back to cleartext first.
            data = dict(row.client_info or {})
            if data.get("client_secret"):
                data["client_secret"] = _decrypt_client_secret(data["client_secret"])
            return OAuthClientInformationFull.model_validate(data)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with Session(engine) as db:
            data = client_info.model_dump(mode="json")
            # Encrypt the client secret at rest (get_client decrypts it back).
            if data.get("client_secret"):
                data["client_secret"] = _encrypt_client_secret(data["client_secret"])
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
        # Single-use: atomically claim the code row, then issue tokens. (The SDK
        # already verified PKCE, expiry, redirect_uri, and client_id before
        # calling us.) Read user_id first, then DELETE ... WHERE code_hash=:h and
        # proceed only if exactly one row was removed — so two concurrent
        # exchanges of the same code can't both mint tokens (the loser sees 0
        # rows and is rejected as already-used).
        code_hash = _hash(authorization_code.code)
        with Session(engine) as db:
            row = db.get(OAuthCode, code_hash)
            if row is None:
                raise TokenError("invalid_grant", "authorization code already used")
            user_id = row.user_id
            result = db.exec(
                delete(OAuthCode).where(OAuthCode.code_hash == code_hash)
            )
            db.commit()
            if result.rowcount != 1:
                raise TokenError("invalid_grant", "authorization code already used")
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
        # Rotate atomically: claim the presented refresh token's row with a
        # conditional UPDATE ... SET revoked_at WHERE token_hash=:h AND
        # revoked_at IS NULL, and issue a fresh pair only if exactly one row was
        # claimed. Two concurrent refreshes of the same token then can't both
        # rotate (the loser updates 0 rows and is rejected).
        # NOTE: reuse-detection family-revoke (revoking the whole token lineage
        # if an already-rotated refresh token is replayed) is intentionally not
        # done here — it needs a lineage/family column on OAuthToken, which is a
        # schema change out of scope for this pass. PUNTED.
        token_hash = _hash(refresh_token.token)
        with Session(engine) as db:
            row = db.exec(
                select(OAuthTokenRow).where(OAuthTokenRow.refresh_token_hash == token_hash)
            ).first()
            if row is None or row.revoked_at is not None:
                raise TokenError("invalid_grant", "refresh token not found")
            user_id = row.user_id
            resource = row.resource
            result = db.exec(
                update(OAuthTokenRow)
                .where(
                    OAuthTokenRow.refresh_token_hash == token_hash,
                    OAuthTokenRow.revoked_at.is_(None),
                )
                .values(revoked_at=_utcnow())
            )
            db.commit()
            if result.rowcount != 1:
                raise TokenError("invalid_grant", "refresh token not found")
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
        # token is an AccessToken or RefreshToken; match either column. Conditional
        # UPDATE ... WHERE revoked_at IS NULL so a concurrent rotation/revoke can't
        # clobber an already-set revoked_at timestamp.
        h = _hash(token.token)
        with Session(engine) as db:
            db.exec(
                update(OAuthTokenRow)
                .where(
                    or_(
                        OAuthTokenRow.access_token_hash == h,
                        OAuthTokenRow.refresh_token_hash == h,
                    ),
                    OAuthTokenRow.revoked_at.is_(None),
                )
                .values(revoked_at=_utcnow())
            )
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


def _require_portal_origin(request: Request) -> None:
    """404 the consent routes on a child-app subdomain.

    The OAuth consent flow is a portal-origin, admin-only surface; it must not
    answer on ``<slug>.apps.<SITE_URL>`` (mirrors the /mcp Host gate in
    mcp_server._AuthASGIApp). ``app_slug`` is set by HostDispatchMiddleware.
    """
    if getattr(request.state, "app_slug", None) is not None:
        raise HTTPException(status_code=404, detail="Not found")


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
    _require_portal_origin(request)
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
    _require_portal_origin(request)
    # Re-login check BEFORE CSRF: a lapsed session has no CSRF token to match,
    # so enforcing CSRF first would yield a confusing 403 instead of a clean
    # bounce to /login (which returns the admin to consent afterwards).
    if user is None:
        return _login_redirect(txn)
    check_csrf(request, csrf)
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


class _HostGateASGI:
    """404 an OAuth AS endpoint when reached on a child-app subdomain.

    The OAuth AS lives on the portal origin only; it must not answer on
    ``<slug>.apps.<SITE_URL>`` (mirrors the /mcp Host gate). These SDK routes
    are raw ASGI apps that don't see ``request.state.app_slug``, so we resolve
    the slug from the Host header directly here.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in (scope.get("headers") or [])
            }
            if resolve_app_slug_from_host(headers.get("host", ""), settings.site_url) is not None:
                await JSONResponse({"error": "Not found"}, status_code=404)(scope, receive, send)
                return
        await self._app(scope, receive, send)


# Per-IP throttle on dynamic client registration (RFC 7591 /register). Open
# registration is convenient for the connector but an unauthenticated write, so
# bound how fast one IP can mint clients. Same in-process rolling-window shape as
# the login limiter in main.py; lost on restart (acceptable — this is anti-abuse,
# not correctness), and prune_oauth still sweeps abandoned client rows.
_REGISTER_WINDOW_SECONDS = 3600
_REGISTER_LIMIT = 10
_register_hits: dict[str, list[float]] = {}


def _register_rate_limited(ip: str) -> bool:
    import time

    now = time.monotonic()
    cutoff = now - _REGISTER_WINDOW_SECONDS
    # Prune every IP's window so the dict can't grow without bound.
    for key in list(_register_hits.keys()):
        fresh = [t for t in _register_hits[key] if t > cutoff]
        if fresh:
            _register_hits[key] = fresh
        else:
            _register_hits.pop(key, None)
    hits = _register_hits.get(ip, [])
    if len(hits) >= _REGISTER_LIMIT:
        return True
    _register_hits.setdefault(ip, []).append(now)
    return False


class _RegisterGuardASGI:
    """Wrap /register: per-IP rate limit + a security-log line on success (201).

    A 429 is returned before the SDK handler runs once an IP exceeds the window
    limit. On a successful registration (201) an ``OAUTH_CLIENT_REGISTERED`` line
    is emitted so operators (and fail2ban dashboards) can see new clients appear.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        ip = _ip_from_scope(scope)
        if _register_rate_limited(ip):
            try:
                emit_security_line("OAUTH_REGISTER_RATE_LIMITED", ip, endpoint="/register")
            except Exception:
                pass
            await JSONResponse(
                {"error": "too_many_requests", "error_description": "registration rate limit exceeded"},
                status_code=429,
            )(scope, receive, send)
            return
        seen = {"status": None}

        async def _send(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
            await send(message)

        await self._app(scope, receive, _send)
        if seen["status"] == 201:
            try:
                emit_security_line("OAUTH_CLIENT_REGISTERED", ip, endpoint="/register")
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
    # route dispatches to; replacing it keeps routing + CORS intact. /register
    # additionally gets the rate-limit + success-log guard, and every route is
    # Host-gated so the AS never answers on a child-app subdomain.
    for route in routes:
        path = getattr(route, "path", None)
        if path == "/register":
            route.app = _RegisterGuardASGI(route.app)
        if path in _AUDITED_PATHS:
            route.app = _OAuthAuditASGI(route.app, path)
        route.app = _HostGateASGI(route.app)
    return routes
