"""Regression test: outbound email is verified, capped, and checked consistently.

Run from anywhere:

    python3 tests/test_email_limits.py

**What's being pinned.**

  - SMTP STARTTLS / implicit TLS ran without certificate or hostname checks
    (smtplib's default context verifies neither), so anyone on the path could
    pose as the mail server and collect the SMTP login.
  - ``/api/v1/email/send`` took any number of recipients for one rate slot,
    and a CR/LF subject crashed mid-send with a 500.
  - App-tool email checked the recipient-domain allowlist against the text
    after the last ``@``, which SMTP doesn't use: ``x@evil.com (a@ok.com``
    passed the check and was delivered to x@evil.com. Its rate limit (and the
    PDF one) was consulted only AFTER sending / rendering, so it never
    stopped anything.
  - A form's notify address (from the untrusted manifest) skipped the
    recipient-domain allowlist entirely.

SMTP is faked throughout; nothing leaves the machine.
"""
import smtplib
import ssl
import time
from collections import deque

import _harness as h

from sqlmodel import Session, select

from portal import api, app_tools
from portal.apps import PortalAppTool
from portal.db import engine
from portal.models import App, ShareLink
from portal.settings_store import set_setting

SENT: list[list[str]] = []
CONTEXTS: list = []


class FakeSMTP:
    def __init__(self, *a, context=None, **k):
        if context is not None:
            CONTEXTS.append(context)

    def starttls(self, context=None):
        CONTEXTS.append(context)

    def login(self, *a):
        pass

    def send_message(self, msg, *a, **k):
        SENT.append([a.strip() for a in str(msg["To"]).split(",")])

    def quit(self):
        pass


smtplib.SMTP = FakeSMTP
smtplib.SMTP_SSL = FakeSMTP

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
token = h.api_token_headers(admin_id)
h.install_app(client, token, {
    "slug": "mailer", "name": "Mailer", "services": ["email", "pdf"],
    "forms": [{"name": "contact", "title": "Contact", "notify_email": "exfil@attacker.example",
               "fields": [{"name": "msg", "label": "Message", "type": "textarea"}]}],
}, {"index.html": "<h1>mailer</h1>"})
with Session(engine) as db:
    for k, v in (("smtp_host", "smtp.test"), ("smtp_port", "587"), ("smtp_use_tls", "true"),
                 ("smtp_from", "office@mybiz.example")):
        set_setting(db, k, v)
    db.commit()

print("--- the mail server's certificate is verified ---")
mailer = h.open_app("mailer", admin_id)
csrf = h.csrf_of(mailer)
H = {"X-CSRF-Token": csrf}
r = mailer.post("/api/v1/email/send", headers=H,
                json={"to": "a@mybiz.example", "subject": "hi", "text": "x"})
h.check("send succeeds", r.status_code, 200)
ctx = CONTEXTS[-1] if CONTEXTS else None
h.check("STARTTLS gets a verifying context",
        (getattr(ctx, "verify_mode", None), getattr(ctx, "check_hostname", None)),
        (ssl.CERT_REQUIRED, True))

print("--- the SDK send endpoint ---")
many = [f"r{i}@mybiz.example" for i in range(21)]
r = mailer.post("/api/v1/email/send", headers=H, json={"to": many, "subject": "s", "text": "x"})
h.check("21 recipients refused", r.status_code, 422)
r = mailer.post("/api/v1/email/send", headers=H,
                json={"to": "a@mybiz.example", "subject": "a\r\nBcc: x@evil.example", "text": "x"})
h.check("CR/LF subject refused as a 422 (not a 500)", r.status_code, 422)
api._email_send_log[admin_id] = deque([time.monotonic()] * 98)
SENT.clear()
r = mailer.post("/api/v1/email/send", headers=H,
                json={"to": ["a@mybiz.example", "b@mybiz.example", "c@mybiz.example"],
                      "subject": "s", "text": "x"})
h.check("3 recipients with 2 slots left -> 429, nothing sent", (r.status_code, SENT), (429, []))
r = mailer.post("/api/v1/email/send", headers=H,
                json={"to": ["a@mybiz.example", "b@mybiz.example"], "subject": "s", "text": "x"})
h.check("2 recipients with 2 slots left -> sent", r.status_code, 200)
api._email_send_log.clear()

print("--- app-tool email ---")
with Session(engine) as db:
    set_setting(db, "email_recipient_domains", "mybiz.example")
    db.commit()


def tool(to: str, kind: str = "email") -> dict:
    deliver = {"kind": kind}
    if kind == "email":
        deliver.update(to=to, subject="Line one\nline two")
    params = [{"name": "who", "type": "string"}]
    return PortalAppTool(name="t", description="d", params=params,
                         render={"html": "<p>report</p>"}, deliver=deliver).model_dump()


def run(t: dict, **args):
    try:
        return app_tools.run_tool(slug="mailer", tool=t, args=args, user_id=admin_id)
    except app_tools.AppToolError as e:
        return f"refused: {e}"


for label, addr in (("comment trick", "x@evil.example (a@mybiz.example"),
                    ("angle-bracket trick", "<boss@rival.example>@mybiz.example"),
                    ("display name", "Boss <boss@rival.example>")):
    SENT.clear()
    out = run(tool("{{ who }}"), who=addr)
    h.check(f"{label} refused, nothing sent",
            (str(out).startswith("refused"), SENT), (True, []))
SENT.clear()
out = run(tool("{{ who }}"), who="ok@mybiz.example")
h.check("allowed recipient delivered", (out, SENT),
        ({"delivered": "email", "count": 1}, [["ok@mybiz.example"]]))
api._email_send_log[admin_id] = deque([time.monotonic()] * api._EMAIL_RECIPIENTS_PER_HOUR)
SENT.clear()
out = run(tool("{{ who }}"), who="ok@mybiz.example")
h.check("over the rate limit: refused BEFORE sending",
        (str(out).startswith("refused"), SENT), (True, []))
api._email_send_log.clear()

print("--- app-tool PDF rate limit is checked before rendering ---")
api._pdf_render_log[admin_id] = deque([time.monotonic()] * api._PDF_RATE_LIMIT_PER_HOUR)
with Session(engine) as db:
    before = len(db.exec(select(ShareLink)).all())
out = run(tool("", kind="share"))
with Session(engine) as db:
    after = len(db.exec(select(ShareLink)).all())
h.check("share tool over the PDF limit: refused, no share minted",
        (str(out).startswith("refused"), after - before), (True, 0))
api._pdf_render_log.clear()

print("--- form notification email ---")
SENT.clear()
anon = h.app_host_client("mailer")
r = anon.post("/forms/contact", data={"msg": "hello"})
h.check("submission still accepted", r.status_code, 200)
h.check("  ...but not mailed to an off-allowlist notify address", SENT, [])
with Session(engine) as db:
    app_row = db.exec(select(App).where(App.slug == "mailer")).first()
    app_row.forms = [{**app_row.forms[0], "notify_email": "owner@mybiz.example"}]
    db.add(app_row)
    db.commit()
r = anon.post("/forms/contact", data={"msg": "hello again"})
h.check("an on-allowlist notify address is mailed", SENT, [["owner@mybiz.example"]])

h.finish()
