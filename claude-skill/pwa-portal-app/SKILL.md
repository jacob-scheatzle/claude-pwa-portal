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

## First-time setup the user must complete

Before this skill can upload anything, the user needs:

1. A running **PWA Portal** instance reachable at some URL.
2. An **API token** they create at `<portal-url>/admin/tokens`. The token is shown once on creation — they need to save it.
3. Config written to `~/.config/pwa-portal/config.json`. The fastest way: have them run
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/configure.py
   ```
   Alternatively, `PORTAL_URL` and `PORTAL_TOKEN` environment variables work for one-off use.

If a build fails because config is missing, stop and ask the user to complete this setup before continuing — don't guess at URLs.

## App structure

Every app is a folder containing:

- `portal.json` — manifest (required, at the root of the zip)
- `index.html` — entry page (default; override with manifest `entry`)
- App's own CSS/JS/icons/images
- Optional `icon.png` (referenced from `portal.json.icon`)

When uploaded, the portal extracts to `data/apps/<slug>/` and serves the app.

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
  "min_portal_version": "0.1"
}
```

| Field | Required | Notes |
|---|---|---|
| `slug` | yes | kebab-case, 2–40 chars, lowercase `a-z 0-9 -`, no leading/trailing hyphen |
| `name` | yes | 1–60 chars, human-readable |
| `version` | yes | freeform string up to 20 chars (semver recommended) |
| `description` | no | up to 200 chars |
| `icon` | no | relative path inside the bundle; recommended 192×192 PNG |
| `entry` | no | defaults to `index.html`; must exist in the zip |
| `services` | no | declarative list; allowed: `pdf`, `email`, `storage`; informational for now |
| `min_portal_version` | no | hint for compatibility |

The slug becomes the URL: an app with slug `expense-tracker` is reachable at `/apps/expense-tracker/`.

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
```

The HTML you pass is rendered server-side. You can include `<style>` blocks and inline CSS. External resources (images, fonts) are blocked by a strict URL fetcher and must be embedded as `data:` URIs.

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

## Build workflow

1. **Understand requirements.** Ask the user 1–2 clarifying questions max. Default to something simple and shippable. Don't over-engineer.
2. **Pick a slug.** Kebab-case, short and descriptive (`receipt-emailer`, `quote-calc`, `contact-form`).
3. **Scaffold.** Copy the basic template:
   ```
   cp -r ~/.claude/skills/pwa-portal-app/templates/basic /path/to/<slug>
   ```
   Then edit `portal.json` (slug/name/version) and `index.html` (initial content).
4. **Implement.** Edit `index.html`. Add CSS/JS inline or as separate files. Keep it minimal — non-coder users don't want elaborate code.
5. **Package.**
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/package.py /path/to/<slug>
   ```
   Produces `<slug>-<version>.zip` next to the source folder.
6. **Upload.**
   ```
   python3 ~/.claude/skills/pwa-portal-app/scripts/upload.py /path/to/<slug>-<version>.zip
   ```
   On success prints `Uploaded <name> (slug: ..., version: ...)`.
7. **Confirm.** Tell the user the app is live at `<portal_url>/apps/<slug>/`.

## Coding conventions for child apps

- **HTML/CSS/JS only.** No build step, no npm, no bundler. The zip ships as-is.
- **Single-file layout is fine** for small apps — inline `<style>` and `<script>` in `index.html`. Only split into separate files when it helps readability.
- **Load the SDK first**: `<script src="/portal-sdk.js"></script>` in `<head>`, your own scripts after it (or as `defer`).
- **Don't ship a service worker.** The portal's SW handles the origin.
- **No external CSS frameworks** unless the user specifically asks. Lean styling is fine.
- **Persistent data goes through `portal.storage`.** Never hit external APIs for user data without asking.
- **Be defensive about user input** — validate emails, numbers, etc. before sending to the portal API.
- **Match the portal's quiet aesthetic** when in doubt: system font stack, accent color around `#075985`, generous spacing.

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
    body { font-family: -apple-system, "Segoe UI", sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }
  </style>
</head>
<body>
  <h1 id="greeting">Loading…</h1>
  <script>
    portal.user.current().then(me => {
      document.getElementById("greeting").textContent = `Hi, ${me.email}`;
    });
  </script>
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
