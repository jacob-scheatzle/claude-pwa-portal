"""Regression test: deleting a user leaves nothing a later account can inherit.

Run from anywhere:

    python3 tests/test_user_delete_cleanup.py

**What's being pinned.** User delete revoked credentials but left the user's
per-app storage (``storage/<slug>/<uid>/``) and share links behind, and SQLite
handed the deleted user's id to the next account created. The newcomer then
read the deleted user's files through the SDK, and the deleted user's public
``/s/<token>`` links kept serving — now whatever the newcomer saved under the
same key.

Now delete removes the user's storage in every app and every share link they
created, and ``user`` ids are AUTOINCREMENT, so an id is never handed out twice.
"""
import re

import _harness as h

from sqlmodel import Session, select

from portal.db import engine
from portal.models import App, ShareLink, UserAppAccess
from portal.storage_backend import get_storage

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
h.install_app(client, h.api_token_headers(admin_id),
              {"slug": "notes", "name": "Notes", "services": ["storage"]},
              {"index.html": "<h1>notes</h1>"})
with Session(engine) as db:
    notes_id = db.exec(select(App).where(App.slug == "notes")).first().id


def grant(user_id: int) -> None:
    with Session(engine) as db:
        db.add(UserAppAccess(user_id=user_id, app_id=notes_id))
        db.commit()


alice_id = h.add_user("alice@example.com")
grant(alice_id)
alice = h.open_app("notes", alice_id)
csrf = h.csrf_of(alice)
alice.put("/api/v1/storage/payroll.json", content=b"alice's salary",
          headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
share = alice.post("/api/v1/share/create", json={"kind": "storage", "key": "payroll.json"},
                   headers={"X-CSRF-Token": csrf})
share_path = "/s/" + share.json()["url"].rsplit("/s/", 1)[1]
h.check("alice's share link serves before delete", client.get(share_path).status_code, 200)

h.login(client, "admin@example.com")
csrf_admin = re.search(r'name="_csrf" value="([^"]+)"', client.get("/admin/users").text).group(1)
r = client.post(f"/admin/users/{alice_id}/delete", data={"_csrf": csrf_admin},
                follow_redirects=False)
h.check("admin deletes alice", r.status_code, 303)

print("--- nothing of alice's survives ---")
h.check("her storage namespace is gone",
        get_storage().list(get_storage().namespace_prefix("notes", alice_id)), [])
with Session(engine) as db:
    h.check("her share links are gone",
            len(db.exec(select(ShareLink).where(ShareLink.created_by == alice_id)).all()), 0)
h.check("her public link 404s", client.get(share_path).status_code, 404)

print("--- the next account starts clean ---")
bob_id = h.add_user("bob@example.com")
h.check("bob doesn't inherit alice's id", bob_id != alice_id, True)
grant(bob_id)
bob = h.open_app("notes", bob_id)
h.check("bob can't read alice's file", bob.get("/api/v1/storage/payroll.json").status_code, 404)
h.check("bob's storage is empty", bob.get("/api/v1/storage").json().get("items", []), [])
csrf = h.csrf_of(bob)
bob.put("/api/v1/storage/payroll.json", content=b"bob's salary",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
h.check("alice's old link doesn't serve bob's file", client.get(share_path).status_code, 404)

h.finish()
