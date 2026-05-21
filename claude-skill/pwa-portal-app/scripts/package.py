#!/usr/bin/env python3
"""Package a PWA Portal app directory into a .zip suitable for upload.

Usage:
    package.py <source_dir> [output_path]

If output_path is omitted, writes <slug>-<version>.zip next to <source_dir>.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKIP_DIR_NAMES = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".idea", ".vscode"}


def _should_skip(rel: Path) -> bool:
    for part in rel.parts:
        if part in SKIP_DIR_NAMES:
            return True
        if part.startswith(".") and part not in (".",):  # any hidden file/dir
            return True
    return False


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2

    src = Path(sys.argv[1]).resolve()
    if not src.is_dir():
        print(f"Not a directory: {src}", file=sys.stderr)
        return 1

    manifest_path = src / "portal.json"
    if not manifest_path.is_file():
        print(f"Missing portal.json in {src}", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        print(f"portal.json is not valid JSON: {e}", file=sys.stderr)
        return 1

    for required in ("slug", "name", "version"):
        if not manifest.get(required):
            print(f"portal.json: missing required field '{required}'", file=sys.stderr)
            return 1

    slug = manifest["slug"]
    if not SLUG_RE.match(slug):
        print(
            f"portal.json: slug must be lowercase kebab-case (a-z, 0-9, hyphens); got {slug!r}",
            file=sys.stderr,
        )
        return 1
    if slug == "REPLACE-ME":
        print(
            "portal.json: slug is still 'REPLACE-ME' — set a real slug before packaging.",
            file=sys.stderr,
        )
        return 1

    entry = manifest.get("entry", "index.html")
    if not (src / entry).is_file():
        print(f"entry file '{entry}' not found in {src}", file=sys.stderr)
        return 1

    icon = manifest.get("icon")
    if icon and not (src / icon).is_file():
        print(f"icon file '{icon}' not found in {src}", file=sys.stderr)
        return 1

    if len(sys.argv) >= 3:
        output = Path(sys.argv[2]).resolve()
    else:
        output = src.parent / f"{slug}-{manifest['version']}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(src.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(src)
            if _should_skip(rel):
                continue
            z.write(path, rel.as_posix())
            written += 1

    size = output.stat().st_size
    print(f"packaged {written} files into {output} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
