#!/usr/bin/env bash
# Test the 'poll' mode of the focus tracker.
# Starts polling and storing samples until you press Ctrl+C.
set -euo pipefail

PYTHON="/home/jamesbrown/miniforge3/envs/gui_agent/bin/python"
SCRIPT="/home/jamesbrown/Desktop/works/desktop_overview_with_search/window-focus-tracker.py"

# Pass through any extra args (e.g. -v, --interval 5).
echo "Running: poll"
exec "$PYTHON" "$SCRIPT" poll "$@"
