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


_SDK_SERVICE_CALL = re.compile(r"\bportal\.(pdf|email|storage)\.")


def _warn_undeclared_services(src: Path, manifest: dict) -> None:
    """Warn when the code calls an SDK service the manifest doesn't declare.

    The portal refuses (403) any service an app hasn't declared — including
    every service, for an app that declares none.
    """
    declared = set(manifest.get("services") or [])
    used: set[str] = set()
    for path in src.rglob("*"):
        if path.is_symlink() or path.suffix.lower() not in (".html", ".htm", ".js", ".mjs"):
            continue
        if _should_skip(path.relative_to(src)):
            continue
        try:
            used.update(_SDK_SERVICE_CALL.findall(path.read_text(errors="ignore")))
        except OSError:
            continue
    missing = sorted(used - declared)
    if missing:
        print(
            f"warning: the app calls portal.{{{','.join(missing)}}} but portal.json "
            f"doesn't list {missing} in \"services\" — those calls will get a 403. "
            "Add them to \"services\".",
            file=sys.stderr,
        )


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
    if not 2 <= len(slug) <= 40:
        print(f"portal.json: slug must be 2-40 characters; got {len(slug)}", file=sys.stderr)
        return 1
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

    # Every app must ship an icon so the dashboard tile is identifiable —
    # an iconless app falls back to the generic portal placeholder, which
    # makes the home screen useless once the user installs more than one.
    icon = manifest.get("icon")
    if not icon:
        print(
            "portal.json: 'icon' is required. Set it to a relative path "
            "(e.g. \"icon.png\") and place a 192x192 PNG / SVG / JPEG / WebP "
            "in the app directory before packaging.",
            file=sys.stderr,
        )
        return 1
    if not (src / icon).is_file():
        print(f"icon file '{icon}' not found in {src}", file=sys.stderr)
        return 1
    # Refuse the unmodified scaffold icon — it ships as a placeholder so
    # ``package.py`` doesn't fail on a fresh ``cp -r templates/basic``, but
    # an app uploaded with the default icon would be visually indistinguishable
    # from every other freshly-scaffolded one on the dashboard. Either replace
    # the file with one tailored to the app, or overwrite it with a generated
    # icon (e.g. a single accent-colored letter on a tinted background).
    placeholder_path = (
        Path(__file__).resolve().parent.parent
        / "templates" / "basic" / "icon.png"
    )
    try:
        if placeholder_path.is_file() and (src / icon).resolve() != placeholder_path:
            if (src / icon).read_bytes() == placeholder_path.read_bytes():
                print(
                    f"icon '{icon}' is the unmodified scaffold placeholder. "
                    "Replace it with an app-specific icon before packaging "
                    "(a 192x192 PNG with the app's initial on the portal "
                    "accent color works well).",
                    file=sys.stderr,
                )
                return 1
    except OSError:
        # Best-effort check — if we can't read either file for any reason,
        # fall through and let the upload proceed. The validation isn't
        # security-critical, just an ergonomics guard.
        pass

    if len(sys.argv) >= 3:
        output = Path(sys.argv[2]).resolve()
    else:
        output = src.parent / f"{slug}-{manifest['version']}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)

    _warn_undeclared_services(src, manifest)

    written = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(src.rglob("*")):
            if path.is_symlink():
                # Following it would package whatever it points at — possibly
                # a file outside the app (an SSH key, a .env). Skip, loudly.
                print(f"skipping symlink: {path.relative_to(src)}", file=sys.stderr)
                continue
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
