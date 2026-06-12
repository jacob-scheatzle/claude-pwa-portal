# Per-app origin isolation — design spec

> **Status:** ✅ implemented and now the **default** — child apps run on per-app
> subdomains; `CHILD_APPS_SAME_ORIGIN=true` opts back into legacy same-origin.
> This document is retained as the architectural design record + rollout history;
> the live code is the source of truth. Read it before touching the launch /
> exchange / Host-dispatch paths.

## Goal

Today, child apps are served from `portal.example.com/apps/<slug>/` —
**same origin** as the portal. That means a malicious uploaded app can:

- Read or forge any same-origin fetch (the portal's API auth is just cookies)
- Bypass CSRF protection (same-origin scripts can read the token and forge headers)
- Read other apps' storage via the JS SDK
- Iframe the portal's admin UI (mitigated by `frame-ancestors 'none'` but only one layer)

These risks are mitigated by the trust assumption "admins only upload apps
they trust." The structural fix is to put each child app on its **own origin**
so the browser's same-origin policy enforces isolation.

## Decisions (locked)

| # | Question | Decision |
|---|---|---|
| 1 | Origin scheme | **Per-app subdomain**: `<slug>.apps.<SITE_URL>` |
| 2 | TLS path | **On-demand HTTP-01 per subdomain** (default; zero config) |
| 3 | App UX | **Iframe wrapper inside portal** (preserves iPhone PWA identity) |
| 4 | Back-compat | **Same-origin available as opt-out** via `CHILD_APPS_SAME_ORIGIN=true` |

Other smaller choices, decided in the same conversation:

- **Launch token:** URL fragment (`#token=...`), single-use, 60-second TTL. Stripped via `history.replaceState` immediately after read.
- **Localhost dev:** use `lvh.me` (resolves any `*.lvh.me` to 127.0.0.1) so `<slug>.apps.lvh.me` works without `/etc/hosts` edits.
- **`X-Portal-App` header:** drop *for browser SDK calls*. Slug comes from `Host` header server-side now.
  - **What changed during implementation:** the header was **kept for bearer-token clients**. The browser SDK no longer sends it (the subdomain `Host` carries the slug), but a token client (the Claude skill, MCP) has no app subdomain to derive a slug from, so `X-Portal-App` is still the slug source on those calls. `portal/api.py` reads it only when `request.state.auth_method == "token"`; cookie / `app_session` requests use the host-derived slug and ignore the header. So the header is "removed from the SDK, retained for token auth," not removed outright.
- **SDK API surface:** unchanged (`portal.user.current()`, `portal.pdf.*`, etc.). Internals re-architected; apps don't need code changes.
- **Existing apps:** keep working without re-upload. Caddy routes both `portal.example.com/apps/<slug>/...` (legacy) and `<slug>.apps.example.com/...` (new) to the same portal container; switching is a config flag.

## Architecture overview

Three logical origins:

| Origin | Purpose | Cookies |
|---|---|---|
| `portal.<SITE_URL>` (or `<SITE_URL>` if no subdomain) | The portal shell — auth, dashboard, admin pages, API for the portal itself | `session_id` (UserSession), `_csrf` |
| `<slug>.apps.<SITE_URL>` | One per child app — serves the app's HTML/CSS/JS and its API calls | `app_session` (AppSession scoped to slug + user); separate per subdomain |
| (bearer-only) | Programmatic clients (Claude skill) | None — Bearer token in header |

The portal **container** still serves everything; Caddy fronts both hostnames
and routes by `Host`. The FastAPI app reads `request.url.hostname` (or
`request.headers["host"]`) to determine which origin a request belongs to.

### Sequence: user clicks an app tile

```
[user on portal dashboard]
   click tile (slug=hello-receipt)
   ↓
GET portal.example.com/apps/hello-receipt/
   ↓ (portal-origin handler)
mint AppLaunchToken(user_id, slug, expires_in=60s, single_use=True)
   ↓
render iframe-wrapper HTML:
   <header> Portal chrome — back button, app name </header>
   <iframe src="https://hello-receipt.apps.example.com/#token=<token>"
           sandbox="allow-scripts allow-forms allow-popups allow-same-origin
                    allow-modals allow-downloads">
   </iframe>
   ↓
[browser loads the iframe]
GET hello-receipt.apps.example.com/  → returns the child app's index.html
                                       (Caddy → portal container; portal
                                        reads Host=hello-receipt.apps.example.com,
                                        resolves slug, serves data/apps/hello-receipt/index.html)
   ↓
[child app HTML loads <script src="/portal-sdk.js">]
GET hello-receipt.apps.example.com/portal-sdk.js → same SDK file
   ↓
SDK init:
  - reads `window.location.hash`, parses `#token=<launch_token>`
  - history.replaceState — strip token from URL bar
  - fetch(POST hello-receipt.apps.example.com/api/v1/session/exchange,
          body=launch_token, credentials='same-origin')
   ↓ (app-origin handler)
validate token: not expired, not consumed, slug matches, user still exists
mark token consumed
mint AppSession(user_id, slug, ...)
set cookie: name=app_session, value=<opaque>,
            Domain=hello-receipt.apps.example.com,
            HttpOnly, Secure, SameSite=Lax, Path=/
return 200
   ↓
[SDK is now logged in]
all subsequent calls: fetch('/api/v1/...', credentials='same-origin')
cookie is auto-sent. Same-origin to the subdomain.
slug is implicit (from Host header). No X-Portal-App needed.
```

### Sequence: bearer-token call from the Claude skill

Unchanged from today. Skill hits `portal.example.com/api/v1/apps/upload` with
`Authorization: Bearer <token>`. Server checks token, proceeds. The new app-
subdomain origin isn't involved because bearer auth is for portal-management
operations (uploading apps), not child-app sessions.

## Data model

### `AppLaunchToken` (new)

| Column | Type | Notes |
|---|---|---|
| `token` | str | PK, `secrets.token_urlsafe(32)` |
| `user_id` | int | FK → user |
| `slug` | str | the app slug it was minted for |
| `created_at` | datetime | utcnow |
| `expires_at` | datetime | created_at + 60s |
| `consumed_at` | Optional[datetime] | None while pending |

A row is created at `GET /apps/<slug>/`. The token's `slug` is locked to the
URL slug — can't be swapped at exchange time. Single-use: consumed once,
rejected on re-use.

### `AppSession` (new)

Separate from `UserSession`. A user signed into the portal has one `UserSession`;
when they open app `foo`, an `AppSession(user_id, slug='foo')` is also created.
Logging out of the portal revokes the `UserSession` *and* all the user's
`AppSession` rows.

| Column | Type | Notes |
|---|---|---|
| `id` | str | PK, opaque random; this is the cookie value |
| `user_id` | int | FK → user |
| `slug` | str | the app this session is for |
| `parent_user_session_id` | str | FK → UserSession.id (so we can cascade revocation) |
| `created_at` | datetime | utcnow |
| `last_seen_at` | datetime | bumped lazily on each authenticated request (>60s gate) |
| `revoked_at` | Optional[datetime] | None while active |

### `App` (unchanged)

The slug-derived subdomain (`<slug>.apps.<SITE_URL>`) is computed from
`App.slug` at request time; no schema change.

## New + modified endpoints

### `GET /apps/<slug>/` (portal origin) — UPDATED

Currently serves the child app HTML directly. After this change, it returns
the **iframe-wrapper page**.

```python
@app.get("/apps/{slug}/")
def app_launcher(slug: str, request: Request, db: DbDep, user: RequireUserDep):
    app_row = _lookup_app(db, slug)  # 404 if missing or disabled
    if settings.child_apps_same_origin:
        # Legacy path: serve files directly (current behavior)
        return _serve_app_index(slug, db, user)
    token = mint_launch_token(db, user.id, slug)
    iframe_src = f"https://{slug}.apps.{settings.site_url}/#token={token}"
    return render(request, "app_launcher.html", user=user, app=app_row, iframe_src=iframe_src)
```

### `GET /apps/<slug>/<path:path>` (portal origin) — UPDATED

In the new model, this returns 404 (child app files live on the subdomain).
In the same-origin fallback, current behavior preserved.

### `GET /` on `<slug>.apps.<SITE_URL>` (NEW serve path)

Caddy routes this to the portal container. A new FastAPI route dispatches by
Host header:

```python
def _resolve_slug_from_host(host: str) -> Optional[str]:
    # "hello-receipt.apps.example.com" → "hello-receipt"
    base = f".apps.{settings.site_url}"
    if not host.endswith(base): return None
    return host[: -len(base)] or None

@app.get("/", host="*.apps.{site_url}")  # FastAPI doesn't support host-based routing directly
# Workaround: a middleware that rewrites the route based on Host header,
# OR a catch-all route that dispatches internally.
```

FastAPI doesn't natively dispatch by Host. Options:
- Custom middleware: inspect `request.url.hostname`, if it matches the apps wildcard, rewrite the path or call a dedicated handler set.
- Single mount: every request comes through; the same FastAPI app handles both with explicit host checks at the top of each handler.

Recommended: a `HostDispatchMiddleware` that sets `request.state.app_slug` if
the host matches the apps pattern, otherwise sets it to `None`. Existing
routes can then check `request.state.app_slug` and either serve the
child-app content or proceed as portal routes.

### `POST /api/v1/session/exchange` (NEW, on the app subdomain)

Takes a launch token, validates, mints an AppSession cookie.

```python
@router.post("/session/exchange")
def session_exchange(
    request: Request,
    response: Response,
    db: DbDep,
    body: ExchangeRequest,  # { token: str }
):
    slug = request.state.app_slug
    if not slug:
        raise HTTPException(400, "Not on an app subdomain")
    launch = db.get(AppLaunchToken, body.token)
    if not launch or launch.consumed_at or launch.expires_at < utcnow() or launch.slug != slug:
        raise HTTPException(401, "Invalid or expired launch token")
    launch.consumed_at = utcnow()
    user_session_id = ...  # how? we need to know which UserSession this exchange should be tied to
    # ...mint AppSession bound to (user_id=launch.user_id, slug, parent_user_session_id)
    response.set_cookie("app_session", session_id,
                         httponly=True, secure=cookies_secure,
                         samesite="lax", domain=f"{slug}.apps.{site_url}")
    return {"ok": True}
```

> **Implementation note:** the shipped exchange sets the `app_session` cookie
> **host-only** — it does *not* set a `domain=` attribute. A host-only cookie
> is scoped to exactly `<slug>.apps.<SITE_URL>` and is never sent to sibling
> subdomains, which is the isolation we want; a `Domain=`-scoped cookie would
> widen its scope to subdomains of the named host. Omitting `domain=` is the
> deliberate, tighter choice.

**Open issue:** how does the exchange call know which UserSession to bind to?
The exchange happens cross-origin from the iframe; the portal's user cookie
isn't sent. Two options:
- (a) Include the parent UserSession id in the launch token payload (server-only, since the launch token itself is opaque)
- (b) Drop the parent-session linkage; AppSession lifetime is independent of UserSession. Cascade on logout via a query in `logout_handler` that revokes all the user's app sessions too.

Lean (b) — simpler. Cascade explicit in logout/password-change.

> **Resolved (shipped):** option (b). `AppSession` stores no
> `parent_user_session_id` — its lifetime is independent of the parent
> `UserSession` (see `portal/models.py:AppSession`). Logout and
> password-change call `revoke_all_app_sessions_for_user(...)` in
> `portal/main.py` to cascade the revocation explicitly.

### `GET /api/v1/<path>` (on app subdomain) — UPDATED

Existing endpoints check `request.state.app_slug` instead of `X-Portal-App`.
Storage endpoints use the host-derived slug; no client header trust required.

> **Implementation note:** "drop the header handling" applies only to
> **browser** (cookie / `app_session`) requests, which now derive the slug
> from the host and never trust a client-supplied header. **Bearer-token
> requests still read `X-Portal-App`** — they have no app subdomain, so the
> header is their only slug source. `portal/api.py` keys this off
> `request.state.auth_method`: host-derived slug for cookie/`app_session`,
> `X-Portal-App` for `token`. The header isn't fully removed; it's narrowed to
> token auth.

### `GET /apps/<slug>/<path:path>` (NEW serve path on subdomain)

Static file serving for the child app's bundle. The path-traversal guards
from today's `_serve_app_file` carry over verbatim. Host = subdomain; slug
derived from host.

## Caddy config

Pseudocode for Caddyfile:

```
{$SITE_URL:localhost} {
    reverse_proxy portal:8000
    encode zstd gzip

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        # ...existing security headers, including the shell CSP
    }
}

# Child-app subdomains. on_demand_tls fetches a cert per subdomain on first hit.
*.apps.{$SITE_URL} {
    reverse_proxy portal:8000
    encode zstd gzip

    tls {
        on_demand
    }

    header {
        # NOTE (as shipped): Caddy sets NO Content-Security-Policy for child
        # subdomains. The CSP is per-app — its connect-src enumerates the
        # external origins from App.allowed_origins, which lives in the DB and
        # Caddy can't see — so the portal owns it. Caddy only sets the
        # response-agnostic headers below; the per-app CSP comes from the app.
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
    }
}

# Rate-limit on_demand cert requests: ask the portal whether this subdomain
# is allowed (matches a real app slug) before requesting a cert.
{
    on_demand_tls {
        ask http://portal:8000/api/v1/internal/cert-ask
    }
}
```

> **Implementation note (CSP):** the static Caddy CSP above was **not** what
> shipped. Per-app CSP moved into `ChildAppCSPMiddleware`
> (`portal/middleware.py`): for any request that resolved to a child subdomain
> it builds the header from that app's `App.allowed_origins`
> (`connect-src 'self' <approved external origins…>`), with optional strict /
> nonce mode when the app sets `permissions.csp_strict`. **Caddy sets no CSP
> for `*.apps.<SITE_URL>`** — only HSTS + the other defense-in-depth headers.
> `frame-ancestors https://<SITE_URL>` is emitted by the middleware so only the
> portal's iframe can embed the app.

**`/api/v1/internal/cert-ask`** — new endpoint on the portal, restricted to the
internal Docker network (no public exposure). Caddy GETs it with the
requested hostname; portal returns 200 if the hostname matches a real
enabled app slug, 404 otherwise. Prevents an attacker from probing
`<random>.apps.example.com` to exhaust Let's Encrypt rate limits. (The route
lives under the `/api/v1` router as `cert_ask`; the Caddyfile points
`on_demand_tls.ask` at it.)

## SDK changes

The SDK file (`/portal-sdk.js`) is served identically on both origins. The
internals differ:

**On portal origin (legacy fallback):** today's behavior. Same-origin SDK.

**On app subdomain (new default):**
1. On init, check `window.location.hash` for `#token=...`.
2. If present, strip it (`history.replaceState`).
3. POST the token to `/api/v1/session/exchange` (same-origin).
4. On success, continue normally. On failure, set a clear error state
   surfaced via `portal.user.current()` rejecting.
5. CSRF token fetch (existing helper) works as today — same-origin to the
   subdomain.

App-author-visible API surface (`portal.user.current()`, `portal.pdf.*`,
`portal.email.*`, `portal.storage.*`) is unchanged. Existing apps don't
need code changes.

**`X-Portal-App` header:** removed from the **SDK's** outgoing requests (the
subdomain `Host` carries the slug). **The server still reads it for
bearer-token clients** — they have no app subdomain, so the header remains
their slug source. So "server stops reading it" is true only for browser
(cookie / `app_session`) auth; under token auth `portal/api.py` keys off
`request.state.auth_method == "token"` and uses `X-Portal-App`. The header is
narrowed to token clients, not removed.

## Iframe wrapper page

New template: `portal/templates/app_launcher.html`. Served at
`portal.example.com/apps/<slug>/`. Contents:

```html
{% extends "base.html" %}
{% block title %}{{ app.name }} — Portal{% endblock %}
{% block content %}
<div class="app-frame">
  <header class="app-frame-bar">
    <a href="/" class="nav-link">← Apps</a>
    <strong>{{ app.name }}</strong>
    <span class="muted">{{ app.version }}</span>
  </header>
  <iframe
    src="{{ iframe_src }}"
    sandbox="allow-scripts allow-forms allow-popups allow-same-origin allow-modals allow-downloads"
    allow="clipboard-read; clipboard-write; fullscreen"
  ></iframe>
</div>
{% endblock %}
```

Styling: iframe fills the viewport below the bar. The bar shows the app
name + a back link to the portal dashboard. The portal's overall PWA
chrome (topbar with sign-out) is replaced by this slimmer bar while a
child app is open.

**Sandbox attributes:**
- `allow-same-origin` — required so the iframe's cookies/storage are
  scoped to its subdomain rather than treated as null origin
- `allow-scripts` — apps need JS
- `allow-forms` — submitting forms
- `allow-popups` — for PDF downloads opening in a new tab if the app uses that pattern
- `allow-modals` — `confirm()` / `alert()` for app UX
- `allow-downloads` — PDF download
- **Deliberately NOT included:** `allow-top-navigation`, `allow-pointer-lock`,
  `allow-popups-to-escape-sandbox`

## Backward-compat / fallback path

Self-hosters who don't want to set up wildcard DNS can run with
`CHILD_APPS_SAME_ORIGIN=true` in `.env`. The portal then serves child apps
at `/apps/<slug>/` directly, same as today.

The admin UI shows a banner on the dashboard:

> ⚠️ Child apps are running same-origin with the portal. A malicious
> uploaded app can read the portal's session. Set up wildcard DNS at
> `*.apps.<SITE_URL>` and remove `CHILD_APPS_SAME_ORIGIN` from `.env` to
> enable per-app origin isolation.

This is the right default-secure-but-opt-out-honestly story for users
who can't or won't set up DNS.

## Localhost dev

Set `SITE_URL=lvh.me` in `.env` (lvh.me resolves `*.lvh.me` to `127.0.0.1`).
Then:
- Portal: `http://lvh.me/`
- Apps: `http://hello-receipt.apps.lvh.me/`

Caddy serves both over HTTP locally (no certs needed for lvh.me). Document
the dev workflow in `docs/app-authoring.md`.

For pure-localhost development without lvh.me, fall back to
`CHILD_APPS_SAME_ORIGIN=true`. Document both paths.

## Deployment changes

### DNS

Self-hosters add a single wildcard A record at their DNS provider:

```
*.apps.example.com.    A    <VPS IP>
```

(or CNAME pointing at the existing portal hostname). One record; same VPS.

`docs/deploying.md` gets a new step between "Configure" and "Boot it":
"Set up child-app DNS."

### `.env`

New variable:

```
# When unset (recommended for production), child apps are served from
# <slug>.apps.<SITE_URL> on their own origin for security isolation.
# Requires a wildcard DNS A record (*.apps.<SITE_URL>) pointing at this VPS.
#
# Set to true if you can't or don't want to configure wildcard DNS. Child
# apps will be served same-origin with the portal at /apps/<slug>/ — much
# less safe. The portal will display a banner warning.
CHILD_APPS_SAME_ORIGIN=
```

### Docker compose

No change. Caddy already in the stack picks up the wildcard block on
config reload.

## Migration path for existing self-hosters

The first deploy of this feature is non-breaking by default if they don't
opt in:

1. Pull + restart with new code. Default behavior: `CHILD_APPS_SAME_ORIGIN`
   is unset → portal tries the new model. Caddy attempts to serve
   `*.apps.<SITE_URL>` but the user hasn't set up DNS yet → child apps fail.

To avoid this, the **initial release should default to legacy mode** —
treat `CHILD_APPS_SAME_ORIGIN` unset as "true" for one release, then flip
the default in a follow-up release after self-hosters have had time to
configure DNS.

Concretely:
- Release v0.2: ships the new infra; default = same-origin (no behavior change for existing deploys); admins see a banner suggesting they upgrade
- Release v0.3 (some weeks later): default flips to isolated origin; banner now says "if you haven't set up DNS, set `CHILD_APPS_SAME_ORIGIN=true`"

## Rollout phases / implementation milestones

This is too much for one PR. Suggested phasing:

### Phase A — backend skeleton (data model + endpoints, no Caddy)

1. `AppLaunchToken` + `AppSession` tables (Alembic revision)
2. `HostDispatchMiddleware` that sets `request.state.app_slug` from Host
3. `POST /api/v1/session/exchange` endpoint
4. `GET /apps/<slug>/launch` (mint token, redirect)
5. App-subdomain serve path for `/`, `/portal-sdk.js`, `/<path>` static files
6. Logout / password-change cascade revocation of AppSessions
7. Tests against `lvh.me`

Deliverable: works on localhost via `lvh.me`. No Caddy changes yet.

### Phase B — Caddy wildcard + on-demand TLS

1. Caddyfile additions
2. `/internal/cert-ask` endpoint (auth via internal-network-only)
3. on_demand_tls config
4. Test on a real VPS with a real domain

Deliverable: production deploy works end-to-end with one wildcard DNS record.

### Phase C — Iframe wrapper + UI

1. `templates/app_launcher.html`
2. Update `GET /apps/<slug>/` to render the wrapper (gated on
   `CHILD_APPS_SAME_ORIGIN`)
3. Admin-dashboard banner when same-origin mode is active
4. Tile click flow (no UI surface change for end users)

### Phase D — SDK + cleanup

1. SDK detects subdomain context, runs the launch-token exchange
2. Drop `X-Portal-App` header from SDK; keep server-side acceptance for
   one release for safety
3. Update SKILL.md + app-authoring docs (child apps run on a different
   origin now; mostly no app-author-visible change)
4. Update `docs/deploying.md` with the DNS step

### Phase E — Default flip

1. Default `CHILD_APPS_SAME_ORIGIN` to unset/False
2. Release notes call out the change loudly

## Open issues to discover during implementation

- **Service workers in child apps:** apps may want their own SW. With
  per-app subdomain, each app's SW is naturally scoped. But the iframe
  sandbox + same-origin rules may interact oddly. Test early.
- **iOS PWA install of the *portal*:** does the iframe loading
  `<slug>.apps.<SITE_URL>` break the standalone PWA mode? Should test on
  a real iOS device. Hypothesis: iframes within the PWA stay inside;
  only navigations to other origins bounce to Safari. Per Apple's
  behavior, this should work.
- **Cookies in iframe:** Safari has historically had cross-site cookie
  restrictions (ITP). An iframe at `<slug>.apps.<SITE_URL>` embedded in a
  page at `portal.<SITE_URL>` is technically third-party cookies. Will
  Safari block the AppSession cookie? Needs testing. Mitigation if so:
  Storage Access API, or `Sec-Fetch-Site: same-site` handling. Both are
  the same registrable domain (eTLD+1 = `example.com`) so Safari should
  treat them as same-site, not cross-site.
- **CSRF token endpoint:** today's `/api/v1/csrf-token` works for cookie
  auth. With per-app sessions, each subdomain has its own session and
  thus its own CSRF token. Endpoint needs to look at host + AppSession.
  - **Resolved (shipped):** `/api/v1/csrf-token` (`portal/api.py`) serves a
    token for both `cookie` and `app_session` auth — each origin has its own
    independent session and thus its own per-origin CSRF token. The SDK
    fetches it same-origin from the subdomain.
- **Rate limits:** existing per-(IP, email) login rate limit only sees
  the portal-origin login. App-origin sessions are minted via launch
  token, not login. So this is fine, but worth re-confirming during
  implementation that no rate-limited path is bypassed.

## What this design does NOT change

- Bearer-token API auth (Claude skill upload flow)
- The PWA manifest / service worker on the portal origin
- The admin UI (apps list, users, settings, tokens)
- The Alembic migration story (just adds two new tables)
- The session-rotation-on-login fix from the previous round
- The WeasyPrint SSRF / SMTP encryption / CSRF token systems

It's purely about where the child apps' HTML lives and how the portal
authenticates calls from them.
