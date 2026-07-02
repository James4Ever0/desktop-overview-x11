"""daemon/handlers.py — loop-side dispatch handlers (plan 01 §2, 02, 03).

These run on the asyncio loop (registered with ``Runtime.register``).  Each one
validates/enriches a collector event and turns it into ``WindowRegistry`` updates
and DB writes.  This is where the real attribution/association logic lives — the
collector threads only emit raw facts.

Holds the **current virtual-desktop** cache so focus/title rows can be stamped
with desktop index+name without touching live X state (03 §2).
"""
from __future__ import annotations

import hashlib
import logging

from .collectors import clipboard, selection

log = logging.getLogger("dovw.handlers")


class EventHandlers:
    def __init__(self, store, registry, settings, runtime=None):
        self.store = store
        self.reg = registry
        self.s = settings
        self.rt = runtime               # needed for executor (clipboard/selection/paste IO)
        # current desktop association (03 §2)
        self.vdesktop_index: int | None = None
        self.vdesktop_name: str | None = None
        self.desktop_names: list[str] = []
        # hook the keyboard aggregator + capture scheduler set later (flush on focus/title change)
        self._focus_hooks: list = []     # async callable(window_uid)
        self._title_hooks: list = []     # async callable(window_uid)
        # clipboard dedup + PRIMARY Strategy C state (03 §3-4)
        self._last_clip_hash: str | None = None
        self.sel_strategy = selection.SelectionStrategyC()
        # current focused window title/xid for keyboard denylist filtering
        self.current_focus_title: str | None = None
        self.current_focus_xid: int | None = None
        # last recorded title per window to avoid duplicate title_history rows
        self._last_title: dict[int, str] = {}

    def register_all(self, runtime) -> None:
        runtime.register("window_list", self.handle_window_list)
        runtime.register("focus", self.handle_focus)
        runtime.register("title", self.handle_title)
        runtime.register("screen_lock", self.handle_screen_lock)
        runtime.register("vdesktop_current", self.handle_vdesktop_current)
        runtime.register("vdesktop_meta", self.handle_vdesktop_meta)

    def add_focus_hook(self, fn) -> None:
        self._focus_hooks.append(fn)

    def add_title_hook(self, fn) -> None:
        self._title_hooks.append(fn)

    def register_io(self, runtime) -> None:
        """Register the executor-backed clipboard/selection/paste handlers (03 §3-5)."""
        self.rt = runtime
        runtime.register("clipboard_write", self.handle_clipboard_write)
        runtime.register("selection_owner", self.handle_selection_owner)
        runtime.register("read_event", self.handle_read_event)

    # ───────────────────────── window lifecycle (02 §3) ─────────────────────────
    async def handle_window_list(self, ev) -> None:
        ts = ev.get("ts")
        added, gone = ev.get("added", []), ev.get("gone", [])
        log.debug("window_list: added=%d gone=%d", len(added), len(gone))
        for xid in added:
            await self.reg.ensure_window(int(xid), None, ts)
        for xid in gone:
            await self.reg.mark_dead(int(xid), ts)

    # ───────────────────────── focus (02 §4-5, 03 §1) ─────────────────────────
    async def handle_focus(self, ev) -> None:
        xid = int(ev["x_window_id"])
        ts = ev.get("ts")
        title = ev.get("title")
        uid = await self.reg.ensure_window(xid, None, ts)
        self.current_focus_xid = int(xid)
        self.current_focus_title = title
        self.reg.set_focus(uid)
        await self.reg.bump_last_seen(uid, ts)
        await self.reg.set_vdesktop(uid, self.vdesktop_index, self.vdesktop_name)
        log.debug("focus window_uid=%d x=0x%08x title=%s vdesktop=%s",
                  uid, xid, title, self.vdesktop_name)
        self.store.enqueue(
            "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
            " VALUES(?,?,?,?)", (uid, self.vdesktop_index, self.vdesktop_name, ts))
        for hook in self._focus_hooks:
            await hook(uid)

    async def handle_screen_lock(self, ev) -> None:
        """Record a lock/unlock boundary so focus spans do not include locked time."""
        ts = ev.get("ts")
        locked = 1 if ev.get("locked") else 0
        method = ev.get("method")
        self.store.enqueue(
            "INSERT INTO screen_lock_event(locked, method, changed_at) VALUES(?,?,?)",
            (locked, method, ts))
        log.debug("screen_lock recorded locked=%d method=%s", locked, method)

    # ───────────────────────── title history (03 §1) ─────────────────────────
    async def handle_title(self, ev) -> None:
        xid = int(ev["x_window_id"])
        ts = ev.get("ts")
        new = ev.get("new")
        if not new:
            return
        if xid == self.current_focus_xid:
            self.current_focus_title = new
        uid = await self.reg.ensure_window(xid, None, ts)
        await self.reg.bump_last_seen(uid, ts)

        cached = self._last_title.get(uid)
        if cached == new:
            log.debug("title dedup (cache) window_uid=%d unchanged=%s", uid, new)
            return
        if cached is None:
            row = await self.store.fetchone(
                "SELECT title FROM title_history WHERE window_uid = ? ORDER BY changed_at DESC LIMIT 1",
                (uid,))
            latest = row["title"] if row else None
            if latest == new:
                self._last_title[uid] = new
                log.debug("title dedup (db) window_uid=%d unchanged=%s", uid, new)
                return
        self._last_title[uid] = new
        log.debug("title window_uid=%d x=0x%08x new=%s", uid, xid, new)
        self.store.enqueue(
            "INSERT INTO title_history(window_uid, title, changed_at) VALUES(?,?,?)",
            (uid, new, ts))
        for hook in self._title_hooks:
            await hook(uid)

    # ───────────────────────── virtual desktop (03 §2) ─────────────────────────
    async def handle_vdesktop_current(self, ev) -> None:
        self.vdesktop_index = ev.get("index")
        self.vdesktop_name = ev.get("name")
        self.store.enqueue(
            "INSERT INTO vdesktop_state(idx, name, count, changed_at) VALUES(?,?,?,?)",
            (self.vdesktop_index, self.vdesktop_name, len(self.desktop_names) or None,
             ev.get("ts")))

    async def handle_vdesktop_meta(self, ev) -> None:
        self.desktop_names = list(ev.get("names", []))
        count = ev.get("count")
        # keep current name fresh if its index now maps to a new label
        if self.vdesktop_index is not None and 0 <= self.vdesktop_index < len(self.desktop_names):
            self.vdesktop_name = self.desktop_names[self.vdesktop_index]
        self.store.enqueue(
            "INSERT INTO vdesktop_state(idx, name, count, changed_at) VALUES(?,?,?,?)",
            (self.vdesktop_index, self.vdesktop_name, count, ev.get("ts")))

    # ───────────────────────── clipboard copy/WRITE (03 §3) ─────────────────────────
    async def handle_clipboard_write(self, ev) -> None:
        ts = ev.get("ts")
        sel = ev.get("selection", "CLIPBOARD")
        targets = await self.rt.run_in_executor(clipboard.get_targets, sel)
        if not targets:
            log.debug("clipboard write: no targets (empty)")
            return
        kind = clipboard.classify(targets)
        uid = self.reg.current_focus_window_uid

        # PASSWORD: store the fact + redaction marker, never read the content (03 §3).
        if kind == "PASSWORD":
            log.info("clipboard PASSWORD redacted for window_uid=%s", uid)
            self.store.enqueue(
                "INSERT INTO clipboard_event(window_uid, kind, text, image_rel,"
                " n_chars, n_bytes, created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, "PASSWORD", "<REDACTED: password-manager hint>", None, 0, 0, ts))
            return

        raw = await self.rt.run_in_executor(
            clipboard.read_content_bytes, sel, kind, targets)
        h = hashlib.md5(raw).hexdigest()
        if self._last_clip_hash == h:        # dedup (owner re-assertion / same copy)
            log.debug("clipboard write: dedup (same hash)")
            return
        self._last_clip_hash = h

        text = image_rel = None
        n_chars = 0
        n_bytes = len(raw)
        log.debug("clipboard write: kind=%s bytes=%d window_uid=%s", kind, n_bytes, uid)
        if kind in ("TEXT", "HTML", "FILES", "OTHER"):
            text = raw.decode("utf-8", "replace") if raw else ""
            n_chars = len(text)
        elif kind == "IMAGE":
            w, hgt = clipboard.image_dims(raw)
            n_chars = (w or 0) * (hgt or 0)
            ext = (clipboard.pick_image_target(targets).split("/")[-1] or "png").lower()
            image_rel = f"window_captures/clip/{int(ts * 1000)}.{ext}"
            await self.rt.run_in_executor(
                clipboard.save_bytes, raw, str(self.s.data_dir / image_rel))

        self.store.enqueue(
            "INSERT INTO clipboard_event(window_uid, kind, text, image_rel,"
            " n_chars, n_bytes, created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, kind, text, image_rel, n_chars, n_bytes, ts))

    # ───────────────────────── PRIMARY highlight (03 §4) ─────────────────────────
    async def handle_selection_owner(self, ev) -> None:
        text, _ = await self.rt.run_in_executor(selection.read_primary_text)
        seg = self.sel_strategy.feed(text, ev.get("ts"))
        if seg is None:
            return
        log.debug("selection owner: stored segment chars=%d window_uid=%s",
                  seg["chars"], self.reg.current_focus_window_uid)
        uid = self.reg.current_focus_window_uid
        self.store.enqueue(
            "INSERT INTO selection_event(window_uid, text, n_chars, created_at)"
            " VALUES(?,?,?,?)", (uid, seg["text"], seg["chars"], seg["start_ts"]))

    # ───────────────────────── paste candidate / read event (03 §5) ─────────────────────────
    async def handle_read_event(self, ev) -> None:
        uid = self.reg.current_focus_window_uid
        text = None
        log.debug("read_event: selection=%s gesture=%s window_uid=%s",
                  ev.get("selection"), ev.get("gesture"), uid)
        if self.s.read_event_capture_content:
            if ev.get("selection") == "primary":
                text, _ = await self.rt.run_in_executor(selection.read_primary_text)
            else:
                text = await self.rt.run_in_executor(_read_clipboard_text)
        self.store.enqueue(
            "INSERT INTO read_event(window_uid, selection, gesture, confidence,"
            " text, server_time, created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, ev.get("selection"), ev.get("gesture"), ev.get("confidence"),
             text, ev.get("server_time"), ev.get("ts")))


def _read_clipboard_text():
    """Blocking CLIPBOARD text read for paste enrichment (run via executor)."""
    raw = clipboard.xclip("CLIPBOARD", "UTF8_STRING")
    return raw.decode("utf-8", "replace") if raw else None
