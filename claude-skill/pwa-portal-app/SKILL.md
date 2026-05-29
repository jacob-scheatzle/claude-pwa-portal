---
name: pwa-portal-app
description: Build, package, and upload a small PWA app for a self-hosted PWA Portal. Use when the user wants to create or modify a tool/app for their portal — e.g. "make me a receipt app for my portal", "build a quote calculator", "I need a contact form app". Handles scaffolding, implementation, packaging, and upload via the portal API.
---

# PWA Portal App Builder

This skill builds child PWAs ("apps") that live inside a self-hosted **PWA Portal**. The portal hosts multiple small-business apps under one origin; each app is a folder of HTML/CSS/JS plus a `portal.json` manifest, packaged as a `.zip` and uploaded.

The portal provides shared services that apps call from JavaScript via a built-in SDK: **PDF generation, email, per-user/per-app storage, and user info**.

## When to use this skill

Use when the user asks you to:
- Create a new app for their portal ("make me a [X] app for my portal", "build a quote tool", "I need a contact form")
- Modify or extend an app you previously built (re-package and re-upload)
- Package or upload an existing app folder

**Don't use** if the user is asking about the portal itself (admin UI, settings, auth) — that's the host application, not a child app you're building.

## First-time setup: connect to a portal

You upload finished apps to a running **PWA Portal**. There are two ways to
connect — pick whichever the user has set up.

### Option A — MCP connector (recommended)

If the portal has its MCP server enabled (the admin sets `MCP_ENABLED=true`;
see the portal's `docs/mcp.md`), you manage apps as **tool calls** with no
config file. First check whether a portal MCP connection is already available
(look for `whoami` / `list_apps` / `upload_app` tools). If it is, use it — and
confirm with `whoami` that the token is an admin.

If it's NOT connected yet, walk the user through it conversationally — **don't
guess at URLs**:

1. Ask for their portal URL (e.g. `https://portal.example.com`).
2. Have them mint an **admin** API token at `<portal-url>/admin/tokens` (it's
   shown once — they copy it).
3. Give them the command to run:
   ```
   claude mcp add --transport http portal <portal-url>/mcp \
     --header "Authorization: Bearer <token>"
   ```
4. Once connected, call `whoami` to confirm (expect `role: admin`). Upload with
   the `upload_app` tool — base64 the packaged `.zip`; pass `replace=true` to
   update an existing app in place (preserves per-user storage).

### Option B — upload script (portal without the MCP server)

If the portal doesn't run the MCP server, use the bundled scripts + a saved
config. The user needs:

1. A running portal reachable at some URL.
2. An **API token** from `<portal-url>/admin/tokens` (shown once — save it).
3. Config at `~/.config/pwa-portal/config.json`. Fastest:
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/configure.py
   ```
   Or set `PORTAL_URL` / `PORTAL_TOKEN` env vars for one-off use.

If you're on Option B and config is missing, ask the user for their portal URL
and token and offer to write the config for them (or run `configure.py`) before
continuing — **don't guess at URLs**.

## App structure

Every app is a folder containing:

- `portal.json` — manifest (required, at the root of the zip)
- `index.html` — entry page (default; override with manifest `entry`)
- App's own CSS/JS/icons/images
- **`icon.png` (required)** — referenced from `portal.json.icon`, drives the
  dashboard tile and the iOS home-screen icon. See "Icons" below — every app
  you build must ship a real one, not the scaffold placeholder.

When uploaded, the portal extracts to `data/apps/<slug>/` and serves the app.

### Icons (required for every app)

Every app MUST ship a real icon. The dashboard tile and the iOS
"Add-to-Home-Screen" badge both pull from `portal.json.icon`; an app
with no icon falls back to the generic portal placeholder, which makes
the home screen useless after the user installs more than one app.

**Make a pictogram, not a letter.** A user with six or seven apps on
their home screen reads icons by shape, not by initial. Six letter tiles
all in the same accent green look identical at a glance. A speedometer
for mileage, a receipt for expenses, a clock for time tracking — each is
recognizable in a tenth of a second.

The scaffold ships a placeholder at `templates/basic/icon.png`. **You
must replace it** before packaging — `package.py` refuses to build a zip
whose icon byte-for-byte matches the placeholder.

#### Drawing a pictogram with Pillow

Pillow is stdlib-adjacent (one pip install, no other dependencies) and
can compose simple shapes — rectangles, circles, polygons, lines, arcs
— on a solid background. That's all a recognizable business-app icon
needs. Aim for 192×192 PNG with the portal accent (`#059669`) as the
background and a single white silhouette.

A working template — replace the `# --- pictogram ---` body with shapes
that represent your app:

```python
from PIL import Image, ImageDraw
W = 192
ACCENT = "#059669"  # portal default emerald; pick another hex if it fits the app better
im = Image.new("RGB", (W, W), ACCENT)
d = ImageDraw.Draw(im)

# --- pictogram --- (replace this with shapes that match the app)
# Example: a simple receipt outline for an expense logger
margin = 36
d.rounded_rectangle([margin, margin - 12, W - margin, W - margin + 12], radius=8, outline="white", width=6)
for y in (72, 96, 120):
    d.line([margin + 14, y, W - margin - 14, y], fill="white", width=4)
# zig-zag bottom edge typical of receipts
zigzag = [(margin, W - margin + 12)]
step = 12
x = margin
while x < W - margin:
    x += step
    zigzag.append((x, W - margin))
    x += step
    zigzag.append((x, W - margin + 12))
d.polygon(zigzag + [(W - margin, W - margin + 12)], fill=ACCENT, outline="white")

im.save("/path/to/<slug>/icon.png")
```

Pillow primitives you'll reach for most:

- `d.rectangle([x0, y0, x1, y1], fill=..., outline=..., width=N)` — bars, panels, frames
- `d.rounded_rectangle(...)` — modern rounded panels
- `d.ellipse(...)` — circles, dots, clock faces, gauges
- `d.polygon([(x, y), ...], fill=..., outline=...)` — triangles, arrows, custom shapes
- `d.line([(x0, y0), (x1, y1)], fill=..., width=N)` — clock hands, divider lines, axes
- `d.arc(box, start, end, fill=..., width=N)` — gauges, progress rings
- `d.pieslice(box, start, end, fill=..., outline=...)` — pie wedges, hour markers

**Pictogram recipes for common app categories:**

| App type | Pictogram approach |
|---|---|
| Receipt / expense logger | Rounded rect "paper" with horizontal lines for text and a zig-zag bottom edge |
| Invoice / quote builder | Document outline + a `$` or check-mark in the lower-right corner |
| Time tracker / clock-in | Circle (clock face) + two `line`s for hour and minute hands |
| Mileage / odometer | Circle outline + arc for the gauge sweep + a single line for the needle |
| Customer directory / CRM | Rounded rect + circle (head) + half-circle (shoulders) — contact card silhouette |
| Calendar / scheduler | Rounded rect with a thicker top band and a 3×3 grid of dots inside |
| Inventory / items | 3–4 stacked rectangles, slightly offset to suggest stacked boxes |
| Forms / surveys | Document outline + a sequence of small filled circles down the left side (radio buttons) |
| Photo / image gallery | Rounded rect frame + two diagonal lines forming a mountain + a circle for the sun |
| Window / property cleaning | Rounded square frame split into 4 quadrants by a `+` (window pane) |

**If you genuinely can't think of a pictogram** (purely abstract apps,
miscellaneous utilities), fall back to a centered letter — but only as a
last resort. Same Pillow recipe as the pictogram, just with `d.text((W/2,
W/2), "X", fill="white", font=f, anchor="mm")` instead of shapes.

**User-supplied icons:** if the user attached their own PNG / SVG / JPEG
/ WebP, skip the Pillow step and save it to `<slug>/icon.png`. Keep it
square — 192×192 PNG is the sweet spot for both the dashboard tile and
iOS home-screen icons.

Rules the portal enforces:

- Manifest's `icon` must be a relative path inside the bundle (no `..`,
  no leading `/`).
- The file must exist in the zip.
- `package.py` rejects the unmodified scaffold placeholder.
- Supported formats: PNG, SVG, JPEG, WebP. PNG at 192×192 renders
  cleanest across iOS, Android, and desktop browsers.

**Authoring rule**: never `package.py` an app whose icon is still the
scaffold default. Draw the pictogram as part of the build workflow
below — not as an afterthought.

### Where the app runs

By default each app runs on its own subdomain — `<slug>.apps.<SITE_URL>` —
inside an iframe wrapper on the portal. This isolates apps from each other
and from the portal via the browser's same-origin policy. **The change is
transparent to you as an app author**: the SDK at `/portal-sdk.js` handles
cross-origin authentication automatically via a single-use launch token,
and the existing `<script src="/portal-sdk.js"></script>` HTML pattern keeps
working because the SDK is served same-origin from the subdomain at
`<slug>.apps.<SITE_URL>/portal-sdk.js`.

Self-hosters who can't set up wildcard DNS can opt into a legacy
same-origin mode (`CHILD_APPS_SAME_ORIGIN=true`), in which case the app
runs at `<SITE_URL>/apps/<slug>/`. Either way, the SDK and your app's code
are identical.

## `portal.json` schema

```json
{
  "slug": "my-app",
  "name": "My App",
  "version": "0.1.0",
  "description": "A short description.",
  "icon": "icon.png",
  "entry": "index.html",
  "services": ["pdf", "email"],
  "permissions": {
    "network": ["https://api.open-meteo.com"]
  },
  "min_portal_version": "0.1"
}
```

| Field | Required | Notes |
|---|---|---|
| `slug` | yes | kebab-case, 2–40 chars, lowercase `a-z 0-9 -`, no leading/trailing hyphen |
| `name` | yes | 1–60 chars, human-readable |
| `version` | yes | freeform string up to 20 chars (semver recommended) |
| `description` | no | up to 200 chars |
| `icon` | **yes** | relative path inside the bundle, 192×192 PNG / SVG / JPEG / WebP. See "Icons" above — placeholder is rejected at package time. |
| `entry` | no | defaults to `index.html`; must exist in the zip |
| `services` | **yes if calling** | declarative list of portal services the app will use; allowed: `pdf`, `email`, `storage`. Enforced server-side — see "Services" below |
| `permissions.network` | no | external HTTPS origins the app's `fetch()` calls need to reach — see "Network permissions" below |
| `permissions.csp_strict` | no | when `true`, opt into a strict CSP that drops `'unsafe-inline'`/`'unsafe-eval'` — see "Strict CSP" below |
| `min_portal_version` | no | hint for compatibility |
| `tools` | no | declarative operations an MCP-connected Claude can run server-side — see "Tools (let Claude run your app)" below |

The slug becomes the URL: an app with slug `expense-tracker` is reachable at `/apps/expense-tracker/`.

### Services

`services` is the list of portal services your app calls. The portal
enforces this server-side: if your app calls `portal.email.send()` without
`"email"` in `services`, the call returns 403 and the SDK throws.

Declare every service you use:

```json
{ "services": ["pdf", "email", "storage"] }
```

On upload, every declared service is auto-approved (the admin uploaded the
bundle). The admin can later revoke any service per-app under `/admin/apps`
→ expand "Services (.../...)" on that app's row. Revocations persist
across re-uploads of the same slug — an updated bundle can't silently
re-enable a service an admin turned off.

**Back-compat**: an app that declares NO `services` at all is treated as
legacy and not gated. The moment you add even one entry, the gate
activates and only the declared + admin-approved subset is callable.

**Authoring rule**: list every service your `index.html` touches. If you
add a `portal.pdf` call later, bump the manifest first.

### Network permissions

Child apps run under a strict Content-Security-Policy that only allows
same-origin `fetch()` / `XMLHttpRequest` by default. If your app calls any
external HTTP API (a weather service, a geocoder, a public dataset, etc.),
you **must** declare each origin in `permissions.network` — the browser
will block the request otherwise with a CSP violation, and the user will
see a "Failed to fetch" / network error.

Rules:

- Each entry is an **HTTPS origin**: `https://host[:port]`. No path, no
  query string, no wildcards.
- Hostname must be a real DNS name; `localhost` is accepted in dev but
  pointless in production.
- Up to 12 entries per manifest. If you genuinely need more, you're
  probably reaching for the wrong abstraction — front everything through
  one upstream.
- HTTP (plain) is rejected. The portal is HTTPS-only; mixed content would
  fail at the browser anyway.

On upload, every declared origin is auto-approved (the admin uploaded the
bundle, which counts as approval). The admin can later revoke or extend
the list per-app under `/admin/apps` → expand "Network (...)" on that
app's row. Revocations made through the admin UI **persist across
re-uploads of the same slug**, so an updated bundle can't silently
re-grant network access an admin previously turned off.

**Authoring rule**: every time you write a `fetch("https://...")` call
into a child app, add the origin to `permissions.network`. The scaffold
at `templates/basic/portal.json` ships with an empty list; populate it
before packaging if the app calls anything external.

### Strict CSP (opt-in)

By default the portal allows `'unsafe-inline'` and `'unsafe-eval'` so
existing apps that ship inline `<script>` blocks keep working. Apps that
want a stronger guarantee can opt into a strict Content-Security-Policy:

```json
{
  "permissions": {
    "csp_strict": true
  }
}
```

Under strict CSP, every inline `<script>` and `<style>` must carry a
`nonce` attribute that matches the per-response nonce the portal injects.
Use the literal token `{{NONCE}}` in your HTML — the portal substitutes
the real value at serve time:

```html
<script nonce="{{NONCE}}">
  // legitimate inline init
</script>
<style nonce="{{NONCE}}">
  body { background: #fafaf9; }
</style>
```

Rules:

- Only the **app subdomain** mode honors `csp_strict`. Under
  `CHILD_APPS_SAME_ORIGIN=true` the portal launcher and child app share an
  origin; the launcher needs its own inline scripts so the flag is
  silently ignored. If you ship apps for self-hosters who may run in
  same-origin mode, design HTML that works under both CSPs.
- `eval()`, `new Function()`, and string-arg `setTimeout` are blocked.
  Prefer external `.js` files for non-trivial logic; the nonce is for
  small init blocks, not whole apps.
- Imported stylesheets (`<link rel="stylesheet">`) and scripts
  (`<script src="...">`) work without nonces — they're same-origin.

**When to enable it**: customer-facing apps where you want a hard
guarantee an HTML-injection bug can't pivot to script execution. Skip it
for internal tools where the friction outweighs the benefit.

### Tools (let Claude run your app)

If the portal runs the MCP server (see its `docs/mcp.md`), an app can declare
`tools` an MCP-connected Claude calls directly — e.g. "create a quote for Acme
at $1,250 and share it." A tool is **declarative** (no code): the portal renders
an HTML template you provide to a PDF, then shares / downloads / emails / stores
it. Uploaded app code never runs server-side.

```json
{
  "services": ["pdf"],
  "tools": [
    {
      "name": "create_quote",
      "description": "Render a quote PDF and return a shareable link.",
      "params": [
        {"name": "customer", "type": "string", "required": true, "description": "Customer name"},
        {"name": "amount", "type": "number", "required": true}
      ],
      "render": { "html": "<h1>Quote for {{ customer }}</h1><p>Total: ${{ amount }}</p>", "filename": "quote.pdf", "branded": true },
      "deliver": { "kind": "share", "ttl_days": 30 }
    }
  ]
}
```

- `name`: snake_case, unique; Claude sees it as `<slug>__<name>`.
- `params[]`: `{name, type (string|number|boolean), required, description}` — the tool's inputs.
- `render.html`: inline template; `{{ param }}` values are autoescaped and rendered to PDF (no external fetches — embed images/fonts as `data:` URIs). `branded: true` prepends the portal header.
- `deliver.kind`: `share` (→ `{url}`), `download` (→ base64 PDF), `store` (→ saves at `key`), or `email` (→ sends the HTML to `to` with `subject`). `to` / `subject` / `key` may use `{{ param }}`.
- A tool may only use services you also list in `services` (`pdf` always; `email` / `storage` for those deliver kinds) — the upload is rejected otherwise, and an admin can revoke the capability per-app.

**Authoring rules**: keep templates self-contained (inline CSS); template
storage keys from IDs, not free-text names (spaces/punctuation are rejected as
storage keys).

## Portal SDK — how apps call services

In your app's HTML, include the SDK before your own scripts:

```html
<script src="/portal-sdk.js"></script>
```

This exposes `window.portal` with these methods. **All calls use the signed-in user's session automatically** — apps don't manage auth.

### User info
```js
const me = await portal.user.current();
// { id: 3, email: "owner@example.com", role: "admin" }
```

### PDF generation (server-side, via WeasyPrint)
```js
// Trigger a browser download:
await portal.pdf.download({
  html: "<h1>Receipt</h1><p>Total: $42</p>",
  filename: "receipt.pdf",
});

// Or get a Blob to attach/upload/render yourself:
const blob = await portal.pdf.render({ html: "...", filename: "..." });

// Opt-in: prepend the portal's branding header (business name + logo + accent
// border). Pulls from /admin/settings → Branding. Pass branded: true on any
// PDF where the document represents the business — quotes, invoices,
// receipts, statements:
await portal.pdf.download({
  html: "<html><body><h1>Quote</h1>...</body></html>",
  filename: "quote.pdf",
  branded: true,
});
```

The HTML you pass is rendered server-side. You can include `<style>` blocks and inline CSS. External resources (images, fonts) are blocked by a strict URL fetcher and must be embedded as `data:` URIs.

**When to set `branded: true`**: customer-facing documents (quotes, invoices, receipts, statements, work orders). Skip it for internal-only reports where the extra header would just waste space.

### Email
```js
await portal.email.send({
  to: "customer@example.com",        // single email OR array of emails
  subject: "Your receipt",
  html: "<p>Hi there.</p>",
  text: "Hi there.",                 // include at least one of html/text
});
```

Returns `{ status: "sent", count: N }` on success. Throws if SMTP isn't configured on the portal (503) or send fails.

### Storage (per-app, per-user namespace)
```js
// Put — value can be a Blob, string, or any JSON-serializable value
await portal.storage.put("notes/today.json", { entries: ["a", "b"] });
await portal.storage.put("receipt.pdf", pdfBlob);

// Get — auto-detects type from content type
const notes = await portal.storage.get("notes/today.json");   // parsed JSON
const blob = await portal.storage.get("receipt.pdf");          // Blob

await portal.storage.list();
// { items: [{ key, size }], usage: <bytes>, limit: 104857600 }

await portal.storage.delete("notes/today.json");
```

Keys allow `A-Z a-z 0-9 . _ -` and `/` (forward slash acts as folder separator). 10MB per object, 100MB total per namespace.

### Share links (public, tokenized URLs)

For sending a document to a customer who isn't a portal user — quotes,
invoices, signed contracts, anything where "make them sign up" would be
friction:

```js
// Kind 1: share something already in storage.
const shareA = await portal.share.create({
  kind: "storage",
  key: "receipts/123.pdf",       // must exist in this user's storage
  filename: "receipt.pdf",       // shown on download
  ttlSeconds: 7 * 24 * 3600,     // 7 days default; 90d cap
  maxViews: 0,                   // 0 = unlimited; cap at 10000
});
// → { token, url: "https://<site>/s/<token>", expires_at, kind, max_views }

// Kind 2: render a fresh PDF at share-create time.
const shareB = await portal.share.create({
  kind: "pdf",
  html: "<html><body><h1>Quote #42</h1>...</body></html>",
  filename: "quote-42.pdf",
  ttlSeconds: 30 * 24 * 3600,
  maxViews: 3,
});
```

Hand `shareA.url` to your user — email it, paste it into Messages, etc.
Anyone with the link can open it until it expires, hits its view cap, or
the admin revokes from `/admin/shares`.

Storage shares stream the file live, so editing the stored object
updates what recipients see. PDF shares are rendered once and frozen.

Requires the corresponding service in your manifest's `services`
(`storage` for kind=storage, `pdf` for kind=pdf).

## Build workflow

1. **Understand requirements.** Ask the user 1–2 clarifying questions max. Default to something simple and shippable. Don't over-engineer.
2. **Pick a slug.** Kebab-case, short and descriptive (`receipt-emailer`, `quote-calc`, `contact-form`).
3. **Scaffold.** Copy the basic template:
   ```
   cp -r ~/.claude/skills/pwa-portal-app/templates/basic /path/to/<slug>
   ```
   Then edit `portal.json` (slug/name/version) and `index.html` (initial content).
4. **Implement.** Edit `index.html`. Add CSS/JS inline or as separate files. Keep it minimal — non-coder users don't want elaborate code.
5. **Draw a pictogram icon** — REQUIRED, see the "Icons" section above.
   Pick a shape from the recipe table that fits the app (receipt, clock,
   gauge, contact card, etc.) and draw it with Pillow primitives on the
   portal accent background. Save to `<slug>/icon.png`. A letter on a
   colored square is the *last-resort* fallback, not the default —
   pictograms are how a user with five apps on their home screen tells
   them apart. `package.py` refuses to build a zip whose icon is still
   the scaffold placeholder, so the step is enforced, not optional.
6. **Package.**
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/package.py /path/to/<slug>
   ```
   Produces `<slug>-<version>.zip` next to the source folder.
7. **Upload.** If connected via the portal's MCP server (Option A), call the
   `upload_app` tool — base64-encode the `.zip` into `zip_base64`, pass
   `replace=true` to update an existing app. Otherwise use the script:
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py /path/to/<slug>-<version>.zip
   ```
   On success prints `Uploaded <name> (slug: ..., version: ...)`.
8. **Confirm.** Tell the user the app is live at `<portal_url>/apps/<slug>/`.

## Coding conventions for child apps

- **HTML/CSS/JS only.** No build step, no npm, no bundler. The zip ships as-is.
- **Single-file layout is fine** for small apps — inline `<style>` and `<script>` in `index.html`. Only split into separate files when it helps readability.
- **Load the SDK first**: `<script src="/portal-sdk.js"></script>` in `<head>`, your own scripts after it (or as `defer`).
- **Don't ship a service worker.** The portal's SW handles the origin.
- **No external CSS frameworks** unless the user specifically asks. Lean styling is fine.
- **Persistent data goes through `portal.storage`.** Never hit external APIs for user data without asking.
- **Be defensive about user input** — validate emails, numbers, etc. before sending to the portal API.
- **Match the portal's visual style.** The portal has a stone-neutral / emerald-accent design system; the basic scaffold at `templates/basic/index.html` already includes the design tokens (CSS variables). When generating new HTML for a child app, **start from the scaffold's `<style>` block** and use the tokens below — apps then look consistent with the portal chrome and adapt to light/dark automatically.

## Visual style — design tokens

Child apps should embed these CSS variables at the top of their `<style>` block. Light + dark are both defined; the browser picks based on `prefers-color-scheme`.

```css
:root {
  color-scheme: light dark;
  --bg: #fafaf9;
  --surface: #ffffff;
  --surface-2: #f5f5f4;
  --border: #e7e5e4;
  --border-strong: #d6d3d1;
  --text: #1c1917;
  --text-muted: #57534e;
  --text-faint: #78716c;
  --accent: #059669;          /* emerald — primary actions, links */
  --accent-hover: #047857;
  --accent-fg: #ffffff;
  --accent-tint: #ecfdf5;
  --accent-soft: rgba(5, 150, 105, 0.12);
  --danger: #b91c1c;
  --success: #15803d;
  --radius-sm: 0.4rem;
  --radius: 0.55rem;
  --shadow-sm: 0 1px 2px rgba(15,23,42,0.05), 0 1px 1px rgba(15,23,42,0.025);
  --shadow: 0 2px 4px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
  --font-sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
    Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0c0a09;
    --surface: #1c1917;
    --surface-2: #292524;
    --border: #292524;
    --border-strong: #44403c;
    --text: #fafaf9;
    --text-muted: #a8a29e;
    --accent: #10b981;
    --accent-hover: #34d399;
    --accent-fg: #0c0a09;
    --accent-tint: #064e3b;
    --accent-soft: rgba(16, 185, 129, 0.18);
    --danger: #f87171;
    --success: #4ade80;
  }
}
```

**Usage conventions:**
- `body { font-family: var(--font-sans); background: var(--bg); color: var(--text); }`
- Wrap page content in `<main class="shell">` with `max-width: 36rem` for forms / 60rem for wide layouts; padding `2rem 1.5rem 4rem`.
- Buttons use `background: var(--accent)`, `color: var(--accent-fg)`, `border-radius: var(--radius-sm)`, `box-shadow: var(--shadow-sm)`, hover → `--accent-hover`.
- Secondary buttons: `background: var(--surface)`, `color: var(--accent)`, `border: 1px solid var(--border-strong)`.
- Inputs: `background: var(--surface)`, `border: 1px solid var(--border-strong)`, focus → `border-color: var(--accent)` + `box-shadow: 0 0 0 3px var(--accent-soft)`.
- Headings: `font-weight: 700`, `letter-spacing: -0.025em` for h1.
- Cards / panels: `background: var(--surface)`, `border: 1px solid var(--border)`, `border-radius: var(--radius)`, optional `box-shadow: var(--shadow-sm)`.
- Pills / badges: `padding: 0.15rem 0.625rem`, `border-radius: 9999px`, `font-size: 0.7rem`, `font-weight: 600`. Use `--success-tint` / `--success` for "good", `--danger-tint` / `--danger` for "bad", `--accent-tint` / `--accent` for neutral-emphasized.
- Status messages: small text in `--text-muted`; errors in `--danger`; success in `--success`.

If an app needs a one-off color (chart legend, brand callout), invent a CSS variable scoped to that element rather than dropping a hex literal mid-template — keeps the palette legible.

## Minimal example: hello user

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hello</title>
  <script src="/portal-sdk.js"></script>
  <style>
    /* (Paste the full :root token block from "Visual style" above) */
    :root {
      color-scheme: light dark;
      --bg: #fafaf9; --surface: #ffffff; --border: #e7e5e4;
      --text: #1c1917; --text-muted: #57534e;
      --accent: #059669; --accent-fg: #ffffff;
      --radius-sm: 0.4rem;
      --font-sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root { --bg: #0c0a09; --surface: #1c1917; --border: #292524; --text: #fafaf9; --text-muted: #a8a29e; --accent: #10b981; --accent-fg: #0c0a09; }
    }
    * { box-sizing: border-box; }
    body { font-family: var(--font-sans); background: var(--bg); color: var(--text); margin: 0; }
    .shell { max-width: 32rem; margin: 0 auto; padding: 2rem 1.5rem; }
    h1 { font-weight: 700; letter-spacing: -0.025em; }
  </style>
</head>
<body>
  <main class="shell">
    <h1 id="greeting">Loading…</h1>
    <script>
      portal.user.current().then(me => {
        document.getElementById("greeting").textContent = `Hi, ${me.email}`;
      });
    </script>
  </main>
</body>
</html>
```

## Example: receipt → PDF → email

```html
<form id="form">
  <label>Customer email <input name="customer" type="email" required></label>
  <label>Amount <input name="amount" type="number" step="0.01" required></label>
  <button>Generate & send</button>
</form>
<p id="status"></p>
<script src="/portal-sdk.js"></script>
<script>
const f = document.getElementById("form");
const status = document.getElementById("status");
f.onsubmit = async (e) => {
  e.preventDefault();
  status.textContent = "Working…";
  const data = new FormData(f);
  const html = `<h1>Receipt</h1><p>Amount: $${data.get("amount")}</p>`;
  try {
    await portal.pdf.download({ html, filename: "receipt.pdf" });
    await portal.email.send({
      to: data.get("customer"),
      subject: "Your receipt",
      html,
    });
    status.textContent = "Sent.";
  } catch (err) {
    status.textContent = "Error: " + err.message;
  }
};
</script>
```

## Re-uploading an updated app

To ship an update in place, preserving per-user storage:

1. Bump `version` in `portal.json`.
2. Repackage:
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/package.py /path/to/<slug>
   ```
3. Upload with `--replace`:
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py --replace /path/to/<slug>-<version>.zip
   ```
   On success prints `Replaced <name> (slug: ..., version: ...)`. Per-user data
   under `data/storage/<slug>/<user_id>/*` is left untouched.

Omitting `--replace` keeps the original behavior: the portal rejects an upload
whose slug already exists. Use that for first-time installs, and `--replace`
for updates. Avoid deleting and re-uploading — deletion wipes per-user storage.

## Things to remember

- **One slug = one app.** Pick wisely; it's the URL.
- **The skill is non-interactive.** Don't expect to prompt during a build — gather inputs up front.
- **Read the portal's error.** If `upload.py` prints an HTTP error, the message body is the portal's validation feedback. Adjust `portal.json` or the bundle accordingly.
- **Storage is per `(app_slug, user_id)`** — Alice's data in `expense-tracker` is invisible to Bob in `expense-tracker` and to Alice in `quote-calc`.
- **Apps are gated by portal login.** You don't need to add auth — the portal handles it.
