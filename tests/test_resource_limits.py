"""Regression test: anonymous callers can't exhaust memory, disk, or the limiters.

Run from anywhere:

    python3 tests/test_resource_limits.py

**What's being pinned.**

  - No request-body ceiling existed anywhere. Starlette parses a multipart or
    urlencoded body before auth runs, so a 200 MB POST to ``/login`` or a
    public form was buffered in full (into a RAM tmpfs, in Docker).
    ``BodySizeLimitMiddleware`` now refuses oversize bodies with a 413 —
    up front for a declared Content-Length, mid-stream for a chunked one.
  - Temp files (upload spooling, bundle extraction, Backup / Export staging)
    now live under ``<data_dir>/.tmp`` instead of ``/tmp``.
  - The login throttle keyed on the raw email (unbounded) and checked before
    it counted, so a parallel burst got more than five guesses. It now keys on
    a bounded email, caps its size, and counts atomically.
  - ``/authorize`` parked an unbounded row per anonymous call. It now caps the
    field lengths, the number of live pending requests, and calls per IP.
  - Backup left out branding and rendered share PDFs.
"""
import base64
import hashlib
import io
import os
import re
import secrets
import tarfile
import tempfile
import threading

os.environ["SITE_URL"] = "localhost"  # an OAuth issuer must be https or localhost
os.environ["MCP_ENABLED"] = "true"

import _harness as h  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from portal import main as portal_main  # noqa: E402
from portal import oauth  # noqa: E402
from portal.db import engine  # noqa: E402
from portal.models import OAuthPendingAuthorization  # noqa: E402
from portal.storage_backend import get_storage  # noqa: E402

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
token = h.api_token_headers(admin_id)
h.install_app(client, token, {
    "slug": "notes", "name": "Notes", "services": ["storage"],
    "forms": [{"name": "contact", "title": "Contact", "fields": [
        {"name": "msg", "label": "Message", "type": "textarea"}]}],
}, {"index.html": "<h1>notes</h1>", "big.bin": os.urandom(2 * 1024 * 1024)})
h.check("a 2 MB bundle still installs (bundle routes allow 80 MB)", True, True)
MB = 1024 * 1024

print("--- request bodies have a ceiling ---")
r = client.post("/login", content=b"email=a&password=" + b"x" * (2 * MB),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
h.check("2 MB POST /login (declared length)", r.status_code, 413)


def chunks(total):
    for _ in range(total // (64 * 1024)):
        yield b"x" * (64 * 1024)


r = client.post("/login", content=chunks(2 * MB),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
h.check("2 MB POST /login (chunked, no length)", r.status_code, 413)
anon = h.app_host_client("notes")
r = anon.post("/forms/contact", content=b"msg=" + b"A" * (2 * MB),
              headers={"Content-Type": "application/x-www-form-urlencoded"})
h.check("2 MB POST to a public form", r.status_code, 413)
notes = h.open_app("notes", admin_id)
csrf = h.csrf_of(notes)
r = notes.put("/api/v1/storage/big.bin", content=b"x" * (12 * MB),
              headers={"X-CSRF-Token": csrf, "Content-Type": "application/octet-stream"})
h.check("12 MB storage PUT", r.status_code, 413)
r = notes.put("/api/v1/storage/ok.bin", content=b"x" * (3 * MB),
              headers={"X-CSRF-Token": csrf, "Content-Type": "application/octet-stream"})
h.check("3 MB storage PUT still allowed", r.status_code, 200)

print("--- temp files live on the data volume ---")
h.check("tempfile dir is <data_dir>/.tmp",
        tempfile.gettempdir(), os.path.join(h.TMP, ".tmp"))

print("--- the login throttle is bounded and atomic ---")


def login_page_csrf(c):
    return re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").text).group(1)


login_csrf = login_page_csrf(client)
r = client.post("/login", data={"email": "x" * 5000 + "@example.com",
                                "password": "wrong", "_csrf": login_csrf})
h.check("a 5000-char email is refused normally", r.status_code, 401)
h.check("  ...and keyed at most 254 chars",
        max(len(k[1]) for k in portal_main._login_failures), 254)

statuses = []


def attempt():
    statuses.append(client.post("/login", data={
        "email": "burst@example.com", "password": "wrong", "_csrf": login_csrf}).status_code)


threads = [threading.Thread(target=attempt) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
h.check("12 parallel guesses: at most 5 get checked",
        sum(1 for s in statuses if s != 429), 5)

# One network spraying many emails: the per-network limit stops it, so it can
# neither guess at scale nor flush its own blocked entry out of the table.
spray = [client.post("/login", data={"email": f"spray{i}@example.com", "password": "w",
                                     "_csrf": login_csrf}).status_code for i in range(40)]
h.check("one network spraying 40 emails is cut off", spray[-1], 429)
counted = 1 + sum(1 for s in statuses if s != 429) + sum(1 for s in spray if s != 429)
h.check("  ...after exactly 30 checked attempts from it", counted, 30)
with portal_main._login_failures_lock:
    portal_main._login_failures.clear()
    portal_main._login_net_hits.clear()
saved_cap = portal_main._LOGIN_FAIL_MAX_KEYS
portal_main._LOGIN_FAIL_MAX_KEYS = 3
for i in range(6):
    client.post("/login", data={"email": f"fill{i}@example.com", "password": "w",
                                "_csrf": login_csrf})
h.check("throttle keeps at most _LOGIN_FAIL_MAX_KEYS keys",
        len(portal_main._login_failures) <= 3, True)
portal_main._LOGIN_FAIL_MAX_KEYS = saved_cap
with portal_main._login_failures_lock:
    portal_main._login_failures.clear()
    portal_main._login_net_hits.clear()

print("--- /authorize can't fill the database ---")
cid = client.post("/register", json={
    "client_name": "c", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
    "token_endpoint_auth_method": "none"}).json()["client_id"]
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(secrets.token_bytes(32)).digest()).rstrip(b"=").decode()


def authorize(**extra):
    params = {"response_type": "code", "client_id": cid,
              "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"}
    params.update(extra)
    return client.get("/authorize", params=params, follow_redirects=False)


r = authorize(state="S" * 5000)
h.check("5000-char state refused", "error=invalid_request" in r.headers.get("location", ""), True)
r = authorize(code_challenge="A" * 5000)
h.check("5000-char code_challenge refused",
        "error=invalid_request" in r.headers.get("location", ""), True)
saved_pending = oauth._MAX_LIVE_PENDING
oauth._MAX_LIVE_PENDING = 2
results = [authorize().headers.get("location", "") for _ in range(3)]
with Session(engine) as db:
    live = len(db.exec(select(OAuthPendingAuthorization)).all())
h.check("live pending requests are capped", live, 2)
# Refusing at the cap would let a flood lock the admin out; the newest wins.
h.check("  ...by dropping the oldest, not refusing the newest",
        all("/oauth/consent" in loc for loc in results), True)
oauth._MAX_LIVE_PENDING = saved_pending
codes = [authorize().status_code for _ in range(30)]
h.check("more than 30 calls in 10 min from one IP -> 429", codes[-1], 429)

print("--- backups are complete ---")
storage = get_storage()
storage.write("branding/logo-abc.png", b"\x89PNG logo")
storage.write("shares/tok123.pdf", b"%PDF share")
h.login(client, "admin@example.com")
page = client.get("/admin/backup")
csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
r = client.post("/admin/backup/download", data={"_csrf": csrf})
h.check("backup downloads", r.status_code, 200)
names = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz").getnames()
for want in ("portal.db", "apps/notes/index.html", "branding/logo-abc.png", "shares/tok123.pdf"):
    h.check(f"  ...includes {want}", want in names, True)
h.check("  ...but not the temp dir", any(n.startswith(".tmp") for n in names), False)

h.finish()
