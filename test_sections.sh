#!/usr/bin/env bash
# Test the 'sections' mode of the focus tracker.
# Covers the last 1 day: FROM = now - 24h, TO = now (CST, 'YYYY-MM-DD HH:MM'),
# bucketed into 60-minute sections.
set -euo pipefail

PYTHON="/home/jamesbrown/miniforge3/envs/gui_agent/bin/python"
SCRIPT="/home/jamesbrown/Desktop/works/desktop_overview_with_search/window-focus-tracker.py"

# Read the current date and derive a 1-day window.
TO="$(date '+%Y-%m-%d %H:%M')"
FROM="$(date -d '1 day ago' '+%Y-%m-%d %H:%M')"
INTERVAL=60   # minutes per section

echo "Running: sections --interval $INTERVAL --from '$FROM' --to '$TO'"
exec "$PYTHON" "$SCRIPT" sections --interval "$INTERVAL" --from "$FROM" --to "$TO" "$@"
