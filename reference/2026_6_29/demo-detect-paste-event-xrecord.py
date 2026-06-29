#!/usr/bin/env python3
"""
Paste-event detector (XRecord backend) — demo.

Detects PASTE *candidates* via input gestures (X11 has no broadcast "paste"
event; see plan_detect_paste_event.txt §0/§4). Two gestures are watched:

  - Ctrl+V / Ctrl+Shift+V   -> CLIPBOARD paste   (confidence: strong)
  - Middle mouse button     -> PRIMARY paste     (confidence: weak/overloaded)

On each gesture we read the matching selection via xclip to capture the likely
pasted content. A gesture proves the KEYS/BUTTON fired, NOT that the focused app
actually pasted — hence "candidate".

Backend: Xlib RECORD extension (low-level). Passive global monitor that SEES
events without consuming them. Unlike pynput, the cooked modifier `state` field
is delivered on each event (no manual Ctrl/Shift bookkeeping), and we also get
the X server event timestamp and the event window.

Best practice: two display connections — one to drive the record context, one
("local") for keycode->keysym lookups.

Dependencies:
  sudo apt install python3-xlib xclip   (or: pip install python-xlib)

Target: Kubuntu 22.04 / KDE Plasma (KWin) on X11.
"""

import sys
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta

from Xlib import X, XK, display
from Xlib.ext import record
from Xlib.protocol import rq

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
READ_RETRY_MAX = 3
READ_RETRY_DELAY_S = 0.02
CST_TIMEDELTA = timedelta(hours=8)

# ---------------------------------------------------------------------------
# Logging — verbose, stdout, CST timestamps
# ---------------------------------------------------------------------------
class CstFormatter(logging.Formatter):
    def formatTime(self, record_, datefmt=None):
        cst = datetime.fromtimestamp(record_.created, tz=timezone(CST_TIMEDELTA))
        if datefmt:
            return cst.strftime(datefmt)
        return cst.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

_log = logging.getLogger("paste.xrecord")
_log.handlers.clear()
_handler = logging.StreamHandler(sys.stdout)
_handler.setLevel(logging.DEBUG)
_handler.setFormatter(CstFormatter(
    "[%(asctime)s.%(msecs)03d CST] %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
))
_log.addHandler(_handler)
_log.setLevel(logging.DEBUG)
_log.propagate = False
log = _log


def _fmt_cst(ts):
    return datetime.fromtimestamp(ts, tz=timezone(CST_TIMEDELTA)).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Selection reader (via xclip)
# ---------------------------------------------------------------------------
def _read_selection_text(selection):
    """Read PRIMARY/CLIPBOARD as UTF-8. Returns (text|None, bytes_len)."""
    for attempt in range(READ_RETRY_MAX):
        try:
            proc = subprocess.run(
                ["xclip", "-selection", selection, "-o", "-t", "UTF8_STRING"],
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
            raw = proc.stdout
            return raw.decode("utf-8", errors="replace"), len(raw)

        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if attempt < READ_RETRY_MAX - 1:
            time.sleep(READ_RETRY_DELAY_S)
        else:
            log.debug("xclip(%s) failed (exit=%d, stderr=%r)", selection, proc.returncode, stderr)
    return None, 0


def _escape_control(text):
    out = []
    for ch in text:
        cp = ord(ch)
        if cp in (9, 10, 13):
            out.append(ch)
        elif cp < 32:
            out.append(f"^{chr(64 + cp)}")
        elif cp == 127:
            out.append("^?")
        else:
            out.append(ch)
    return "".join(out)


def _preview_text(text, max_chars=200):
    safe = _escape_control(text)
    if len(safe) <= max_chars:
        return safe
    return safe[:max_chars] + f"[+{len(text) - max_chars} more chars]"


# ---------------------------------------------------------------------------
# Paste-candidate reporting
# ---------------------------------------------------------------------------
_total_pastes = 0

def report_paste(gesture, selection, confidence, server_time=None, extra=""):
    global _total_pastes
    now = time.time()
    text, raw_bytes = _read_selection_text(selection)

    _total_pastes += 1
    when = _fmt_cst(now)

    if text is None:
        chars = bytes_len = 0
        preview = "<selection read failed / empty>"
    else:
        chars = len(text)
        bytes_len = raw_bytes
        preview = _preview_text(text)

    print()
    print("─" * 72)
    print(f"  PASTE CANDIDATE #{_total_pastes}   [{confidence}]")
    print(f"  When:        {when} CST  (handler wall-clock)")
    if server_time is not None:
        print(f"  Server time: {server_time} ms  (X event timestamp)")
    print(f"  Gesture:     {gesture}{('  ' + extra) if extra else ''}")
    print(f"  Source:      {selection.upper()} selection")
    print(f"  Size:        {chars} chars, {bytes_len} bytes")
    print(f"  Preview:     {preview}")
    print("─" * 72)
    print()

    log.info(
        "PASTE CANDIDATE #%d | %s | gesture=%s | src=%s | %d chars | %s | srv=%s",
        _total_pastes, confidence, gesture, selection, chars, when, server_time,
    )


# ---------------------------------------------------------------------------
# XRecord setup + event decoding
# ---------------------------------------------------------------------------
local_dpy = display.Display()        # for keycode->keysym lookups
record_dpy = display.Display()       # drives the record context

_v_held = False                      # autorepeat debounce for 'v'


def _handle_event(event):
    """Decode one captured X event and fire paste candidates."""
    global _v_held

    if event.type == X.KeyPress:
        keysym = local_dpy.keycode_to_keysym(event.detail, 0)
        ctrl = bool(event.state & X.ControlMask)
        shift = bool(event.state & X.ShiftMask)

        if keysym == XK.XK_v:
            if _v_held:
                return                # ignore autorepeat
            _v_held = True
            if ctrl:
                gesture = "Ctrl+Shift+V" if shift else "Ctrl+V"
                log.debug("Key gesture detected: %s (keycode=%d, state=0x%x)",
                          gesture, event.detail, event.state)
                report_paste(gesture, "clipboard", "strong candidate",
                             server_time=getattr(event, "time", None))

    elif event.type == X.KeyRelease:
        keysym = local_dpy.keycode_to_keysym(event.detail, 0)
        if keysym == XK.XK_v:
            _v_held = False

    elif event.type == X.ButtonPress:
        if event.detail == 2:         # button 2 = middle
            log.debug("Middle-click (root_x=%s, root_y=%s)",
                      getattr(event, "root_x", "?"), getattr(event, "root_y", "?"))
            report_paste("middle-click", "primary", "weak candidate",
                         server_time=getattr(event, "time", None),
                         extra=f"@({getattr(event, 'root_x', '?')},"
                               f"{getattr(event, 'root_y', '?')})")


def record_callback(reply):
    """Called by record_enable_context for each batch of captured events."""
    if reply.category != record.FromServer:
        return
    if reply.client_swapped:
        return
    if not len(reply.data) or reply.data[0] < 2:
        return                        # not a device event we care about

    data = reply.data
    while len(data):
        event, data = rq.EventField(None).parse_binary_value(
            data, record_dpy.display, None, None
        )
        _handle_event(event)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("Paste-event detector (XRecord backend)")
    log.info("Watching: Ctrl+V / Ctrl+Shift+V (CLIPBOARD), middle-click (PRIMARY)")
    log.info("Note: gestures are CANDIDATES, not confirmed pastes.")
    log.info("=" * 60)

    if not record_dpy.has_extension("RECORD"):
        log.error("RECORD extension not available on this X server.")
        sys.exit(1)

    # Capture KeyPress(2)..ButtonPress(4) — covers KeyPress, KeyRelease, ButtonPress.
    ctx = record_dpy.record_create_context(
        0,
        [record.AllClients],
        [{
            "core_requests": (0, 0),
            "core_replies": (0, 0),
            "ext_requests": (0, 0, 0, 0),
            "ext_replies": (0, 0, 0, 0),
            "delivered_events": (0, 0),
            "device_events": (X.KeyPress, X.ButtonPress),
            "errors": (0, 0),
            "client_started": False,
            "client_died": False,
        }],
    )
    log.info("RECORD context created (id=%s). Listening ... (Ctrl+C to quit)", ctx)

    try:
        # Blocks, delivering events to record_callback until the context is freed.
        record_dpy.record_enable_context(ctx, record_callback)
    except KeyboardInterrupt:
        log.info("")
        log.info("Received SIGINT. Shutting down.")
    finally:
        # Disable from the OTHER connection, then free the context.
        local_dpy.record_disable_context(ctx)
        local_dpy.flush()
        record_dpy.record_free_context(ctx)
        log.info("Final stats: total paste candidates = %d", _total_pastes)
        record_dpy.close()
        local_dpy.close()
        log.info("RECORD context freed. Goodbye.")


if __name__ == "__main__":
    main()
