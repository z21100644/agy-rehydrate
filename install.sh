#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGIN_NAME="agy-rehydrate"
SRC="$SCRIPT_DIR/plugins/$PLUGIN_NAME"
PLUGINS_DST="${HOME}/.gemini/config/plugins"
DST="$PLUGINS_DST/$PLUGIN_NAME"

if [ ! -f "$SRC/plugin.json" ]; then
  printf '[ERROR] Source plugin manifest missing: %s/plugin.json\n' "$SRC" >&2
  exit 1
fi

mkdir -p "$PLUGINS_DST"
rm -rf "$DST"
cp -R "$SRC" "$DST"
find "$DST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$DST" -name '*.py[cod]' -type f -exec rm -f {} + 2>/dev/null || true
find "$DST" -name '*.py' -exec chmod +x {} + 2>/dev/null || true
printf 'Installed %s to %s\n' "$PLUGIN_NAME" "$DST"
