"""daemon/collectors/focus.py — focus, title & window-list collector (plan 03 §1).

Refactor of ``reference_v2/demo-get-window-title-change.py``: the X11 mechanics
(atoms, PropertyChangeMask, dedup, BadWindow tolerance) are kept verbatim; the
demo's ``log(...)`` reporting is replaced by an injected ``emit(event: dict)``
callback (the thread-safe shim from 01 §2).

This object does **not** own the connection or the event loop — it is driven by
``collectors/xpump.py`` (Thread A), which owns the shared display and routes
PropertyNotify events here by atom.  Emits:

  {"kind":"focus",       "x_window_id":wid, "title":t, "ts":now}
  {"kind":"window_list", "added":[ids], "gone":[ids], "ts":now}
  {"kind":"title",       "x_window_id":wid, "old":o, "new":n, "ts":now}
"""
from __future__ import annotations

import time

from Xlib import X, Xatom, error


def wid_str(wid: int) -> str:
    return f"0x{wid:08x}"


class FocusTitleCollector:
    ROOT_ATOMS_FOCUS = {"_NET_ACTIVE_WINDOW"}
    ROOT_ATOMS_LIST = {"_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"}
    TITLE_ATOMS = {"_NET_WM_NAME", "WM_NAME", "_NET_WM_VISIBLE_NAME"}

    def __init__(self, dpy, root, emit):
        self.dpy = dpy
        self.root = root
        self.emit = emit
        self.A_NET_CLIENT_LIST = dpy.intern_atom("_NET_CLIENT_LIST")
        self.A_NET_CLIENT_LIST_STACKING = dpy.intern_atom("_NET_CLIENT_LIST_STACKING")
        self.A_NET_ACTIVE_WINDOW = dpy.intern_atom("_NET_ACTIVE_WINDOW")
        self.A_NET_WM_NAME = dpy.intern_atom("_NET_WM_NAME")
        self.A_UTF8_STRING = dpy.intern_atom("UTF8_STRING")
        self.titles: dict[int, str | None] = {}
        self.watched: set[int] = set()
        self.active: int | None = None

    # ----------------------------- X reads (verbatim) -----------------------------
    @staticmethod
    def _decode(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    def _win(self, wid):
        return self.dpy.create_resource_object("window", wid)

    def read_title(self, win):
        try:
            p = win.get_full_property(self.A_NET_WM_NAME, self.A_UTF8_STRING)
            if p and p.value:
                return self._decode(p.value)
        except error.XError:
            return None
        try:
            p = win.get_full_property(Xatom.WM_NAME, X.AnyPropertyType)
            if p and p.value:
                return self._decode(p.value)
        except error.XError:
            return None
        return None

    def get_client_list(self):
        for atom in (self.A_NET_CLIENT_LIST, self.A_NET_CLIENT_LIST_STACKING):
            try:
                p = self.root.get_full_property(atom, X.AnyPropertyType)
            except error.XError:
                p = None
            if p and p.value:
                return list(p.value)
        return []

    def get_active_window(self):
        try:
            p = self.root.get_full_property(self.A_NET_ACTIVE_WINDOW, X.AnyPropertyType)
        except error.XError:
            p = None
        if p and p.value:
            return int(p.value[0]) or None
        return None

    def watch(self, wid):
        if wid in self.watched:
            return
        win = self._win(wid)
        try:
            win.change_attributes(event_mask=X.PropertyChangeMask)
            title = self.read_title(win)
        except error.XError:
            return
        self.watched.add(wid)
        self.titles[wid] = title

    # ----------------------------- emit-translated handlers -----------------------------
    def sync_clients(self, initial=False):
        current = set(self.get_client_list())
        added = current - self.watched
        gone = self.watched - current
        for wid in added:
            self.watch(wid)
        for wid in gone:
            self.watched.discard(wid)
            self.titles.pop(wid, None)
        self.dpy.sync()
        if added or gone or initial:
            self.emit({"kind": "window_list", "added": [int(w) for w in added],
                       "gone": [int(w) for w in gone], "ts": time.time()})

    def report_active(self, initial=False):
        wid = self.get_active_window()
        if not wid:
            return
        if wid == self.active and not initial:
            return
        self.active = wid
        self.watch(wid)
        title = self.titles.get(wid) or self.read_title(self._win(wid))
        self.emit({"kind": "focus", "x_window_id": int(wid), "title": title,
                   "ts": time.time()})

    def handle_title_change(self, win):
        wid = win.id
        new = self.read_title(win)
        old = self.titles.get(wid)
        if new == old:
            return
        self.titles[wid] = new
        self.emit({"kind": "title", "x_window_id": int(wid), "old": old,
                   "new": new, "ts": time.time()})

    # ----------------------------- routing (called by xpump) -----------------------------
    def on_property_notify(self, ev, atom_name):
        """Return True if this event was handled here."""
        if ev.window.id == self.root.id:
            if atom_name in self.ROOT_ATOMS_FOCUS:
                self.report_active()
                return True
            if atom_name in self.ROOT_ATOMS_LIST:
                self.sync_clients()
                return True
            return False
        if atom_name in self.TITLE_ATOMS:
            self.handle_title_change(ev.window)
            return True
        return False

    def prime(self):
        """Initial baseline (called once after the pump selects root input)."""
        self.sync_clients(initial=True)
        self.report_active(initial=True)
