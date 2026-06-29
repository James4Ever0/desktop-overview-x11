#!/usr/bin/env python3
"""
PRIMARY selection event listener v1 — detect text selection (highlight) before copy.

Strategy C (content-stability lockout, §8c of the plan):
  - On each XFIXES SetSelectionOwnerNotify for PRIMARY, read the selection text.
  - Hash content; skip if same hash (owner re-assertion).
  - If content changed AND gap since last enqueue < THRESHOLD (250ms):
      Only update "latest_candidate". Do NOT enqueue.
  - If gap >= THRESHOLD:
      Enqueue latest_candidate into ring buffer, print it, reset timestamps.
  - First event in a burst: record timestamp, do NOT enqueue (selection may be partial).

Buffer: ring buffer of 100 entries.
On enqueue: print latest candidate preview + total accumulated count.

Dependencies:
  sudo apt install python3-xlib xclip
  (or xsel for the fallback path)

Target: Kubuntu 22.04 / KDE Plasma (KWin) on X11.
"""

import os
import sys
import time
import hashlib
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from collections import OrderedDict

from Xlib import display, X
from Xlib.ext import xfixes

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ENQUEUE_THRESHOLD_S = 0.25        # 250 ms — gap must exceed this to enqueue
RING_BUFFER_MAX = 100             # max entries in the ring buffer
READ_RETRY_MAX = 3                # retries when reading PRIMARY races a release
READ_RETRY_DELAY_S = 0.02         # 20 ms between retries
CST_TIMEDELTA = timedelta(hours=8)  # China Standard Time (UTC+8)

# ---------------------------------------------------------------------------
# Logging — verbose, stdout, CST timestamps
# ---------------------------------------------------------------------------
class CstFormatter(logging.Formatter):
    """Custom formatter that uses CST (UTC+8) for timestamps."""
    def formatTime(self, record, datefmt=None):
        cst = datetime.fromtimestamp(record.created, tz=timezone(CST_TIMEDELTA))
        if datefmt:
            return cst.strftime(datefmt)
        return cst.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
)
_log = logging.getLogger("selection")
_log.handlers.clear()

_handler = logging.StreamHandler(sys.stdout)
_handler.setLevel(logging.DEBUG)
_fmt = CstFormatter(
    "[%(asctime)s.%(msecs)03d CST] %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
_handler.setFormatter(_fmt)
_log.addHandler(_handler)
_log.propagate = False

log = _log


def _fmt_cst(ts):
    """Format a Unix timestamp as a CST (UTC+8) HH:MM:SS.mmm string."""
    return datetime.fromtimestamp(ts, tz=timezone(CST_TIMEDELTA)).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# State — Strategy C
# ---------------------------------------------------------------------------
_ring_buffer = []                  # ring buffer: list of {ts, text, chars, hash}
_total_enqueued = 0                # monotonic counter across all time
_candidate = None                  # dict: {text, hash, ts} for in-progress candidate
_last_enqueue_ts = None            # timestamp of the last enqueue (for gap calc)
_last_seen_hash = None             # hash of the last READ content (skip dedup)
_prev_text_for_overlap = None      # for prefix/suffix overlap detection (bonus)

# ---------------------------------------------------------------------------
# PRIMARY text reader (via xclip)
# ---------------------------------------------------------------------------
def _read_primary_text():
    """Read the current PRIMARY selection as UTF-8 text.

    Returns (text: str | None, bytes_len: int).
    text is None if reading failed (no selection, owner gone, etc.).
    """
    for attempt in range(READ_RETRY_MAX):
        try:
            proc = subprocess.run(
                ["xclip", "-selection", "primary", "-o", "-t", "UTF8_STRING"],
                capture_output=True,
                timeout=2.0,
            )
        except FileNotFoundError:
            log.error("xclip not found. Install: sudo apt install xclip")
            return None, 0
        except subprocess.TimeoutExpired:
            log.warning("xclip timed out (attempt %d/%d)", attempt + 1, READ_RETRY_MAX)
            continue

        if proc.returncode == 0:
            # success
            raw = proc.stdout
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("latin-1")
            return text, len(raw)

        # xclip may fail transiently (owner released before we read)
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if attempt < READ_RETRY_MAX - 1:
            log.debug(
                "xclip attempt %d/%d failed (exit=%d, stderr=%r); retrying in %d ms ...",
                attempt + 1, READ_RETRY_MAX, proc.returncode, stderr or "",
                READ_RETRY_DELAY_S * 1000,
            )
            time.sleep(READ_RETRY_DELAY_S)
        else:
            log.debug(
                "xclip final attempt failed (exit=%d, stderr=%r)",
                proc.returncode, stderr or "",
            )
    return None, 0


# ---------------------------------------------------------------------------
# Content classification (same rules as clipboard plan)
# ---------------------------------------------------------------------------
def _escape_control(text):
    """Replace control characters (except \n, \t) with visible escapes."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 10 or cp == 13:       # \n \r -> keep
            out.append(ch)
        elif cp == 9:                   # \t -> keep
            out.append(ch)
        elif cp < 32:                   # other controls -> ^@, ^A, ...
            out.append(f"^{chr(64 + cp)}")
        elif cp == 127:                 # DEL
            out.append("^?")
        else:
            out.append(ch)
    return "".join(out)


def _preview_text(text, max_chars=200):
    """Build a compact preview string: first N chars, escaped."""
    safe = _escape_control(text)
    if len(safe) <= max_chars:
        return safe
    return safe[:max_chars] + f"[+{len(text) - max_chars} more chars]"


def _text_summary(text):
    """Return (chars, bytes, preview) for a text selection."""
    chars = len(text)
    bytes_len = len(text.encode("utf-8"))
    return chars, bytes_len


# ---------------------------------------------------------------------------
# Overlap detection (bonus heuristic for continuous drag)
# ---------------------------------------------------------------------------
def _longest_common_prefix(a, b, min_chars=5):
    """Return length of common prefix if >= min_chars, else 0."""
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return i if i >= min_chars else 0


def _longest_common_suffix(a, b, min_chars=5):
    """Return length of common suffix if >= min_chars, else 0."""
    i = 0
    while i < len(a) and i < len(b) and a[-(i + 1)] == b[-(i + 1)]:
        i += 1
    return i if i >= min_chars else 0


# ---------------------------------------------------------------------------
# Enqueue & print
# ---------------------------------------------------------------------------
def _enqueue(text, chars, bytes_len, start_ts):
    """Add an entry to the ring buffer and print it.

    start_ts is when this candidate first began aggregating (the selection /
    drag start), NOT the enqueue moment. We store and display start_ts as the
    entry timestamp, but keep the gap clock (_last_enqueue_ts) on the real
    enqueue instant so the 250 ms lockout window is unaffected.
    """
    global _total_enqueued, _last_enqueue_ts, _candidate, _last_seen_hash, _prev_text_for_overlap

    enqueue_ts = time.time()          # actual flush moment (drives the gap clock)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    entry = {
        "ts": start_ts,               # selection-start time (aggregation start)
        "enqueue_ts": enqueue_ts,     # when it was actually flushed
        "text": text,
        "chars": chars,
        "bytes": bytes_len,
        "hash": h,
    }

    # Ring buffer
    _ring_buffer.append(entry)
    if len(_ring_buffer) > RING_BUFFER_MAX:
        _ring_buffer.pop(0)

    _total_enqueued += 1
    _last_enqueue_ts = enqueue_ts

    # Preview
    preview = _preview_text(text)

    # CST timestamps: when the selection started vs when it was flushed.
    start_str = _fmt_cst(start_ts)
    enqueue_str = _fmt_cst(enqueue_ts)

    print()
    print("─" * 72)
    print(f"  SELECTION #{_total_enqueued}")
    print(f"  Started:  {start_str} CST   (selection began aggregating)")
    print(f"  Enqueued: {enqueue_str} CST   (flushed to buffer)")
    print(f"  Size: {chars} chars, {bytes_len} bytes")
    print(f"  Hash: {h}")
    print(f"  Buffer: {len(_ring_buffer)}/{RING_BUFFER_MAX} entries")
    print(f"  Preview: {preview}")
    print("─" * 72)
    print()

    # Reset candidate state — wait for a fresh selection start
    _candidate = None
    _last_seen_hash = None
    _prev_text_for_overlap = None

    log.info(
        "ENQUEUED #%d | start=%s CST | flushed=%s CST | %d chars, %d bytes | buffer=%d/%d | hash=%s",
        _total_enqueued, start_str, enqueue_str, chars, bytes_len,
        len(_ring_buffer), RING_BUFFER_MAX, h,
    )


# ---------------------------------------------------------------------------
# Core handler — Strategy C (content-stability lockout)
# ---------------------------------------------------------------------------
def handle_selection_event(ev):
    """Called on each XFIXES SetSelectionOwnerNotify for PRIMARY."""
    global _candidate, _last_enqueue_ts, _last_seen_hash, _prev_text_for_overlap

    now = time.time()

    # 1. Read the PRIMARY content
    text, raw_bytes = _read_primary_text()
    if text is None:
        log.debug("Selection event fired but PRIMARY read returned None (owner gone?).")
        return
    if not text.strip():
        log.debug("Selection event fired but PRIMARY is empty/whitespace.")
        return

    # 2. Hash dedup: skip if content hasn't changed since last read
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if _last_seen_hash == h:
        log.debug(
            "DEDUP (hash match) | hash=%s | preview=%s",
            h, _preview_text(text, 80),
        )
        return
    _last_seen_hash = h

    chars, bytes_len = _text_summary(text)
    log.debug(
        "CONTENT CHANGED | %d chars, %d bytes | hash=%s | preview=%s",
        chars, bytes_len, h, _preview_text(text, 80),
    )

    # 3. Overlap detection — bonus: if text shares prefix/suffix with previous,
    #    treat as same continuous drag. Reset the gap clock.
    if _prev_text_for_overlap is not None:
        pref = _longest_common_prefix(_prev_text_for_overlap, text)
        suff = _longest_common_suffix(_prev_text_for_overlap, text)
        if pref > 0 or suff > 0:
            log.debug(
                "OVERLAP detected (prefix=%d, suffix=%d) — same drag, resetting gap clock.",
                pref, suff,
            )
            # Push the candidate forward so the gap clock restarts. This is the
            # SAME drag continuing, so preserve the candidate's original
            # start_ts (only text/hash/last-activity ts move forward).
            if _candidate is not None:
                _candidate["text"] = text
                _candidate["hash"] = h
                _candidate["ts"] = now
            else:
                _candidate = {"text": text, "hash": h, "ts": now, "start_ts": now}
                log.debug("OVERLAP with no candidate — drag start at %s CST.", _fmt_cst(now))
            _prev_text_for_overlap = text
            return
    _prev_text_for_overlap = text

    # 4. Gap calculation
    if _last_enqueue_ts is None:
        # First selection ever — just record it, do NOT enqueue.
        log.info(
            "FIRST SELECTION (drag start) | start=%s CST | recording candidate (no enqueue) | preview=%s",
            _fmt_cst(now), _preview_text(text, 80),
        )
        _last_enqueue_ts = now
        _candidate = {"text": text, "hash": h, "ts": now, "start_ts": now}
        return

    if _candidate is None:
        # We're in a fresh "waiting for selection start" state.
        # This event is the start — record, do not enqueue.
        log.info(
            "SELECTION START (drag start) | start=%s CST | recording candidate (no enqueue) | preview=%s",
            _fmt_cst(now), _preview_text(text, 80),
        )
        _candidate = {"text": text, "hash": h, "ts": now, "start_ts": now}
        return

    gap = now - _last_enqueue_ts
    log.debug("GAP = %.1f ms (threshold = %d ms)", gap * 1000, ENQUEUE_THRESHOLD_S * 1000)

    if gap >= ENQUEUE_THRESHOLD_S:
        # Gap exceeds threshold → enqueue the candidate (the stable selection)
        candidate_text = _candidate["text"]
        candidate_start = _candidate["start_ts"]
        c_chars, c_bytes = _text_summary(candidate_text)
        log.info(
            "GAP >= THRESHOLD (%.0f ms >= %d ms) → enqueuing candidate (start=%s CST).",
            gap * 1000, ENQUEUE_THRESHOLD_S * 1000, _fmt_cst(candidate_start),
        )
        _enqueue(candidate_text, c_chars, c_bytes, candidate_start)
        # Note: _enqueue resets candidate state for us.
        # But this new event is a fresh selection — start a new candidate (drag start).
        _candidate = {"text": text, "hash": h, "ts": now, "start_ts": now}
        _prev_text_for_overlap = text
        log.debug("New candidate (drag start) at %s CST from post-enqueue event.", _fmt_cst(now))
    else:
        # Within the gap window — update candidate with latest content
        log.debug(
            "UPDATING CANDIDATE (gap=%.0f ms < %d ms) | preview=%s",
            gap * 1000, ENQUEUE_THRESHOLD_S * 1000,
            _preview_text(text, 80),
        )
        _candidate["text"] = text
        _candidate["hash"] = h
        _candidate["ts"] = now


# ---------------------------------------------------------------------------
# Periodic stats printer
# ---------------------------------------------------------------------------
_last_stats_ts = [0.0]  # mutable for closure
_STATS_INTERVAL_S = 30

def _maybe_print_stats():
    """Print periodic buffer stats every _STATS_INTERVAL_S seconds."""
    now = time.time()
    if now - _last_stats_ts[0] < _STATS_INTERVAL_S:
        return
    _last_stats_ts[0] = now

    cst_ts = datetime.fromtimestamp(now, tz=timezone(CST_TIMEDELTA))
    time_str = cst_ts.strftime("%H:%M:%S.%f")[:-3]
    last_text = ""
    if _ring_buffer:
        last_text = _preview_text(_ring_buffer[-1]["text"], 100)

    log.info(
        "[BUFFER STATS] %s CST | total=%d | buffered=%d/%d | last=\"%s\"",
        time_str, _total_enqueued, len(_ring_buffer), RING_BUFFER_MAX, last_text,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("PRIMARY Selection Event Listener v1 (Strategy C)")
    log.info("Target: X11 (XFIXES) on Kubuntu 22.04 / KDE Plasma")
    log.info("Config:")
    log.info("  Enqueue threshold: %d ms", ENQUEUE_THRESHOLD_S * 1000)
    log.info("  Ring buffer max:   %d", RING_BUFFER_MAX)
    log.info("  Read retries:      %d (delay %d ms)", READ_RETRY_MAX, READ_RETRY_DELAY_S * 1000)
    log.info("=" * 60)
    log.info("Waiting for PRIMARY selection events ...")
    log.info("(Highlight text anywhere to trigger; enqueue fires after selection stabilizes)")
    log.info("")

    # Open display
    dpy = display.Display()
    screen = dpy.screen()
    root = screen.root

    # Check XFIXES
    if not dpy.has_extension("XFIXES"):
        log.error("XFIXES extension not available. This only works on X11.")
        sys.exit(1)

    # Query XFIXES version
    dpy.xfixes_query_version()

    # Intern the PRIMARY atom
    primary_atom = dpy.intern_atom("PRIMARY")
    log.info("PRIMARY atom = %d", primary_atom)

    # Select XFIXES selection input on PRIMARY
    mask = (
        xfixes.XFixesSetSelectionOwnerNotifyMask
        | xfixes.XFixesSelectionWindowDestroyNotifyMask
        | xfixes.XFixesSelectionClientCloseNotifyMask
    )
    dpy.xfixes_select_selection_input(root, primary_atom, mask)
    log.info("XFIXES selection input registered for PRIMARY.")

    # Flush and sync
    dpy.sync()

    # The XFIXES selection-notify is registered as a *subevent*, so the code is
    # a (type, sub_code) tuple — e.g. (87, 0): X event type 87, sub_code 0 for
    # SetSelectionOwnerNotify. ev.type alone (87) is shared by every
    # XFixesSelectionNotify subtype, so we must match BOTH halves. Comparing
    # ev.type (an int) to the whole tuple is what was silently dropping events.
    sel_code = dpy.extension_event.SetSelectionOwnerNotify
    sel_type, sel_sub = sel_code if isinstance(sel_code, tuple) else (sel_code, None)
    log.info("Event code for SetSelectionOwnerNotify = type %d, sub_code %s", sel_type, sel_sub)

    # Event loop
    try:
        while True:
            ev = dpy.next_event()

            # Log every raw event so we can see exactly what arrives. Note: the
            # event itself never carries the selected TEXT — X11 selections are
            # pull-based, so content is always read from PRIMARY in the handler.
            ev_sub = getattr(ev, "sub_code", None)
            ev_sel = getattr(ev, "selection", None)
            ev_time = getattr(ev, "timestamp", None)   # X server event time (ms)
            log.debug("RAW EVENT | type=%s sub_code=%s selection=%s server_time=%s",
                      ev.type, ev_sub, ev_sel, ev_time)

            is_set_owner = (ev.type == sel_type and (sel_sub is None or ev_sub == sel_sub))

            if is_set_owner and ev_sel == primary_atom:
                log.debug("SetSelectionOwnerNotify for PRIMARY -> reading content.")
                handle_selection_event(ev)
                _maybe_print_stats()
            elif is_set_owner:
                log.debug("SetSelectionOwnerNotify for non-PRIMARY atom %s -> ignoring.", ev_sel)
            else:
                log.debug("Ignoring event type=%s sub_code=%s (not SetSelectionOwnerNotify).",
                          ev.type, ev_sub)


    except KeyboardInterrupt:
        log.info("")
        log.info("Received SIGINT. Shutting down.")
        log.info("Final stats: total enqueued=%d, buffer=%d/%d",
                 _total_enqueued, len(_ring_buffer), RING_BUFFER_MAX)
    finally:
        dpy.close()
        log.info("Display closed. Goodbye.")


if __name__ == "__main__":
    main()
