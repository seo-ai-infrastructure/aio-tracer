#!/bin/sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  exec python3 graph_studio.py --demo
fi
printf '%s\n' 'Install Python 3.11 or newer, then run python3 graph_studio.py --demo.'
