#!/usr/bin/env python3
"""
Paste-event detector (pynput backend) — demo.

Detects PASTE *candidates* via input gestures (X11 has no broadcast "paste"
event; see plan_detect_paste_event.txt §0/§4). Two gestures are watched:

  - Ctrl+V / Ctrl+Shift+V   -> CLIPBOARD paste   (confidence: strong)
  - Middle mouse button     -> PRIMARY paste     (confidence: weak/overloaded)

On each gesture we read the matching selection via xclip to capture the likely
pasted content. A gesture proves the KEYS/BUTTON fired, NOT that the focused app
actually pasted — hence "candidate".

Backend: pynput (high-level). On Linux it rides the Xlib RECORD extension under
the hood, so it is passive and does NOT consume the event. pynput delivers one
event per physical key with NO cooked modifier state, so Ctrl/Shift are tracked
manually here.

Dependencies:
  pip install pynput          (X11 only — no Wayland)
  sudo apt install xclip

Target: Kubuntu 22.04 / KDE Plasma (KWin) on X11.
"""

import sys
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta

from pynput import keyboard, mouse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
READ_RETRY_MAX = 3                  # retries when reading a selection races
READ_RETRY_DELAY_S = 0.02           # 20 ms between retries
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

_log = logging.getLogger("paste.pynput")
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
    """Format a Unix timestamp as a CST (UTC+8) HH:MM:SS.mmm string."""
    return datetime.fromtimestamp(ts, tz=timezone(CST_TIMEDELTA)).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Selection reader (via xclip) — selection in {"primary", "clipboard"}
# ---------------------------------------------------------------------------
def _read_selection_text(selection):
    """Read the current PRIMARY/CLIPBOARD selection as UTF-8 text.

    Returns (text: str | None, bytes_len: int). text is None if read failed.
    """
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
    """Replace control characters (except \\n, \\t) with visible escapes."""
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

def report_paste(gesture, selection, confidence, extra=""):
    """Read the relevant selection and log a paste candidate."""
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
    print(f"  When:    {when} CST")
    print(f"  Gesture: {gesture}{('  ' + extra) if extra else ''}")
    print(f"  Source:  {selection.upper()} selection")
    print(f"  Size:    {chars} chars, {bytes_len} bytes")
    print(f"  Preview: {preview}")
    print("─" * 72)
    print()

    log.info(
        "PASTE CANDIDATE #%d | %s | gesture=%s | src=%s | %d chars | %s",
        _total_pastes, confidence, gesture, selection, chars, when,
    )


# ---------------------------------------------------------------------------
# Keyboard listener — manual modifier tracking
# ---------------------------------------------------------------------------
# pynput sets KeyCode.vk to the X keysym on Linux; 'v' is the same regardless of
# Ctrl/Shift, so match on vk (key.char becomes a control code / None with Ctrl).
_V_VK = keyboard.KeyCode.from_char("v").vk

_ctrl_held = False
_shift_held = False
_v_held = False                      # autorepeat debounce for 'v'

_CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
_SHIFT_KEYS = {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r}


def _is_v(key):
    vk = getattr(key, "vk", None)
    if vk is not None and vk == _V_VK:
        return True
    ch = getattr(key, "char", None)
    return ch in ("v", "V", "\x16")   # \x16 = Ctrl-V control code


def on_press(key):
    global _ctrl_held, _shift_held, _v_held
    if key in _CTRL_KEYS:
        _ctrl_held = True
        return
    if key in _SHIFT_KEYS:
        _shift_held = True
        return
    if _is_v(key):
        if _v_held:
            return                    # ignore autorepeat
        _v_held = True
        if _ctrl_held:
            gesture = "Ctrl+Shift+V" if _shift_held else "Ctrl+V"
            log.debug("Key gesture detected: %s", gesture)
            report_paste(gesture, "clipboard", "strong candidate")


def on_release(key):
    global _ctrl_held, _shift_held, _v_held
    if key in _CTRL_KEYS:
        _ctrl_held = False
    elif key in _SHIFT_KEYS:
        _shift_held = False
    elif _is_v(key):
        _v_held = False


# ---------------------------------------------------------------------------
# Mouse listener — middle-click = PRIMARY paste candidate
# ---------------------------------------------------------------------------
def on_click(x, y, button, pressed):
    if pressed and button == mouse.Button.middle:
        log.debug("Middle-click at (%d, %d)", x, y)
        report_paste("middle-click", "primary", "weak candidate", extra=f"@({x},{y})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("Paste-event detector (pynput backend)")
    log.info("Watching: Ctrl+V / Ctrl+Shift+V (CLIPBOARD), middle-click (PRIMARY)")
    log.info("Note: gestures are CANDIDATES, not confirmed pastes.")
    log.info("=" * 60)
    log.info("Listening ... (Ctrl+C to quit)")

    kb = keyboard.Listener(on_press=on_press, on_release=on_release)
    ms = mouse.Listener(on_click=on_click)
    kb.start()
    ms.start()

    try:
        while kb.running and ms.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        log.info("")
        log.info("Received SIGINT. Shutting down.")
    finally:
        kb.stop()
        ms.stop()
        log.info("Final stats: total paste candidates = %d", _total_pastes)
        log.info("Listeners stopped. Goodbye.")


if __name__ == "__main__":
    main()
