"""daemon/collectors/keyboard.py — visible-char keystroke capture (plan 04 §1).

The KeyPress branch of the same XRecord context used by the paste detector
(Thread B, 03 §5/§6) — XRecord is the **preferred** backend.  For each KeyPress
we have ``event.detail`` (keycode) and ``event.state`` (cooked modifiers), so:

  - drop shortcut chords (Ctrl / Alt / Super held) — those are not text, and this
    also keeps ``Ctrl+V`` from being logged as a ``v`` (the paste detector owns
    that gesture);
  - decode the keysym (Shift index from ``event.state``) → a Unicode char;
  - keep printable chars plus space/tab/enter as the whitespace chars
    ``' '``/``'\\t'``/``'\\n'`` (whitespace is *content*, not a delimiter, 04 §1);
  - report Backspace as an edit so the aggregator can pop the last char (04 §4).

We **never special-case passwords** — there is no reliable X11 signal (04 §1);
the privacy levers are ``KBD_ENABLED`` and ``KBD_APP_DENYLIST`` (handled upstream).

Emits the same ``kbd_char`` events the pynput alternative (04 §1a) would, so the
aggregator (``daemon/aggregator.py``) is backend-agnostic:

  {"kind":"kbd_char", "char":"a", "ts":now}
  {"kind":"kbd_char", "backspace":True, "ts":now}
"""
from __future__ import annotations

import logging
import time

from Xlib import X, XK

log = logging.getLogger("dovw.keyboard")


# modifiers whose presence means "this is a shortcut chord, not typed text" (04 §1)
_CHORD_MASK = X.ControlMask | X.Mod1Mask | X.Mod4Mask     # Ctrl / Alt / Super

_WHITESPACE = {
    XK.XK_space: " ",
    XK.XK_Tab: "\t",
    XK.XK_Return: "\n",
    XK.XK_KP_Enter: "\n",
    XK.XK_KP_Space: " ",
    XK.XK_KP_Tab: "\t",
}


def keysym_to_unicode(keysym: int):
    """Map an X keysym to a printable Unicode char, or None.

    Standard algorithm: Latin-1 / ASCII keysyms equal their codepoint; the
    Unicode keysym range is ``0x01000000 + codepoint``.  Anything else (function
    keys, navigation, dead keys, etc.) returns None → dropped as non-visible.
    """
    if keysym in _WHITESPACE:
        return _WHITESPACE[keysym]
    # direct Latin-1 / ASCII (0x20..0x7e printable, 0xa0..0xff Latin-1 supplement)
    if 0x20 <= keysym <= 0x7e or 0xa0 <= keysym <= 0xff:
        ch = chr(keysym)
        return ch if ch.isprintable() else None
    # Unicode keysyms: 0x01000000 | codepoint
    if 0x01000000 <= keysym <= 0x0110ffff:
        ch = chr(keysym - 0x01000000)
        return ch if ch.isprintable() else None
    return None


class KeyboardCollector:
    def __init__(self, local_dpy, emit, settings):
        self.local_dpy = local_dpy
        self.emit = emit
        self.s = settings

    def on_event(self, event) -> None:
        if event.type != X.KeyPress:
            return
        state = event.state
        if state & _CHORD_MASK:                 # Ctrl/Alt/Super held → shortcut, drop
            return

        # Backspace is an edit, not a char (04 §4) — index 0, no shift needed.
        if self.local_dpy.keycode_to_keysym(event.detail, 0) == XK.XK_BackSpace:
            log.debug("kbd backspace")
            self.emit({"kind": "kbd_char", "backspace": True, "ts": time.time()})
            return

        shift = bool(state & X.ShiftMask)
        keysym = self.local_dpy.keycode_to_keysym(event.detail, 1 if shift else 0)
        ch = keysym_to_unicode(keysym)
        if ch is None:
            return
        # Caps Lock affects letters independently of Shift.
        if state & X.LockMask and ch.isalpha():
            ch = ch.swapcase()
        log.debug("kbd char=%r", ch)
        self.emit({"kind": "kbd_char", "char": ch, "ts": time.time()})
