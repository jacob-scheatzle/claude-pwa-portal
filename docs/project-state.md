# Project state — May 21, 2026

A snapshot of where ProgressiveWebAppPortal stands at the end of a long
build + two review-fix sweeps. Read this first if you're picking the
project up on a different machine or after a break.

---

## TL;DR

- **Where we are:** all 9 original build milestones shipped, plus a full
  code review, a security review, two parallel fix sweeps, and 3 of the
  4 "before initial testing" follow-ups. The 4th (per-app origin
  isolation) is designed but not implemented.
- **The repo is live on GitHub** at
  `https://github.com/jacob-scheatzle/claude-pwa-portal` (private).
  Main branch is `main`.
- **The codebase is solid enough for solo testing on a real VPS.** What's
  unbuilt is what you need before inviting a second person to upload apps.
- **Big decisions are documented**, not just in commit messages. The
  per-app-origin design lives in [per-app-origin-design.md](per-app-origin-design.md);
  this file is the meta-status.

---

## What got built (9 milestones)

A self-hostable PWA portal targeting small businesses. One business per
deployment; staff are users; admins can upload child PWAs ("apps") that
all live behind one auth boundary and one installable PWA icon on the iPhone
home screen.

Components:

| Layer | What |
|---|---|
| Portal backend | FastAPI + SQLite + Caddy in docker-compose; sessions via Starlette's `SessionMiddleware` + a server-side `UserSession` table |
| Portal frontend | Server-rendered Jinja templates + light HTMX-friendly forms; no JS framework, no build step |
| Auth | Email + bcrypt password, admin/user roles, login rate limit, server-side sessions revocable on logout / password change |
| PWA shell | Manifest + service worker scoped to `/static/*`, iPhone Add-to-Home-Screen tested |
| Admin pages | Apps (upload, enable, replace, delete), Users (CRUD), Settings (SMTP, site URL), API tokens (one-time display) |
| Child-app API | `/api/v1/{user,pdf,email,storage,apps,csrf-token}` with cookie auth or `Authorization: Bearer <token>` |
| JS SDK | `/portal-sdk.js` exposes `portal.user.current()`, `.pdf.{render,download}()`, `.email.send()`, `.storage.{put,get,list,delete}()`; CSRF token fetched + sent on state-changing calls |
| Claude skill | `claude-skill/pwa-portal-app/` — SKILL.md + scaffolding template + stdlib-only `configure.py` / `package.py` / `upload.py` |
| Reference app | `examples/hello-receipt/` — exercises every SDK service |
| Migrations | Alembic with on-startup `upgrade head`; existing pre-Alembic DBs auto-stamped |
| Docs | Deploying, app-authoring, API reference, this file, the per-app-origin design |

Smoke-tested live: end-to-end PDF generation through Caddy via HTTPS,
session login + logout + revocation, in-place app replace preserving
per-user storage, Fernet-encrypted SMTP password at rest, CSRF blocking
unauthenticated state-changing calls.

---

## Commit history

```
4792f83 docs: design spec for per-app origin isolation (item 4)
108b7ea Adopt Alembic for schema migrations
97cec37 Server-side sessions + JSON API CSRF
0e68323 Second-pass review fixes: real encryption, bearer-spoof, session rotation
889cfd7 Address code review + security review findings
14b2429 README: document two skill-install paths (Claude Code vs claude.ai)
b575c69 Dockerfile: install Pango et al for WeasyPrint runtime
f1ccf18 Initial portal scaffold, Claude skill, reference app, and docs
```

Each commit message is detailed enough to stand on its own; `git show <hash>`
gives you the full context of what landed and why.

---

## The two reviews + fix sweeps, in one paragraph each

**First review (~60 findings):** uncovered the same-origin escalation
surface (malicious uploaded apps can read portal cookies, forge admin
calls), WeasyPrint SSRF on `/api/v1/pdf/render`, plaintext SMTP password
in DB, the client-controlled `X-Portal-App` storage spoofing vector, no
CSRF anywhere, Dockerfile-as-root, `COOKIES_SECURE=false` default, no
schema migrations, no logout invalidation, and a long tail of mediums
and nits. Five parallel agents fixed the contained issues; the
architectural ones (separate origin) were deferred.

**Second review (post-fix):** verified what landed and found that the
first sweep had introduced or missed four real bugs — the `_resolve_app_slug`
bearer-spoof bypass (a malicious `Authorization: Bearer x` header from a
cookie-authenticated child app re-opened the X-Portal-App vector), the
SMTP "encryption" was actually just `itsdangerous` signing (no encryption)
and wasn't being called by the admin save path anyway, mismatched-manifest
uploads to `/admin/apps/<slug>/replace` overwrote the WRONG app before
returning 400, and Uvicorn behind Caddy never saw real client IPs so the
login rate-limit collapsed. Four agents fixed these; verified end-to-end
that the bearer-spoof returns 400, the SMTP password lands in DB as
`enc:v2:gAAA...` (real Fernet AES-128-CBC+HMAC), and session cookies
rotate across the login boundary.

---

## "Before initial testing" — the 4-item follow-up queue

| # | Item | Status | Where |
|---|---|---|---|
| 1 | Server-side session table | ✅ Shipped | commit `97cec37`, `portal/sessions.py`, `portal/models.py:UserSession` |
| 2 | CSRF on JSON-body `/api/v1/*` | ✅ Shipped | commit `97cec37`, new `GET /api/v1/csrf-token`, SDK auto-fetches + retries |
| 3 | Alembic migrations | ✅ Shipped | commit `108b7ea`, initial revision `7d3122820cf2_initial_schema`, `portal/db.py:init_db()` auto-stamps pre-Alembic DBs |
| 4 | Separate origin per child app | 📋 Designed, not implemented | commit `4792f83`, full spec in [per-app-origin-design.md](per-app-origin-design.md), 5-phase rollout |

---

## What's still genuinely open

1. **LICENSE file.** Repo is public-ish (private GitHub for now) without
   one. MIT or Apache-2.0 are the conventional picks. README already has
   a placeholder note.
2. **First real-VPS deploy.** Everything works locally on this Mac under
   docker compose. The image build + Caddy auto-TLS on a real domain
   hasn't been exercised. Likely to surface: pango/Caddy/Caddyfile
   interactions you can't see without HTTPS + a real DNS name.
3. **Item 4 implementation.** Spec is locked, phasing is documented;
   when you're ready, the design doc is the brief.
4. **First schema-change ship.** Now that Alembic is in, the next
   time you add a column to a model, the workflow is
   `alembic revision --autogenerate -m "..."` then commit + deploy.
   `portal/db.py:init_db()` runs `upgrade head` on container boot.

---

## How to pick this up on another machine

### Clone + bring up

```bash
git clone https://github.com/jacob-scheatzle/claude-pwa-portal.git
cd claude-pwa-portal
cp .env.example .env
# Generate SECRET_KEY and paste:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Edit .env: set SECRET_KEY, set SITE_URL=localhost (or your domain)
docker compose up --build -d
docker compose logs -f portal  # watch boot; alembic upgrade head should run
```

Then visit `https://localhost/` (accept the self-signed cert). First-run
wizard creates the admin account.

### Or run without Docker (Python venv)

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env  # set SECRET_KEY, leave SITE_URL=localhost, set COOKIES_SECURE=false for local HTTP
PYTHONPATH=. .venv/bin/uvicorn portal.main:app --host 127.0.0.1 --port 8000
```

Visit `http://localhost:8000/`. First-run wizard creates the admin.

### Verify the state of a fresh deploy

```bash
# Health
curl https://localhost/health      # → {"status":"ok"}

# Confirm Alembic is in charge
.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('data/portal.db')
print('alembic_version:', con.execute('SELECT version_num FROM alembic_version').fetchall())
print('tables:', [r[0] for r in con.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()])
"
# Expect: ('7d3122820cf2',) and 6 tables including alembic_version
```

### Build + upload an app from Claude Code

```bash
bash claude-skill/pwa-portal-app/install.sh         # symlinks into ~/.claude/skills/
python3 ~/.claude/skills/pwa-portal-app/scripts/configure.py  # interactive; saves URL + token
# In Claude Code, ask: "Make me a [whatever] for my portal"
```

---

## Reference docs in this repo

| Doc | Read when |
|---|---|
| [../README.md](../README.md) | First-time orientation, project overview, install paths |
| [deploying.md](deploying.md) | Production deploy on a real VPS, SMTP setup, hardening checklist |
| [app-authoring.md](app-authoring.md) | Manually writing a child app (no Claude) — schema, SDK, packaging |
| [api-reference.md](api-reference.md) | HTTP API + SDK reference for child apps |
| [per-app-origin-design.md](per-app-origin-design.md) | Spec for item 4 (separate origin per child app) — read before implementing |
| [../claude-skill/pwa-portal-app/SKILL.md](../claude-skill/pwa-portal-app/SKILL.md) | What Claude knows when invoking the skill |

---

## Threat model (in one paragraph)

Single-tenant: one small business per deployment. Portal users are
trusted staff. **Child apps uploaded by admins are not trusted code** —
they're arbitrary HTML/JS served at portal-controlled URLs. Two auth
modes: same-origin session cookie (UI + child apps), and
`Authorization: Bearer <token>` (Claude skill + automation). The biggest
remaining structural risk is that uploaded apps currently run
**same-origin** with the portal, so a malicious app can read the portal
session and forge admin calls. Item 4 in the deferred queue (per-app
subdomain) is the architectural fix; today, the defense is "the admin
trusts what they upload." For solo testing this is fine; before
inviting a second admin, item 4 should ship.

---

## Things to double-check before a real-world deploy

1. **`LICENSE`** added.
2. **`.env` has a real `SECRET_KEY`** (32+ random chars from `secrets.token_urlsafe(32)`), **not** the placeholder. The portal will refuse to start with the placeholder if `SITE_URL != localhost`.
3. **`COOKIES_SECURE=true`** when serving over HTTPS.
4. **`chmod 600 .env`** so other users on the VPS can't read SMTP creds.
5. **DNS** points at the VPS; ports 80/443 are open.
6. **SMTP** configured via the admin UI (Settings) after first-run; click "Send test email" to confirm before depending on it.
7. **`data/` is backed up** (rsync, restic, whatever) since it contains the DB + every uploaded app + every user's per-app storage.
8. **The first real test app** is one *you* uploaded, not from a third party. Item 4 isn't shipped yet.

---

## Working notes — context that's hard to recover from commit history

- **Python 3.14 + Starlette Jinja2 quirk:** the old `TemplateResponse("name.html", {"request": request})` form throws "TypeError: cannot use 'tuple' as a dict key" on Python 3.14. All templates use the modern `TemplateResponse(request, "name.html", {...})` form. If you upgrade Starlette and see template errors, that's the failure mode.

- **WeasyPrint system libs:** Pango/HarfBuzz/Fontconfig/fonts-dejavu-core are apt-installed in the Dockerfile. On macOS local dev, `brew install pango` is required for WeasyPrint to import.

- **Caddy on localhost:** Caddy issues an internal CA cert for `localhost` and the browser will warn. For curl, use `-k`. The Dockerfile sets up Caddy to handle both `localhost` and real domains via `{$SITE_URL:localhost}` in the Caddyfile.

- **Docker + bind-mounted data/:** the container runs as uid 1001 (non-root) per the Dockerfile. If you bring up a new compose stack against an existing `./data/` directory owned by your host user, you'll see permission errors. Fix: `sudo chown -R 1001:1001 data/` or recreate the directory.

- **SDK CSRF token cache:** the SDK fetches `/api/v1/csrf-token` lazily on first state-changing call and caches it in module scope. On 403 it clears and retries once. If you ever see persistent 403s from API calls in a working portal, the cache is stale — reload the page.

- **Migration footgun:** `alembic revision --autogenerate` emits `sqlmodel.sql.sqltypes.AutoString` references but doesn't add `import sqlmodel`. The `alembic/script.py.mako` template was patched to auto-include it, so this is handled — but if you ever regenerate the mako template (e.g., upgrading Alembic and accepting the new default), re-apply the patch.

- **Existing dev DB on this Mac:** `data/portal.db` has an admin user from earlier setup (you'll know the email + password from the session). It's stamped at Alembic revision `7d3122820cf2`. Don't delete it unless you want to re-run the first-run wizard. On a fresh checkout (different machine), there is no DB — the wizard will run.

- **The Claude skill ships in the repo at `claude-skill/pwa-portal-app/`** — `install.sh` symlinks it into `~/.claude/skills/`. From any device with this repo cloned, running `bash claude-skill/pwa-portal-app/install.sh` plus `configure.py` reactivates the skill.

- **Two auth methods in the same dep:** `current_user_or_token` sets `request.state.auth_method` to `"cookie"` or `"token"`. Any new code that needs to differentiate (e.g., a new endpoint with different CSRF rules for browsers vs. bots) should read that, never inspect headers directly.
