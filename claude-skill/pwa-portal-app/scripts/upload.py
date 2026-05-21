#!/usr/bin/env python3
"""Upload a packaged PWA Portal app zip to a portal.

Usage:
    upload.py <zip_path> [--replace] [--portal-url URL] [--token TOKEN]

Defaults are read (in order) from:
    1. CLI flags
    2. PORTAL_URL / PORTAL_TOKEN environment variables
    3. ~/.config/pwa-portal/config.json (created by configure.py)

When --replace is passed, the script targets `PUT /api/v1/apps/<slug>`
(in-place update; per-user storage is preserved). The slug is read from the
zip's `portal.json`. Without --replace, the script POSTs to
`/api/v1/apps/upload` and the portal rejects an existing slug.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "pwa-portal" / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def build_multipart(zip_path: Path) -> tuple[bytes, str]:
    boundary = "----PortalSkill" + secrets.token_hex(8)
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="bundle"; filename="{zip_path.name}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/zip\r\n\r\n")
    parts.append(zip_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def read_slug_from_zip(zip_path: Path) -> str:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("portal.json") as f:
                manifest = json.load(f)
    except (KeyError, zipfile.BadZipFile) as e:
        raise SystemExit(
            f"Could not read portal.json from {zip_path}: {e}"
        )
    except json.JSONDecodeError as e:
        raise SystemExit(f"portal.json in {zip_path} is not valid JSON: {e}")
    slug = manifest.get("slug")
    if not isinstance(slug, str) or not slug:
        raise SystemExit(
            f"portal.json in {zip_path} is missing a 'slug' field."
        )
    return slug


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a portal app zip.")
    parser.add_argument("zip_path", help="Path to the packaged .zip")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing app in place (preserves per-user storage).",
    )
    parser.add_argument("--portal-url", help="Portal base URL (e.g. https://portal.example.com)")
    parser.add_argument("--token", help="API token (from <portal_url>/admin/tokens)")
    args = parser.parse_args()

    cfg = load_config()
    portal_url = args.portal_url or os.environ.get("PORTAL_URL") or cfg.get("portal_url")
    token = args.token or os.environ.get("PORTAL_TOKEN") or cfg.get("token")

    if not portal_url:
        print(
            "Portal URL not set. Use --portal-url, $PORTAL_URL, "
            "or run scripts/configure.py.",
            file=sys.stderr,
        )
        return 2
    if not token:
        print(
            "API token not set. Use --token, $PORTAL_TOKEN, "
            "or run scripts/configure.py.",
            file=sys.stderr,
        )
        return 2

    portal_url = portal_url.rstrip("/")
    zip_path = Path(args.zip_path).resolve()
    if not zip_path.is_file():
        print(f"Not a file: {zip_path}", file=sys.stderr)
        return 1

    body, boundary = build_multipart(zip_path)
    if args.replace:
        slug = read_slug_from_zip(zip_path)
        url = f"{portal_url}/api/v1/apps/{slug}"
        method = "PUT"
    else:
        url = f"{portal_url}/api/v1/apps/upload"
        method = "POST"
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"Upload failed: HTTP {e.code}", file=sys.stderr)
        try:
            data = json.loads(detail)
            if "detail" in data:
                print(f"  {data['detail']}", file=sys.stderr)
            else:
                print(f"  {detail[:500]}", file=sys.stderr)
        except json.JSONDecodeError:
            print(f"  {detail[:500]}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        return 1

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError:
        print(response_body)
        return 0

    verb = "Replaced" if args.replace else "Uploaded"
    print(
        f"{verb} {result.get('name')} "
        f"(slug: {result.get('slug')}, version: {result.get('version')})"
    )
    print(f"Live at: {portal_url}/apps/{result.get('slug')}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
