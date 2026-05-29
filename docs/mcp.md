# MCP server — manage the portal from Claude

The portal can expose a **Model Context Protocol (MCP)** endpoint at `/mcp` so
Claude (Claude Code, Claude Desktop, or a claude.ai connector) connects with a
URL + an admin API token and manages your child apps **as tool calls** — list,
inspect, upload, replace, enable/disable — instead of the skill's
`upload.py` + `~/.config/pwa-portal/config.json` plumbing.

It's **opt-in** and **off by default**: it's a write-capable, admin-authed
surface, and it needs an optional dependency. Turn it on deliberately.

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

### 1. Mint an admin API token

In the portal: **Admin → Tokens → New token**. The raw token is shown **once** —
copy it. It must belong to an **admin** user; app management requires it.

### 2. Add the connector

**Claude Code:**

```bash
claude mcp add --transport http portal https://<your-portal>/mcp \
  --header "Authorization: Bearer <your-token>"
```

(Local dev: `http://localhost:8000/mcp`. Check `claude mcp add --help` for your
version's exact flags.)

**Claude Desktop / claude.ai:** add a custom HTTP/streamable MCP connector
pointing at `https://<your-portal>/mcp` with an `Authorization: Bearer <token>`
header. Exact UI varies by client version; the URL and header are the same.

### 3. Verify

Ask Claude to call **`whoami`**. It should return your admin user
(`{"id": …, "email": …, "role": "admin"}`). If it does, you're connected.

---

## Tools

| Tool | What it does |
|---|---|
| `whoami` | Returns `{id, email, role}` — confirms the connection and that the token is an admin. |
| `list_apps` | All installed apps in dashboard order: slug, name, version, enabled, declared + admin-approved services. |
| `get_app(slug)` | Full detail for one app: version, enabled, declared vs approved services, requested vs allowed network origins, `csp_strict`, entry, icon, upload time. |
| `upload_app(filename, zip_base64, replace=false)` | Install a packaged app `.zip` (base64-encoded). `replace=false` installs new (fails if the slug exists); `replace=true` updates in place, **preserving per-user storage**. Slug/name/version come from the bundle's `portal.json`. |
| `set_app_enabled(slug, enabled)` | Enable or disable an app. Disabling hides it from the dashboard and revokes open app sessions. |

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
- **Admin token only.** Auth reuses the portal's existing bearer-token
  validation; non-admin tokens get `403`, missing/invalid tokens get `401`.
- **Auth failures are logged + banned.** Every 401/403 on `/mcp` writes an
  `MCP_AUTH_FAILED` line to `data/security.log`; the bundled fail2ban filter
  bans IPs that flood it (see [fail2ban.md](fail2ban.md)). Tokens are
  high-entropy, so this is DoS/observability hardening, not anti-brute-force.
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
