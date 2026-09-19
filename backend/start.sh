#!/usr/bin/env bash
# Start the cross-search backend.
# The server binds to 0.0.0.0:5005 by default (configurable via PORT env).
#
# Usage:
#   ./start.sh                # foreground (for development)
#   PORT=8080 ./start.sh      # different port
#
# The backend serves:
#   GET  /                → index.html (the dashboard)
#   POST /api/search      → JSON product lookup + cross-platform search

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="/Users/rajansharma/Downloads/files/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "ERROR: venv python not found at $VENV_PYTHON" >&2
  exit 1
fi

echo "===" >&2
echo "Starting cross-search backend on http://0.0.0.0:5005" >&2
echo "===" >&2

exec "$VENV_PYTHON" "$HERE/app.py"