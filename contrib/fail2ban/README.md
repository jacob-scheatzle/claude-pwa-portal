# fail2ban configs for ProgressiveWebAppPortal

Drop-in [fail2ban](https://www.fail2ban.org/) filters and jails that ban
brute-force login attempts and scanner traffic on a self-hosted portal.

| File | Goes to | What it does |
|---|---|---|
| `filter.d/pwa-portal-login.conf` | `/etc/fail2ban/filter.d/` | Matches `FAILED_LOGIN` / `LOGIN_RATE_LIMITED` lines in `data/security.log` |
| `filter.d/pwa-portal-caddy.conf` | `/etc/fail2ban/filter.d/` | Matches scanner-bait paths (`/.env`, `/.git/`, `/wp-admin/`, …) in Caddy's JSON access log |
| `jail.d/pwa-portal.conf` | `/etc/fail2ban/jail.d/` | Wires the two filters with sensible thresholds |

## Quickstart

```bash
sudo cp filter.d/*.conf /etc/fail2ban/filter.d/
sudo cp jail.d/pwa-portal.conf /etc/fail2ban/jail.d/

# Edit the logpath in jail.d/pwa-portal.conf to match where your repo /
# docker-compose.yml lives on the VPS
sudo $EDITOR /etc/fail2ban/jail.d/pwa-portal.conf

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
