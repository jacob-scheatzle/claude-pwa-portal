#!/usr/bin/env bash
#
# spamhaus-drop.sh — refresh the Spamhaus DROP block-list once a day.
#
# The Spamhaus DROP (Don't Route Or Peer) list enumerates netblocks
# controlled by spammers, botnet C2s, and other actors no legitimate
# user-facing service should ever talk to. Blocking it at the firewall
# layer means scanners from those ranges never even reach Caddy.
#
# Mechanics:
#   - Maintains an ipset called ``spamhaus-drop`` (hash:net).
#   - Iptables INPUT rule "match set + DROP" is created idempotently.
#   - Refresh is atomic: fetch into a TEMP ipset, swap, destroy old.
#     If the fetch fails or the file looks empty/truncated, we abort
#     before swapping — the previous list keeps protecting you.
#   - All output goes to syslog so it shows up in journalctl.
#
# Install (run as root once). The standard VPS deploy doesn't check out the
# repo on the host, so pull the script from raw GitHub:
#   sudo apt-get install -y ipset curl
#   sudo curl -fsSL \
#     https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/contrib/scripts/spamhaus-drop.sh \
#     -o /usr/local/sbin/spamhaus-drop.sh
#   sudo chmod 0755 /usr/local/sbin/spamhaus-drop.sh
#   sudo /usr/local/sbin/spamhaus-drop.sh           # populate immediately
#   sudo tee /etc/cron.d/spamhaus-drop > /dev/null <<'EOF'
#   # Refresh Spamhaus DROP list daily at 04:17 (offset from the hour to
#   # avoid hammering Spamhaus at :00). Output is captured by syslog via the
#   # `logger` calls inside the script.
#   17 4 * * * root /usr/local/sbin/spamhaus-drop.sh
#   EOF
#
# Persistence across reboots:
#   The ipset is in-memory; restart wipes it. The cron job will repopulate
#   on its next run, but if you want zero gap on reboot, install
#   ``ipset-persistent`` / ``iptables-persistent`` and run
#   ``ipset save > /etc/iptables/ipsets`` after the first run.

set -euo pipefail

readonly SET_NAME="spamhaus-drop"
readonly TMP_SET="spamhaus-drop-tmp"
readonly DROP_URL="https://www.spamhaus.org/drop/drop.txt"
readonly MIN_ENTRIES=100   # sanity floor; real list has ~1000

log()  { logger -t spamhaus-drop -p daemon.info  -- "$*"; }
warn() { logger -t spamhaus-drop -p daemon.warning -- "$*"; }
die()  { logger -t spamhaus-drop -p daemon.err   -- "$*"; echo "spamhaus-drop: $*" >&2; exit 1; }

command -v ipset >/dev/null    || die "ipset not installed (apt-get install ipset)"
command -v iptables >/dev/null || die "iptables not installed"
command -v curl >/dev/null     || die "curl not installed"
[ "$(id -u)" -eq 0 ] || die "must run as root"

# 1) Make sure the *real* set exists so the iptables rule can reference it
#    even on first run (we'll populate via the swap below).
if ! ipset list -n | grep -qx "$SET_NAME"; then
  ipset create "$SET_NAME" hash:net family inet hashsize 4096 maxelem 65536
  log "created ipset $SET_NAME"
fi

# 2) Ensure the DROP rule is in place. -C tests for existence; if it's not
#    there, -I inserts at the top of INPUT so it runs before any ACCEPT.
if ! iptables -C INPUT -m set --match-set "$SET_NAME" src -j DROP 2>/dev/null; then
  iptables -I INPUT 1 -m set --match-set "$SET_NAME" src -j DROP
  log "installed iptables DROP rule for $SET_NAME"
fi

# 3) Fetch + parse into a temporary set, then atomically swap.
#    Spamhaus DROP format: "<cidr> ; <comment>" with ; comment lines.
tmpfile="$(mktemp)"
trap 'rm -f "$tmpfile"; ipset destroy "$TMP_SET" 2>/dev/null || true' EXIT

if ! curl -fsSL --max-time 30 --retry 2 "$DROP_URL" -o "$tmpfile"; then
  die "fetch from $DROP_URL failed; keeping previous list"
fi

# Strip comments + blank lines, keep just the CIDR.
mapfile -t cidrs < <(awk '/^[0-9]/ {print $1}' "$tmpfile")

if [ "${#cidrs[@]}" -lt "$MIN_ENTRIES" ]; then
  die "fetched list has only ${#cidrs[@]} entries (< $MIN_ENTRIES); refusing to swap"
fi

# Build the temp set fresh.
ipset destroy "$TMP_SET" 2>/dev/null || true
ipset create "$TMP_SET" hash:net family inet hashsize 4096 maxelem 65536

for cidr in "${cidrs[@]}"; do
  ipset add "$TMP_SET" "$cidr" 2>/dev/null || true
done

# 4) Atomic swap — iptables match-set keeps working through the swap because
#    the rule references the *name*, and ipset swap exchanges contents
#    without recreating the named set.
ipset swap "$TMP_SET" "$SET_NAME"
ipset destroy "$TMP_SET"

count=$(ipset list "$SET_NAME" | grep -c '^[0-9]')
log "refreshed $SET_NAME: $count CIDRs active"
