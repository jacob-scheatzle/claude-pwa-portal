#!/bin/sh
# Pick the right Caddyfile at container start.
#
# HTTP_ONLY=true selects /etc/caddy/Caddyfile.http (plain HTTP, no
# auto-HTTPS, no Let's Encrypt). Anything else (including unset) keeps the
# default /etc/caddy/Caddyfile which does automatic HTTPS via Let's Encrypt
# + on-demand TLS per child-app subdomain.
#
# Both files are baked into the image; the dev compose override bind-mounts
# them from the host so edits are visible without rebuilding.
set -eu

case "${HTTP_ONLY:-false}" in
	true|TRUE|True|1|yes|YES|on|ON)
		CONFIG=/etc/caddy/Caddyfile.http
		;;
	*)
		CONFIG=/etc/caddy/Caddyfile
		;;
esac

echo "[portal-caddy] HTTP_ONLY=${HTTP_ONLY:-false}, using ${CONFIG}"
exec caddy run --config "${CONFIG}" --adapter caddyfile
