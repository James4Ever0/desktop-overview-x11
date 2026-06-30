"""daemon/window_captures.py — capture scheduler, file save & GC (plan 05 §2-4).

The power-efficient refresh from ``power_efficient_refresh.txt``, as a daemon
asyncio task (runs whether or not the frontend is open — that's the point):

  startup:        FULL sweep — capture every window, fill the DB immediately
  every X seconds: focused window + up to Y background windows (shuffled queue)

Images are files; the DB stores the path **relative to data_dir** (05 §3):
  window_captures/<daemon_boot_id>/<window_uid>/<ts_millis>.png

Capture itself is blocking → always via ``runtime.run_in_executor`` (01 §4).
"""
from __future__ import annotations

import logging
import os
import time

from . import capture

log = logging.getLogger("dovw.window_captures")


class WindowCaptureScheduler:
    def __init__(self, runtime, store, registry, settings, daemon_boot_id: str):
        self.rt = runtime
        self.store = store
        self.reg = registry
        self.s = settings
        self.boot = daemon_boot_id
        self._bg_queue: list[str] = []   # shuffled background x_window_id (hex) rotation
        self._shuffle_i = 0              # deterministic-rotation cursor (no Math.random needed)

    # ───────────────────────── event hooks ─────────────────────────
    async def on_focus_change(self, window_uid: int) -> None:
        """Capture the newly focused window immediately (config: capture_on_focus)."""
        if not self.s.capture_on_focus:
            return
        now = time.time()
        wid_hex = await self._wid_hex_for_uid(window_uid)
        if wid_hex is None:
            return
        title = await self._title_for_uid(window_uid)
        await self._capture_and_save(wid_hex, title or "", is_focused=True, now=now)
        log.debug("capture_on_focus window_uid=%d", window_uid)

    async def on_title_change(self, window_uid: int) -> None:
        """Capture the focused window when its title changes (config: capture_on_title)."""
        if not self.s.capture_on_title:
            return
        uid = self.reg.current_focus_window_uid
        if uid is None or uid != window_uid:
            return
        now = time.time()
        wid_hex = await self._wid_hex_for_uid(uid)
        if wid_hex is None:
            return
        title = await self._title_for_uid(uid)
        await self._capture_and_save(wid_hex, title or "", is_focused=True, now=now)
        log.debug("capture_on_title window_uid=%d", uid)

    async def _wid_hex_for_uid(self, window_uid: int) -> str | None:
        """Look up x_window_id from the window table, return as hex string."""
        row = await self.store.fetchone(
            "SELECT x_window_id FROM window WHERE window_uid=?", (window_uid,))
        if row is None:
            return None
        return f"0x{int(row[0]):08x}"

    async def _title_for_uid(self, window_uid: int) -> str | None:
        """Get the latest known title for a window_uid."""
        row = await self.store.fetchone(
            "SELECT title FROM title_history WHERE window_uid=? "
            "ORDER BY changed_at DESC LIMIT 1", (window_uid,))
        return row[0] if row else None

    # ───────────────────────── capture orchestration ─────────────────────────
    async def full_sweep(self, now: float) -> int:
        windows = await self.rt.run_in_executor(capture.get_window_list)
        await self._reconcile(windows, now)
        n = 0
        for wid_hex, title in windows:
            if await self._capture_and_save(wid_hex, title, is_focused=False, now=now):
                n += 1
        log.info("full sweep captured %d/%d windows", n, len(windows))
        return n

    async def tick(self, now: float) -> int:
        windows = await self.rt.run_in_executor(capture.get_window_list)
        await self._reconcile(windows, now)
        if not windows:
            return 0
        title_by_id = {w: t for w, t in windows}
        current = set(title_by_id)

        active_int = await self.rt.run_in_executor(capture.get_active_window_id)
        focused = None
        if active_int is not None:
            for wid in current:
                if capture.normalize_win_id(wid) == active_int:
                    focused = wid
                    break

        targets, seen = [], set()
        if focused is not None:
            targets.append((focused, True))
            seen.add(focused)
        for wid in self._select_background_batch(current, focused):
            if wid not in seen:
                targets.append((wid, False))
                seen.add(wid)

        n = 0
        for wid_hex, is_focused in targets:
            if await self._capture_and_save(wid_hex, title_by_id[wid_hex], is_focused, now):
                n += 1
        return n

    def _select_background_batch(self, current_ids, focused_id) -> list[str]:
        """Shuffled-rotation queue (lifted from the demo's _select_background_batch).

        Rotation order is a deterministic shuffle (index-stepped) so we avoid
        Math.random-style nondeterminism while still cycling every window.
        """
        pool = [w for w in current_ids if w != focused_id]
        if not pool:
            return []
        limit = min(self.s.refresh_batch_size, len(pool))
        batch = []
        while len(batch) < limit:
            if not self._bg_queue:
                refill = sorted(w for w in current_ids if w != focused_id)
                # rotate the refill so successive exhaustions start at a new offset
                self._shuffle_i = (self._shuffle_i + 1) % max(1, len(refill))
                self._bg_queue = refill[self._shuffle_i:] + refill[:self._shuffle_i]
            wid = self._bg_queue.pop(0)
            if wid not in current_ids or wid == focused_id or wid in batch:
                continue
            batch.append(wid)
        return batch

    # ───────────────────────── save (05 §3) ─────────────────────────
    async def _capture_and_save(self, wid_hex: str, title: str, is_focused: bool,
                                now: float) -> bool:
        xid = capture.normalize_win_id(wid_hex)
        if xid is None:
            return False
        wm = await self.rt.run_in_executor(capture.get_app_name, wid_hex)
        uid = await self.reg.ensure_window(xid, wm or None, now)
        if self.s.filter_no_vdesktop:
            vrow = await self.store.fetchone(
                "SELECT 1 FROM focus_event WHERE window_uid=? AND vdesktop_index IS NOT NULL"
                " LIMIT 1", (uid,))
            if vrow is None:
                return False
        img = await self.rt.run_in_executor(
            capture.capture_window, wid_hex, title, self.s.window_capture_max_dim)
        if img is None:
            return False

        ts_ms = int(now * 1000)
        rel = os.path.join("window_captures", self.boot, str(uid), f"{ts_ms}.png")
        abs_path = self.s.data_dir / rel
        await self.rt.run_in_executor(_save_png, img, str(abs_path))

        # Immediate (not batched) write: window_captures are low-rate (every few
        # seconds), and the GC below must see this row committed to prune
        # correctly — batching it would race the read.  (Batching is for the
        # high-rate keyboard/event path, not capture.)
        await self.store.execute(
            "INSERT INTO window_capture(window_uid, rel_path, width, height, captured_at, is_focused)"
            " VALUES(?,?,?,?,?,?)", (uid, rel, img.width, img.height, now, 1 if is_focused else 0))
        await self.reg.bump_last_seen(uid, now)
        await self._gc_window(uid)
        return True

    async def _reconcile(self, windows, now: float) -> None:
        ids = {capture.normalize_win_id(w) for w, _ in windows}
        ids.discard(None)
        wm_of = {capture.normalize_win_id(w): None for w, _ in windows}
        await self.reg.reconcile(ids, wm_class_of=lambda x: wm_of.get(x), now=now)

    # ───────────────────────── GC (05 §4) ─────────────────────────
    async def _gc_window(self, uid: int) -> None:
        keep = self.s.window_capture_keep_per_window
        rows = await self.store.fetchall(
            "SELECT id, rel_path FROM window_capture WHERE window_uid=? "
            "ORDER BY captured_at DESC", (uid,))
        for rid, rel in rows[keep:]:
            await self.store.execute("DELETE FROM window_capture WHERE id=?", (rid,))
            await self.rt.run_in_executor(_unlink, str(self.s.data_dir / rel))

    # ───────────────────────── task entry ─────────────────────────
    async def run(self, stop, clock) -> None:
        """Long-lived scheduler task. ``clock()`` returns epoch seconds."""
        import asyncio
        await self.full_sweep(clock())            # 05 §2: first refresh is always full
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.refresh_interval_s)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                await self.tick(clock())
            except Exception:                    # 01 §7: a bad tick never kills the task
                log.exception("window_capture tick failed")


def _save_png(img, abs_path: str) -> None:
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    img.save(abs_path, format="PNG")


def _unlink(abs_path: str) -> None:
    try:
        os.remove(abs_path)
    except FileNotFoundError:
        pass
