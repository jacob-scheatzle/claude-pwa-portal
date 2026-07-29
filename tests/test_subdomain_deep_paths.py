"""Regression test for cookie-less deep paths on a child-app subdomain.

Run from anywhere:

    python3 tests/test_subdomain_deep_paths.py

Boots the real portal app against a throwaway SQLite database in a temp dir
(nothing touches ``data/``), installs one enabled app row, then hits
``<slug>.apps.<SITE_URL>/<deep path>`` with no AppSession cookie and asserts
what each kind of request gets back.

**What's being pinned.** ``serve_subdomain_request`` splits the no-cookie deep
path by request kind:

  - a top-level navigation (a bookmarked or typed deep link) gets a 303 to the
    portal-origin launcher, which re-mints a launch token and lands the user
    back in the app;
  - anything else — script / style / image / fetch subresources — gets a flat
    404.

The split exists because a 303 was never useful to a subresource: the browser
follows it, receives the launcher's HTML, and hands HTML to a caller expecting
JS/CSS/JSON, surfacing as a MIME or parse error that reads like an app bug
rather than "your session expired".

The decision runs off ``Sec-Fetch-Dest``, which every current browser sends and
which page JavaScript cannot forge (it's a forbidden header name). ``Accept``
is only consulted when ``Sec-Fetch-Dest`` is absent entirely. Getting that
precedence backwards would quietly restore the old behaviour for every
``<script>`` tag that carries a permissive ``Accept``, which is why it has its
own case below.

The second half guards the things this must NOT change: the launch-token
bootstrap page, the pre-auth ``portal-sdk.js``, and the portal-origin
``/apps/<slug>/`` routes — including that a real and a bogus slug are still
indistinguishable to an unauthenticated caller.

Requires ``httpx`` (Starlette's TestClient dependency), which is not a runtime
dependency of the portal:  pip install httpx
"""
import logging
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Must be set before portal.config is imported — settings are read at import
# time. A temp DATA_DIR keeps the run from touching a real data/ directory.
_TMP = tempfile.mkdtemp(prefix="portal-subdomain-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["DATA_DIR"] = _TMP
os.environ["SECRET_KEY"] = "x" * 40
os.environ["SITE_URL"] = "portal.test"
os.environ["COOKIES_SECURE"] = "false"
# Keep the run hermetic: no MCP server, no OAuth routes, no scheduler noise.
os.environ["MCP_ENABLED"] = "false"

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:  # pragma: no cover - setup guidance
    raise SystemExit(f"missing dependency: {exc.name} (pip install httpx)")

from sqlmodel import Session  # noqa: E402

from portal.db import engine  # noqa: E402
from portal.main import app  # noqa: E402
from portal.models import App  # noqa: E402

SUB = "hazard-checklist.apps.portal.test"
DEEP = "/reports/q3.json"

fails: list[str] = []
checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    ok = got == want
    if not ok:
        fails.append(label)
    print(f"{'OK   ' if ok else 'FAIL '} {label:<52} got={got!s:<12} want={want}")


def get(path: str, **headers):
    return client.get(
        path, headers={"Host": SUB, **headers}, follow_redirects=False
    )


# Alembic's migration log would otherwise bury the results; init_db runs
# `upgrade head` inside the lifespan hook that TestClient enters below.
logging.disable(logging.INFO)
with TestClient(app) as client:
    logging.disable(logging.NOTSET)

    with Session(engine) as db:
        db.add(
            App(
                slug="hazard-checklist",
                name="Hazard Checklist",
                version="1.0.0",
                entry="index.html",
                enabled=True,
            )
        )
        db.commit()

    print("--- cookie-less deep path on an app subdomain ---")
    check("subresource (Sec-Fetch-Dest: script)",
          get(DEEP, **{"Sec-Fetch-Dest": "script"}).status_code, 404)
    check("fetch/XHR (Sec-Fetch-Dest: empty)",
          get(DEEP, **{"Sec-Fetch-Dest": "empty", "Accept": "*/*"}).status_code, 404)
    # Precedence guard: a <script> tag can carry a text/html Accept. If Accept
    # were consulted first, this would 303 and the fix would be undone.
    check("Sec-Fetch-Dest beats a text/html Accept",
          get(DEEP, **{"Sec-Fetch-Dest": "image",
                       "Accept": "text/html,*/*"}).status_code, 404)

    nav = get(DEEP, **{"Sec-Fetch-Dest": "document"})
    check("navigation (Sec-Fetch-Dest: document)", nav.status_code, 303)
    check("  ...lands on the portal launcher", nav.headers.get("location"),
          "http://portal.test/apps/hazard-checklist/")

    check("no Sec-Fetch-Dest, Accept: text/html",
          get(DEEP, Accept="text/html").status_code, 303)
    check("no Sec-Fetch-Dest, Accept: */* (curl, scanners)",
          get(DEEP, Accept="*/*").status_code, 404)
    check("no Sec-Fetch-Dest, no Accept",
          get(DEEP).status_code, 404)

    print("--- must not regress: the launch handshake ---")
    root = get("/", **{"Sec-Fetch-Dest": "document"})
    check("subdomain root still serves bootstrap HTML", root.status_code, 200)
    check("  ...and it is the token-exchange page",
          "session/exchange" in root.text, True)
    check("portal-sdk.js still served pre-auth",
          get("/portal-sdk.js", **{"Sec-Fetch-Dest": "script"}).status_code, 200)

    print("--- must not regress: portal origin ---")
    real = client.get("/apps/hazard-checklist/", headers={"Host": "portal.test"},
                      follow_redirects=False)
    bogus = client.get("/apps/no-such-app/", headers={"Host": "portal.test"},
                       follow_redirects=False)
    check("/apps/<real>/ unauth -> login", real.status_code, 303)
    check("/apps/<bogus>/ unauth -> login", bogus.status_code, 303)
    # The point of the pair: an unauthenticated caller cannot tell a real slug
    # from an invented one on the portal origin.
    check("  ...and the two are indistinguishable",
          real.headers.get("location") != bogus.headers.get("location")
          and "hazard" in (bogus.headers.get("location") or ""), False)
    check("/health (what the container healthcheck hits)",
          client.get("/health", headers={"Host": "portal.test"}).status_code, 200)

print(f"\n{checks - len(fails)}/{checks} OK")
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
