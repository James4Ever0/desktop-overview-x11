#!/usr/bin/env bash
# ==============================================================================
# Clipboard event listener (bash + xclip polling)
# Polls xclip for clipboard changes and logs WRITE events.
#
# LIMITATION: X11 cannot detect READ/paste events globally (see plan §1).
# This script only sees WRITE/copy events by detecting content changes.
#
# Usage:
#   ./listen-to-clipboard-events.sh              # watch CLIPBOARD only
#   ./listen-to-clipboard-events.sh --primary    # also watch PRIMARY (noisy)
#   ./listen-to-clipboard-events.sh --interval 0.3  # custom poll interval
# ==============================================================================

INTERVAL=0.3
WATCH_PRIMARY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --primary) WATCH_PRIMARY=true; shift ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        *) echo "usage: $0 [--primary] [--interval SECONDS]" >&2; exit 1 ;;
    esac
done

# Dependencies
for cmd in xclip md5sum date timeout; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "error: $cmd not found" >&2
        exit 1
    fi
done

if [[ "$XDG_SESSION_TYPE" != "x11" ]]; then
    echo "error: X11 session required (currently: $XDG_SESSION_TYPE)" >&2
    exit 1
fi

# ---- helpers -----------------------------------------------------------------

human_bytes() {
    local n=$1
    if (( n < 1024 )); then echo "${n}B"
    elif (( n < 1048576 )); then echo "$(awk "BEGIN { printf \"%.1fKB\", $n/1024 }")"
    else echo "$(awk "BEGIN { printf \"%.1fMB\", $n/1048576 }")"
    fi
}

now_ms() {
    date '+%H:%M:%S.%3N'
}

# Fetch clipboard content (text only, via primary text target)
clip_text() {
    local sel="$1"
    timeout 1.5 xclip -selection "$sel" -o -t UTF8_STRING 2>/dev/null || true
}

# Fetch TARGETS list
clip_targets() {
    local sel="$1"
    timeout 1.5 xclip -selection "$sel" -o -t TARGETS 2>/dev/null || echo ""
}

# Classify by TARGETS
classify_targets() {
    local targets="$1"
    if echo "$targets" | grep -qi 'password\|secret'; then
        echo "PASSWORD"
    elif echo "$targets" | grep -q '^image/'; then
        echo "IMAGE"
    elif echo "$targets" | grep -q 'text/uri-list'; then
        echo "FILES"
    elif echo "$targets" | grep -q 'UTF8_STRING\|text/plain'; then
        echo "TEXT"
    else
        echo "OTHER"
    fi
}

# ---- main loop ---------------------------------------------------------------

echo "Listening for clipboard changes (PID $$, interval ${INTERVAL}s)" >&2
echo "Press Ctrl+C to stop." >&2

declare -A last_hash

while true; do
    selections=(clipboard)
    $WATCH_PRIMARY && selections+=(primary)

    for sel in "${selections[@]}"; do
        # Get TARGETS
        targets_raw="$(clip_targets "$sel")"
        [[ -z "$targets_raw" ]] && continue

        # Classify
        kind="$(classify_targets "$targets_raw")"

        # Get content hash for dedup
        content=""
        case "$kind" in
            TEXT|HTML) content="$(clip_text "$sel")" ;;
            *) content="$(clip_text "$sel")" ;;  # best-effort for others
        esac

        h="$(echo "$content" | md5sum | cut -d' ' -f1)"
        if [[ "${last_hash[$sel]}" == "$h" ]]; then
            continue  # dedup
        fi
        last_hash[$sel]="$h"

        ts="$(now_ms)"
        sel_upper="$(echo "$sel" | tr '[:lower:]' '[:upper:]')"

        case "$kind" in
            TEXT|HTML)
                n_chars="${#content}"
                n_bytes="$(echo -n "$content" | wc -c)"
                preview="${content:0:100}"
                # escape newlines/tabs for display
                preview="${preview//$'\n'/\\n}"
                preview="${preview//$'\t'/\\t}"
                more=""
                (( n_chars > 100 )) && more=" (+$((n_chars - 100)) more chars)"
                echo "$ts | WRITE | $sel_upper | TEXT chars=$n_chars bytes=$n_bytes  preview=\"$preview\"$more"
                ;;
            IMAGE)
                echo "$ts | WRITE | $sel_upper | IMAGE <detected>"
                ;;
            FILES)
                # Try to list files
                files="$(timeout 1.5 xclip -selection "$sel" -o -t text/uri-list 2>/dev/null | grep '^file://' | head -5 | sed 's/^file:\/\///' | tr '\n' '; ')"
                echo "$ts | WRITE | $sel_upper | FILES count=$(echo "$files" | grep -c .)  $files"
                ;;
            PASSWORD)
                echo "$ts | WRITE | $sel_upper | TEXT <REDACTED: password-manager hint>"
                ;;
            *)
                echo "$ts | WRITE | $sel_upper | OTHER targets=$(echo "$targets_raw" | tr '\n' ' ')"
                ;;
        esac
    done

    sleep "$INTERVAL"
done
