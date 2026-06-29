#!/usr/bin/env bash
# Test the 'timeline' mode of the focus tracker.
# Covers the last 1 day: FROM = now - 24h, TO = now (CST, 'YYYY-MM-DD HH:MM').
set -euo pipefail

PYTHON="/home/jamesbrown/miniforge3/envs/gui_agent/bin/python"
SCRIPT="/home/jamesbrown/Desktop/works/desktop_overview_with_search/window-focus-tracker.py"

# Read the current date and derive a 1-day window.
TO="$(date '+%Y-%m-%d %H:%M')"
FROM="$(date -d '1 day ago' '+%Y-%m-%d %H:%M')"

echo "Running: timeline --from '$FROM' --to '$TO'"
exec "$PYTHON" "$SCRIPT" timeline --from "$FROM" --to "$TO" "$@"
