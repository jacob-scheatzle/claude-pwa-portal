# ProgressiveWebAppPortal

An open-source, self-hosted **Progressive Web App portal** for small businesses. Deploy it on a small VPS and give your staff a single, installable home for the internal tools they use every day. New tools can be scaffolded, packaged, and uploaded by a non-developer working with Claude.

> **Status:** early. Single-tenant per deployment. All core building blocks in place; expect sharp edges.

## Why

Most small businesses have a handful of little internal tools — quote builders, receipt emailers, time loggers, lookup utilities — that would be useful if they lived behind a single login on every staff member's phone home screen. Building each tool standalone is heavy; building them on top of an opinionated portal that already handles auth, hosting, PDFs, email, and per-user storage is light.

The portal is designed so the apps inside it can be authored by someone who isn't a coder, working with Claude.

## What's included

- A self-hostable **portal** (FastAPI + SQLite + Caddy in Docker) with:
  - Email/password auth, admin and user roles
  - PWA manifest + service worker — installable on iPhone home screen
  - Admin pages for staff users, app uploads, SMTP, and API tokens
  - JSON HTTP API at `/api/v1/*`
- A **JavaScript SDK** (`/portal-sdk.js`) automatically available to child apps, exposing:
  - `portal.user.current()` — current signed-in user
  - `portal.pdf.render/download()` — server-rendered PDF via WeasyPrint
  - `portal.email.send()` — outgoing mail via the portal's SMTP config
  - `portal.storage.{put,get,list,delete}` — per-app, per-user key/value storage
- A **Claude skill** ([`claude-skill/pwa-portal-app/`](claude-skill/pwa-portal-app/)) so a non-developer can ask Claude to build an app and have it scaffolded, packaged, and uploaded automatically.
- A **reference example** ([`examples/hello-receipt/`](examples/hello-receipt/)) — a working PWA that uses every SDK service.

## Quick start

Requires a host with **Docker** and **Docker Compose**.

```bash
git clone https://github.com/<your-org>/ProgressiveWebAppPortal.git
cd ProgressiveWebAppPortal
cp .env.example .env
# Edit .env: set SECRET_KEY (any long random string) and SITE_URL.
docker compose up --build -d
```

Open `https://<your SITE_URL>/` (or `http://localhost` for local dev) and walk through the first-run wizard to create your admin account.

See [docs/deploying.md](docs/deploying.md) for the full VPS guide (Caddy auto-HTTPS, domains vs sslip.io, SMTP setup, backups, troubleshooting).

## Building apps for it

Two paths.

### With Claude (recommended for non-developers)

```bash
bash claude-skill/pwa-portal-app/install.sh
python3 ~/.claude/skills/pwa-portal-app/scripts/configure.py
```

Then ask Claude something like:

> *"Make me a quoting tool for my portal — should let me enter line items, generate a PDF, and email it to the customer."*

Claude reads the skill, scaffolds the app, implements your spec, packages it, and uploads it through the portal's API. See [`claude-skill/pwa-portal-app/SKILL.md`](claude-skill/pwa-portal-app/SKILL.md) for everything Claude knows.

### Manually

See [docs/app-authoring.md](docs/app-authoring.md) for the full `portal.json` schema, file layout, SDK usage, and the packaging/upload commands. For the wire-level API, see [docs/api-reference.md](docs/api-reference.md).

## Project layout

```
portal/                  FastAPI portal app
  main.py                  Routes, app factory, PWA endpoints, session/login
  api.py                   /api/v1/* — JSON API for child apps + automation
  apps.py                  Child-app upload, validation, extraction, serving
  admin.py                 /admin/settings, /admin/tokens, /admin/users
  models.py                SQLModel tables: User, Setting, App, ApiToken
  settings_store.py        DB-first config with .env fallback
  security.py              bcrypt password hashing + validation
  deps.py                  FastAPI deps: current_user, current_user_or_token, require_admin
  templates/               Jinja2 templates
  static/                  SW, manifest, default icons, portal-sdk.js
claude-skill/
  pwa-portal-app/          Drop-in Claude skill — scaffolding + packager + uploader
examples/
  hello-receipt/           Reference child app (form → PDF + email + history)
docs/                    Deployment, app authoring, API reference
docker-compose.yml       Portal + Caddy
Dockerfile               Single-stage Python build
Caddyfile                Reverse proxy with auto-HTTPS for {$SITE_URL}
.env.example             Configuration template
pyproject.toml           Python dependencies
```

## Architecture decisions

- **Single-tenant per deployment.** One business per portal instance. Multi-tenant SaaS is explicitly not a goal — every small business self-hosts.
- **Subpath routing for child apps.** They live at `/apps/<slug>/...` rather than subdomains, so one domain and one TLS cert is enough.
- **Server-side Python services.** PDF and email are server endpoints; child apps are HTML/CSS/JS that call them. Keeps a small VPS happy and lets non-coders ship working tools.
- **DB-first config with `.env` fallback.** Admins can edit SMTP/site URL from the UI; clearing a field falls back to the env value.
- **Stdlib-only Claude skill tooling.** `package.py` and `upload.py` use only the Python standard library (`urllib`, `zipfile`), so they work anywhere Python 3.11+ runs.
- **No frontend build step in the portal.** Server-rendered Jinja + HTMX + a touch of inline JS. Easier for non-coders to read, fork, and patch.

## License

This project is intended to be open source. The repository does not yet ship a `LICENSE` file — add one before publishing (MIT and Apache-2.0 are common picks for projects in this niche).

## Contributing

This is an early project. Issues and PRs welcome. Skim the architectural notes above before proposing significant structural changes.
