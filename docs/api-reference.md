# Portal HTTP API

The portal exposes a JSON HTTP API under `/api/v1/`. It's consumed both by:

- **Child apps via the SDK** (`window.portal.*`) using same-origin session cookies
- **External automation** (e.g. the Claude skill) using Bearer tokens

The same endpoints accept either auth method.

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

## App context header

Storage endpoints require an `X-Portal-App: <slug>` header so the portal can scope the namespace. The SDK auto-fills this from `window.location.pathname` (`/apps/<slug>/...`); if you're calling from outside an app, supply it yourself.

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
{ "html": "<h1>...</h1>", "filename": "out.pdf" }
```

**Response**: `application/pdf` body with `Content-Disposition: attachment; filename="out.pdf"`.

**Failures**:

- `503 PDF service unavailable: WeasyPrint not installed`
- `500 PDF render failed: <detail>` (malformed HTML, unreachable external assets)

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

## JS SDK reference

Included automatically in child apps:

```html
<script src="/portal-sdk.js"></script>
```

The script attaches a single global, `window.portal`, exposing:

| Method | Returns |
|---|---|
| `portal.user.current()` | `{ id, email, role }` |
| `portal.pdf.render({ html, filename })` | `Blob` (application/pdf) |
| `portal.pdf.download({ html, filename })` | `void` (triggers browser download) |
| `portal.email.send({ to, subject, html, text })` | `{ status, count }` |
| `portal.storage.put(key, value)` | `{ key, size, content_type }` |
| `portal.storage.get(key)` | `Blob`, `string`, or parsed JSON (auto by Content-Type) |
| `portal.storage.list()` | `{ items, usage, limit }` |
| `portal.storage.delete(key)` | `{ deleted }` |
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
