FROM python:3.12-slim

# System libraries WeasyPrint needs at runtime, plus a non-root runtime user.
RUN apt-get update && apt-get install -y --no-install-recommends \
		libpango-1.0-0 \
		libpangoft2-1.0-0 \
		libharfbuzz0b \
		libfontconfig1 \
		fonts-dejavu-core \
	&& rm -rf /var/lib/apt/lists/* \
	&& groupadd --system --gid 1001 portal \
	&& useradd --system --uid 1001 --gid portal --no-create-home --shell /usr/sbin/nologin portal

WORKDIR /build
COPY pyproject.toml ./
COPY portal/ ./portal/
COPY alembic.ini ./
COPY alembic/ ./alembic/
# The image bundles the MCP app-management server's dependency by default
# (docs/mcp.md), so /mcp comes up automatically when the container starts
# (mcp_enabled defaults to auto-on-when-importable). Build a lean image without
# it via: docker compose build --build-arg INSTALL_MCP=false
#
# INSTALL_AWS adds the AWS deployment backends (boto3 for S3 + psycopg for
# PostgreSQL/RDS). Off by default so the standard Docker/Caddy image stays lean;
# the AWS image (aws/) builds with --build-arg INSTALL_AWS=true. psycopg ships a
# binary wheel and boto3 is pure Python, so no extra system libraries are needed.
ARG INSTALL_MCP=true
ARG INSTALL_AWS=false
RUN extras=""; \
	if [ "$INSTALL_MCP" = "true" ]; then extras="${extras}mcp,"; fi; \
	if [ "$INSTALL_AWS" = "true" ]; then extras="${extras}aws,"; fi; \
	extras="$(echo "$extras" | sed 's/,$//')"; \
	if [ -n "$extras" ]; then \
		pip install --no-cache-dir ".[$extras]"; \
	else \
		pip install --no-cache-dir .; \
	fi

WORKDIR /
USER portal
ENV PYTHONUNBUFFERED=1
# Required so the container runs cleanly with ``read_only: true`` in
# docker-compose.yml — Python would otherwise try to write .pyc files
# into the read-only site-packages directory on every import. Disabling
# bytecode generation costs a small one-time startup penalty (imports are
# parsed from source) but matters once at boot, not per-request.
ENV PYTHONDONTWRITEBYTECODE=1
# Re-point $HOME at /tmp (which is a tmpfs in docker-compose.yml) so anything
# that caches under ``$HOME/.cache`` — most notably fontconfig, which
# WeasyPrint pulls in — has a writable scratch dir under read_only:true.
# The runtime user has no real home (--no-create-home), so $HOME defaults
# to ``/`` and writes would EROFS.
ENV HOME=/tmp
# Alembic config lives in /build alongside the source we COPY'd above. The
# portal package itself gets pip-installed into site-packages, so
# Path(__file__).parent.parent inside portal/db.py won't find these files —
# init_db() reads these env vars to locate them.
ENV ALEMBIC_DIR=/build/alembic
ENV ALEMBIC_INI=/build/alembic.ini
EXPOSE 8000
# Container healthcheck. ``/health`` is a plain 200 that touches no database,
# but it only starts answering AFTER the lifespan startup hook has finished —
# and that hook runs ``alembic upgrade head`` (portal/db.py init_db). So
# "healthy" means precisely "migrations are done and uvicorn is serving",
# which is exactly what a front proxy wants to gate on.
#
# urllib rather than curl/wget: the python:3.12-slim base ships neither, and
# installing one just for this would grow the image and its CVE surface for no
# gain. The check runs as the unprivileged ``portal`` user against the
# container's own port, so it needs no writable path and works under
# ``read_only: true``.
#
# --start-period gives a slow first-boot migration room without counting
# failures against --retries; --start-interval polls every 2s inside that
# window so a proxy gated on ``condition: service_healthy`` starts promptly
# instead of waiting out a full 30s interval. --start-interval needs Docker
# Engine 25.0+ (Jan 2024) both to build and to honor at runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
	CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
# --proxy-headers makes uvicorn honor X-Forwarded-For/Proto from Caddy, which
# sits on the Docker bridge in front of us. --forwarded-allow-ips lists the proxy
# hops uvicorn trusts: the default is the RFC1918 private ranges, so it trusts
# Caddy on the bridge but NOT a client-supplied X-Forwarded-For. (With "*" uvicorn
# reads the left-most, client-spoofable entry — letting a forged header choose the
# IP used for login throttling and the fail2ban security log. With a real proxy
# hop trusted instead, uvicorn walks the header from the right and stops at the
# genuine client.) Override FORWARDED_ALLOW_IPS only if a TLS-terminating load
# balancer forwards to the portal from a PUBLIC IP without Caddy in front. NOTE:
# surfacing real client IPs also needs Docker `userland-proxy: false` so Caddy
# sees the true peer — see docs/deploying.md. Shell form + exec so the env var
# expands while uvicorn stays PID 1 (clean SIGTERM on `docker stop`).
CMD ["sh", "-c", "exec uvicorn portal.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}\""]
