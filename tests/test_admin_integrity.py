"""Regression test: admin state can't be raced, misread, or flushed away.

Run from anywhere:

    python3 tests/test_admin_integrity.py

**What's being pinned.**

  - Two simultaneous first-run /setup submissions could both create an admin.
  - Two admins demoting each other at once both passed the "last admin"
    check, leaving none — and the CLI had no way back (now ``cli promote``).
  - MCP tools read booleans with ``bool()`` (schema validation is off), so the
    string ``"false"`` meant True: create_app(replace="false") replaced an
    app, set_app_enabled(enabled="false") enabled one.
  - OAuth client secrets created at Admin → MCP OAuth clients were stored in
    plaintext (only self-registered clients were encrypted).
  - Anonymous failed logins shared one retention bucket with admin actions,
    so a flood pushed admin history out of the audit view and the table.
"""
import hashlib
import json
import os
import re
import secrets
import threading

os.environ["MCP_ENABLED"] = "true"

import _harness as h  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from portal import audit, cli  # noqa: E402
from portal.db import engine  # noqa: E402
from portal.models import ApiToken, App, AuditEvent, OAuthClient, User  # noqa: E402

client = h.boot()


def csrf_from(c, path: str) -> str:
    return re.search(r'name="_csrf" value="([^"]+)"', c.get(path).text).group(1)


def admins() -> list[str]:
    with Session(engine) as db:
        return sorted(u.email for u in db.exec(select(User).where(User.role == "admin")).all())


print("--- first-run setup can't be raced ---")
racers = [h.TestClient(h.app, base_url=f"http://{h.SITE}") for _ in range(2)]
forms = [csrf_from(c, "/setup") for c in racers]
results = []


def run_setup(i):
    results.append(racers[i].post("/setup", data={
        "_csrf": forms[i], "email": f"first{i}@example.com", "password": h.PASSWORD,
        "password_confirm": h.PASSWORD, "site_url": h.SITE}, follow_redirects=False).status_code)


threads = [threading.Thread(target=run_setup, args=(i,)) for i in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
h.check("two simultaneous setups create exactly one admin", len(admins()), 1)

print("--- admins can't demote each other into zero ---")
h.add_user("a@example.com", "admin")
h.add_user("b@example.com", "admin")
with Session(engine) as db:
    for u in db.exec(select(User).where(User.email.like("first%"))).all():
        u.role = "user"
        db.add(u)
    db.commit()
a = h.TestClient(h.app, base_url=f"http://{h.SITE}")
b = h.TestClient(h.app, base_url=f"http://{h.SITE}")
h.login(a, "a@example.com")
h.login(b, "b@example.com")
with Session(engine) as db:
    ids = {u.email: u.id for u in db.exec(select(User)).all()}
csrf_a, csrf_b = csrf_from(a, "/admin/users"), csrf_from(b, "/admin/users")
threads = [
    threading.Thread(target=lambda: a.post(f"/admin/users/{ids['b@example.com']}/role",
                                           data={"_csrf": csrf_a, "role": "user"})),
    threading.Thread(target=lambda: b.post(f"/admin/users/{ids['a@example.com']}/role",
                                           data={"_csrf": csrf_b, "role": "user"})),
]
for t in threads:
    t.start()
for t in threads:
    t.join()
h.check("at least one admin survives a mutual demotion", len(admins()) >= 1, True)
with Session(engine) as db:
    for u in db.exec(select(User).where(User.role == "admin")).all():
        u.role = "user"
        db.add(u)
    db.commit()
h.check("cli promote restores an admin", (cli.promote("a@example.com"), admins()),
        (0, ["a@example.com"]))

print("--- MCP booleans mean what they say ---")
raw = secrets.token_urlsafe(24)
with Session(engine) as db:
    db.add(ApiToken(name="t", token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    prefix=raw[:8], created_by=ids["a@example.com"]))
    db.commit()


def mcp(name: str, args: dict):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {raw}",
                                     "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    result = r.json().get("result", {})
    text = " ".join(c.get("text", "") for c in result.get("content", []))
    return result.get("isError", False), result.get("structuredContent"), text


manifest = {"slug": "notes", "name": "Notes", "version": "1.0.0", "entry": "index.html"}
mcp("create_app", {"files": {"portal.json": json.dumps(manifest), "index.html": "<h1>v1</h1>"}})
manifest["version"] = "2.0.0"
err, _, _ = mcp("create_app", {"files": {"portal.json": json.dumps(manifest), "index.html": "<h1>v2</h1>"},
                               "replace": "false"})
with Session(engine) as db:
    version = db.exec(select(App).where(App.slug == "notes")).first().version
h.check('create_app(replace="false") does not replace', (err, version), (True, "1.0.0"))
mcp("set_app_enabled", {"slug": "notes", "enabled": "false"})
with Session(engine) as db:
    enabled = db.exec(select(App).where(App.slug == "notes")).first().enabled
h.check('set_app_enabled(enabled="false") disables', enabled, False)
err, _, text = mcp("set_app_enabled", {"slug": "notes", "enabled": "maybe"})
h.check('a non-boolean is an error, not "true"', (err, "true or false" in text), (True, True))

print("--- OAuth client secrets are encrypted at rest ---")
h.login(client, "a@example.com")
client.post("/admin/oauth-clients", data={
    "_csrf": csrf_from(client, "/admin/oauth-clients"), "name": "Desk",
    "redirect_uris": "https://claude.ai/api/mcp/auth_callback"})
with Session(engine) as db:
    desk = [c for c in db.exec(select(OAuthClient)).all() if c.client_info.get("client_name") == "Desk"]
    h.check("admin-created secret stored encrypted",
            desk[0].client_info["client_secret"].startswith("enc:"), True)
    db.add(OAuthClient(client_id="portal-legacy", client_info={
        "client_id": "portal-legacy", "client_secret": "plaintext-secret", "client_name": "Old",
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}))
    db.commit()
from portal.oauth import oauth_provider, prune_oauth  # noqa: E402

with Session(engine) as db:
    prune_oauth(db)
with Session(engine) as db:
    legacy = db.get(OAuthClient, "portal-legacy")
    h.check("a legacy plaintext secret is encrypted at startup",
            legacy.client_info["client_secret"].startswith("enc:"), True)
import asyncio  # noqa: E402

restored = asyncio.run(oauth_provider.get_client("portal-legacy"))
h.check("  ...and still decrypts to the original", restored.client_secret, "plaintext-secret")

print("--- failed logins can't flush admin history ---")
with Session(engine) as db:
    db.add(AuditEvent(action="user.role.change", actor_email="a@example.com", target="user:x"))
    for _ in range(300):
        db.add(AuditEvent(action="login.failure", actor_email="bot@example.com", target="email:x"))
    db.commit()
page = client.get("/admin/audit").text
h.check("default audit view still shows the admin action", "user.role.change" in page, True)
h.check("  ...and hides failed logins", "login.failure" in page, False)
h.check("failed logins are one click away",
        "login.failure" in client.get("/admin/audit?failed_logins=1").text, True)
saved = audit.MAX_NOISY_ROWS
audit.MAX_NOISY_ROWS = 10
with Session(engine) as db:
    audit.prune(db)
with Session(engine) as db:
    kept = db.exec(select(AuditEvent).where(AuditEvent.action == "user.role.change")).all()
    noisy = db.exec(select(AuditEvent).where(AuditEvent.action == "login.failure")).all()
h.check("pruning trims failed logins without touching admin actions",
        (len(noisy) <= 10, len(kept) >= 1), (True, True))
audit.MAX_NOISY_ROWS = saved

h.finish()
