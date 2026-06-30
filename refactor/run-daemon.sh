#!/usr/bin/env bash
# run-daemon.sh — start the desktop-overview daemon (event capture + API).
#
# Usage:
#   ./run-daemon.sh                 # UDS only (default, recommended)
#   ./run-daemon.sh --tcp           # also serve 127.0.0.1:8765 (debugging)
#   ./run-daemon.sh --tcp 9000      # debug TCP on a custom port
#   ./run-daemon.sh --no-keyboard   # start with keyboard capture off
#   ./run-daemon.sh --log-level debug --data-dir ~/tmp/dovw
#
# Override the interpreter with PYTHON=/path/to/python ./run-daemon.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: '$PYTHON' not found on PATH. Set PYTHON=/path/to/python." >&2
    exit 1
fi

# Logs go to BOTH stdout and a rotating file under the data dir
# ($XDG_DATA_HOME/desktop-overview/logs/daemon.log, default ~/.local/share/...).
exec "$PYTHON" -u -m daemon --log-level debug "$@"
