#!/usr/bin/env sh
set -eu

PLUGIN_NAME="agy-rehydrate"
DST="${HOME}/.gemini/config/plugins/$PLUGIN_NAME"

if [ -d "$DST" ]; then
  rm -rf "$DST"
  printf 'Removed %s\n' "$DST"
else
  printf 'Not installed: %s\n' "$DST"
fi
