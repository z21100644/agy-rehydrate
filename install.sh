#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGIN_NAME="agy-rehydrate"
SRC="$SCRIPT_DIR/plugins/$PLUGIN_NAME"
PLUGINS_DST="${AGY_PLUGINS_DST:-${HOME}/.gemini/config/plugins}"
DST="$PLUGINS_DST/$PLUGIN_NAME"

if [ ! -f "$SRC/plugin.json" ]; then
  printf '[ERROR] Source plugin manifest missing: %s/plugin.json\n' "$SRC" >&2
  exit 1
fi

PYTHON_BIN=""
for CAND in python3 python; do
  if command -v "$CAND" >/dev/null 2>&1; then
    RESOLVED="$("$CAND" -B -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>/dev/null || true)"
    if [ -n "$RESOLVED" ]; then
      PYTHON_BIN="$RESOLVED"
      break
    fi
  fi
done

mkdir -p "$PLUGINS_DST"
rm -rf "$DST"
cp -R "$SRC" "$DST"
find "$DST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$DST" -name '*.py[cod]' -type f -exec rm -f {} + 2>/dev/null || true
find "$DST" -name '*.py' -exec chmod +x {} + 2>/dev/null || true
if [ -n "$PYTHON_BIN" ] && [ -f "$DST/hooks.json" ]; then
  "$PYTHON_BIN" -B -c "import json, shlex, sys; from pathlib import Path; p = Path(sys.argv[1]); exe = sys.argv[2]; inv = shlex.quote(exe); data = json.loads(p.read_text(encoding='utf-8')); [(h.__setitem__('command', inv + ' -B ' + h['command'].split('||', 1)[0].split(' -B ', 1)[1].strip()) if isinstance(h, dict) and h.get('type') == 'command' and isinstance(h.get('command'), str) and ' -B ' in h['command'].split('||', 1)[0] else None) for v in data.values() if isinstance(v, dict) for k, arr in v.items() if isinstance(arr, list) for entry in arr if isinstance(entry, dict) for h in (entry.get('hooks') if isinstance(entry.get('hooks'), list) else [entry])]; p.write_bytes((json.dumps(data, indent=2) + '\n').encode('utf-8'))" "$DST/hooks.json" "$PYTHON_BIN" 2>/dev/null || true
fi
printf 'Installed %s to %s\n' "$PLUGIN_NAME" "$DST"
