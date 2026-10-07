"""Regression test: Secure deployments use ``__Host-`` cookie names.

Run from anywhere:

    python3 tests/test_secure_cookies.py

**What's being pinned.** A child app on ``<slug>.apps.<SITE_URL>`` can set a
cookie with ``Domain=<SITE_URL>``, which the browser then sends to the portal
origin too. With plain names, an app could plant a ``session`` cookie there
(logging the admin out, or into another account) or an ``app_session`` cookie
on a sibling app. The ``__Host-`` prefix makes the browser refuse any cookie of
that name carrying a Domain attribute. It requires ``Secure`` and ``Path=/``,
so it applies when ``COOKIES_SECURE`` is on (every real deployment).
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

os.environ["COOKIES_SECURE"] = "true"

import _harness as h  # noqa: E402

from sqlmodel import Session  # noqa: E402

from portal.db import engine  # noqa: E402
from portal.models import AppLaunchToken  # noqa: E402

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
h.install_app(client, h.api_token_headers(admin_id),
              {"slug": "notes", "name": "Notes"}, {"index.html": "<h1>notes</h1>"})

secure = h.TestClient(h.app, base_url=f"https://{h.SITE}")
h.login(secure, "admin@example.com")
names = [c.name for c in secure.cookies.jar]
h.check("portal session cookie is __Host-session", "__Host-session" in names, True)
h.check("  ...and no plain 'session' cookie", "session" in names, False)
cookie = next(c for c in secure.cookies.jar if c.name == "__Host-session")
h.check("  ...Secure, Path=/, no Domain",
        (cookie.secure, cookie.path, cookie.domain_specified), (True, "/", False))
h.check("portal session works", secure.get("/profile").status_code, 200)

app_client = h.TestClient(h.app, base_url=f"https://notes.apps.{h.SITE}")
tok = secrets.token_urlsafe(32)
now = datetime.now(timezone.utc)
with Session(engine) as db:
    db.add(AppLaunchToken(token=tok, user_id=admin_id, slug="notes", created_at=now,
                          expires_at=now + timedelta(seconds=60)))
    db.commit()
app_client.post("/api/v1/session/exchange", json={"token": tok})
names = [c.name for c in app_client.cookies.jar]
h.check("app session cookie is __Host-app_session", "__Host-app_session" in names, True)
cookie = next(c for c in app_client.cookies.jar if c.name == "__Host-app_session")
h.check("  ...Secure, Path=/, no Domain",
        (cookie.secure, cookie.path, cookie.domain_specified), (True, "/", False))
h.check("app session works", app_client.get("/api/v1/user/me").status_code, 200)

h.finish()
