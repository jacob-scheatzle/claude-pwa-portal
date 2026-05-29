# contrib/scripts

Optional host-side helpers for hardening a VPS that runs the portal. None
of these are required — they live here so self-hosters can opt in.

## spamhaus-drop.sh

Daily refresh of the [Spamhaus DROP](https://www.spamhaus.org/drop/)
block-list, applied at the kernel firewall via ipset + iptables. Traffic
from listed netblocks (botnet C2, hijacked ranges, well-known spammers)
is dropped before it ever reaches Caddy.

### Install (production VPS, no repo checkout)

The standard VPS deploy only has `docker-compose.yml` + `.env` on the host,
so pull the script directly from raw GitHub:

```bash
sudo apt-get install -y ipset curl
sudo curl -fsSL https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/contrib/scripts/spamhaus-drop.sh \
  -o /usr/local/sbin/spamhaus-drop.sh
sudo chmod 0755 /usr/local/sbin/spamhaus-drop.sh

# Populate immediately
sudo /usr/local/sbin/spamhaus-drop.sh

# Schedule a daily refresh at 04:17
sudo tee /etc/cron.d/spamhaus-drop > /dev/null <<'EOF'
17 4 * * * root /usr/local/sbin/spamhaus-drop.sh
EOF
```

### Install (from a cloned repo)

```bash
sudo apt-get install -y ipset curl
sudo install -m 0755 contrib/scripts/spamhaus-drop.sh /usr/local/sbin/
sudo /usr/local/sbin/spamhaus-drop.sh
echo '17 4 * * * root /usr/local/sbin/spamhaus-drop.sh' | sudo tee /etc/cron.d/spamhaus-drop
```

### Verify

```bash
# How many CIDRs are loaded
sudo ipset list spamhaus-drop | grep -c '^[0-9]'

# That the firewall rule is in place
sudo iptables -L INPUT -n --line-numbers | grep spamhaus-drop

# Recent runs in the journal
journalctl -t spamhaus-drop --since "2 days ago"
```

### Persistence across reboots

ipsets live in memory only. After a reboot the set is empty until cron
fires it again the next morning. If that gap bothers you, install
`ipset-persistent` / `iptables-persistent` and save the current state
after the first successful run:

```bash
sudo apt-get install -y iptables-persistent ipset-persistent
sudo ipset save > /etc/iptables/ipsets
sudo iptables-save > /etc/iptables/rules.v4
```

### Uninstall

```bash
sudo rm /etc/cron.d/spamhaus-drop /usr/local/sbin/spamhaus-drop.sh
sudo iptables -D INPUT -m set --match-set spamhaus-drop src -j DROP
sudo ipset destroy spamhaus-drop
```

## portal-docker-prune.sh

Reclaim Docker disk space that piles up from redeploys (`docker compose pull`
leaves the superseded `:latest` image dangling) and local rebuilds (build
cache). **Safe by design** — it prunes only stopped containers, dangling
(untagged) images, and unused build cache. It never passes `--volumes`, so
Caddy's TLS-cert volumes (`caddy_data` / `caddy_config`) are untouched; it
never removes the `./data` bind mount or the images backing the running stack.
So it's safe to run at any time, including right after `docker compose up -d`.

### Run after every redeploy

```bash
docker compose pull && docker compose up -d && portal-docker-prune.sh
```

### Install (production VPS, no repo checkout)

```bash
sudo curl -fsSL https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/contrib/scripts/portal-docker-prune.sh \
  -o /usr/local/sbin/portal-docker-prune.sh
sudo chmod 0755 /usr/local/sbin/portal-docker-prune.sh

# Run once now
sudo /usr/local/sbin/portal-docker-prune.sh

# Schedule a weekly sweep (Sundays 03:23, offset from the hour)
sudo tee /etc/cron.d/portal-docker-prune > /dev/null <<'EOF'
23 3 * * 0 root /usr/local/sbin/portal-docker-prune.sh
EOF
```

### Install (from a cloned repo)

```bash
sudo install -m 0755 contrib/scripts/portal-docker-prune.sh /usr/local/sbin/
sudo /usr/local/sbin/portal-docker-prune.sh
echo '23 3 * * 0 root /usr/local/sbin/portal-docker-prune.sh' | sudo tee /etc/cron.d/portal-docker-prune
```

### Going further — `--all`

On a VPS **dedicated to this stack** (nothing else uses Docker), pass `--all`
to also drop superseded *tagged* images, not just dangling ones:

```bash
sudo /usr/local/sbin/portal-docker-prune.sh --all      # or PRUNE_ALL_IMAGES=1
```

Don't use `--all` on a host shared with other Docker workloads — it would
remove their unused images too. And **never** add `--volumes` to a Docker prune
on this host: it would wipe Caddy's cert volume and force a full Let's Encrypt
re-issue on the next boot.

### Verify

```bash
docker system df                       # SIZE / RECLAIMABLE per type
journalctl -t portal-docker-prune --since "2 weeks ago"
```

### Uninstall

```bash
sudo rm /etc/cron.d/portal-docker-prune /usr/local/sbin/portal-docker-prune.sh
```
