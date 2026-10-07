"""Regression test: PDF share links obey the PDF limits, and renders are gated.

Run from anywhere:

    python3 tests/test_pdf_limits.py

**What's being pinned.**

  - ``share/create`` with ``kind: "pdf"`` renders a PDF but skipped both the
    2 MB HTML cap and the per-user PDF rate limit that ``/pdf/render``
    enforces, and its stored files counted against no quota.
  - WeasyPrint renders can take tens of seconds and nothing bounded how many
    ran at once, so a burst could occupy the whole sync thread pool (login,
    admin, every API call). Every render now takes one of a few slots, and a
    caller that can't get one in time gets a 503.
"""
import time
from collections import deque

import _harness as h

from sqlmodel import Session, select

from portal import api, shares
from portal.db import engine
from portal.models import ShareLink

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
h.install_app(client, h.api_token_headers(admin_id),
              {"slug": "docs", "name": "Docs", "services": ["pdf"]},
              {"index.html": "<h1>docs</h1>"})
docs = h.open_app("docs", admin_id)
H = {"X-CSRF-Token": h.csrf_of(docs)}


def share_count() -> int:
    with Session(engine) as db:
        return len(db.exec(select(ShareLink)).all())


print("--- PDF share links obey the PDF limits ---")
r = docs.post("/api/v1/share/create", headers=H,
              json={"kind": "pdf", "html": "<p>" + "x" * (2 * 1024 * 1024 + 10) + "</p>"})
h.check("HTML over 2 MB refused", r.status_code, 422)
api._pdf_render_log[admin_id] = deque([time.monotonic()] * api._PDF_RATE_LIMIT_PER_HOUR)
before = share_count()
r = docs.post("/api/v1/share/create", headers=H, json={"kind": "pdf", "html": "<p>hi</p>"})
h.check("over the PDF rate limit: 429", r.status_code, 429)
h.check("  ...and no share minted", share_count() - before, 0)
api._pdf_render_log.clear()
r = docs.post("/api/v1/share/create", headers=H, json={"kind": "pdf", "html": "<p>hi</p>"})
h.check("normal PDF share works", r.status_code, 200)
h.check("  ...and used one rate slot", len(api._pdf_render_log.get(admin_id, ())), 1)
saved = shares.MAX_PDF_SHARE_BYTES_PER_USER
shares.MAX_PDF_SHARE_BYTES_PER_USER = 3000  # about one tiny PDF
r = docs.post("/api/v1/share/create", headers=H, json={"kind": "pdf", "html": "<p>again</p>"})
h.check("over the per-user share storage cap: 413", r.status_code, 413)
h.check("  ...and the rate slot was refunded", len(api._pdf_render_log.get(admin_id, ())), 1)
shares.MAX_PDF_SHARE_BYTES_PER_USER = saved

print("--- renders are gated ---")
api._PDF_SLOT_WAIT_SECONDS = 0.2
held = [api._pdf_slots.acquire() for _ in range(api._PDF_CONCURRENCY)]
r = docs.post("/api/v1/pdf/render", headers=H, json={"html": "<p>hi</p>"})
h.check("no free render slot: 503", r.status_code, 503)
r = docs.post("/api/v1/share/create", headers=H, json={"kind": "pdf", "html": "<p>hi</p>"})
h.check("  ...for share links too", r.status_code, 503)
for _ in held:
    api._pdf_slots.release()
r = docs.post("/api/v1/pdf/render", headers=H, json={"html": "<p>hi</p>"})
h.check("slot free again: renders", (r.status_code, r.content[:4]), (200, b"%PDF"))

h.finish()
