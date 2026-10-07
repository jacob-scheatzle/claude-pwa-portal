"""Regression test: OAuth client redirect URIs are restricted and shown.

Run from anywhere:

    python3 tests/test_oauth_redirect_uris.py

**What's being pinned.** ``/register`` (dynamic client registration) is open
to anyone, and the SDK types ``redirect_uris`` as ``AnyUrl`` — any scheme. A
client could register ``javascript:…`` and, once an admin answered the consent
page (Approve *or* Deny), the interstitial rendered it as a "click here to
continue" link on the portal origin: script in the admin's session. Separately,
the consent page showed only the client's self-chosen name, so a client calling
itself "Claude" with its own https redirect got an admin-equivalent MCP token
from one click.

Now registration accepts only ``https://`` or loopback ``http://`` (the rule
admin-created clients already had, minus its ``http://localhost.evil``
prefix-match hole), the consent page names the host access goes to and flags
self-registered clients, and a client stored before the check can never put
its redirect in front of the admin.
"""
import base64
import hashlib
import html
import os
import re
import secrets

os.environ["SITE_URL"] = "localhost"  # an OAuth issuer must be https or localhost
os.environ["MCP_ENABLED"] = "true"

import _harness as h  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from portal.db import engine  # noqa: E402
from portal.models import OAuthClient, OAuthCode  # noqa: E402

client = h.boot()
h.add_user("admin@example.com", "admin")


def register(uri: str):
    return client.post("/register", json={
        "client_name": "Claude", "redirect_uris": [uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })


def consent_page(client_id: str, redirect_uri: str):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st",
    }, follow_redirects=False)
    assert r.status_code == 302, f"authorize: {r.status_code} {r.text}"
    return client.get(r.headers["location"].split("localhost", 1)[1])


def answer(page, decision: str):
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    txn = re.search(r'name="txn" value="([^"]+)"', page.text).group(1)
    return client.post("/oauth/consent", data={"_csrf": csrf, "txn": txn, "decision": decision})


print("--- dynamic registration only accepts https / loopback http ---")
for uri in ("javascript:alert(document.cookie)//", "data:text/html,<script>x</script>",
            "http://evil.example/cb", "http://localhost.evil.example/cb",
            "https://claude.ai/cb#frag"):
    r = register(uri)
    h.check(f"register {uri[:34]}", (r.status_code, r.json().get("error")),
            (400, "invalid_redirect_uri"))
for uri in ("https://claude.ai/api/mcp/auth_callback", "http://localhost:3000/cb",
            "http://127.0.0.1:8765/callback"):
    h.check(f"register {uri[:34]}", register(uri).status_code, 201)

h.login(client, "admin@example.com")

print("--- the consent page names where access goes ---")
cid = register("https://evil.example/cb").json()["client_id"]
page = consent_page(cid, "https://evil.example/cb")
h.check("consent shows the destination host", "evil.example" in page.text, True)
h.check("  ...and flags a self-registered client", "registered itself" in page.text, True)
done = answer(page, "approve")
h.check("redirect page names the host, not 'Claude'",
        ("Connecting you back to evil.example" in done.text, "back to Claude" in done.text),
        (True, False))

print("--- a client stored before the check can't reach the admin ---")
legacy = "legacy-client"
bad = "javascript:fetch('/admin/tokens')//"
with Session(engine) as db:
    db.add(OAuthClient(client_id=legacy, client_info={
        "client_id": legacy, "client_name": "Claude", "redirect_uris": [bad],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }))
    db.commit()


def legacy_txn() -> str:
    verifier = secrets.token_urlsafe(48)
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": legacy, "redirect_uri": bad, "state": "st",
        "code_challenge": base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    }, follow_redirects=False)
    return r.headers["location"].split("txn=", 1)[1]


page = client.get(f"/oauth/consent?txn={legacy_txn()}")
h.check("consent refuses to render", "name=\"decision\"" in page.text, False)
h.check("  ...and never shows the URI", "javascript:" in page.text, False)
csrf = re.search(r'name="_csrf" value="([^"]+)"', client.get("/profile").text).group(1)
with Session(engine) as db:
    codes_before = len(db.exec(select(OAuthCode)).all())
for decision in ("approve", "deny"):
    # A POST crafted without ever seeing the consent form.
    r = client.post("/oauth/consent", data={"_csrf": csrf, "txn": legacy_txn(), "decision": decision})
    h.check(f"forced {decision}: refused", "redirect address isn't allowed" in html.unescape(r.text), True)
    h.check(f"  ...no javascript: link", "javascript:" in r.text, False)
with Session(engine) as db:
    h.check("  ...and no authorization code minted",
            len(db.exec(select(OAuthCode)).all()), codes_before)

print("--- admin-created clients ---")
form = client.get("/admin/oauth-clients")
csrf = re.search(r'name="_csrf" value="([^"]+)"', form.text).group(1)
r = client.post("/admin/oauth-clients", data={
    "_csrf": csrf, "name": "Sneaky", "redirect_uris": "http://localhost.evil.example/cb"})
with Session(engine) as db:
    sneaky = [c for c in db.exec(select(OAuthClient)).all()
              if (c.client_info or {}).get("client_name") == "Sneaky"]
h.check("http://localhost.evil.example rejected", len(sneaky), 0)
r = client.post("/admin/oauth-clients", data={
    "_csrf": csrf, "name": "Desk", "redirect_uris": "https://claude.ai/api/mcp/auth_callback"})
with Session(engine) as db:
    desk = [c for c in db.exec(select(OAuthClient)).all()
            if (c.client_info or {}).get("client_name") == "Desk"]
h.check("https redirect accepted", len(desk), 1)
page = consent_page(desk[0].client_id, "https://claude.ai/api/mcp/auth_callback")
h.check("pre-registered client: host shown", "claude.ai" in page.text, True)
h.check("  ...without the self-registered warning", "registered itself" in page.text, False)

h.finish()
