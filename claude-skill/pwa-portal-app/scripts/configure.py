#!/usr/bin/env python3
"""Save PWA Portal URL + API token to ~/.config/pwa-portal/config.json.

Interactive; run once per workstation. Token is shown once when you create it at
<portal_url>/admin/tokens — paste it here.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_DIR = Path.home() / ".config" / "pwa-portal"
CONFIG_PATH = CONFIG_DIR / "config.json"


def main() -> int:
    print("PWA Portal — Claude skill setup\n")

    current: dict = {}
    if CONFIG_PATH.is_file():
        try:
            current = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass

    url_default = current.get("portal_url", "")
    url_prompt = f"Portal URL (e.g. https://portal.example.com) [{url_default}]: "
    url = input(url_prompt).strip() or url_default

    token_keep = bool(current.get("token"))
    token_prompt = (
        "API token (leave blank to keep current): "
        if token_keep
        else "API token (from <portal_url>/admin/tokens): "
    )
    # getpass: an admin API token shouldn't be echoed to the screen (or into
    # a terminal recording / scrollback).
    token = getpass.getpass(token_prompt).strip()
    if not token and token_keep:
        token = current["token"]

    if not url:
        print("Portal URL is required.", file=sys.stderr)
        return 1
    if not token:
        print("Token is required.", file=sys.stderr)
        return 1

    url = url.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        print(
            "Portal URL must be https:// (http:// only for localhost) — the API "
            "token is sent with every upload.",
            file=sys.stderr,
        )
        return 1

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Create the file owner-only from the start (no window where it's
    # world-readable), and tighten an existing file's mode too.
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"portal_url": url, "token": token}, indent=2) + "\n")
    os.chmod(CONFIG_PATH, 0o600)

    print(f"\nSaved → {CONFIG_PATH}")
    print("You can now use the skill to package and upload apps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
