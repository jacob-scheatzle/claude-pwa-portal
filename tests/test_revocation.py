"""Regression test: admin actions actually end access, and sessions expire.

Run from anywhere:

    python3 tests/test_revocation.py

**What's being pinned.**

  - An admin (or CLI) password reset — the usual response to a compromised
    account — changed the password but left the attacker's portal session,
    open app sessions, and MCP OAuth grants working.
  - Demoting an admin left their API tokens working, and token callers
    skipped the per-app access check, so a demoted admin kept storage / email
    / share access to every app. Their schedules kept firing as them, too.
  - Sessions never expired server-side: the portal cookie slides forward on
    every response and the app cookie is a bare id.
  - ``/api/v1`` responses (storage reads included) had no caching directive.
"""
import getpass
import re
from datetime import datetime, timedelta, timezone

import _harness as h

from sqlmodel import Session, select

from portal import cli
from portal.db import engine
from portal.models import (
    ApiToken,
    App,
    AppLaunchToken,
    AppSession,
    OAuthToken,
    ScheduledRun,
    User,
    UserSession,
)
from portal.scheduler import fire_schedule

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
h.install_app(client, h.api_token_headers(admin_id),
              {"slug": "notes", "name": "Notes", "services": ["storage"]},
              {"index.html": "<h1>notes</h1>"})


def portal_client(email: str):
    c = h.TestClient(h.app, base_url=f"http://{h.SITE}")
    h.login(c, email)
    return c


def grant_oauth(user_id: int) -> None:
    with Session(engine) as db:
        db.add(OAuthToken(access_token_hash=f"hash-{user_id}", client_id="c", user_id=user_id,
                          scopes="mcp"))
        db.commit()


def oauth_count(user_id: int) -> int:
    with Session(engine) as db:
        return len(db.exec(select(OAuthToken).where(OAuthToken.user_id == user_id)).all())


def admin_post(path: str, **data):
    page = admin.get("/admin/users")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    return admin.post(path, data={"_csrf": csrf, **data}, follow_redirects=False)


admin = portal_client("admin@example.com")

print("--- an admin password reset signs the user out everywhere ---")
victim_id = h.add_user("victim@example.com", "admin")
stolen = portal_client("victim@example.com")
stolen_app = h.open_app("notes", victim_id)
grant_oauth(victim_id)
planted_token = h.api_token_headers(victim_id)  # minted with the stolen account
with Session(engine) as db:
    now = datetime.now(timezone.utc)
    db.add(AppLaunchToken(token="pending-launch", user_id=victim_id, slug="notes",
                          created_at=now, expires_at=now + timedelta(seconds=60)))
    db.commit()
h.check("stolen session works before", stolen.get("/profile").status_code, 200)
admin_post(f"/admin/users/{victim_id}/reset-password", password="Brand-new-pass-1")
h.check("stolen portal session is dead", stolen.get("/profile", follow_redirects=False).status_code != 200, True)
h.check("stolen app session is dead", stolen_app.get("/api/v1/user/me").status_code, 401)
h.check("OAuth grants are gone", oauth_count(victim_id), 0)
h.check("an API token minted with the account is dead",
        client.get("/api/v1/user/me", headers=planted_token).status_code, 401)
r = h.app_host_client("notes").post("/api/v1/session/exchange", json={"token": "pending-launch"})
h.check("an unexchanged launch token can't mint a new app session", r.status_code, 401)

print("--- so does a CLI reset ---")
cli_id = h.add_user("cli@example.com")
cli_session = portal_client("cli@example.com")
grant_oauth(cli_id)
getpass.getpass = lambda prompt="": "Another-new-pass-2"
h.check("cli reset-password exits 0", cli.reset_password("cli@example.com"), 0)
h.check("existing session is dead", cli_session.get("/profile", follow_redirects=False).status_code != 200, True)
h.check("OAuth grants are gone", oauth_count(cli_id), 0)

print("--- a self-service password change ends other sessions and grants ---")
self_id = h.add_user("self@example.com", "admin")
other_device = portal_client("self@example.com")
here = portal_client("self@example.com")
grant_oauth(self_id)
own_token = h.api_token_headers(self_id)
page = here.get("/profile")
csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
here.post("/profile/change-password", data={
    "_csrf": csrf, "old_password": h.PASSWORD,
    "new_password": "Changed-pass-3", "new_password_confirm": "Changed-pass-3"})
h.check("this browser stays signed in", here.get("/profile").status_code, 200)
h.check("the other device is signed out",
        other_device.get("/profile", follow_redirects=False).status_code != 200, True)
h.check("OAuth grants are gone", oauth_count(self_id), 0)
h.check("the user's own API token is kept",
        client.get("/api/v1/user/me", headers=own_token).status_code, 200)

print("--- demotion takes away admin-only credentials ---")
demoted_id = h.add_user("demoted@example.com", "admin")
token = h.api_token_headers(demoted_id)
grant_oauth(demoted_id)
h.check("token works while admin", client.get("/api/v1/user/me", headers=token).status_code, 200)
with Session(engine) as db:
    db.add(ScheduledRun(app_slug="notes", tool_name="any", user_id=demoted_id, created_by=demoted_id,
                        next_run_at=datetime.now(timezone.utc) + timedelta(days=1)))
    db.commit()
    sched_id = db.exec(select(ScheduledRun).where(ScheduledRun.user_id == demoted_id)).first().id
admin_post(f"/admin/users/{demoted_id}/role", role="user")
with Session(engine) as db:
    h.check("API tokens deleted",
            len(db.exec(select(ApiToken).where(ApiToken.created_by == demoted_id)).all()), 0)
h.check("OAuth grants deleted", oauth_count(demoted_id), 0)
h.check("old token no longer authenticates",
        client.get("/api/v1/user/me", headers=token).status_code, 401)
out = fire_schedule(sched_id)
h.check("their schedule refuses to run",
        (out["status"], "no longer an admin" in out["result"]), ("error", True))

print("--- a token never reaches an app its owner can't ---")
staff_id = h.add_user("staff@example.com")
staff_token = h.api_token_headers(staff_id)  # e.g. one left over from before a demotion
r = client.get("/api/v1/storage", headers={**staff_token, "X-Portal-App": "notes"})
h.check("non-admin token on an ungranted app", r.status_code, 403)

print("--- sessions expire on the server ---")
idle_id = h.add_user("idle@example.com")
idle = portal_client("idle@example.com")
with Session(engine) as db:
    for row in db.exec(select(UserSession).where(UserSession.user_id == idle_id)).all():
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(days=15)
        db.add(row)
    db.commit()
h.check("idle 15 days (> SESSION_MAX_AGE): signed out",
        idle.get("/profile", follow_redirects=False).status_code != 200, True)
old_id = h.add_user("old@example.com")
old = portal_client("old@example.com")
with Session(engine) as db:
    for row in db.exec(select(UserSession).where(UserSession.user_id == old_id)).all():
        row.created_at = datetime.now(timezone.utc) - timedelta(days=31)
        db.add(row)
    db.commit()
h.check("31 days old but active: signed out",
        old.get("/profile", follow_redirects=False).status_code != 200, True)
app_user = h.open_app("notes", admin_id)
h.check("fresh app session works", app_user.get("/api/v1/user/me").status_code, 200)
with Session(engine) as db:
    for row in db.exec(select(AppSession).where(AppSession.user_id == admin_id)).all():
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(days=15)
        db.add(row)
    db.commit()
h.check("idle app session: refused", app_user.get("/api/v1/user/me").status_code, 401)

print("--- API responses are never cached ---")
notes = h.open_app("notes", admin_id)
csrf = h.csrf_of(notes)
notes.put("/api/v1/storage/a.json", content=b"{}", headers={"X-CSRF-Token": csrf,
                                                             "Content-Type": "application/json"})
r = notes.get("/api/v1/storage/a.json")
h.check("storage GET: Cache-Control no-store", r.headers.get("cache-control"), "no-store")
h.check("portal page unaffected", "no-store" in (client.get("/login").headers.get("cache-control") or ""), False)

h.finish()
