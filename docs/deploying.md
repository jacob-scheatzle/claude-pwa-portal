# Deploying ProgressiveWebAppPortal

This guide walks through deploying the portal on a small VPS. A box with **1 vCPU and 1–2 GB of RAM** is plenty for a single-business deployment with a handful of apps.

## Quickstart (no source clone required)

If you just want a running portal and don't need to modify the source:

```bash
# On the VPS, in a fresh directory:
mkdir my-portal && cd my-portal

# 1. Download the production compose file + env template
curl -O https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/.env.example

# 2. Edit .env — set SITE_URL to your domain (or <ip>.sslip.io),
#    paste a SECRET_KEY generated with:
#      python3 -c "import secrets; print(secrets.token_urlsafe(32))"
nano .env
chmod 600 .env

# 3. Pre-create the data dir owned by the container's runtime uid
mkdir -p data && sudo chown -R 1001:1001 data

# 4. Pull and run
docker compose pull
docker compose up -d
docker compose logs -f
```

When you see `Application startup complete`, visit `https://<SITE_URL>/` and walk through the first-run wizard.

**To upgrade later:** `docker compose pull && docker compose up -d` from the same directory.

**To pin a version** (recommended once you've tested an upgrade): add `PORTAL_IMAGE_TAG=v0.1.0` (or `sha-<short>`) to `.env`. Default is `latest`.

If you need wildcard DNS for per-app origin isolation (the default), see [section 2.6 below](#26-set-up-wildcard-dns-for-child-apps).

If you're putting this behind a load balancer that terminates TLS, or you want to run plain HTTP for local testing, see [section 2.7: HTTP-only mode](#27-optional-http-only-mode).

If you want to build from source instead (for development or local patches), skip the Quickstart and use the **Build from source** section after the troubleshooting block.

---

## Prerequisites

- A Linux VPS with SSH access (DigitalOcean, Hetzner, Vultr, etc.)
- **Docker** and **Docker Compose** installed
- Ports 80 and 443 open
- One of:
  - A **domain you control**, with an A record pointing at your VPS IP — gives you free auto-renewing HTTPS via Let's Encrypt
  - Your **VPS public IP**, used as `<ip>.sslip.io` — sslip.io is a free wildcard DNS service that resolves any `<ip>.sslip.io` back to the IP, so Let's Encrypt can issue you a cert without a real domain
  - `localhost` — local dev only, HTTP

## Build from source

This path is for developers who want to modify the portal. Production deployers should use the **Quickstart** above.

## 1. Get the code

```bash
ssh you@your-vps
git clone https://github.com/jacob-scheatzle/claude-pwa-portal.git
cd claude-pwa-portal
```

The cloned repo includes `docker-compose.override.yml`, which auto-merges with `docker-compose.yml` to make `docker compose up` build the portal image locally instead of pulling from GHCR. From here, the rest of the steps below apply the same way as for a from-source build.

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

## 2.6. Set up wildcard DNS for child apps

Child apps are served from a per-app subdomain — `<slug>.apps.<SITE_URL>` — so each app has its own browser origin. This requires a wildcard DNS A record:

```
*.apps.<your-domain>     A     <VPS IP>
```

Most DNS providers support wildcards. If you're using sslip.io (`<ip>.sslip.io`), you don't need to do anything — `<slug>.apps.<ip>.sslip.io` already resolves to the IP.

Caddy will fetch a Let's Encrypt cert for each subdomain on first access using HTTP-01 challenges; no DNS provider credentials needed. The portal protects against cert-issuance probing via an `/internal/cert-ask` endpoint that only approves real app slugs.

> ### ⚠️ sslip.io's Let's Encrypt rate limit
>
> Let's Encrypt enforces a **250,000 certificates per registered domain
> per 168 hours** rate limit. For `sslip.io`, the "registered domain" is
> `sslip.io` itself — **shared across every user of the service**. When
> the global pool is exhausted, Caddy can't get new certs for any
> `*.sslip.io` subdomain, and child-app subdomains fail to load with a
> browser "the content is blocked" / "your connection is not secure"
> error. You'll see this in the `caddy-1` logs as:
>
> ```
> HTTP 429 urn:ietf:params:acme:error:rateLimited — too many certificates
> (250000) already issued for "sslip.io" in the last 168h0m0s
> ```
>
> It's not something you've done wrong — somebody else (or many somebodies)
> burned the shared quota that week. The status is at
> [letsencrypt.status.io](https://letsencrypt.status.io/) but the
> per-domain counter isn't public.
>
> **If this happens to you**, you have two options:
>
> 1. **Flip to same-origin mode** (immediate, no DNS change):
>    ```bash
>    echo "CHILD_APPS_SAME_ORIGIN=true" >> .env
>    sudo docker compose up -d
>    ```
>    Apps then serve from `<SITE_URL>/apps/<slug>/` on the portal origin,
>    sharing one cert with the portal shell. The admin dashboard shows a
>    banner reminding you isolation is off; for a single-tenant portal
>    where you upload every app yourself, that trade-off is usually fine.
>
> 2. **Point a real domain at the VPS** (proper fix). Any domain you
>    control gets its own 250k/week quota that nobody else can exhaust.
>    Set `SITE_URL` in `.env` to that domain, add a wildcard A record for
>    `*.apps.<your-domain>`, and you can stay in per-app-origin mode with
>    full isolation.
>
> For non-toy production deployments, option 2 is the right answer regardless
> of whether sslip.io is currently rate-limited — relying on a shared free
> dynamic-DNS service for the load-bearing piece of your security model is a
> "works until it doesn't" arrangement.

> ### ⚠️ Cloud-provider VPS hostnames have the same problem
>
> Most VPS providers hand you a hostname for the new machine — OVH gives
> you something like `vps-afface1f.vps.ovh.us`, Linode gives you
> `<id>.ip.linodeusercontent.com`, DigitalOcean droplets get
> `<region>.cluster.digitalocean.com` aliases, and so on. **These look
> like usable domain names but they're not yours.** The provider owns
> the parent zone (`vps.ovh.us`, `ip.linodeusercontent.com`, etc.) and
> publishes exactly one A record — the bare hostname pointing at your
> machine.
>
> You **cannot** add a wildcard `*.apps.<provider-hostname>` record
> because you don't control the DNS zone. So per-app subdomains like
> `<slug>.apps.vps-afface1f.vps.ovh.us` return **NXDOMAIN**, the iframe
> wrapper fails to connect, and the browser shows "the content is
> blocked" / "this site can't be reached" inside the app launcher.
>
> Quick check from anywhere:
>
> ```bash
> dig +short <slug>.apps.<your-vps-hostname>
> ```
>
> If that comes back empty (compare to a `dig +short <your-vps-hostname>`
> which returns the IP), per-app-origin mode can't work on this hostname.
>
> **Same two ways out as the sslip.io case:**
>
> 1. **Flip to same-origin mode** — works immediately, no domain needed:
>    ```bash
>    echo "CHILD_APPS_SAME_ORIGIN=true" >> .env
>    sudo docker compose up -d
>    ```
>
> 2. **Buy a real domain (~$10/year)** at any registrar
>    (Cloudflare Registrar, Porkbun, Namecheap), point an A record at the
>    VPS IP, add a wildcard `*.apps.<your-domain>` to the same IP, and
>    set `SITE_URL=portal.<your-domain>`. That's the only path that keeps
>    per-app-origin isolation working long-term — and it gets you your own
>    Let's Encrypt quota into the bargain.
>
> Provider-issued hostnames are fine for the **portal itself** (you only
> need one A record there), so a stack running entirely in same-origin
> mode is perfectly viable on the free hostname. The wildcard requirement
> only kicks in when you want per-app-origin isolation.

If you'd rather skip this step entirely, set `CHILD_APPS_SAME_ORIGIN=true` in `.env`. The portal will then serve apps at `<SITE_URL>/apps/<slug>/` (same origin) with a security warning in the admin UI.

## 2.7. Optional: HTTP-only mode

The default deployment terminates TLS in the bundled Caddy and gets Let's Encrypt certs automatically. If that doesn't fit, set `HTTP_ONLY=true` in `.env` to make Caddy serve plain HTTP instead. Two situations this is meant for:

### Behind a load balancer that terminates TLS

If you're putting this stack behind a cloud load balancer (AWS ALB, Cloudflare, nginx on a separate box, etc.) that already handles HTTPS, you don't want Caddy fighting it for port 443 or fetching its own certs. Set:

```ini
HTTP_ONLY=true
COOKIES_SECURE=true   # client → LB is still HTTPS, so cookies stay Secure-flagged
SITE_URL=portal.example.com   # the public hostname your users see at the LB
```

The LB should forward to the portal VPS on port 80 and set `X-Forwarded-Proto: https`. uvicorn already trusts that header (we set `--proxy-headers --forwarded-allow-ips=*` in the container CMD), so cookies, launch-redirect URLs, and CSP `frame-ancestors` all come out HTTPS at the browser.

Port 443 in `docker-compose.yml` is harmless to leave (Caddy won't bind it in HTTP_ONLY mode) — comment it out if something else on the host needs that port.

### Local testing without self-signed cert warnings

For dev or strictly-internal use:

```ini
HTTP_ONLY=true
COOKIES_SECURE=false   # browser will refuse Secure cookies over plain HTTP
SITE_URL=localhost     # or lvh.me for per-app subdomain testing
```

The portal is then reachable at `http://localhost/` with no cert warnings.

### What HTTP_ONLY actually changes

- The bundled Caddy boots from `Caddyfile.http` instead of `Caddyfile`. It listens on port 80 only, never requests Let's Encrypt certs, and emits no HSTS header.
- The portal process itself is unchanged. All scheme decisions (cookie `Secure` attribute, launch-redirect URL) cascade through `COOKIES_SECURE`, so the right combination depends on whether you're behind a TLS-terminating proxy.

### Common gotchas in HTTP-only mode

Three failure modes that almost every first-time deployer hits at least one of:

- **`SITE_URL=localhost` (the default) but you're hitting a real IP/hostname.** Caddy's site address block is keyed on `{$SITE_URL}` — if the request's `Host` header doesn't match, the request lands in Caddy's default empty site and the browser sees a blank response with no access log entry. **Fix:** set `SITE_URL` to the exact host you're typing in the browser (the EC2 public IP, an `<ip>.sslip.io` form, or a real domain), then `docker compose up -d` to recreate the containers with the new env.

- **`COOKIES_SECURE=true` (the .env.example default) with `HTTP_ONLY=true` and *no* TLS-terminating proxy.** The portal sets the `Secure` flag on every session cookie and renders the per-app iframe URL as `https://...` — but Caddy is only on port 80, so the iframe is blank and login looks broken on the next request. **Tell:** the portal dashboard loads fine on first visit, then any action that depends on the session or any app you click into shows nothing. **Fix:** set `COOKIES_SECURE=false` in `.env` and recreate (`docker compose up -d`). The portal also logs a warning at startup when this combo is set — check `docker compose logs portal` for `HTTP_ONLY=true with COOKIES_SECURE=true: ...`.

- **No wildcard DNS for `*.apps.<SITE_URL>` (subdomain mode).** Per-app subdomains need to resolve to the same IP. With a bare-IP `SITE_URL` or a real domain without a `*.apps.<domain>` A record, the iframe blanks on `ERR_NAME_NOT_RESOLVED`. **Fix:** use `SITE_URL=<ip>.sslip.io` — sslip.io's wildcard covers `*.<ip>.sslip.io` for free — or set up the wildcard A record yourself. As a workaround for an internal test box, `CHILD_APPS_SAME_ORIGIN=true` keeps apps on the portal origin and skips the DNS requirement entirely (you'll see a security-warning banner in the admin UI).

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

## Optional: fail2ban

Once the portal is publicly reachable, brute-force login attempts and
scanner traffic start within minutes. The repo ships drop-in
[`fail2ban`](https://www.fail2ban.org/) configs that ban offenders at
the host firewall — see [fail2ban.md](fail2ban.md) for the full walkthrough.

Three layers, all safe to enable together:

- **Portal login jail** — reads `./data/security.log` (the focused
  fail2ban-friendly text log written by `portal/audit.py`); bans IPs
  after 5 failed `/login` POSTs in 10 minutes. Scoped to `http,https` so
  it doesn't lock SSH out.
- **Caddy scanner jail** — reads Caddy's JSON access log from journald;
  bans IPs probing for `/.env`, `/.git/`, `/wp-admin/`, `/actuator/`,
  `/boaform/`, etc. 60+ bait paths covering observed real-world
  scanner traffic.
- **sshd jail** — package-shipped filter watches the sshd journal;
  bans IPs after 5 failed auth attempts. Default `port = 22`; override
  in `jail.local` if you're running sshd on a non-standard port (which
  cuts roughly 99% of brute-force noise on its own).

The portal log is the high-value one (it's the only thing standing
between an attacker and an admin account); the Caddy layer is easy
hygiene that keeps scanner garbage out of your logs; the sshd layer is
table stakes.

> ### ⚠️ Required: `userland-proxy: false` for real client IPs
>
> All three jails depend on Caddy and the portal seeing the **real client
> IP**, not Docker's bridge gateway (`172.18.0.1`). When Docker's
> ``userland-proxy`` mode is enabled (the default on some installs),
> incoming connections to a published port get accepted by a
> ``docker-proxy`` process on the host and **then** re-opened to the
> container from the bridge gateway — the real client IP is lost
> before Caddy ever sees it, so X-Forwarded-For is set to
> `172.18.0.1`, fail2ban bans the bridge gateway (i.e. nothing useful),
> and the audit log records every login as coming from inside.
>
> Symptom: `data/security.log` shows `ip=172.18.0.1` for every entry
> even though you can reach the portal from the public internet.
>
> **Fix:** create or edit `/etc/docker/daemon.json` on the VPS:
>
> ```bash
> sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
> {
>   "userland-proxy": false
> }
> EOF
>
> sudo systemctl restart docker
> ```
>
> This makes Docker rely on iptables DNAT alone, which preserves source
> IPs end-to-end. Side effects: slightly different IPv6 behaviour, no
> ability to publish two processes on the same host port (neither
> matters for this stack).
>
> Verify after restart with one external request from your laptop:
>
> ```bash
> curl https://<your-portal>/health
> ```
>
> Then on the VPS:
>
> ```bash
> sudo journalctl -t pwa-portal-caddy --since "1 min ago" | \
>   grep -oP '"client_ip":"[^"]*"' | head -1
> ```
>
> Should print your laptop's real public IP, not `172.18.0.1`.

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

Alembic runs `upgrade head` automatically on container start, applying any pending schema migrations shipped with the release. If a migration fails, the container exits with a clear error in `docker compose logs portal` — fix forward (or roll back the image), then restart. To roll back the schema itself, exec into the container and run `alembic downgrade <revision>` (you'll need to know the previous revision id from `alembic history`).

**Always take a backup of `data/portal.db` before any update** (see [Backups](#backups)). Migrations alter table structure; a bad migration on an un-backed-up DB is hard to recover from.

### Migrations

- Schema migrations live in `alembic/versions/` and ship with releases. They run automatically when the container starts; no manual step is needed during a normal upgrade.
- If you're extending the portal yourself, after changing a model in `portal/models.py`:
  ```bash
  .venv/bin/alembic revision --autogenerate -m "describe change"
  ```
  Run this from the repo root with the venv active.
- **Always inspect the generated migration before committing it.** Alembic's autogenerate is best-effort and can miss things like column-type changes, server-side defaults, custom `sa.JSON` columns, and renames-vs-drop+add. Open the new file in `alembic/versions/`, sanity-check the `upgrade()`/`downgrade()` ops against the diff you intended, and edit by hand if needed.

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

**Child-app subdomain returns "no such host" / cert error**
- Verify wildcard DNS: `dig <random>.apps.your-domain.com` should return your VPS IP.
- Check Caddy logs: `docker compose logs caddy | grep cert-ask` to see whether `/internal/cert-ask` is being called.
- If you don't want subdomain isolation, set `CHILD_APPS_SAME_ORIGIN=true` and rebuild.

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
