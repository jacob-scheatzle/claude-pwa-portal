# Deploying ProgressiveWebAppPortal

This guide walks through deploying the portal on a small VPS. A box with **1 vCPU and 1–2 GB of RAM** is plenty for a single-business deployment with a handful of apps.

## Prerequisites

- A Linux VPS with SSH access (DigitalOcean, Hetzner, Vultr, etc.)
- **Docker** and **Docker Compose** installed
- Ports 80 and 443 open
- One of:
  - A **domain you control**, with an A record pointing at your VPS IP — gives you free auto-renewing HTTPS via Let's Encrypt
  - Your **VPS public IP**, used as `<ip>.sslip.io` — sslip.io is a free wildcard DNS service that resolves any `<ip>.sslip.io` back to the IP, so Let's Encrypt can issue you a cert without a real domain
  - `localhost` — local dev only, HTTP

## 1. Get the code

```bash
ssh you@your-vps
git clone https://github.com/<your-org>/ProgressiveWebAppPortal.git
cd ProgressiveWebAppPortal
```

## 2. Configure

```bash
cp .env.example .env
```

Open `.env` and set:

```ini
# Public hostname/URL.
# Examples:
#   portal.example.com          (your own domain)
#   123.45.67.89.sslip.io       (no domain — uses VPS IP)
#   localhost                   (local dev, HTTP only)
SITE_URL=portal.example.com

# Random 32+ char secret. Generate with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
SECRET_KEY=<paste here>

# Set to true once you're running behind HTTPS.
COOKIES_SECURE=true

# Optional pre-config — admins can also fill this in via Settings later.
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_USE_TLS=true
```

If you're using a custom domain, point its A record at your VPS IP *before* the first `docker compose up`. Caddy will request a Let's Encrypt cert on startup.

## 2.5. Secure the env file

`.env` holds your `SECRET_KEY` and (optionally) SMTP credentials. Lock it down so only the deploying user can read it:

```bash
chmod 600 .env
```

Anyone with read access to this file can forge session cookies and read your SMTP password.

## 3. Boot it

```bash
docker compose up --build -d
```

Tail logs to watch cert provisioning + startup:

```bash
docker compose logs -f
```

When you see `Application startup complete`, visit `https://<SITE_URL>/`. You should land on the first-run wizard.

## 4. First-run wizard

The wizard creates the initial **admin** account. After submitting, you'll land on the dashboard, with the topbar showing admin nav links.

## 5. Configure SMTP

Visit **Settings** in the admin nav. Fill in SMTP host/port/username/password/from address.

Save, then click **Send test email to my account** to confirm delivery before staff start relying on email-sending apps.

Common SMTP providers for small business use:

- **AWS SES** — pay per email, cheap, transparent pricing; needs domain/sender verification
- **Mailgun** — free tier covers most small orgs
- **Postmark** — solid transactional deliverability
- **Gmail** — works for low volume via SMTP relay (`smtp.gmail.com:587` with an app password)

## 6. Add staff users

Visit **Users**. Add an email + initial password for each staff member; share the password out of band. They sign in at `<SITE_URL>/login`.

You can change roles, reset passwords, or delete users from this page. The portal prevents demoting or deleting the last remaining admin, and prevents you from deleting yourself.

## 7. Upload apps

Two paths:

- **Web UI:** **Apps → Upload** in the admin nav. Drop in a `.zip` whose root contains `portal.json`.
- **Claude skill:** see [app-authoring.md](app-authoring.md) and the skill's [SKILL.md](../claude-skill/pwa-portal-app/SKILL.md).

## 8. Install on a phone

On iOS Safari: open the portal URL → tap **Share** → **Add to Home Screen** → name it → **Add**. Tapping the home screen icon launches it fullscreen. Each staff member does this once per device.

On Chrome desktop, look for the "install app" affordance in the address bar.

## Backups

All persistent state is in the `./data/` directory:

```
data/portal.db                          SQLite — users, settings, apps, tokens
data/apps/<slug>/                       Extracted child-app bundles
data/storage/<slug>/<user_id>/<key>     Per-app, per-user storage
```

For a small portal, a nightly `rsync` is fine:

```bash
# Stop the portal briefly during copy for SQLite safety
docker compose stop portal
rsync -a --delete data/ backup-host:/backups/portal-$(date +%F)/
docker compose start portal
```

For zero-downtime backups, take a SQLite online-backup of `data/portal.db` and rsync `data/apps/` + `data/storage/` separately.

## Updates

```bash
cd ProgressiveWebAppPortal
git pull
docker compose up --build -d
```

SQLModel applies non-destructive schema migrations on startup (new tables get added; existing tables are not altered). Take a backup before any update.

## CLI for admin emergencies

If you can't reach the UI (forgotten admin password, broken email, etc.) you can reach the SQLite DB through the running container:

```bash
docker compose exec portal python -m portal.cli list-users
docker compose exec portal python -m portal.cli reset-password admin@example.com
```

The reset command prompts you for the new password.

## Troubleshooting

**Caddy can't get a certificate**
- Verify ports 80 and 443 are open on the VPS
- Verify your domain's A record points at the VPS IP (`dig +short <your-domain>`)
- Tail Caddy logs: `docker compose logs caddy`
- If using sslip.io, make sure `SITE_URL` is exactly `<ip>.sslip.io` (no protocol prefix, no path)

**Portal returns 500 on the dashboard, logs show WeasyPrint import error**
- Pango/Cairo libs missing from the container — open an issue; we ship them by default

**SMTP test fails immediately with a DNS error**
- The host string is wrong, or the VPS can't resolve external DNS. Try `docker compose exec portal getent hosts <smtp-host>`

**`/api/v1/email/send` returns 503 even though Settings looks right**
- An admin saved an empty `SMTP_HOST` after the test — empty values clear the row and fall back to `.env`. Re-enter the host in **Settings** and save.

**Service worker keeps serving stale assets after a deploy**
- Hard-refresh once (Cmd+Shift+R / Ctrl+Shift+R). The portal SW is network-first, so the next reload picks up the new version automatically.

**iPhone Add-to-Home-Screen icon is wrong / blurry**
- Replace `portal/static/icons/apple-touch-icon.png` with your own 180×180 PNG, rebuild the container.

## Hardening notes

- **Secret rotation:** rotating `SECRET_KEY` invalidates every signed cookie — all users have to sign in again. Plan for it.
- **Token blast radius:** API tokens act as the user who created them. Treat admin-created tokens like admin passwords; revoke unused ones.
- **Backups encryption:** SQLite files contain hashed passwords (bcrypt) but also plaintext SMTP credentials. Encrypt backup destinations.
- **Outbound network:** if your VPS firewall blocks outbound traffic, allow your SMTP host on the relevant port (usually 587 or 465). Public SMTP usage is often blocked on residential ISPs; cloud VPSes are typically fine.

## Production hardening checklist

Run through these before you point real users at the portal:

- [ ] `chmod 600 .env` — only the deploying user can read secrets.
- [ ] `COOKIES_SECURE=true` in `.env` (now the default in `.env.example`).
- [ ] SMTP password is encrypted at rest in the SQLite DB (automatic — nothing to configure).
- [ ] Back up `data/` regularly (see [Backups](#backups) above).
- [ ] Plan for `SECRET_KEY` rotation — it boots every user out, so ideally only rotate on confirmed compromise.
- [ ] Restrict outbound network on the VPS to your SMTP host(s) (egress firewall / security group).
- [ ] Don't share the host with untrusted users — the SQLite DB on disk contains SMTP credentials (encrypted, but the decryption key lives next to it in `.env`).
- [ ] The Dockerfile now runs as UID 1001. The `data/` directory on the host must be writable by that uid; if you see permission errors after first boot, run `chown -R 1001:1001 data/` on the host and `docker compose restart portal`.
- [ ] *(Optional, further hardening)* Try `read_only: true` on the portal service in `docker-compose.yml`. The container already gets `tmpfs: [/tmp]` for scratch space; `/data` stays writable via the bind mount. Test thoroughly before relying on it — some libraries write outside the expected paths.
