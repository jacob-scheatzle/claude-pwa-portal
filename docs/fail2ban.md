# fail2ban integration

A guide to wiring [`fail2ban`](https://www.fail2ban.org/) into a
self-hosted PWA Portal deployment so brute-force login attempts and
scanner traffic get auto-banned at the host firewall.

The repo ships drop-in fail2ban config under [`contrib/fail2ban/`](../contrib/fail2ban/)
— you can copy those files directly onto the VPS or use this doc as a
reference for writing your own.

---

## TL;DR

Two layers, independently useful:

| Layer | What it bans | Reads | Latency to ban |
|---|---|---|---|
| **Portal login** | 5 failed `/login` POSTs in 10 min from the same IP | `data/security.log` written by `portal/audit.py` | Instant (fail2ban tails the file) |
| **Caddy scanner sweep** | IPs hitting `/.env`, `/.git/`, `/wp-admin/`, etc. via Caddy | Caddy JSON access log in journald | Instant |

Both work with the default Docker compose stack. The portal login jail
is the high-value one — it's the only thing standing between an attacker
and an admin account. The Caddy sweep is icing: it costs nothing and
keeps scanner garbage out of your logs.

---

## Prerequisites

- A VPS running `docker compose` with the portal stack
  (`docker-compose.yml` + `.env`).
- `fail2ban` installed on the host (`sudo apt install fail2ban` on Ubuntu/Debian).
  This is **not** in a container — fail2ban needs to modify the host's
  firewall, and the host has the journal + the bind-mounted data dir.
- systemd-journald running (default on Ubuntu/Debian) so fail2ban can
  read Caddy's access logs via `journalmatch`.

The portal stack since **v0.5.1** writes a fail2ban-friendly text log to
`./data/security.log` and tags container logs with stable journald
identifiers (`pwa-portal`, `pwa-portal-caddy`). Earlier versions don't
emit those signals — upgrade before configuring fail2ban.

---

## Install the configs

Assuming this repo is checked out at `/home/ubuntu/my-portal`:

```bash
# Copy the two filter files
sudo cp /home/ubuntu/my-portal/contrib/fail2ban/filter.d/pwa-portal-login.conf \
        /etc/fail2ban/filter.d/
sudo cp /home/ubuntu/my-portal/contrib/fail2ban/filter.d/pwa-portal-caddy.conf \
        /etc/fail2ban/filter.d/

# Copy the jail definitions
sudo cp /home/ubuntu/my-portal/contrib/fail2ban/jail.d/pwa-portal.conf \
        /etc/fail2ban/jail.d/
```

Open `/etc/fail2ban/jail.d/pwa-portal.conf` and **edit the `logpath`** in
the `[pwa-portal-login]` block if your repo lives somewhere other than
`/home/ubuntu/my-portal`. The path must be the host-side bind mount of
the portal's data dir.

Then:

```bash
sudo systemctl restart fail2ban
sudo fail2ban-client status
```

You should see `pwa-portal-login` and `pwa-portal-caddy` in the jail list.

---

## Verify before turning anyone away

Before any ban hits a real user, sanity-check both filters against the
actual log data.

**Portal login filter:**

```bash
# Generate at least one failed login to give the regex something to match
curl -k -d 'email=admin@example.com&password=wrong&_csrf=test' \
     https://your-domain/login

# Then test the filter
sudo fail2ban-regex /home/ubuntu/my-portal/data/security.log \
                    /etc/fail2ban/filter.d/pwa-portal-login.conf
```

Expected output:

```
Lines: 1 lines, 0 ignored, 1 matched, 0 missed
```

**Caddy scanner filter:**

```bash
# Dump the last hour of Caddy logs and feed them to the regex
sudo journalctl -t pwa-portal-caddy --since "1 hour ago" | \
  sudo fail2ban-regex - /etc/fail2ban/filter.d/pwa-portal-caddy.conf
```

The match count depends on how much scanner traffic you've seen — but
it should be > 0 within an hour of being public-facing.

If either filter returns 0 matches when you know there's matching
traffic, the most likely cause is a path mismatch (logpath wrong) or
journald not tagging the container (verify with `journalctl -t pwa-portal`).

---

## See bans in flight

```bash
# Active bans for a jail
sudo fail2ban-client status pwa-portal-login
sudo fail2ban-client status pwa-portal-caddy

# Unban an IP manually (you, after a self-inflicted brute-force)
sudo fail2ban-client set pwa-portal-login unbanip 1.2.3.4

# Tail the fail2ban log to watch matches happen
sudo journalctl -u fail2ban -f
```

The portal also records every login attempt in its own audit log at
[`/admin/audit`](https://your-portal/admin/audit) — useful for forensic
context after the fact, since the AuditEvent table captures the email
that was being tried (which fail2ban itself doesn't store).

---

## How the signals work

### `data/security.log` (portal login jail)

Written by `portal/audit.py` from inside the portal container. Each
login failure appends one line in a fixed format:

```
2026-05-27T14:35:18Z FAILED_LOGIN ip=45.88.138.44 email=admin@example.com reason=bad_credentials
2026-05-27T14:36:02Z LOGIN_RATE_LIMITED ip=45.88.138.44 email=admin@example.com reason=rate_limited
```

- **`FAILED_LOGIN`** — bad password, missing user, or any other auth-time
  rejection.
- **`LOGIN_RATE_LIMITED`** — the portal's in-process limiter dropped the
  attempt (5 fails per 10 minutes per `(ip, email)` tuple). Banning on
  this catches a fast attacker before they trip many bad-credentials
  matches.

The file lives at `./data/security.log` on the host (it's the same
bind-mounted data dir that holds `portal.db`). A `RotatingFileHandler`
caps it at 5 × 1MB ≈ 5MB; older entries roll off automatically.

### Caddy JSON access log (Caddy scanner jail)

Each handled request emits one JSON line on Caddy's stdout. The
`Caddyfile` adds `log { output stdout; format json }` inside each site
block; Docker's `journald` log driver (configured in `docker-compose.yml`
under the `caddy` service) tags the stream as `pwa-portal-caddy`. The
fail2ban filter anchors on the JSON fields `"client_ip"` and `"uri"`.

`client_ip` (not `remote_ip`) is the right field: Caddy populates it
from `X-Forwarded-For` when it's behind a TLS-terminating load balancer
(your "behind the cloud LB" mode in `docs/deploying.md`), and falls back
to the socket peer otherwise.

---

## Customizing

### Less aggressive bans

The shipped defaults (`5/10min → 1h` for login, `3/5min → 24h` for
scanners) are conservative. For a personal portal you might want:

```ini
[pwa-portal-login]
maxretry = 10
findtime = 30m
bantime  = 30m
```

Don't lower the threshold further — the portal's own per-(ip, email)
rate limit kicks in at 5; banning before that means a single typo from
yourself on a flaky connection can take you out for an hour.

### Permanent bans for repeat offenders

The shipped configs use `bantime.increment = true` so an IP that's been
banned, released, and re-banned gets doubled ban time each round (capped
at 24h for login, 7d for scanners). To make bans permanent after N
strikes, add:

```ini
[pwa-portal-login]
bantime  = 1h
bantime.maxtime = -1   # 0 or negative = forever
```

### Different firewall backend

The default `banaction` is `iptables-multiport`. If your VPS uses
nftables (Debian 11+, Ubuntu 22.04+ default) or `ufw`, set per-jail:

```ini
[pwa-portal-login]
banaction = nftables-multiport
# or
banaction = ufw
```

### Notify on ban

To email yourself when fail2ban bans someone, drop into the jail:

```ini
[pwa-portal-login]
action = %(action_mwl)s     # mail-with-logs
destemail = you@example.com
sender    = fail2ban@your-domain
```

This uses the host's MTA. The portal's own SMTP config does **not** apply
to fail2ban — they're separate sender chains.

### Whitelist your own IP

Add to `/etc/fail2ban/jail.local` (or to each jail block):

```ini
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 203.0.113.42
```

`203.0.113.42` is the example — put your real office / home IP. You can
also use CIDR ranges.

---

## Common gotchas

**fail2ban can't see `data/security.log` (filter reports 0 matches even
when failures exist).** The path in the jail must be the host-side
filesystem path of the bind mount, not a path inside the container.
Run `docker compose config` and verify the `portal` service has
`./data:/data` — the host path is the resolved version of `./data`.

**`journalctl -t pwa-portal-caddy` shows nothing.** The container is
probably running with an older `docker-compose.yml` that doesn't have
the `logging.driver: journald` block. Pull the latest compose file and
`docker compose up -d --force-recreate caddy`. Verify with
`docker inspect <caddy-container> | grep -A2 LogConfig`.

**Bans don't survive container restarts.** They do — the bans live in
the host's firewall (iptables / nftables), not in any container.
fail2ban also persists the ban database across its own restarts.

**The portal's per-(ip, email) rate limiter trips before fail2ban.** It's
in-process state in uvicorn, only reset on restart. fail2ban runs on the
host and persists bans across container restarts AND across uvicorn
worker resets. They complement each other: the in-process limiter
absorbs a burst, fail2ban locks the IP out at the firewall.

**Caddy logs are noisy when nothing's actually being scanned.** That's
expected — every page load, every static asset fetch, every favicon
request shows up. The filter only matches on bait-path substrings, so
legitimate traffic doesn't trigger bans even though it's all in the
journal.

---

## Where the logs live

| What | Path | Format | Source |
|---|---|---|---|
| Portal login failures | `./data/security.log` (bind-mounted from container) | Plain text, one line per event | `portal/audit.py:emit_security_line()` |
| Portal admin actions | SQLite `auditevent` table — render at `/admin/audit` | DB row | `portal/audit.py:record_event()` |
| Portal HTTP access | journald, `SYSLOG_IDENTIFIER=pwa-portal` | uvicorn access-log text | uvicorn stdout |
| Caddy HTTP access | journald, `SYSLOG_IDENTIFIER=pwa-portal-caddy` | Caddy JSON | Caddy `log` directive |
| Caddy TLS / startup | journald, same tag | Caddy text + JSON mixed | Caddy default logger |
| fail2ban itself | journald, `_SYSTEMD_UNIT=fail2ban.service` | fail2ban text | fail2ban daemon |

---

## What this doesn't cover

- **DDoS / volumetric attacks.** fail2ban bans individual IPs after a
  pattern match; it doesn't help against a flood from thousands of
  unique IPs. If you need that, look at upstream WAFs (Cloudflare,
  Caddy's `rate_limit` module) or fronting the deployment with a CDN.
- **Credential stuffing with valid emails.** A scanner that tests one
  password per email across many emails won't hit the per-(ip, email)
  threshold. The IP-only count in `findtime` does catch this in
  aggregate, but for serious targets enable 2FA (not yet a portal
  feature — track in the roadmap).
- **Application bugs.** fail2ban can't ban an IP that's exploiting a
  legitimate endpoint. Keep the portal up to date, review the audit log
  at `/admin/audit`, and treat every "weird thing happened" as worth
  investigating before assuming fail2ban handled it.

---

## See also

- [`contrib/fail2ban/`](../contrib/fail2ban/) — the drop-in filter and
  jail files referenced above
- [`docs/deploying.md`](deploying.md) — production deploy walkthrough
- [`docs/project-state.md`](project-state.md) — session-state notes
