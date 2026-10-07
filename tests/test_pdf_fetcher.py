"""Regression test: PDFs embed data: assets, fetch nothing else, and don't break.

Run from anywhere:

    python3 tests/test_pdf_fetcher.py

**What's being pinned.** Every PDF render passes WeasyPrint a URL fetcher that
only resolves ``data:`` URIs (no SSRF, no local-file reads). It used to wrap
``weasyprint.urls.default_url_fetcher``, which WeasyPrint 69 removed; on 70 a
plain-function fetcher also trips WeasyPrint's own error path, so any render
that touched a resource — a branded PDF's logo, any <img> — failed outright.
Unpinned dependencies let that version into the image. The fetcher is now
WeasyPrint's ``URLFetcher`` restricted to ``data:``; this test runs against
whatever WeasyPrint is installed (the lockfile pins the one the image ships).
"""
import base64
import struct
import zlib

import _harness as h

import weasyprint

from portal.storage_backend import get_storage

client = h.boot()
admin_id = h.add_user("admin@example.com", "admin")
h.install_app(client, h.api_token_headers(admin_id),
              {"slug": "docs", "name": "Docs", "services": ["pdf"]}, {"index.html": "d"})
docs = h.open_app("docs", admin_id)
H = {"X-CSRF-Token": h.csrf_of(docs)}
print(f"(WeasyPrint {weasyprint.__version__})")


def png(color: bytes) -> bytes:
    raw = b"".join(b"\x00" + color * 8 for _ in range(8))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def render(html: str, **extra):
    return docs.post("/api/v1/pdf/render", headers=H, json={"html": html, **extra})


data_uri = "data:image/png;base64," + base64.b64encode(png(b"\x10\x80\x30")).decode()
r = render(f'<p>logo</p><img src="{data_uri}">')
h.check("a data: image renders", r.status_code, 200)
h.check("  ...and is embedded in the PDF", b"/Subtype /Image" in r.content, True)

secret = h.TMP + "/secret.txt"
open(secret, "w").write("TOP-SECRET-MARKER")
r = render(f'<p>hi</p><img src="file://{secret}"><link rel="stylesheet" href="file://{secret}">'
           '<img src="http://127.0.0.1:9/x.png"><img src="http://169.254.169.254/latest/meta-data/">')
h.check("file:// and http:// refs don't break the render", r.status_code, 200)
h.check("  ...and nothing was read from disk", b"TOP-SECRET-MARKER" in r.content, False)
h.check("  ...and no image was embedded", b"/Subtype /Image" in r.content, False)

# Branded PDFs inline the uploaded logo as a data: URI — the case that broke.
get_storage().write("branding/logo-test.png", png(b"\xc0\x40\x20"))
from sqlmodel import Session  # noqa: E402

from portal.db import engine  # noqa: E402
from portal.settings_store import set_setting  # noqa: E402

with Session(engine) as db:
    set_setting(db, "branding_logo_path", "logo-test.png")
    db.commit()
r = render("<p>invoice</p>", branded=True)
h.check("a branded PDF with a logo renders", r.status_code, 200)
h.check("  ...with the logo embedded", b"/Subtype /Image" in r.content, True)

share = docs.post("/api/v1/share/create", headers=H,
                  json={"kind": "pdf", "html": f'<img src="{data_uri}">'})
h.check("a PDF share with an image renders", share.status_code, 200)

h.finish()
