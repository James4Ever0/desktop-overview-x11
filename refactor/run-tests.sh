#!/usr/bin/env bash
# run-tests.sh — run all headless test modules with a per-module timeout.
#
# Uses GNU coreutils `timeout` so a hung test cannot block the whole suite.
# Override the per-test deadline:  TEST_TIMEOUT=60 ./run-tests.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python}"
TIMEOUT="${TEST_TIMEOUT:-120}"

if ! command -v timeout >/dev/null 2>&1; then
    echo "error: GNU coreutils 'timeout' is required." >&2
    exit 1
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: '$PYTHON' not found." >&2
    exit 1
fi

TESTS=(
    tests.test_db
    tests.test_identity_windows
    tests.test_runtime
    tests.test_collectors
    tests.test_window_captures
    tests.test_keyboard_aggregator
    tests.test_api_search
    tests.test_heartbeat
    tests.test_frontend
)

FAILED=0
echo "Running ${#TESTS[@]} test module(s) with timeout=${TIMEOUT}s"
for t in "${TESTS[@]}"; do
    printf "\n===== %s =====\n" "$t"
    if timeout --signal=TERM --kill-after=5 "$TIMEOUT" "$PYTHON" -u -m "$t"; then
        echo "  -> PASS"
    else
        echo "  -> FAIL (exit $?; may have timed out after ${TIMEOUT}s)"
        FAILED=$((FAILED + 1))
    fi
done

printf "\n===== SUMMARY =====\n"
if [ "$FAILED" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "$FAILED module(s) failed"
    exit 1
fi
