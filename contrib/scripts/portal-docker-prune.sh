#!/usr/bin/env bash
#
# portal-docker-prune.sh — reclaim Docker disk space safely.
#
# Repeatedly pulling ghcr.io/.../claude-pwa-portal:latest on redeploys (prod) or
# rebuilding the images locally (dev) leaves behind dangling images, stopped
# containers, and build cache that quietly eat disk. This reclaims them.
#
# SAFE BY DESIGN — it removes ONLY:
#   - stopped containers        (docker container prune)
#   - dangling/untagged images  (docker image prune)  <- the superseded :latest
#   - unused build cache         (docker builder prune)
#
# It NEVER passes --volumes, so Caddy's TLS-cert volumes (caddy_data /
# caddy_config) and any other named volume are untouched — wiping those would
# force Let's Encrypt to re-issue every cert on the next boot and risk the
# rate limit. The portal's ./data is a bind mount, which prune never touches
# either. The running portal/caddy containers and the images backing them are
# in use, so they survive too — meaning this is safe to run at ANY time,
# including right after `docker compose up -d`.
#
# Why dangling-only is enough: the host pulls the images as :latest, so when a
# newer :latest arrives the previous image loses its tag and becomes dangling —
# exactly what `docker image prune` removes. You almost never need --all.
#
# Usage:
#   portal-docker-prune.sh           # safe default (dangling images only)
#   portal-docker-prune.sh --all     # ALSO drop unused *tagged* images
#                                     # (docker image prune -a). Fine on a VPS
#                                     # dedicated to this stack; on a host shared
#                                     # with other Docker workloads it would also
#                                     # remove THEIR unused images. (Same as
#                                     # setting PRUNE_ALL_IMAGES=1.)
#
# Run after a redeploy:
#   docker compose pull && docker compose up -d && portal-docker-prune.sh
#
# Install on a production VPS (no repo checkout) — pull from raw GitHub:
#   sudo curl -fsSL https://raw.githubusercontent.com/jacob-scheatzle/claude-pwa-portal/main/contrib/scripts/portal-docker-prune.sh \
#     -o /usr/local/sbin/portal-docker-prune.sh
#   sudo chmod 0755 /usr/local/sbin/portal-docker-prune.sh
#   sudo /usr/local/sbin/portal-docker-prune.sh          # run once now
#   # Schedule a weekly sweep, Sundays at 03:23 (offset from the hour):
#   sudo tee /etc/cron.d/portal-docker-prune > /dev/null <<'EOF'
#   23 3 * * 0 root /usr/local/sbin/portal-docker-prune.sh
#   EOF
#
set -uo pipefail

PRUNE_ALL=0
if [ "${1:-}" = "--all" ] || [ "${PRUNE_ALL_IMAGES:-0}" = "1" ]; then
  PRUNE_ALL=1
fi

log() {
  # Mirror to syslog (shows up in journalctl) and stdout.
  logger -t portal-docker-prune "$*" 2>/dev/null || true
  echo "portal-docker-prune: $*"
}

if ! command -v docker >/dev/null 2>&1; then
  log "docker not found on PATH; nothing to do"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  log "docker daemon not reachable; skipping"
  exit 0
fi

extra=""
[ "$PRUNE_ALL" = 1 ] && extra=" + unused tagged images (--all)"
log "starting safe prune: stopped containers + dangling images + build cache${extra}"

# Each step is best-effort: one failing sub-command shouldn't abort the rest.
docker container prune -f               || log "container prune failed (continuing)"
if [ "$PRUNE_ALL" = 1 ]; then
  docker image prune -af                || log "image prune -a failed (continuing)"
else
  docker image prune -f                 || log "image prune failed (continuing)"
fi
docker builder prune -f                 || log "builder prune failed (continuing)"

log "done — current Docker disk usage:"
docker system df 2>/dev/null || true
