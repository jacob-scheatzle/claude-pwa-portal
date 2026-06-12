# Portal HTTP API

The portal exposes a JSON HTTP API under `/api/v1/`. It's consumed both by:

- **Child apps via the SDK** (`window.portal.*`) using same-origin session cookies
- **External automation** (e.g. the Claude skill) using Bearer tokens

The same endpoints accept either auth method.

> **Managing apps from Claude?** The portal also ships a built-in **MCP
> server** at `/mcp` (admin-token authed) so Claude lists / uploads / replaces /
> enables apps — and runs each app's declared tools — as tool calls. It wraps
> the same app operations described here. See [mcp.md](mcp.md).

## Authentication

### Session cookie

Users sign in at `POST /login` and the portal sets a signed session cookie (`SameSite=Lax`, `HttpOnly`, `Secure` when `COOKIES_SECURE=true`). Same-origin requests — including from child apps at `/apps/<slug>/...` — include it automatically.

### Bearer token

Admins create tokens at **Tokens** in the admin UI. The raw token is shown once at creation; only its SHA-256 hash is persisted. Tokens act as the user who created them.

Send as:

```
Authorization: Bearer <token>
```

Revoke at any time from the same UI.

### Failure modes

- Unauthenticated: `401 {"detail": "Sign in required"}`
- Authenticated but role doesn't permit: `403 {"detail": "Admin role required"}`

## App context

Every service endpoint (`pdf`, `email`, `storage`, `share`) is scoped to
**one app**, so the portal needs to know which app is making the call. How the
slug is resolved depends on how the request is authenticated
(`request.state.auth_method`):

- **Cookie auth (a signed-in user in the portal UI / a child app).** The slug
  is derived from context, **not** from `X-Portal-App`: the `Host` header on a
  per-app subdomain (`<slug>.apps.<SITE_URL>`, the default), or the
  `/apps/<slug>/` path of the request's `Referer`/`Origin` in same-origin mode
  (`CHILD_APPS_SAME_ORIGIN=true`). The bundled SDK relies on this and does
  **not** send `X-Portal-App`; any `X-Portal-App` a cookie client sends is
  ignored (this is a deliberate anti-spoofing measure).
- **Bearer-token auth (automation hitting the portal directly).** Token
  clients have no app context, so they **must** send `X-Portal-App: <slug>` to
  name the app. This is the only branch where the header is honored.

So: if you're scripting against `/api/v1/*` with an API token, send
`X-Portal-App`. If you're calling as a logged-in user, target the app's
subdomain (or `/apps/<slug>/` page) instead.

Beyond the resolution: the app must declare each service it calls in its
`portal.json`'s `services` array. Calls to a service the app didn't
declare (or that an admin has revoked) return `403` with a message like
`App '<slug>' is not authorized to use the '<service>' service. Ask an
admin to enable it under /admin/apps.`

## Endpoints

### `GET /api/v1/user/me`

Returns the current user.

```json
{ "id": 3, "email": "owner@example.com", "role": "admin" }
```

### `POST /api/v1/pdf/render`

Renders HTML to a PDF via WeasyPrint.

**Body**:

```json
{
  "html": "<h1>...</h1>",
  "filename": "out.pdf",
  "branded": false
}
```

- `html` — required, up to 2 MiB.
- `filename` — suggested attachment filename; sanitized server-side.
- `branded` — optional. When `true`, the portal prepends a branding
  header (business name, logo, accent border) configured under
  **Admin → Settings → Branding** before WeasyPrint runs.

**Response**: `application/pdf` body with `Content-Disposition: attachment; filename="out.pdf"`.

**Failures**:

- `400 Invalid html length` if `html` exceeds the 2 MiB cap.
- `429 PDF render rate limit exceeded` — 120 renders/hour/user/process.
- `503 PDF service unavailable` — WeasyPrint missing or its system libs (Pango/Cairo) failed to load.
- `500 PDF render failed` — generic. Detail is logged server-side.

**SSRF protection**: WeasyPrint is configured to refuse any URL scheme
other than `data:`. Embed images as base64 `data:` URIs.

### `POST /api/v1/email/send`

Sends email through the portal's configured SMTP.

**Body**:

```json
{
  "to": "a@example.com",
  "subject": "Hi",
  "html": "<p>Hi</p>",
  "text": "Hi"
}
```

`to` can be a single email or a list. Include at least one of `html` or `text`. `subject` max 200 chars.

**Response**:

```json
{ "status": "sent", "count": 1 }
```

**Failures**:

- `503 Email service unavailable: SMTP not configured`
- `400 Provide at least one of \`text\` or \`html\``
- `502 Email send failed: <smtp exception>`

### `GET /api/v1/storage`

Lists keys in the `(app_slug, user_id)` namespace.

**Header**: `X-Portal-App: <slug>`

**Response**:

```json
{
  "items": [{ "key": "notes/today.json", "size": 217 }],
  "usage": 217,
  "limit": 104857600
}
```

### `GET /api/v1/storage/{key:path}`

Returns the stored object with `Content-Disposition: attachment`. The
**Content-Type is inferred from the key's file extension** (`mimetypes`
lookup), **not** from the type the object was PUT with. Keys with no
recognizable extension come back as `application/octet-stream` — through the
SDK, `portal.storage.get()` then yields a `Blob` rather than a string or
parsed JSON. If you need a specific type back, give the key a matching suffix
(e.g. `notes/today.json`).

**Header**: `X-Portal-App: <slug>` (bearer-token auth only — see above)

### `PUT /api/v1/storage/{key:path}`

Stores an object. The raw request body is the value; `Content-Type` is preserved for subsequent GETs.

**Header**: `X-Portal-App: <slug>`

**Response**:

```json
{ "key": "notes/today.json", "size": 217, "content_type": "application/json" }
```

**Failures**:

- `400 key may only contain A-Z a-z 0-9 . _ - and /`
- `400 key may not contain empty, '.', or '..' segments`
- `413 object exceeds 10MB limit`
- `507 storage namespace exceeds 100MB limit`

### `DELETE /api/v1/storage/{key:path}`

Removes the object.

**Header**: `X-Portal-App: <slug>`

**Response**:

```json
{ "deleted": "notes/today.json" }
```

### `POST /api/v1/apps/upload`

Admin-only programmatic app upload. Used by the Claude skill's `upload.py`.

**Body**: `multipart/form-data` with a `bundle` field containing the `.zip`.

**Response**:

```json
{ "slug": "hello-receipt", "name": "Hello Receipt", "version": "0.1.0" }
```

**Failures**:

- `400 <validation error>` — invalid manifest, slug already exists, path traversal in zip, etc.
- `403 Admin role required`

### `PUT /api/v1/apps/{slug}`

Admin-only in-place **replace** of an existing app. Used by the Claude skill's
`upload.py --replace`. The uploaded bundle's manifest slug must match `{slug}`
in the path (a mismatch is rejected before anything is overwritten), and the
app's **per-user storage is preserved** across the replace.

**Body**: `multipart/form-data` with a `bundle` field containing the `.zip`.

**Response**:

```json
{ "slug": "hello-receipt", "name": "Hello Receipt", "version": "0.2.0", "replaced": true }
```

**Failures**:

- `400 <validation error>` — invalid manifest, manifest slug ≠ path slug, path traversal in zip, etc.
- `401 Sign in required`
- `403 Admin role required`
- `404 App '<slug>' not found` — no existing app with that slug to replace.

### `POST /api/v1/share/create`

Mint a public, capability-style URL that anyone with the link can hit
without signing in. Two kinds: `storage` shares an object already in
storage; `pdf` renders fresh HTML server-side and stores the result.

**Body** (storage variant):

```json
{
  "kind": "storage",
  "key": "receipts/2025-04.pdf",
  "filename": "April Receipt.pdf",
  "ttl_seconds": 604800,
  "max_views": 5
}
```

**Body** (pdf variant):

```json
{
  "kind": "pdf",
  "html": "<h1>Hello</h1>",
  "filename": "hello.pdf",
  "ttl_seconds": 86400,
  "max_views": 0
}
```

- `ttl_seconds` — link expiry. Default 7 days, max 90 days.
- `max_views` — view cap. `0` (or omitted) means unlimited within TTL. Any
  positive value is **clamped to 1000** (the effective maximum) before it's
  stored, so the `max_views` echoed back in the response may be lower than
  what you requested.
- `filename` — optional, up to 80 chars; shown as the download filename.

**Response**:

```json
{
  "token": "9XqA…",
  "url": "https://portal.example.com/s/9XqA…",
  "expires_at": "2026-06-03T12:00:00Z",
  "kind": "pdf",
  "max_views": 0
}
```

The `/s/<token>` URL serves with `Content-Disposition: attachment` and
no portal cookie — links can be safely shared. View counts are atomic
against `max_views`; concurrent hits past the cap get 404.

Active shares are listed and revoked by an admin from the **Admin → Shares**
page (`/admin/shares`) — there is no per-token `/api/v1` revoke endpoint.

**Failures**:

- `400 kind must be 'storage' or 'pdf'` — unknown `kind`.
- `400 storage shares require a 'key'` — `kind=storage` without `key`.
- `400 pdf shares require 'html'` — `kind=pdf` without `html`.
- `403` — the app isn't authorized for the service this share kind needs
  (`storage` for `kind=storage`, `pdf` for `kind=pdf`).
- `404 key not found` — `kind=storage` references an object that isn't in the creator's namespace.
- `500` — `kind=pdf` render failed (WeasyPrint error).

## JS SDK reference

Included automatically in child apps:

```html
<script src="/portal-sdk.js"></script>
```

The script attaches a single global, `window.portal`, exposing:

| Method | Returns |
|---|---|
| `portal.user.current()` | `{ id, email, role }` |
| `portal.pdf.render({ html, filename, branded })` | `Blob` (application/pdf) |
| `portal.pdf.download({ html, filename, branded })` | `void` (triggers browser download) |
| `portal.email.send({ to, subject, html, text })` | `{ status, count }` |
| `portal.storage.put(key, value)` | `{ key, size, content_type }` |
| `portal.storage.get(key)` | `Blob`, `string`, or parsed JSON (auto by Content-Type) |
| `portal.storage.list()` | `{ items, usage, limit }` |
| `portal.storage.delete(key)` | `{ deleted }` |
| `portal.share.create({ kind, key?, html?, filename?, ttl_seconds?, max_views? })` | `{ token, url, expires_at, kind, max_views }` |
| `portal.appSlug` | string or `null` — auto-detected from URL |

All methods are async (return Promises). On HTTP failure they throw an `Error` with `.status` (HTTP code) and `.detail` (server-provided message) populated.

**`storage.put` value handling:**

- `Blob` → sent as-is with the Blob's `Content-Type`
- `string` → sent as `text/plain; charset=utf-8`
- anything else → JSON-stringified, sent as `application/json`

**`storage.get` return handling:**

- `Content-Type` starts with `application/json` → returns parsed JSON
- `Content-Type` starts with `text/` → returns string
- otherwise → returns Blob

## Limits

| Resource | Limit |
|---|---|
| App bundle upload (zip) | 50 MB compressed |
| App bundle uncompressed | 100 MB |
| Files per app bundle | 1,000 |
| Storage object | 10 MB |
| Storage namespace | 100 MB per `(app, user)` |
| Email subject | 200 chars |
| Session cookie lifetime | 14 days (`SESSION_MAX_AGE`) |

## CORS

The API is same-origin only. Cross-origin requests will be blocked at the browser level for cookie-based auth; for Bearer-token use cases, you can call from any origin (no `Access-Control-Allow-Origin` header is sent, so browsers will reject — server-to-server only).
