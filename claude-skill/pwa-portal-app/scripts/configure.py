#!/usr/bin/env python3
"""Save PWA Portal URL + API token to ~/.config/pwa-portal/config.json.

Interactive; run once per workstation. Token is shown once when you create it at
<portal_url>/admin/tokens — paste it here.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

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
    token = input(token_prompt).strip()
    if not token and token_keep:
        token = current["token"]

    if not url:
        print("Portal URL is required.", file=sys.stderr)
        return 1
    if not token:
        print("Token is required.", file=sys.stderr)
        return 1

    url = url.rstrip("/")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"portal_url": url, "token": token}, indent=2) + "\n")
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        pass

    print(f"\nSaved → {CONFIG_PATH}")
    print("You can now use the skill to package and upload apps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
