"""daemon/collectors/xrecord.py — Thread B: the shared XRecord pump (plan 03 §5).

One RECORD context capturing ``(KeyPress, ButtonPress)`` feeds **both** the
keyboard collector (visible chars, 04) and the paste detector (gesture → read
event, 03 §5).  We never open two RECORD contexts.

Setup is lifted **verbatim** from
``reference_v2/demo-detect-paste-event-xrecord.py`` (two displays: ``record_dpy``
drives the context, ``local_dpy`` does keycode→keysym lookups).  The demo's
single ``_handle_event`` is generalised to a list of registered callbacks so the
keyboard and paste decoders each get every event.

``record_enable_context`` blocks until the context is freed; a tiny waiter thread
disables it (from the *local* connection, per the demo) when ``stop`` is set.
"""
from __future__ import annotations

import threading

from Xlib import X, display
from Xlib.ext import record
from Xlib.protocol import rq


class XRecordPump:
    def __init__(self):
        self.local_dpy = display.Display()      # keycode→keysym lookups
        self.record_dpy = display.Display()     # drives the record context
        self.ctx = None
        self._handlers = []                     # list[callable(event)]

    def add_handler(self, fn) -> None:
        self._handlers.append(fn)

    def _callback(self, reply):
        """record_enable_context callback — parse each captured device event."""
        if reply.category != record.FromServer or reply.client_swapped:
            return
        if not len(reply.data) or reply.data[0] < 2:
            return                              # not a device event we care about
        data = reply.data
        while len(data):
            event, data = rq.EventField(None).parse_binary_value(
                data, self.record_dpy.display, None, None)
            for fn in self._handlers:
                try:
                    fn(event)
                except Exception:               # 01 §7: one bad decode ≠ crash pump
                    pass

    def run(self, stop) -> None:
        """Blocking pump; returns when ``stop`` (threading.Event) is set."""
        if not self.record_dpy.has_extension("RECORD"):
            return
        self.ctx = self.record_dpy.record_create_context(
            0, [record.AllClients],
            [{
                "core_requests": (0, 0), "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.KeyPress, X.ButtonPress),
                "errors": (0, 0),
                "client_started": False, "client_died": False,
            }])

        def _waiter():
            stop.wait()
            try:    # break the blocking enable from the OTHER connection
                self.local_dpy.record_disable_context(self.ctx)
                self.local_dpy.flush()
            except Exception:
                pass
        threading.Thread(target=_waiter, name="xrecord-stop", daemon=True).start()

        try:
            self.record_dpy.record_enable_context(self.ctx, self._callback)
        finally:
            try:
                self.record_dpy.record_free_context(self.ctx)
            except Exception:
                pass
            for d in (self.record_dpy, self.local_dpy):
                try:
                    d.close()
                except Exception:
                    pass


def run(stop, emit, settings) -> None:
    """Entry point for Thread B (per 03's run(stop_event, emit) convention)."""
    from .keyboard import KeyboardCollector
    from .paste import PasteDetector

    pump = XRecordPump()
    paste = PasteDetector(pump.local_dpy, emit)
    pump.add_handler(paste.on_event)
    if settings.kbd_enabled and settings.kbd_backend == "xrecord":
        kbd = KeyboardCollector(pump.local_dpy, emit, settings)
        pump.add_handler(kbd.on_event)
    pump.run(stop)
