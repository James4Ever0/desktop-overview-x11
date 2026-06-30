#!/usr/bin/env bash
# run.sh — start the daemon in the background, then the UI in the foreground.
#
# Convenience launcher for a single desktop session: it boots the daemon, waits
# for its UNIX socket to appear, then opens the search UI. When you close the UI
# (or hit Ctrl-C), the daemon it started is shut down too.
#
# If a daemon is already running (its socket exists), this reuses it and only
# launches the UI.
#
# Usage:
#   ./run.sh                 # daemon (UDS) + UI
#   ./run.sh --no-keyboard   # extra args are passed to the daemon
#
# Override the interpreter with PYTHON=/path/to/python ./run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "error: '$PYTHON' not found." >&2; exit 1; }

# Resolve the daemon's socket path the same way the code does (XDG runtime dir,
# else data dir). We only need it to decide whether a daemon is already up.
SOCK="$("$PYTHON" -c 'from daemon.config import Settings; print(Settings().uds_path)')"

DAEMON_PID=""
cleanup() {
    if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[run.sh] stopping daemon (pid $DAEMON_PID)…"
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if [[ -S "$SOCK" ]]; then
    echo "[run.sh] reusing daemon already listening on $SOCK"
else
    echo "[run.sh] starting daemon…"
    "$PYTHON" -u -m daemon --log-level debug "$@" &
    DAEMON_PID=$!
    # wait up to ~10s for the socket to appear
    for _ in $(seq 1 50); do
        [[ -S "$SOCK" ]] && break
        kill -0 "$DAEMON_PID" 2>/dev/null || { echo "[run.sh] daemon exited early." >&2; exit 1; }
        sleep 0.2
    done
    [[ -S "$SOCK" ]] || echo "[run.sh] warning: socket not seen yet; UI will retry."
fi

echo "[run.sh] launching UI…"
"$PYTHON" -u -m frontend --log-level debug
