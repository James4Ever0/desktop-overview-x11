#!/usr/bin/env bash
# run-frontend.sh — start the desktop-overview search UI (thin Tk client).
#
# The daemon must already be running (see ./run-daemon.sh). The UI auto-discovers
# the daemon's UNIX socket from XDG; if it can't reach it you'll see a
# "daemon not running" banner instead of a crash.
#
# Usage:
#   ./run-frontend.sh                       # connect over the UNIX socket (default)
#   ./run-frontend.sh --tcp                 # connect to 127.0.0.1:8765 instead
#   ./run-frontend.sh --tcp 10.0.0.5:9000   # custom TCP endpoint
#   ./run-frontend.sh --socket /path/daemon.sock
#   ./run-frontend.sh --columns 5 --log-level debug
#
# Override the interpreter with PYTHON=/path/to/python ./run-frontend.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: '$PYTHON' not found on PATH. Set PYTHON=/path/to/python." >&2
    exit 1
fi

# Logs go to BOTH stdout and a rotating file
# ($XDG_DATA_HOME/desktop-overview/logs/frontend.log, default ~/.local/share/...).
exec "$PYTHON" -u -m frontend "$@"
