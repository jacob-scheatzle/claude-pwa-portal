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
RUN pip install --no-cache-dir .

WORKDIR /
USER portal
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips=* makes uvicorn honor X-Forwarded-For/Proto
# from Caddy. Caddy is the only thing in front of uvicorn and sits on the Docker
# bridge network (so it's not 127.0.0.1, which is uvicorn's default trust scope);
# without this, request.client.host is Caddy's container IP for every request and
# the (IP, email) login rate-limit collapses to a single bucket.
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
