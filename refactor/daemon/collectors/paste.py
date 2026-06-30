"""daemon/collectors/paste.py — paste/read-event detector (plan 03 §5).

Refactor of ``reference_v2/demo-detect-paste-event-xrecord.py``'s
``_handle_event``: the KeyPress/KeyRelease/ButtonPress decode is kept
**verbatim**; ``report_paste(...)`` is replaced by an ``emit`` of a lightweight
``read_event``.  The selection *content* (what was likely pasted) is read by the
loop-side handler via the executor, not here (Thread B must not block on xclip).

Gestures → candidates (never "confirmed", 03 §5):
  Ctrl+V / Ctrl+Shift+V  → CLIPBOARD read  (strong)
  middle-click           → PRIMARY read    (weak/overloaded)
"""
from __future__ import annotations

import logging
import time

from Xlib import X, XK

log = logging.getLogger("dovw.paste")


class PasteDetector:
    def __init__(self, local_dpy, emit):
        self.local_dpy = local_dpy          # keycode→keysym lookups
        self.emit = emit
        self._v_held = False                # autorepeat debounce for 'v'

    def on_event(self, event) -> None:
        """Decode one captured X event and emit paste candidates (verbatim logic)."""
        if event.type == X.KeyPress:
            keysym = self.local_dpy.keycode_to_keysym(event.detail, 0)
            ctrl = bool(event.state & X.ControlMask)
            shift = bool(event.state & X.ShiftMask)
            if keysym == XK.XK_v:
                if self._v_held:
                    return                  # ignore autorepeat
                self._v_held = True
                if ctrl:
                    gesture = "Ctrl+Shift+V" if shift else "Ctrl+V"
                    self._emit("clipboard", gesture, "strong",
                               getattr(event, "time", None))
        elif event.type == X.KeyRelease:
            keysym = self.local_dpy.keycode_to_keysym(event.detail, 0)
            if keysym == XK.XK_v:
                self._v_held = False
        elif event.type == X.ButtonPress:
            if event.detail == 2:           # button 2 = middle
                self._emit("primary", "middle-click", "weak",
                           getattr(event, "time", None))

    def _emit(self, selection, gesture, confidence, server_time):
        log.debug("paste gesture: %s -> %s (%s)", gesture, selection, confidence)
        self.emit({"kind": "read_event", "selection": selection, "gesture": gesture,
                   "confidence": confidence, "server_time": server_time,
                   "ts": time.time()})
