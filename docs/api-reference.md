# Portal HTTP API

The portal exposes a JSON HTTP API under `/api/v1/`. It's consumed both by:

- **Child apps via the SDK** (`window.portal.*`) using same-origin session cookies
- **External automation** (e.g. the Claude skill) using Bearer tokens

The same endpoints accept either auth method.

> **Managing apps from Claude?** The portal can also expose an opt-in **MCP
> server** at `/mcp` (admin-token authed) so Claude lists / uploads / replaces /
> enables apps as tool calls. It wraps the same app operations described here.
> See [mcp.md](mcp.md).

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
**one app**, so the portal needs to know which app is making the call.
There are two resolution paths, applied in this order:

1. **Per-app subdomain (default).** Requests from `<slug>.apps.<SITE_URL>`
   carry the slug in the `Host` header; the portal extracts it via
   middleware. The SDK uses this transparently when it's loaded inside a
   child-app iframe.
2. **`X-Portal-App: <slug>` header.** Required when calling from outside
   the app's subdomain — e.g. bearer-token automation hitting the portal
   directly, or same-origin mode (`CHILD_APPS_SAME_ORIGIN=true`).

The SDK in subdomain mode does NOT send `X-Portal-App` — the Host header
already carries it. If you're scripting against `/api/v1/*` directly,
either target the subdomain or send the header.

Beyond the resolution: the app must declare each service it calls in its
`portal.json`'s `services` array. Calls to a service the app didn't
declare (or that an admin has revoked) return `403 service not enabled
for this app`.

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

Returns the stored object. Content-Type is the type the object was PUT with.

**Header**: `X-Portal-App: <slug>`

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

- `ttl_seconds` — link expiry. Default 7 days, max 30 days.
- `max_views` — view cap. `0` (or omitted) means unlimited within TTL.
- `filename` — optional, up to 80 chars; shown as the download filename.

**Response**:

```json
{ "url": "https://portal.example.com/s/9XqA…", "expires_at": "2026-06-03T12:00:00Z" }
```

The `/s/<token>` URL serves with `Content-Disposition: attachment` and
no portal cookie — links can be safely shared. View counts are atomic
against `max_views`; concurrent hits past the cap get 404.

### `GET /api/v1/share/list`

Lists active shares the calling user has created for this app.

### `POST /api/v1/share/{token}/revoke`

Immediately invalidates a share. Subsequent `/s/<token>` hits return 404.

**Failures**:

- `403 service not enabled for this app` — `services` array missing `share`.
- `404 storage key not found` — `kind=storage` references a missing object.

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
| `portal.share.create({ kind, key?, html?, filename?, ttl_seconds?, max_views? })` | `{ url, expires_at }` |
| `portal.share.list()` | `{ items: [...] }` |
| `portal.share.revoke(token)` | `{ revoked }` |
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
