"""daemon/aggregator.py — keyboard segment state machine (plan 04 §3).

Concatenates consecutive visible keystrokes for the focused window into one
**segment**, then flushes it (→ ``kbd_segment`` + ``fts_kbd``) on any of three
triggers (04 §3):

  1. idle      — no visible key for ``kbd_idle_flush_s``
  2. focus     — active window changed (subscribes to the ``focus`` hook)
  3. title     — focused window's title changed (subscribes to the ``title`` hook)

This is the chunking logic of
``demo-keyevent-listener-with-idle-chunking-and-focus-change-chunking.py``, moved
onto the asyncio loop.  It owns no X connection — focus/title come from Thread A's
collector via the hooks ``EventHandlers`` exposes (04 §3); keystrokes come from
the ``kbd_char`` events Thread B emits.  One open segment at a time (typing goes
to the focused window).

Whitespace is content (appended), **not** a flush trigger; Backspace pops the
last char (best-effort edit fidelity, 04 §4).  At flush time the segment text is
**stripped** and dropped if it is not longer than ``kbd_min_segment_chars``
(default 3) — sub-threshold noise never reaches the buffer/db (04 §3).
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("dovw.aggregator")


class KeyboardAggregator:
    def __init__(self, store, registry, settings, vdesktop_provider=None, title_provider=None):
        self.store = store
        self.reg = registry
        self.s = settings
        # () -> (vdesktop_index, vdesktop_name); snapshotted per segment for context
        self._vdesktop = vdesktop_provider or (lambda: (None, None))
        # () -> current focused window title (for denylist filtering)
        self._title = title_provider or (lambda: None)
        self._open = None        # {"window_uid","buf","started_at","last_key_at","vidx","vname"}
        self._lock = asyncio.Lock()

    def register(self, runtime) -> None:
        runtime.register("kbd_char", self.on_char)

    # ───────────────────────── keystroke (04 §3) ─────────────────────────
    async def on_char(self, ev) -> None:
        if not self.s.kbd_enabled:
            return
        ts = ev.get("ts")
        async with self._lock:
            if ev.get("backspace"):
                if self._open and self._open["buf"]:
                    self._open["buf"].pop()           # 04 §4: best-effort edit
                    self._open["last_key_at"] = ts
                return

            ch = ev.get("char")
            if ch is None:
                return
            focus_uid = self.reg.current_focus_window_uid
            if self._denied_title():
                log.debug("kbd_char dropped: focused window title is denied")
                return
            if focus_uid is None:
                # No attributable window (e.g. focus landed on a denied window).
                # Flush any previous segment and drop this keystroke.
                await self._flush_locked("focus_change")
                return
            if self._open is None or self._open["window_uid"] != focus_uid:
                await self._flush_locked("focus_change")
                vidx, vname = self._vdesktop()
                self._open = {"window_uid": focus_uid, "buf": [], "started_at": ts,
                              "last_key_at": ts, "vidx": vidx, "vname": vname}
            self._open["buf"].append(ch)
            self._open["last_key_at"] = ts

    def _denied_title(self) -> bool:
        title = self._title()
        if not title:
            return False
        return title in self.s.window_title_denylist

    # ───────────────────────── flush triggers (04 §3) ─────────────────────────
    async def on_focus_change(self, _window_uid=None) -> None:
        if self.s.kbd_flush_on_focus_change:
            async with self._lock:
                await self._flush_locked("focus_change")

    async def on_title_change(self, _window_uid=None) -> None:
        if self.s.kbd_flush_on_title_change:
            async with self._lock:
                await self._flush_locked("title_change")

    async def maybe_idle_flush(self, now: float) -> bool:
        """Flush if the open segment has been idle past the threshold (04 §3)."""
        async with self._lock:
            if (self._open and self._open["buf"]
                    and now - self._open["last_key_at"] >= self.s.kbd_idle_flush_s):
                await self._flush_locked("idle")
                return True
        return False

    async def flush(self, reason="shutdown") -> None:
        """Public flush (e.g. on shutdown — finalize the last open segment)."""
        async with self._lock:
            await self._flush_locked(reason)

    # ───────────────────────── flush core ─────────────────────────
    async def _flush_locked(self, reason: str) -> None:
        seg = self._open
        self._open = None                         # close in every path
        if not seg or not seg["buf"]:
            return
        # Strip first, then apply the min-length threshold (04 §3): a segment
        # whose stripped text is not longer than ``kbd_min_segment_chars`` is
        # noise (stray keys, lone whitespace) and never reaches the buffer/db.
        text = "".join(seg["buf"]).strip()
        if len(text) <= self.s.kbd_min_segment_chars:
            log.debug("flush(%s): segment too short (%d chars), dropped",
                      reason, len(text))
            return
        log.debug("flush(%s): window_uid=%s chars=%d text=%r",
                  reason, seg["window_uid"], len(text), text[:80])
        self.store.enqueue(
            "INSERT INTO kbd_segment(window_uid, text, started_at, ended_at,"
            " vdesktop_index, vdesktop_name, flush_reason) VALUES(?,?,?,?,?,?,?)",
            (seg["window_uid"], text, seg["started_at"], seg["last_key_at"],
             seg["vidx"], seg["vname"], reason))
        if seg["window_uid"] is not None:
            await self.reg.bump_last_seen(seg["window_uid"], seg["last_key_at"])

    # ───────────────────────── idle task ─────────────────────────
    async def idle_loop(self, stop, clock) -> None:
        """Tick every ``kbd_idle_check_s``; idle-flush when the deadline passes."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.kbd_idle_check_s)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            await self.maybe_idle_flush(clock())
