"""Shared setup for the standalone regression scripts in this directory.

Import this FIRST in a test script — before anything from ``portal`` — because
``portal.config`` reads settings at import time. It points the portal at a
throwaway SQLite database + data dir (nothing touches ``data/``) and pins every
setting that changes routing, so a developer's ``.env`` can't leak in. A script
that needs a different value (e.g. ``MCP_ENABLED``) sets it in ``os.environ``
before importing this module.

Requires ``httpx`` (Starlette's TestClient dependency), which is not a runtime
dependency of the portal:  pip install httpx
"""
import hashlib
import io
import json
import logging
import os
import secrets
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TMP = tempfile.mkdtemp(prefix="portal-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["DATA_DIR"] = TMP
os.environ["SECRET_KEY"] = "x" * 40
os.environ.setdefault("SITE_URL", "portal.test")
os.environ["COOKIES_SECURE"] = "false"
os.environ["CHILD_APPS_SAME_ORIGIN"] = "false"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["HTTP_ONLY"] = "false"
os.environ.setdefault("MCP_ENABLED", "false")

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:  # pragma: no cover - setup guidance
    raise SystemExit(f"missing dependency: {exc.name} (pip install httpx)")

from sqlmodel import Session  # noqa: E402

from portal.db import engine  # noqa: E402
from portal.main import app  # noqa: E402
from portal.models import ApiToken, AppLaunchToken, User  # noqa: E402
from portal.security import hash_password  # noqa: E402

PASSWORD = "Correct-horse-9"
SITE = os.environ["SITE_URL"]

_fails: list[str] = []
_checks = 0


def check(label: str, got, want) -> None:
    global _checks
    _checks += 1
    ok = got == want
    if not ok:
        _fails.append(label)
    print(f"{'OK   ' if ok else 'FAIL '} {label:<60} got={got!s:<12} want={want}")


def finish() -> None:
    print(f"\n{_checks - len(_fails)}/{_checks} OK")
    if _fails:
        print(f"FAILURES: {_fails}")
        raise SystemExit(1)


def boot() -> TestClient:
    """Enter the app lifespan (runs the migrations) and return a portal client."""
    # Alembic's migration log would otherwise bury the results.
    logging.disable(logging.INFO)
    client = TestClient(app, base_url=f"http://{SITE}")
    client.__enter__()
    logging.disable(logging.NOTSET)
    return client


def app_host_client(slug: str) -> TestClient:
    return TestClient(app, base_url=f"http://{slug}.apps.{SITE}")


def add_user(email: str, role: str = "user") -> int:
    with Session(engine) as db:
        user = User(email=email, password_hash=hash_password(PASSWORD), role=role)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id


def api_token_headers(user_id: int) -> dict:
    raw = secrets.token_urlsafe(24)
    with Session(engine) as db:
        db.add(ApiToken(
            name="test", token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8], created_by=user_id,
        ))
        db.commit()
    return {"Authorization": f"Bearer {raw}"}


def make_zip(manifest: dict, files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("portal.json", json.dumps(manifest))
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def install_app(client: TestClient, headers: dict, manifest: dict, files: dict) -> None:
    bundle = make_zip({"version": "1.0.0", "entry": "index.html", **manifest}, files)
    r = client.post(
        "/api/v1/apps/upload", headers=headers,
        files={"bundle": ("app.zip", bundle, "application/zip")},
    )
    assert r.status_code == 200, f"install {manifest.get('slug')}: {r.status_code} {r.text}"


def open_app(slug: str, user_id: int) -> TestClient:
    """A client holding ``user_id``'s AppSession on ``slug``'s subdomain,
    obtained through the real launch-token exchange."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(AppLaunchToken(
            token=token, user_id=user_id, slug=slug,
            created_at=now, expires_at=now + timedelta(seconds=60),
        ))
        db.commit()
    client = app_host_client(slug)
    r = client.post("/api/v1/session/exchange", json={"token": token})
    assert r.status_code == 200, f"exchange: {r.status_code} {r.text}"
    return client


def csrf_of(client: TestClient) -> str:
    return client.get("/api/v1/csrf-token").json()["csrf_token"]


def login(client: TestClient, email: str) -> None:
    """Sign ``client`` in through the real portal login form."""
    import re

    page = client.get("/login")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = client.post(
        "/login", data={"email": email, "password": PASSWORD, "_csrf": csrf, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"login {email}: {r.status_code}"
