# fail2ban configs for ProgressiveWebAppPortal

Drop-in [fail2ban](https://www.fail2ban.org/) filters and jails that ban
brute-force login attempts and scanner traffic on a self-hosted portal.

| File | Goes to | What it does |
|---|---|---|
| `filter.d/pwa-portal-login.conf` | `/etc/fail2ban/filter.d/` | Matches `FAILED_LOGIN` / `LOGIN_RATE_LIMITED` (cookie login) and `MCP_AUTH_FAILED` / `API_AUTH_FAILED` (bearer/MCP token) lines in `data/security.log` |
| `filter.d/pwa-portal-caddy.conf` | `/etc/fail2ban/filter.d/` | Matches scanner-bait paths (`/.env`, `/.git/`, `/wp-admin/`, …) in Caddy's JSON access log |
| `jail.d/pwa-portal.conf` | `/etc/fail2ban/jail.d/` | Wires the two filters with sensible thresholds |
| `jail.d/sshd.conf`       | `/etc/fail2ban/jail.d/` | sshd brute-force protection (separate from portal so the portal can't ban your own SSH) |

## Quickstart (production VPS, no repo checkout)

The standard portal deploy doesn't put the source on the host, so fetch
each file directly from raw GitHub:

```bash
RAW=https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/contrib/fail2ban

sudo curl -fsSL $RAW/filter.d/pwa-portal-login.conf -o /etc/fail2ban/filter.d/pwa-portal-login.conf
sudo curl -fsSL $RAW/filter.d/pwa-portal-caddy.conf -o /etc/fail2ban/filter.d/pwa-portal-caddy.conf
sudo curl -fsSL $RAW/jail.d/pwa-portal.conf         -o /etc/fail2ban/jail.d/pwa-portal.conf
sudo curl -fsSL $RAW/jail.d/sshd.conf               -o /etc/fail2ban/jail.d/sshd.conf

# Edit the logpath in jail.d/pwa-portal.conf to match where your
# docker-compose.yml lives on the VPS
sudo $EDITOR /etc/fail2ban/jail.d/pwa-portal.conf

sudo systemctl restart fail2ban
sudo fail2ban-client status
```

## Quickstart (from a cloned repo)

```bash
sudo install -m 0644 filter.d/pwa-portal-*.conf /etc/fail2ban/filter.d/
sudo install -m 0644 jail.d/pwa-portal.conf jail.d/sshd.conf /etc/fail2ban/jail.d/
sudo $EDITOR /etc/fail2ban/jail.d/pwa-portal.conf  # tweak logpath
sudo systemctl restart fail2ban
sudo fail2ban-client status
```

Full setup (Caddyfile changes, docker-compose log driver, verification
recipes, customization options) is in [`docs/fail2ban.md`](../../docs/fail2ban.md).

## Requirements

- Portal **v0.5.1 or later** — earlier versions don't write
  `data/security.log` or tag container logs with stable journald
  identifiers.
- fail2ban **>= 0.10** for `backend = systemd`; **>= 0.11** for
  `bantime.increment` (exponential ban escalation). The configs work
  without `bantime.increment` — just remove those lines on older
  fail2ban.
- The portal stack running via the shipped `docker-compose.yml` so the
  journald log driver is in effect.
