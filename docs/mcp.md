# MCP server — manage the portal from Claude

The portal can expose a **Model Context Protocol (MCP)** endpoint at `/mcp` so
Claude (Claude Code, Claude Desktop, or a claude.ai connector) connects — with an
admin API token, or via OAuth for claude.ai — and manages your child apps **as
tool calls** — list, inspect, upload, replace, enable/disable — instead of the
skill's `upload.py` + `~/.config/pwa-portal/config.json` plumbing.

It's **on by default in the Docker image** (the dependency is bundled and the
toggle auto-enables) but inert without a valid admin token. It's a
write-capable, admin-authed surface — set `MCP_ENABLED=false` if you don't use it.

> The server does two things: **manage** apps (list / upload / replace / enable)
> and **run** the tools an app declares in its manifest (see
> [App tools](#app-tools--let-claude-use-an-apps-functions)).

---

## Enable it

**Docker (default): nothing to do.** The image bundles the `mcp` dependency and
the server auto-enables, so `/mcp` is live the moment the container starts — boot
logs show `MCP management server available at /mcp`. To **disable** it, set
`MCP_ENABLED=false` in `.env`. To build a lean image without the dependency,
build with `--build-arg INSTALL_MCP=false`.

**From source / pip:** install the extra and it auto-enables:

```bash
pip install 'pwa-portal[mcp]'
```

### The `MCP_ENABLED` toggle

| Value | Behavior |
|---|---|
| *blank / unset* (default) | **Auto** — on when the `mcp` package is importable (it is, in the Docker image). |
| `false` | Force off. |
| `true` | Force on; logs a warning and skips `/mcp` if the package isn't installed. |

---

## Connect

Two ways to authenticate, depending on the client:

- **Static admin API token** — Claude Code and Claude Desktop. Mint a token,
  send it as a bearer header.
- **OAuth** — **claude.ai** custom connectors. claude.ai can't use a static
  token; it runs a browser OAuth sign-in instead (nothing to copy — you
  authorize as a portal admin). Requires the portal to be reachable over
  **HTTPS at a public domain**: the OAuth issuer must be HTTPS (localhost is
  allowed for dev).

### Claude Code / Claude Desktop (API token)

1. **Mint an admin API token** — **Admin → Tokens → New token**. The raw token
   is shown **once**; it must belong to an **admin** user.
2. **Add the connector** (Claude Code):
   ```bash
   claude mcp add --transport http portal https://<your-portal>/mcp \
     --header "Authorization: Bearer <your-token>"
   ```
   (Local dev: `http://localhost:8000/mcp`.) In Claude Desktop, add a custom
   HTTP/streamable MCP server at `https://<your-portal>/mcp` with the same
   `Authorization: Bearer <token>` header.

### claude.ai (OAuth)

1. In claude.ai, add a **custom connector** pointing at
   `https://<your-portal>/mcp` — no token or header.
2. Click **Connect**. claude.ai discovers the portal's OAuth server, registers
   itself dynamically, and sends you to the portal to sign in.
3. Sign in as an **admin** and **Approve** on the consent screen. The connection
   completes and refreshes silently afterward. (Non-admins are refused — MCP is
   admin-only.)

Under the hood the portal runs the OAuth endpoints the connector needs —
authorization-server + protected-resource metadata, dynamic client registration
(RFC 7591), the PKCE authorization-code grant, and refresh — built on the MCP
SDK's auth framework. OAuth access carries the **same privilege as an admin API
token**; disconnect from claude.ai (or revoke server-side) to end it.

#### Pre-registered client (optional)

The default flow above needs nothing pre-configured — claude.ai registers
itself. If your connector instead asks for an explicit **Client ID** and
**Client Secret**, create one under **Admin → MCP OAuth clients**: enter a name
and the connector's redirect URI (for claude.ai, typically
`https://claude.ai/api/mcp/auth_callback`), and the portal mints a client ID +
secret (the secret is shown once). Enter those in the connector's OAuth settings
alongside the `…/mcp` URL.

This only skips the automatic *registration* step — the one-time browser
sign-in + **Approve** still happens (that's inherent to the authorization-code
grant; there's no way to connect claude.ai without it). Deleting the client
revokes it and any tokens it issued.

### Verify

Ask Claude to call **`whoami`**. It should return your admin user
(`{"id": …, "email": …, "role": "admin"}`). If it does, you're connected.

---

## Tools

| Tool | What it does |
|---|---|
| `whoami` | Returns `{id, email, role}` — confirms the connection and that the token is an admin. |
| `authoring_guide` | Returns a self-contained app-authoring spec (manifest + tool DSL + line items + a worked example). Call it before building or changing an app — especially without the local skill. |
| `list_apps` | All installed apps in dashboard order: slug, name, version, enabled, declared + admin-approved services. |
| `get_app(slug)` | Full detail for one app: version, enabled, declared vs approved services, requested vs allowed network origins, `csp_strict`, entry, icon, upload time. |
| `upload_app(filename, zip_base64, replace=false)` | Install a packaged app `.zip` (base64-encoded). `replace=false` installs new (fails if the slug exists); `replace=true` updates in place, **preserving per-user storage**. Slug/name/version come from the bundle's `portal.json`. |
| `set_app_enabled(slug, enabled)` | Enable or disable an app. Disabling hides it from the dashboard and revokes open app sessions. |
| `list_schedules()` | List recurring scheduled tool runs (id, app, tool, cadence, enabled, args, last/next run). |
| `create_schedule(app_slug, tool_name, args, frequency, hour, …)` | Schedule one of an app's tools to run automatically on a cadence (`daily`/`weekly`/`monthly` at a UTC `hour`:`minute`; `day_of_week` 0–6 for weekly, `day_of_month` 1–28 for monthly). Output is delivered through the tool's own deliver action. Runs as the connected admin. |
| `set_schedule_enabled(id, enabled)` | Pause or resume a schedule. |
| `delete_schedule(id)` | Delete a schedule. |
| `run_schedule(id)` | Run a schedule's tool immediately (does not change its cadence). |

Schedules are also managed from the admin UI at **Admin → Schedules**, so the
portal can run them with or without an MCP connection.

`delete_app` is intentionally **not** exposed — deletion wipes per-user storage.
Delete from the admin UI (**Admin → Apps**) when you really mean it.

Every mutating call is written to the portal **audit log** (`Admin → Health`)
with `via: mcp` and the acting admin.

---

## How this relates to the Claude skill

The [`pwa-portal-app` skill](../claude-skill/pwa-portal-app/SKILL.md) still does
the part MCP can't: it carries the **authoring expertise** — how to scaffold an
app, draw a pictogram icon, apply the portal's design tokens, and write a valid
`portal.json`. What MCP replaces is the skill's **transport**: once connected,
"upload this app" is the `upload_app` tool instead of `configure.py` +
`upload.py` + a token file. You can use both together — build with the skill,
ship with the MCP tool — or keep using the upload script; nothing is removed.

---

## Security notes

- **On by default in the Docker image** (the dep is bundled and the toggle
  auto-enables) — but it's inert without a valid admin token. Set
  `MCP_ENABLED=false` to remove the endpoint entirely.
- **Admin only, either way.** Auth accepts a static admin bearer token *or* an
  OAuth access token; both must resolve to an **admin** (non-admin → `403`,
  missing/invalid → `401`). OAuth tokens are minted only after an admin signs in
  and approves the consent screen; the authorization-code grant is **PKCE-only**
  (S256), client secrets/codes/access+refresh tokens are random and stored as
  SHA-256 hashes, refresh tokens rotate on use, and access is admin-equivalent.
  The OAuth issuer must be HTTPS (the SDK allows localhost for dev), so OAuth is
  effectively for real (HTTPS) deployments; static tokens cover everything else.
- **Auth failures are logged + banned.** Every 401/403 on `/mcp` writes an
  `MCP_AUTH_FAILED` line to `data/security.log`, and a 400/401 on the OAuth
  endpoints (`/token`, `/register`, `/revoke`, `/authorize`) writes an
  `OAUTH_AUTH_FAILED` line; the bundled fail2ban filter bans IPs that flood
  either (see [fail2ban.md](fail2ban.md)). Tokens/secrets/codes are
  high-entropy, so this is DoS/observability hardening, not anti-brute-force.
- **Dynamic client registration is bounded.** `/register` is open (as the spec
  intends), but registrations that never complete a connection (no token after
  a week) are pruned, so a registration flood can't grow the client table
  unbounded. On the AWS deploy, the edge WAF rate-limit also blunts floods.
- **Portal origin only.** `/mcp` returns `404` on any child-app subdomain
  (`<slug>.apps.<SITE_URL>`), so an uploaded app can't reach the management
  surface even though it shares the registrable domain.
- **No new trust boundary.** The token already could upload apps via
  `POST /api/v1/apps/upload`; MCP is a new transport over the same capability,
  not a new privilege.
- Treat an admin MCP token like a password — it can upload arbitrary app
  bundles. Revoke it from **Admin → Tokens** if exposed.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Connection fails / no `/mcp` | `MCP_ENABLED=false`, or a source install without the `mcp` extra (`pip install 'pwa-portal[mcp]'`). Boot logs show whether `/mcp` mounted. |
| `401` | Missing or wrong bearer token. |
| `403` | The token belongs to a non-admin user. Mint one as an admin. |
| `404` only from one host | You're hitting an app subdomain. Use the portal's base host: `https://<SITE_URL>/mcp`. |

---

## App tools — let Claude use an app's functions

Beyond managing apps, the MCP server exposes the **tools each app declares** in
its `portal.json`, so Claude can run an app's operations directly — "create a
quote for Acme at $1,250 and give me a share link" becomes one tool call.

These tools are **dynamic**: they appear in the MCP tool list as soon as an app
that declares them is uploaded/enabled and disappear when it's disabled, named
`<slug>__<tool>` (e.g. `quote-tool__create_quote`).

### Declaring tools (in `portal.json`)

```json
{
  "slug": "quote-tool",
  "services": ["pdf"],
  "tools": [
    {
      "name": "create_quote",
      "description": "Render a quote PDF and return a shareable link.",
      "params": [
        {"name": "customer", "type": "string", "required": true, "description": "Customer name"},
        {"name": "amount", "type": "number", "required": true}
      ],
      "render": {
        "html": "<h1>Quote for {{ customer }}</h1><p>Total: ${{ amount }}</p>",
        "filename": "quote.pdf",
        "branded": true
      },
      "deliver": { "kind": "share", "ttl_days": 30 }
    }
  ]
}
```

| Field | Notes |
|---|---|
| `name` | snake_case, unique within the app. Exposed as `<slug>__<name>`. |
| `description` | what the tool does — shown to Claude. |
| `params[]` | each `{name, type, required, description}`. `type` is `string`/`number`/`boolean`, or `array` for a list of objects — an array param adds `fields: [{name, type, required, description}]` for the element shape (line items). Becomes the tool's input schema. |
| `render.html` | inline template; `{{ param }}` placeholders are substituted with **autoescaped** values via a sandboxed Jinja environment, then rendered to PDF (no external resources are fetched). |
| `render.branded` | prepend the portal's branding header (business name + logo). |
| `deliver.kind` | what to do with the rendered PDF — see below. |

### Line items (array params)

For invoices, quotes, work orders — anything with a variable-length list — use
an `array` param and iterate it in the template. The sandboxed Jinja supports
`{% for %}` / `{% if %}`, arithmetic, `{{ '%.2f'|format(n) }}`, running totals
via `{% set ns = namespace(t=0) %}` + `{% set ns.t = ns.t + ... %}`, and
`{{ x | default(0, true) }}` for optional numbers.

```json
{
  "name": "create_invoice",
  "params": [
    {"name": "customer", "type": "string", "required": true},
    {"name": "items", "type": "array", "required": true,
     "fields": [
       {"name": "description", "type": "string", "required": true},
       {"name": "qty", "type": "number", "required": true},
       {"name": "rate", "type": "number", "required": true}
     ]}
  ],
  "render": {"html": "<table>{% set ns = namespace(t=0) %}{% for it in items %}<tr><td>{{ it.description }}</td><td>${{ '%.2f'|format(it.qty * it.rate) }}</td></tr>{% set ns.t = ns.t + it.qty * it.rate %}{% endfor %}</table><p>Total: ${{ '%.2f'|format(ns.t) }}</p>", "branded": true},
  "deliver": {"kind": "share"}
}
```

(Full versions ship in `examples/invoice-gen`, `quote-builder`, and `work-order`.)

### Deliver kinds

| `kind` | Does | Returns |
|---|---|---|
| `share` | Create a public share link to the PDF (`ttl_days`, max 90). | `{url, expires_at}` |
| `download` | Return the PDF inline. | `{filename, content_type, pdf_base64}` |
| `store` | Save the PDF to the user's per-app storage at `key` (templated). | `{key, size}` |
| `email` | Email the rendered HTML to `to` (templated) with `subject`. | `{count}` |

### Rules + safety

- **No uploaded code runs server-side.** A tool is a *declaration*; the portal
  runs it by composing its own primitives. The per-app-origin isolation is
  untouched.
- A tool may only use portal services the manifest also **declares** in
  `services` — `share`/`download` need `pdf`, `store` needs `pdf` + `storage`,
  and `email` needs `email` — so an admin can revoke the capability per-app
  under `/admin/apps`, and the manifest is rejected at upload if a tool uses an
  undeclared service.
- Tool calls run as the **connected admin user**, in that user's per-app storage
  namespace, send email as the business, and count against the same per-user
  PDF/email rate limits as the SDK.
- `email` sends the rendered HTML as the message body (not a PDF attachment) in
  this version.
