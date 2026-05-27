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
RUN pip install --no-cache-dir .

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
# --proxy-headers + --forwarded-allow-ips=* makes uvicorn honor X-Forwarded-For/Proto
# from Caddy. Caddy is the only thing in front of uvicorn and sits on the Docker
# bridge network (so it's not 127.0.0.1, which is uvicorn's default trust scope);
# without this, request.client.host is Caddy's container IP for every request and
# the (IP, email) login rate-limit collapses to a single bucket.
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
