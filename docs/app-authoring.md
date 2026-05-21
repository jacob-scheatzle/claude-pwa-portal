# Building apps for the portal

There are two paths for building a child app:

1. **The Claude skill** — for non-developers. Describe what you want; Claude scaffolds, implements, packages, and uploads.
2. **Manually** — for developers who want full control.

This guide focuses on the manual path. For the Claude path, see [`claude-skill/pwa-portal-app/SKILL.md`](../claude-skill/pwa-portal-app/SKILL.md).

## What an app is

An app is a folder of HTML/CSS/JS plus a `portal.json` manifest, zipped and uploaded. The portal extracts it to `data/apps/<slug>/` and serves it at `/apps/<slug>/`.

Apps run inside the portal's origin, so the JavaScript SDK at `/portal-sdk.js` works automatically — no auth wiring needed.

## Minimum app

```
my-app/
├── portal.json
└── index.html
```

`portal.json`:

```json
{
  "slug": "my-app",
  "name": "My App",
  "version": "0.1.0",
  "entry": "index.html"
}
```

`index.html`:

```html
<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>My App</title>
  <script src="/portal-sdk.js"></script>
</head><body>
  <h1>Hello</h1>
</body></html>
```

That's a valid app. Zip the folder so `portal.json` is at the root, upload it, and it's live at `/apps/my-app/`.

## `portal.json` schema

| Field | Required | Notes |
|---|---|---|
| `slug` | yes | kebab-case, 2–40 chars, `a-z 0-9 -` only, no leading/trailing hyphen |
| `name` | yes | 1–60 chars |
| `version` | yes | freeform string, 1–20 chars (semver recommended) |
| `description` | no | up to 200 chars; shown on the admin app list |
| `icon` | no | relative path inside the bundle; 192×192 PNG recommended |
| `entry` | no | default `index.html`; must exist in the zip |
| `services` | no | informational list of services your app uses; valid: `pdf`, `email`, `storage` |
| `min_portal_version` | no | hint for future compatibility checks |

The slug is the URL: an app with slug `expense-tracker` lives at `/apps/expense-tracker/`.

## Using the SDK

Include the SDK in your HTML head:

```html
<script src="/portal-sdk.js"></script>
```

It exposes `window.portal`. All methods are async.

### Current user

```js
const me = await portal.user.current();
// { id, email, role }
```

### PDF generation (server-rendered via WeasyPrint)

```js
// Trigger a browser download
await portal.pdf.download({
  html: "<h1>Receipt</h1><p>Total: $42</p>",
  filename: "receipt.pdf",
});

// Or get a Blob to attach/upload/render yourself
const blob = await portal.pdf.render({ html: "...", filename: "..." });
```

Inline CSS works in the HTML you pass. External assets loaded from the portal origin will work; arbitrary internet resources may be blocked.

### Email

```js
await portal.email.send({
  to: "customer@example.com",   // string or array of strings
  subject: "Your receipt",
  html: "<p>Hi!</p>",
  text: "Hi!",                  // include at least one of html/text
});
```

Returns `{ status: "sent", count: N }` on success. Throws if SMTP isn't configured on the portal (the error has `.status === 503`).

### Per-user, per-app storage

```js
await portal.storage.put("notes/today.json", { entries: ["a"] });   // JSON
await portal.storage.put("attachment.pdf", pdfBlob);                  // Blob

const notes = await portal.storage.get("notes/today.json");           // parsed JSON
const blob = await portal.storage.get("attachment.pdf");              // Blob

await portal.storage.list();
// { items: [{ key, size }], usage: <bytes>, limit: 104857600 }

await portal.storage.delete("notes/today.json");
```

**Key syntax:** `A-Z a-z 0-9 . _ -` and `/` (slash separates folders). No leading slash, no `..`, no empty segments.

**Limits:** 10 MB per object, 100 MB per `(app, user)` namespace.

**Scope:** each `(app_slug, user_id)` pair is its own namespace. Alice's data in `expense-tracker` is invisible to Bob in `expense-tracker` and to Alice in `quote-calc`.

## Packaging and uploading

The Claude skill bundles the tooling, but you can run the scripts directly without the skill:

```bash
# Package (writes my-app-0.1.0.zip next to the source dir)
python3 claude-skill/pwa-portal-app/scripts/package.py path/to/my-app

# Upload
PORTAL_URL=https://portal.example.com \
PORTAL_TOKEN=<your token from /admin/tokens> \
  python3 claude-skill/pwa-portal-app/scripts/upload.py path/to/my-app-0.1.0.zip
```

Both scripts are stdlib-only Python and run anywhere Python 3.11+ is installed.

Alternatively, upload via the web UI at **Apps → Upload**.

## Conventions

- **HTML/CSS/JS only.** No build step, no npm. The zip ships and runs as-is.
- **One file is fine.** Inline `<style>` and `<script>` in `index.html` until the app actually outgrows it.
- **Load the SDK first**, then your own scripts.
- **No service workers in child apps.** The portal's SW covers the origin.
- **Persist with `portal.storage`**, not `localStorage` — storage survives device changes; `localStorage` doesn't.
- **Avoid external CSS frameworks** unless the app genuinely needs them.
- **Validate input** before calling APIs (especially emails and amounts).
- **Don't store secrets in the bundle.** Anything in the zip is visible to any signed-in user.

## A worked example

See [`examples/hello-receipt/`](../examples/hello-receipt/) — a single-file PWA that uses every SDK service:

- Loads the current user
- Generates a styled PDF receipt with WeasyPrint
- Emails the receipt to the customer
- Stores receipts in per-user storage so you can re-download or delete past ones

## Updating an app

The portal currently rejects uploads to an existing slug. To ship an update:

1. Delete the existing app from **Apps** in the admin UI.
2. Bump `version` in `portal.json`.
3. Package and upload as usual.

**Per-user storage is preserved** through delete-and-reupload because storage is keyed by slug, not by the app's database row id.

A non-destructive in-place update flow is on the roadmap.

## Constraints

- Bundle: 50 MB compressed, 100 MB uncompressed, 1,000 files max
- No symlinks, no `..`, no absolute paths in the zip
- Storage: 10 MB per object, 100 MB per `(app, user)` namespace
- Service worker scope cannot exceed `/apps/<slug>/`
