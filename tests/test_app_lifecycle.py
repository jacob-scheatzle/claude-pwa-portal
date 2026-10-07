"""Regression test: installing, replacing, serving, and sharing apps.

Run from anywhere:

    python3 tests/test_app_lifecycle.py

**What's being pinned.**

  - Apps with ``csp_strict`` could never launch on their subdomain: the
    launch page's inline script/style carried no nonce under the strict CSP.
  - Replace dropped origins an admin had added by hand, and forgot a
    revocation once an intermediate version stopped requesting the origin or
    service — the next version that asked again got it auto-approved.
  - An app that declared no ``services`` could call every service. Now it
    can call none, except apps installed before the change (grandfathered).
  - Two simultaneous first installs of one slug could leave an app with no
    files; a zip with a file/folder clash or a corrupt member was a 500.
  - The portal-origin form link redirected to any hostname (open redirect).
  - PDF share links kept serving after their app was disabled.
  - Storage namespaces had no object cap, and a key/folder clash was a 500.
"""
import io
import re
import threading
import zipfile

import _harness as h

from sqlmodel import Session, select

from portal import api
from portal.db import engine
from portal.models import App
from portal.storage_backend import get_storage

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
token = h.api_token_headers(admin_id)
h.login(client, "admin@example.com")


def app_row(slug: str) -> App:
    with Session(engine) as db:
        return db.exec(select(App).where(App.slug == slug)).first()


def upload(manifest: dict, files: dict, replace_slug: str | None = None):
    bundle = h.make_zip({"version": "1.0.0", "entry": "index.html", **manifest}, files)
    payload = {"bundle": ("app.zip", bundle, "application/zip")}
    if replace_slug:
        return client.put(f"/api/v1/apps/{replace_slug}", headers=token, files=payload)
    return client.post("/api/v1/apps/upload", headers=token, files=payload)


def admin_form(path: str, page: str, **data):
    csrf = re.search(r'name="_csrf" value="([^"]+)"', client.get(page).text).group(1)
    return client.post(path, data={"_csrf": csrf, **data}, follow_redirects=False)


print("--- csp_strict apps launch ---")
upload({"slug": "strict", "name": "Strict", "permissions": {"csp_strict": True}},
       {"index.html": "<h1>strict</h1>"})
boot = h.app_host_client("strict").get("/", headers={"Sec-Fetch-Dest": "document"})
nonce = re.search(r"'nonce-([^']+)'", boot.headers.get("content-security-policy", ""))
h.check("bootstrap page gets a nonce CSP", bool(nonce), True)
h.check("  ...and its inline script carries that nonce",
        f'<script nonce="{nonce.group(1)}">' in boot.text if nonce else False, True)

print("--- replace remembers the admin's decisions ---")
X, Y, EXTRA = "https://x.example", "https://y.example", "https://extra.example"
upload({"slug": "net", "name": "Net", "services": ["email", "pdf"],
        "permissions": {"network": [X, Y]}}, {"index.html": "v1"})
admin_form("/admin/apps/net/network", "/admin/apps", allowed_requested=[Y], extras=EXTRA)
admin_form("/admin/apps/net/services", "/admin/apps", allowed_services=["pdf"])
h.check("admin revoked x + email, added extra",
        (app_row("net").allowed_origins, app_row("net").allowed_services), ([Y, EXTRA], ["pdf"]))
upload({"slug": "net", "name": "Net", "services": ["pdf"],
        "permissions": {"network": [Y]}}, {"index.html": "v2"}, replace_slug="net")
h.check("v2 (drops x + email) keeps the admin's extra origin", EXTRA in app_row("net").allowed_origins, True)
upload({"slug": "net", "name": "Net", "services": ["email", "pdf"],
        "permissions": {"network": [X, Y]}}, {"index.html": "v3"}, replace_slug="net")
row = app_row("net")
h.check("v3 asks for x again: still revoked", X in row.allowed_origins, False)
h.check("v3 asks for email again: still revoked", "email" in row.allowed_services, False)
h.check("  ...extra origin still there", EXTRA in row.allowed_origins, True)

print("--- an app that declares no services gets none ---")
upload({"slug": "bare", "name": "Bare"}, {"index.html": "bare"})
bare = h.open_app("bare", admin_id)
r = bare.put("/api/v1/storage/k.json", content=b"{}",
             headers={"X-CSRF-Token": h.csrf_of(bare), "Content-Type": "application/json"})
h.check("new app, no services: storage 403", r.status_code, 403)
with Session(engine) as db:
    legacy = db.exec(select(App).where(App.slug == "bare")).first()
    legacy.services_ungated = True  # as the migration marks pre-existing apps
    db.add(legacy)
    db.commit()
r = bare.put("/api/v1/storage/k.json", content=b"{}",
             headers={"X-CSRF-Token": h.csrf_of(bare), "Content-Type": "application/json"})
h.check("grandfathered legacy app: storage allowed", r.status_code, 200)
upload({"slug": "bare", "name": "Bare", "services": ["pdf"]}, {"index.html": "bare v2"},
       replace_slug="bare")
h.check("re-uploaded with a services list: no longer ungated", app_row("bare").services_ungated, False)
bare = h.open_app("bare", admin_id)
r = bare.put("/api/v1/storage/k.json", content=b"{}",
             headers={"X-CSRF-Token": h.csrf_of(bare), "Content-Type": "application/json"})
h.check("  ...so undeclared storage is now 403", r.status_code, 403)

print("--- installs are serialized and malformed zips are clean errors ---")
results = []


def install_same():
    results.append(upload({"slug": "racer", "name": "Racer"}, {"index.html": "racer"}).status_code)


threads = [threading.Thread(target=install_same) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
h.check("two simultaneous installs: one wins, one is refused", sorted(results), [200, 400])
h.check("  ...and the winner has its files", get_storage().exists("apps/racer/index.html"), True)

clash = h.make_zip({"slug": "clash", "name": "Clash", "version": "1", "entry": "index.html"},
                   {"index.html": "x", "a": "file", "a/b": "nested"})
r = client.post("/api/v1/apps/upload", headers=token,
                files={"bundle": ("c.zip", clash, "application/zip")})
h.check("file/folder clash: 400, not 500", r.status_code, 400)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
    zf.writestr("portal.json", '{"slug":"corrupt","name":"C","version":"1","entry":"index.html"}')
    zf.writestr("index.html", "hello world " * 50)
data = bytearray(buf.getvalue())
pos = data.find(b"hello world")
data[pos] ^= 0xFF  # flip a byte in the member: CRC check fails on extract
r = client.post("/api/v1/apps/upload", headers=token,
                files={"bundle": ("c.zip", bytes(data), "application/zip")})
h.check("corrupt member (bad CRC): 400, not 500", r.status_code, 400)

print("--- the portal-origin form link isn't an open redirect ---")
upload({"slug": "contact", "name": "Contact", "forms": [{"name": "hello", "title": "Hi", "fields": [
    {"name": "msg", "label": "Message", "type": "textarea"}]}]}, {"index.html": "c"})
anon = h.TestClient(h.app, base_url=f"http://{h.SITE}")
r = anon.get("/forms/evil.example%23/x", follow_redirects=False)
h.check("bogus slug: 404, no redirect", r.status_code, 404)
r = anon.get("/forms/contact/hello", follow_redirects=False)
h.check("real form: redirect to its own origin",
        (r.status_code, r.headers.get("location")),
        (307, f"http://contact.apps.{h.SITE}/forms/hello"))

print("--- share links go dark with their app ---")
upload({"slug": "docs", "name": "Docs", "services": ["pdf"]}, {"index.html": "d"})
docs = h.open_app("docs", admin_id)
share = docs.post("/api/v1/share/create", headers={"X-CSRF-Token": h.csrf_of(docs)},
                  json={"kind": "pdf", "html": "<p>quote</p>"}).json()
path = "/s/" + share["url"].rsplit("/s/", 1)[1]
h.check("PDF share serves while the app is enabled", anon.get(path).status_code, 200)
admin_form("/admin/apps/docs/toggle", "/admin/apps")
h.check("app disabled: PDF share 404s", anon.get(path).status_code, 404)

print("--- storage namespaces ---")
upload({"slug": "store", "name": "Store", "services": ["storage"]}, {"index.html": "s"})
store = h.open_app("store", admin_id)
H = {"X-CSRF-Token": h.csrf_of(store), "Content-Type": "application/octet-stream"}
saved = api.MAX_NAMESPACE_OBJECTS
api.MAX_NAMESPACE_OBJECTS = 3
codes = [store.put(f"/api/v1/storage/k{i}", content=b"", headers=H).status_code for i in range(4)]
h.check("4th empty object over a 3-object cap: 507", codes, [200, 200, 200, 507])
h.check("  ...overwriting an existing key still works",
        store.put("/api/v1/storage/k0", content=b"x", headers=H).status_code, 200)
api.MAX_NAMESPACE_OBJECTS = saved
store.put("/api/v1/storage/folder", content=b"file", headers=H)
h.check("key used as a folder: 409, not 500",
        store.put("/api/v1/storage/folder/inner", content=b"x", headers=H).status_code, 409)

h.finish()
