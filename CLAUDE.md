# Claude context for ProgressiveWebAppPortal

You're working in a self-hostable, single-tenant **PWA portal** for small
businesses. FastAPI + SQLite + Caddy in docker-compose. Admins upload child
PWAs ("apps") as `.zip` bundles; the portal serves them, exposes a JS SDK
that gives apps access to server-side PDF / email / per-user storage, and
ships with a Claude skill so non-coders can build apps conversationally.

If you only read one other thing, read [docs/project-state.md](docs/project-state.md) —
it's the session-state snapshot with bring-up recipes, what's open, and the
working notes that would otherwise be unrecoverable from commit messages.

---

## Architecture at a glance

```
portal/
  main.py             FastAPI app factory, lifespan, auth routes, /profile, login rate limit
  api.py              /api/v1/* (JSON; cookie + bearer auth; storage / pdf / email)
  apps.py             /admin/apps/* + /apps/<slug>/* + child-app zip upload/validation/serving
  admin.py            /admin/{settings,tokens,users} (HTML form routes)
  mcp_server.py       Optional /mcp MCP server (low-level Server, dynamic list_tools) — app-mgmt tools + each app's declared tools; admin-bearer ASGI auth; gated by MCP_ENABLED + [mcp] extra
  app_tools.py        Phase 2 executor — runs an app's declared tools (sandboxed-Jinja template → PDF → share/email/store/download) over trusted primitives
  models.py           SQLModel tables: User, Setting, App, ApiToken, UserSession
  sessions.py         Helpers for the server-side UserSession (create/revoke/touch)
  security.py         bcrypt, password validation, csrf_token() + check_csrf() + check_csrf_header()
  smtp.py             send_message() — used by api.email_send and admin SMTP-test
  settings_store.py   get_setting/set_setting (plain) + get_secret/set_secret (Fernet-encrypted)
  deps.py             current_user (cookie), current_user_or_token (cookie + bearer), authenticate_bearer (shared token→user lookup)
  config.py           Pydantic Settings; SITE_URL, SECRET_KEY, COOKIES_SECURE, SMTP_*
  db.py               engine + init_db() (runs `alembic upgrade head`; auto-stamps pre-Alembic DBs)
  web.py              Shared `templates`, `render()` helper (auto-injects user, flashes, csrf_token)
  cli.py              `python -m portal.cli {list-users,reset-password EMAIL}`
  templates/          Jinja2; all forms include {{ csrf_token }}
  static/             portal-sdk.js, sw.js, manifest.webmanifest, icons/

alembic/              Migrations; initial revision is 7d3122820cf2
claude-skill/         The pwa-portal-app skill (SKILL.md + templates + scripts)
examples/             hello-receipt — reference child app using every SDK service
docs/                 deploying, app-authoring, api-reference, project-state, per-app-origin-design
```

---

## Critical gotchas (read before touching code)

1. **Python 3.14 + Jinja2.** The old `templates.TemplateResponse("x.html", {"request": request})` form throws `TypeError: cannot use 'tuple' as a dict key` on Python 3.14. Always use `templates.TemplateResponse(request, "x.html", {...})`. The `render()` helper in `portal/web.py` already does this.

2. **Never call `SQLModel.metadata.create_all` again.** Schema is managed by Alembic. `init_db()` runs `alembic upgrade head` on boot. To change the schema: edit `portal/models.py`, then `.venv/bin/alembic revision --autogenerate -m "<msg>"`, **inspect the generated file** (autogenerate misses things — server defaults, custom column types, etc.), commit it. The `alembic/script.py.mako` template auto-imports `sqlmodel`; don't remove that.

3. **CSRF is everywhere.** Every form POST template includes `<input type="hidden" name="_csrf" value="{{ csrf_token }}">`. Every POST handler accepts `csrf: Annotated[str, Form(alias="_csrf")] = ""` (the `alias=` is mandatory — Pydantic forbids leading-underscore field names). JSON-body `/api/v1/*` endpoints use `_require_csrf_for_cookie(request, x_csrf)` which skips when `request.state.auth_method == "token"`. New state-changing endpoints MUST include CSRF.

4. **`request.state.auth_method`** is set to `"cookie"` or `"token"` (or unset for unauthenticated) by `current_user_or_token`. Any code that needs to differentiate (CSRF rules, slug resolution, etc.) reads this — **never inspect the `Authorization` header directly.** Doing so caused a critical bearer-spoof bypass we already fixed; don't reintroduce.

5. **Child apps run on per-app subdomains by default.** `<slug>.apps.<SITE_URL>` — different browser origin per app, isolated cookies, no shared access to the portal's session. The `/apps/<slug>/` portal-origin URL renders an iframe wrapper that loads the subdomain. Legacy same-origin mode is available via `CHILD_APPS_SAME_ORIGIN=true` for self-hosters without wildcard DNS — admins see a warning banner. Full design + rollout history in [docs/per-app-origin-design.md](docs/per-app-origin-design.md).

6. **SMTP password is Fernet-encrypted in DB.** Use `settings_store.set_secret(db, key, value)` to save it, `get_secret` to read. Plain `set_setting` for SMTP password writes plaintext — a footgun we already hit once.

7. **The Docker container runs as uid 1001.** Bind-mounted `./data/` on the host needs to be writable by 1001 — `sudo chown -R 1001:1001 data/` if you see EACCES errors on a fresh deploy.

8. **Caddy `localhost` issues an internal-CA cert.** For curl against the Docker stack, use `-k`. Browser will warn once.

9. **Don't break the SDK contract.** `portal/static/portal-sdk.js` exposes `window.portal.{user,pdf,email,storage}`. Child apps depend on this surface. The reference app at `examples/hello-receipt/` exercises every method — if a change there breaks, the SDK changed in a breaking way.

10. **Async carefully.** Many handlers are `def` (FastAPI threadpools them); a few are `async def` (`install_bundle` uses `anyio.to_thread.run_sync` for the blocking zip work). Don't flip one to the other without checking what calls it. `_smtp_send` has a 10s timeout — don't make it longer.

---

## Common commands

### Run the portal locally without Docker
```bash
.venv/bin/pip install -e .
cp .env.example .env  # set SECRET_KEY; for HTTP-only local dev, set COOKIES_SECURE=false
PYTHONPATH=. .venv/bin/uvicorn portal.main:app --host 127.0.0.1 --port 8000 --reload
```

### Run via Docker
```bash
docker compose up --build -d
docker compose logs -f portal
docker compose down       # stop, keep data
docker compose down -v    # also remove Caddy volumes
# After any up/--build/pull, reclaim disk left by dangling images + build cache:
./contrib/scripts/portal-docker-prune.sh
```

**Always prune after bringing the stack up** (`up`, `up --build`, or `pull` +
`up`). `contrib/scripts/portal-docker-prune.sh` is the safe sweep — dangling
images + stopped containers + build cache only. **Never** prune with
`--volumes` (Caddy's TLS certs live in `caddy_data`/`caddy_config`) or with
`-a`/`--all` on a shared host. Deployers schedule the same script on a weekly
cron; see `contrib/scripts/README.md`.

### Smoke-test the running stack
```bash
# Through Caddy (HTTPS, self-signed for localhost)
curl -ksL https://localhost/health
# Direct to portal container (HTTP)
curl http://127.0.0.1:8000/health
```

### Alembic
```bash
.venv/bin/alembic current                       # what rev is the DB at?
.venv/bin/alembic upgrade head                  # apply pending migrations
.venv/bin/alembic revision --autogenerate -m "describe change"
.venv/bin/alembic downgrade -1                  # roll back one step
```

### Build + upload a child app via the skill
```bash
bash claude-skill/pwa-portal-app/install.sh
python3 ~/.claude/skills/pwa-portal-app/scripts/configure.py
# Now in Claude Code: "build me a [whatever] for my portal"
# Or manually:
python3 claude-skill/pwa-portal-app/scripts/package.py path/to/myapp
PORTAL_URL=https://localhost PORTAL_TOKEN=... \
  python3 claude-skill/pwa-portal-app/scripts/upload.py path/to/myapp-0.1.0.zip
# Add --replace to update an existing app in-place (preserves storage)
```

### Reset an admin password without UI access
```bash
docker compose exec portal python -m portal.cli reset-password admin@example.com
```

---

## Code conventions

- **Server-rendered HTML, no JS framework.** Jinja2 + light forms. Don't add React/Vue/htmx unless really necessary.
- **Jinja autoescape is on.** Never use `|safe` or `Markup(...)` on DB values. If you need to render HTML, the value must come from a fixed source.
- **Imports inside `from portal.X import Y` style.** Models import from `sqlmodel`, db from `portal.db`, etc. — see existing files.
- **No `print()` for logging.** Use the FastAPI / uvicorn logger if you need to log.
- **All datetimes are UTC.** `_utcnow()` in `models.py`; never use naive `datetime.now()`.
- **Settings via the `settings` object in `portal.config`** — never read env vars directly in handlers.
- **Path-traversal safety:** any code touching user-supplied filenames must do `.resolve()` then `relative_to(base)` check. Patterns in `portal/apps.py:_safe_extract`, `portal/api.py:storage_*`.
- **Don't add new top-level dependencies casually.** Each one is in `pyproject.toml`; if you add `cryptography` or `alembic`, the Docker image rebuilds. We're at 11 deps; aim to stay under ~15.

---

## Working with multiple agents in parallel

If you spawn parallel agents to fix things across the codebase (we did this twice in the session that built this repo), give each agent **strict file ownership** in the prompt — explicitly list "you may modify these files" and "do NOT touch these files." The previous rounds hit conflicts when two agents both edited `portal/apps.py` or `portal/templates/*.html`. The clean partition that worked:

- Agent A: `portal/api.py`, `portal/smtp.py`, `portal/config.py`, `portal/settings_store.py`, `.env.example`
- Agent B: `portal/deps.py`, `portal/security.py`, `portal/main.py`, `portal/admin.py`, `portal/web.py`, `portal/templates/*.html`
- Agent C: `portal/apps.py`, `portal/models.py`, `portal/static/portal-sdk.js`, `claude-skill/`, `examples/`
- Agent D: `Dockerfile`, `Caddyfile`, `docker-compose.yml`, `docs/deploying.md`

Worktree isolation is preferred (`isolation: "worktree"` on Agent tool calls) — it gives each agent a real branch and you merge afterward. If worktrees aren't available, tell agents explicitly NOT to `git add` / `commit` / `push` and commit yourself after verifying the cumulative diff.

---

## When picking up work — checklist

Before making changes:

1. Read [docs/project-state.md](docs/project-state.md) for what's open.
2. `git log --oneline | head -15` for recent commits.
3. `git status` — should be clean unless you just resumed.
4. If you're going to change models, the next step is `alembic revision --autogenerate`. Don't forget.
5. If you're going to change template forms, every POST form needs `{{ csrf_token }}`.
6. If you're going to add a new state-changing endpoint, decide cookie vs bearer (or both); CSRF gating follows.

Before committing:

1. `.venv/bin/python -c "from portal.main import app; print(len(app.routes))"` — imports clean? expected route count?
2. Run the smoke recipe from project-state.md if the change is non-trivial.
3. Commit messages should explain **why**, not just **what**. The existing commits set the bar.
4. Never `--no-verify` past hooks. Never amend a commit that's already pushed.

---

## Status pointer

All four "before initial testing" items are shipped, including the per-app
origin work (commits `7930fec` → `a47d22b`, plus a final default flip). The
default is now per-app subdomain isolation; legacy same-origin remains as
an opt-out via `CHILD_APPS_SAME_ORIGIN=true`. See
[docs/per-app-origin-design.md](docs/per-app-origin-design.md) for the
architectural context if you're touching the launch / exchange / Host
dispatch code paths.

An **MCP app-management server** lives at `portal/mcp_server.py`, served at
`/mcp` via exact routes (not `app.mount` — the catch-all GET would shadow it).
The Docker image bundles the `mcp` dep and `mcp_enabled` defaults to **auto**
(`Optional[bool]=None` → on when importable), so the container comes up with
`/mcp` live; `MCP_ENABLED=false` disables it, `true` forces it. It exposes
admin-token-authed tools (`whoami`, `list_apps`, `get_app`, `upload_app`,
`set_app_enabled`, and `authoring_guide` — a self-contained authoring spec for
MCP-only clients) that wrap existing internals. Auth reuses
`deps.authenticate_bearer`. See [docs/mcp.md](docs/mcp.md). **Phase 2 is
implemented:** apps declare a `tools` DSL in `portal.json` (validated in
`apps.py`: name/params/render/deliver — params are scalar or `array` line-item
objects — with a cross-check that each tool's
services are declared), stored in the new `App.tools` JSON column (migration
`665c77fdc151`); `portal/app_tools.py` runs them over trusted primitives; and
`mcp_server.py` surfaces each enabled app's tools dynamically as `<slug>__<tool>`
(its `list_tools` reads the DB per request, so no restart/notification needed).
