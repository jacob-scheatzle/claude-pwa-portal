# Building apps for the portal

There are two paths for building a child app:

1. **The Claude skill** — for non-developers. Describe what you want; Claude scaffolds, implements, packages, and uploads.
2. **Manually** — for developers who want full control.

This guide focuses on the manual path. For the Claude path, see [`claude-skill/pwa-portal-app/SKILL.md`](../claude-skill/pwa-portal-app/SKILL.md).

## What an app is

An app is a folder of HTML/CSS/JS plus a `portal.json` manifest, zipped and uploaded. The portal extracts it to `data/apps/<slug>/` and serves it.

### Where the app runs

By default, each app runs on its **own subdomain**: `<slug>.apps.<SITE_URL>`,
loaded inside an iframe wrapper on the portal. This puts each app on a
distinct browser origin so the same-origin policy isolates apps from each
other and from the portal shell.

This is transparent to your code. The SDK at `/portal-sdk.js` is served
same-origin from the subdomain and handles the cross-origin handshake
(a single-use launch token in the URL fragment is exchanged for an
HttpOnly `app_session` cookie) automatically on page load. Just include
`<script src="/portal-sdk.js"></script>` like before — every SDK method
keeps working.

A legacy same-origin mode is available via `CHILD_APPS_SAME_ORIGIN=true`
for self-hosters who can't configure wildcard DNS, in which case the app
runs at `<SITE_URL>/apps/<slug>/`. The SDK and your app's code don't
change between the two modes.

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
| `services` | no | **server-enforced** allowlist of SDK services this app may call. Valid: `pdf`, `email`, `storage`. Auto-approved on first upload; an admin can revoke any of them later from the apps admin page, and a revoked service returns 403 to the SDK at runtime. An empty list (or omitting the field) is treated as a legacy ungated app — all services callable. New apps should declare exactly what they use. |
| `permissions.network` | no | array of external `connect-src` origins your app may fetch from (e.g. `["https://api.stripe.com"]`). Auto-approved on first upload; revocable per-origin. Anything not listed is blocked by the per-app Content-Security-Policy. |
| `permissions.csp_strict` | no | if `true`, the portal serves the app under a strict CSP (no inline scripts/styles, no `eval`). Inline `<script>` tags must carry `nonce="{{NONCE}}"` — the portal substitutes the placeholder per response. |
| `min_portal_version` | no | hint for future compatibility checks |
| `tools` | no | declarative operations an MCP-connected Claude can run (render a PDF → share / email / store). See "App tools" below and [mcp.md](mcp.md). |
| `forms` | no | public, no-sign-in intake forms served on the app's own origin (`<slug>.apps.<SITE_URL>/forms/<form>`). Submissions collect under **Admin → Submissions** (with CSV export) and in the data export. See "Public intake forms" below. |

The slug is the URL: an app with slug `expense-tracker` lives at `/apps/expense-tracker/`.

## App tools (run by Claude over MCP)

If the portal runs its MCP server ([mcp.md](mcp.md)), an app can declare `tools`
an MCP-connected Claude calls directly — each appears as `<slug>__<tool>`. A
tool is **declarative**: the portal renders an HTML template you supply to a PDF
and then shares / downloads / emails / stores it. Your app's code never runs
server-side.

```json
"tools": [
  {
    "name": "create_quote",
    "description": "Render a quote PDF and return a share link.",
    "params": [
      {"name": "customer", "type": "string", "required": true},
      {"name": "amount", "type": "number", "required": true}
    ],
    "render": {"html": "<h1>Quote for {{ customer }}</h1><p>${{ amount }}</p>", "branded": true},
    "deliver": {"kind": "share", "ttl_days": 30}
  }
]
```

`deliver.kind` is `share` | `download` | `store` (needs a templated `key`) |
`email` (needs `to`, optional `subject`); `{{ param }}` placeholders work in the
template and in those fields. A tool may only use services the manifest also
declares in `services` — `share`/`download` need `pdf`, `store` needs `pdf` +
`storage`, `email` needs `email`. Full reference: [mcp.md](mcp.md).

## Public intake forms

Declare `forms` to collect input from people who **aren't signed in** — a
customer requesting a quote, someone booking a job. Each form is served at a
public URL on the app's own origin (`<slug>.apps.<SITE_URL>/forms/<form>`, or
`<SITE_URL>/forms/<slug>/<form>` in same-origin mode) you can share or link from a website;
submissions collect under **Admin → Submissions** (with CSV export) and in the
data export. Forms are declarative — no app code runs server-side.

```json
"forms": [
  {
    "name": "quote_request",
    "title": "Request a quote",
    "description": "Tell us about your project and we'll get back to you.",
    "fields": [
      {"name": "full_name", "label": "Your name", "type": "text", "required": true},
      {"name": "email", "label": "Email", "type": "email", "required": true},
      {"name": "phone", "label": "Phone", "type": "tel"},
      {"name": "details", "label": "Project details", "type": "textarea"}
    ],
    "notify_email": "owner@example.com",
    "success_message": "Thanks — we'll be in touch soon."
  }
]
```

- Field `type` is `text` | `email` | `tel` | `number` | `textarea`.
- `notify_email` (optional) sends a plain-text alert on each submission (needs
  SMTP configured); the submission is stored regardless.
- Forms need **no** `services`. The public endpoint is rate-limited per IP, has
  a spam honeypot, and records only the fields you declared.

## Scheduled runs

Any tool can run on a recurring schedule — a daily summary, a weekly timesheet,
a monthly report — with its output delivered through the tool's own
`deliver` action. Schedules aren't part of the manifest; an admin creates them
at **Admin → Schedules** (or Claude does, over MCP) after the app is uploaded.
See [mcp.md](mcp.md).

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

The Claude skill bundles the packaging + upload scripts. Install it once
on your workstation (this writes the scripts to `~/.claude/skills/pwa-portal-app/scripts/`):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/claude-skill/pwa-portal-app/install.sh)
```

Then package + upload:

```bash
# Package (writes my-app-0.1.0.zip next to the source dir)
python3 ~/.claude/skills/pwa-portal-app/scripts/package.py path/to/my-app

# Upload
PORTAL_URL=https://portal.example.com \
PORTAL_TOKEN=<your token from /admin/tokens> \
  python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py path/to/my-app-0.1.0.zip

# Updating an existing app in place — preserves per-user storage:
PORTAL_URL=... PORTAL_TOKEN=... \
  python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py path/to/my-app-0.2.0.zip --replace
```

Both scripts are stdlib-only Python and run anywhere Python 3.11+ is
installed. Working from a cloned repo? Substitute
`claude-skill/pwa-portal-app/scripts/` for `~/.claude/skills/pwa-portal-app/scripts/`.

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

See [`examples/`](../examples/) for a gallery of eight reference apps —
work order, mileage log, time tracker, expense logger, customer directory,
quote builder, invoice generator, and a minimal [`hello-receipt`](../examples/hello-receipt/)
that exercises every SDK service in one file:

- Loads the current user
- Generates a styled PDF receipt with WeasyPrint
- Emails the receipt to the customer
- Stores receipts in per-user storage so you can re-download or delete past ones

If you're browsing from a deployed portal without a repo checkout, the
examples are at
<https://github.com/jacob-scheatzle/claude-pwa-portal/tree/main/examples>.

## Updating an app

Bump `version` in `portal.json`, repackage, and upload with `--replace`:

```bash
PORTAL_URL=... PORTAL_TOKEN=... \
  python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py my-app-0.2.0.zip --replace
```

The replace flow swaps the on-disk bundle atomically. The manifest
`slug` must match the existing app. **Per-user storage is preserved**
because storage is keyed by slug, not by the app row id. Admin
service/origin approvals are also preserved.

If you'd rather wipe the slate, delete the app from **Apps** in the
admin UI and upload as a fresh install — that drops user storage too.

## Constraints

- Bundle: 50 MB compressed, 100 MB uncompressed, 1,000 files max
- No symlinks, no `..`, no absolute paths in the zip
- Storage: 10 MB per object, 100 MB per `(app, user)` namespace
- Service worker scope cannot exceed `/apps/<slug>/`
