"""daemon/collectors/vdesktop.py — virtual-desktop collector (plan 03 §2).

Refactor of ``reference_v2/demo-virtual-desktop-monitoring.py`` — **only** the
``cmd_monitor`` logic (the daemon never lists or switches desktops, so those
subcommands are dropped).  X reads kept verbatim; ``log(...)`` → ``emit``.

Shares Thread A's connection (xpump): both this and focus watch root
PropertyNotify, so xpump owns the loop and routes by atom.  Emits:

  {"kind":"vdesktop_current", "index":i, "name":n, "ts":now}
  {"kind":"vdesktop_meta",    "count":c, "names":[...], "ts":now}
"""
from __future__ import annotations

import time

from Xlib import X, error


class VirtualDesktopCollector:
    A_CURRENT = "_NET_CURRENT_DESKTOP"
    A_COUNT = "_NET_NUMBER_OF_DESKTOPS"
    A_NAMES = "_NET_DESKTOP_NAMES"
    WATCH_ATOMS = {A_CURRENT, A_COUNT, A_NAMES}

    def __init__(self, dpy, root, emit):
        self.dpy = dpy
        self.root = root
        self.emit = emit
        self.A_NET_CURRENT_DESKTOP = dpy.intern_atom(self.A_CURRENT)
        self.A_NET_NUMBER_OF_DESKTOPS = dpy.intern_atom(self.A_COUNT)
        self.A_NET_DESKTOP_NAMES = dpy.intern_atom(self.A_NAMES)
        self.A_UTF8_STRING = dpy.intern_atom("UTF8_STRING")
        self.current: int | None = None
        self.count: int | None = None
        self.names: list[str] = []

    # ----------------------------- X reads (verbatim) -----------------------------
    def get_current_desktop(self):
        try:
            p = self.root.get_full_property(self.A_NET_CURRENT_DESKTOP, X.AnyPropertyType)
            if p and p.value:
                return int(p.value[0])
        except error.XError:
            pass
        return None

    def get_number_of_desktops(self):
        try:
            p = self.root.get_full_property(self.A_NET_NUMBER_OF_DESKTOPS, X.AnyPropertyType)
            if p and p.value:
                return int(p.value[0])
        except error.XError:
            pass
        return None

    def get_desktop_names(self):
        try:
            p = self.root.get_full_property(self.A_NET_DESKTOP_NAMES, X.AnyPropertyType)
            if p and p.value:
                raw = p.value
                if isinstance(raw, bytes):
                    parts = raw.split(b"\x00")
                    return [s.decode("utf-8", "replace") for s in parts if s != b""]
        except error.XError:
            pass
        return []

    def name_for(self, idx):
        if idx is None:
            return "?"
        if 0 <= idx < len(self.names):
            return self.names[idx]
        return f"Desktop {idx + 1}"

    def refresh_state(self):
        self.count = self.get_number_of_desktops()
        self.names = self.get_desktop_names()
        self.current = self.get_current_desktop()

    # ----------------------------- emit-translated handlers -----------------------------
    def _on_current_changed(self):
        new = self.get_current_desktop()
        if new is None or new == self.current:
            return
        self.current = new
        self.emit({"kind": "vdesktop_current", "index": new,
                   "name": self.name_for(new), "ts": time.time()})

    def _on_count_changed(self):
        new = self.get_number_of_desktops()
        if new == self.count:
            return
        self.count = new
        self.names = self.get_desktop_names()
        self.emit({"kind": "vdesktop_meta", "count": self.count,
                   "names": list(self.names), "ts": time.time()})

    def _on_names_changed(self):
        new = self.get_desktop_names()
        if new == self.names:
            return
        self.names = new
        self.emit({"kind": "vdesktop_meta", "count": self.count,
                   "names": list(self.names), "ts": time.time()})

    # ----------------------------- routing (called by xpump) -----------------------------
    def on_property_notify(self, ev, atom_name):
        if ev.window.id != self.root.id or atom_name not in self.WATCH_ATOMS:
            return False
        if atom_name == self.A_CURRENT:
            self._on_current_changed()
        elif atom_name == self.A_COUNT:
            self._on_count_changed()
        elif atom_name == self.A_NAMES:
            self._on_names_changed()
        return True

    def prime(self):
        """Emit an initial baseline so the desktop cache is populated at startup."""
        self.refresh_state()
        self.emit({"kind": "vdesktop_meta", "count": self.count,
                   "names": list(self.names), "ts": time.time()})
        if self.current is not None:
            self.emit({"kind": "vdesktop_current", "index": self.current,
                       "name": self.name_for(self.current), "ts": time.time()})
