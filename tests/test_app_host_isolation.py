"""Regression test: a child-app subdomain exposes only the child-app surface.

Run from anywhere:

    python3 tests/test_app_host_isolation.py

**What's being pinned.** Every portal route is registered once and used to
answer on any Host, so ``<slug>.apps.<SITE_URL>`` — an origin the untrusted
app's own JavaScript controls — also served the portal's login, setup, admin,
and app-management routes. Two consequences:

  - An admin who opened a malicious app handed it an AppSession that resolves
    to an admin user, and the app's JavaScript could call
    ``PUT /api/v1/apps/<other>`` / ``POST /api/v1/apps/upload`` to rewrite a
    different app and widen its permissions.
  - The real portal ``/login`` rendered on the app's origin; an admin signing
    in there gave the app a same-origin admin session to drive ``/admin/*``.

``AppHostGateMiddleware`` now lets only the SDK, its JSON endpoints, and public
forms through on an app host; every other GET serves the app's bundle (so the
app's own ``/sw.js``, ``/manifest.webmanifest``, ``static/*`` win over portal
routes with the same path) and every other method 404s.

The second half guards what must keep working: the SDK and storage calls, the
public form page (and the portal icon it loads), and the portal origin itself.
"""
import _harness as h

from portal.storage_backend import get_storage

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
token = h.api_token_headers(admin_id)

h.install_app(client, token, {
    "slug": "alpha", "name": "Alpha", "services": ["storage"],
    "forms": [{"name": "contact", "title": "Contact us", "fields": [
        {"name": "msg", "label": "Message", "type": "textarea"}]}],
}, {
    "index.html": "<h1>alpha home</h1>",
    "sw.js": "// alpha worker",
    "manifest.webmanifest": '{"name": "Alpha manifest"}',
    "login": "alpha's own login file",
    "static/app.js": "// alpha app.js",
})
h.install_app(client, token, {"slug": "beta", "name": "Beta"},
              {"index.html": "<h1>beta original</h1>"})

# An admin opened the (untrusted) alpha app: its JavaScript now runs with an
# AppSession that resolves to the admin.
alpha = h.open_app("alpha", admin_id)
csrf = h.csrf_of(alpha)
anon = h.app_host_client("alpha")
nav = {"Sec-Fetch-Dest": "document"}

print("--- a child app can't manage apps, even in an admin's session ---")
trojan = h.make_zip({"slug": "beta", "name": "Beta", "version": "6.6.6",
                     "entry": "index.html", "services": ["email", "pdf", "storage"]},
                    {"index.html": "<h1>trojan</h1>"})
r = alpha.put("/api/v1/apps/beta", headers={"X-CSRF-Token": csrf},
              files={"bundle": ("b.zip", trojan, "application/zip")})
h.check("PUT /api/v1/apps/beta from alpha's origin", r.status_code, 404)
planted = h.make_zip({"slug": "planted", "name": "Planted", "version": "1.0.0",
                      "entry": "index.html"}, {"index.html": "x"})
r = alpha.post("/api/v1/apps/upload", headers={"X-CSRF-Token": csrf},
               files={"bundle": ("p.zip", planted, "application/zip")})
h.check("POST /api/v1/apps/upload from alpha's origin", r.status_code, 404)
h.check("  ...beta's bundle is untouched",
        get_storage().read("apps/beta/index.html"), b"<h1>beta original</h1>")


class _FakeRequest:
    class state:
        auth_method = "app_session"


try:
    from portal.api import _require_app_manager

    from portal.models import User
    _require_app_manager(_FakeRequest(), User(id=admin_id, email="a", password_hash="x", role="admin"))
    second_layer = "allowed"
except Exception as exc:  # HTTPException
    second_layer = getattr(exc, "status_code", repr(exc))
h.check("app-management guard refuses an app_session admin", second_layer, 403)

print("--- portal pages don't answer on an app subdomain ---")
for path in ("/setup", "/admin/users", "/admin/tokens", "/profile"):
    r = alpha.get(path)
    h.check(f"GET {path} (admin's app session)", r.status_code, 404)
r = anon.get("/login", headers=nav, follow_redirects=False)
h.check("GET /login, anonymous navigation -> launcher", r.status_code, 303)
for path in ("/login", "/setup", "/logout", "/profile/change-password"):
    r = anon.post(path, data={"email": "admin@example.com", "password": h.PASSWORD})
    h.check(f"POST {path}", r.status_code, 404)
h.check("  ...and no portal session cookie was set", "session" in anon.cookies, False)

print("--- the app's own files win over same-named portal routes ---")
h.check("/login serves the bundle file", alpha.get("/login").text, "alpha's own login file")
h.check("/sw.js serves the bundle file", alpha.get("/sw.js").text, "// alpha worker")
h.check("/manifest.webmanifest serves the bundle file",
        "Alpha manifest" in alpha.get("/manifest.webmanifest").text, True)
h.check("/static/app.js serves the bundle file", alpha.get("/static/app.js").text, "// alpha app.js")

print("--- must not regress: the child-app surface ---")
h.check("/ serves the app's entry", alpha.get("/").text, "<h1>alpha home</h1>")
h.check("/portal-sdk.js (signed in)", alpha.get("/portal-sdk.js").status_code, 200)
h.check("/portal-sdk.js (pre-auth)", anon.get("/portal-sdk.js").status_code, 200)
r = alpha.put("/api/v1/storage/notes.json", content=b'{"n": 1}',
              headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
h.check("storage PUT", r.status_code, 200)
h.check("storage GET", alpha.get("/api/v1/storage/notes.json").content, b'{"n": 1}')
h.check("user/me", alpha.get("/api/v1/user/me").json().get("email"), "admin@example.com")
form = anon.get("/forms/contact")
h.check("public form page renders", form.status_code, 200)
h.check("  ...without claiming the app's manifest",
        'rel="manifest"' in form.text, False)
h.check("  ...or registering a service worker",
        "serviceWorker.register" in form.text, False)
icon = anon.get("/static/icons/favicon.png")
h.check("  ...and its portal icon still loads",
        (icon.status_code, icon.headers.get("content-type")), (200, "image/png"))

print("--- must not regress: the portal origin ---")
h.check("portal /login", client.get("/login").status_code, 200)
h.check("portal /manifest.webmanifest is the portal's",
        "Alpha manifest" in client.get("/manifest.webmanifest").text, False)
h.check("portal /static/icons/favicon.png", client.get("/static/icons/favicon.png").status_code, 200)
h.check("bundle route 404s on the portal origin",
        client.get("/__app_bundle__/index.html").status_code, 404)
h.check("unknown portal path 404s", client.get("/no/such/page").status_code, 404)

h.finish()
