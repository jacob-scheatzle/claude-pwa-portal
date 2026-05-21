#!/usr/bin/env bash
# Symlink the pwa-portal-app skill into ~/.claude/skills/ so Claude Code can use it.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="$HOME/.claude/skills"
DEST="$DEST_DIR/pwa-portal-app"

mkdir -p "$DEST_DIR"

if [[ -L "$DEST" ]]; then
	rm "$DEST"
elif [[ -e "$DEST" ]]; then
	echo "Path exists and is not a symlink: $DEST"
	echo "Move or remove it first, then re-run this script."
	exit 1
fi

ln -s "$HERE" "$DEST"
chmod +x "$HERE/scripts/"*.py

echo "Linked $DEST -> $HERE"
echo
echo "Next: save your portal URL + API token by running"
echo "  python3 $HERE/scripts/configure.py"
