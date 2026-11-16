#!/usr/bin/env sh
set -eu

PLUGIN_NAME="agy-rehydrate"
PLUGINS_DST="${AGY_PLUGINS_DST:-${HOME}/.gemini/config/plugins}"
DST="$PLUGINS_DST/$PLUGIN_NAME"

if [ -d "$DST" ]; then
  rm -rf "$DST"
  printf 'Removed %s\n' "$DST"
else
  printf 'Not installed: %s\n' "$DST"
fi
