"""daemon/collectors/xpump.py — Thread A: the shared Xlib pump (plan 03 §1-2, §7).

Owns ONE display connection and the ``next_event`` loop.  Both the focus/title
collector and the virtual-desktop collector watch root PropertyNotify, so rather
than two connections we run a single loop here and route each PropertyNotify to
whichever collector claims it (by atom).  Later steps (clipboard, PRIMARY) add
their XFIXES handlers onto this same connection.

Runs in a dedicated OS thread (the loop must never block — 01 §1).  Uses
``select`` on the X fd with a timeout so a ``stop`` Event is honored promptly
instead of blocking forever in ``next_event``.  All X errors are swallowed
(windows vanish constantly → BadWindow; never crash — 01 §7).
"""
from __future__ import annotations

import select

from Xlib import X, display, error

from .clipboard import ClipboardCollector
from .focus import FocusTitleCollector
from .selection import PrimarySelectionCollector
from .vdesktop import VirtualDesktopCollector


class XPump:
    def __init__(self, emit):
        self.emit = emit
        self.dpy = display.Display()
        self.root = self.dpy.screen().root
        self.dpy.set_error_handler(self._on_x_error)
        self._atom_name_cache: dict[int, str] = {}
        self.focus = FocusTitleCollector(self.dpy, self.root, emit)
        self.vdesktop = VirtualDesktopCollector(self.dpy, self.root, emit)
        # collectors consulted, in order, for each PropertyNotify
        self._handlers = [self.focus, self.vdesktop]
        # XFIXES selection-owner collectors (clipboard copy + PRIMARY highlight, 03 §3-4)
        self._xfixes_ok = "XFIXES" in self.dpy.list_extensions()
        self._sel_handlers = []
        self._sel_type = None
        if self._xfixes_ok:
            self.dpy.xfixes_query_version()
            self.clipboard = ClipboardCollector(self.dpy, self.root, emit)
            self.selection = PrimarySelectionCollector(self.dpy, self.root, emit)
            self._sel_handlers = [self.clipboard, self.selection]
            code = self.dpy.extension_event.SetSelectionOwnerNotify
            self._sel_type = code[0] if isinstance(code, tuple) else code

    def _on_x_error(self, err, request):
        pass  # 01 §7: ignore BadWindow/BadAtom from races

    def _atom_name(self, atom):
        if atom in self._atom_name_cache:
            return self._atom_name_cache[atom]
        name = ""
        if atom:
            try:
                name = self.dpy.get_atom_name(atom)
            except error.XError:
                name = ""
        self._atom_name_cache[atom] = name
        return name

    def run(self, stop):
        """Blocking pump; returns when ``stop`` (threading.Event) is set."""
        self.root.change_attributes(event_mask=X.PropertyChangeMask)
        for h in self._sel_handlers:               # XFIXES owner-notify registration
            h.select_input()
        self.dpy.sync()
        self.focus.prime()
        self.vdesktop.prime()

        fd = self.dpy.fileno()
        while not stop.is_set():
            # wait for activity (or wake every 0.5s to re-check stop)
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not r:
                continue
            n = self.dpy.pending_events()
            for _ in range(n):
                ev = self.dpy.next_event()
                if self._sel_type is not None and ev.type == self._sel_type:
                    for h in self._sel_handlers:
                        try:
                            if h.on_selection_owner(ev):
                                break
                        except error.XError:
                            pass
                    continue
                if ev.type != X.PropertyNotify:
                    continue
                name = self._atom_name(ev.atom)
                if not name:
                    continue
                for h in self._handlers:
                    try:
                        if h.on_property_notify(ev, name):
                            break
                    except error.XError:
                        pass

    def close(self):
        try:
            self.dpy.close()
        except Exception:
            pass


def run(stop, emit):
    """Entry point for Thread A (per 03's run(stop_event, emit) convention)."""
    pump = XPump(emit)
    try:
        pump.run(stop)
    finally:
        pump.close()
