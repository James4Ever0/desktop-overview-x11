#!/usr/bin/env bash
# Test the 'top' mode of the focus tracker.
# Shows the most-used windows over the last 24 hours.
set -euo pipefail

PYTHON="/home/jamesbrown/miniforge3/envs/gui_agent/bin/python"
SCRIPT="/home/jamesbrown/Desktop/works/desktop_overview_with_search/window-focus-tracker.py"

# Pass through any extra args (e.g. -v, --hours 1, --limit 5).
echo "Running: top --hours 24"
exec "$PYTHON" "$SCRIPT" top --hours 24 "$@"
