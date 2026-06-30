"""daemon/windows.py — window identity & liveness registry (plan daemon/02).

A logical window instance is ``(session_key, x_window_id, open-interval)``.  We
mint a surrogate ``window_uid`` the first time an x_window_id is seen with no
currently-open row *in this session*, and every event table foreign-keys to that
``window_uid`` — never the raw X id (which the server reuses).

Liveness & jump-to-window key on ``session_key`` (the X session), so a window
keeps its ``window_uid`` across a daemon restart within the same login (02 §2-3).

This registry only touches the DB through :class:`daemon.db.store.Store`; it
holds no X connection.  The focus collector feeds it ``current_focus_window_uid``
for event attribution (02 §5); collectors call :meth:`bump_last_seen` on activity.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("dovw.windows")


class WindowRegistry:
    def __init__(self, store, session_key: str, daemon_run_id: int | None):
        self.store = store
        self.session_key = session_key
        self.daemon_run_id = daemon_run_id
        # fast path: x_window_id -> window_uid for windows currently alive in this session
        self._alive: dict[int, int] = {}
        # attribution pointer, kept hot by the focus collector (02 §5)
        self.current_focus_window_uid: int | None = None

    # ───────────────────────── mint / reuse ─────────────────────────
    async def ensure_window(self, x_window_id: int, wm_class: str | None = None,
                            now: float | None = None) -> int:
        """Return the window_uid for an x_window_id, minting one if needed.

        Reuses the open row in this session (survives daemon restart); a reused
        X id whose previous row was already closed yields a *new* window_uid.
        """
        now = time.time() if now is None else now
        uid = self._alive.get(x_window_id)
        if uid is not None:
            return uid

        row = await self.store.fetchone(
            "SELECT window_uid FROM window "
            "WHERE session_key=? AND x_window_id=? AND alive=1 "
            "ORDER BY window_uid DESC LIMIT 1",
            (self.session_key, x_window_id),
        )
        if row is not None:
            uid = row[0]
            self._alive[x_window_id] = uid
            await self.store.execute(
                "UPDATE window SET last_seen=?, last_daemon_run_id=? WHERE window_uid=?",
                (now, self.daemon_run_id, uid),
            )
            return uid

        uid = await self.store.execute(
            "INSERT INTO window(session_key, first_daemon_run_id, last_daemon_run_id, "
            "x_window_id, wm_class, first_seen, last_seen, alive) VALUES(?,?,?,?,?,?,?,1)",
            (self.session_key, self.daemon_run_id, self.daemon_run_id,
             x_window_id, wm_class, now, now),
        )
        self._alive[x_window_id] = uid
        log.debug("minted window_uid=%d x=0x%08x wm_class=%s", uid, x_window_id, wm_class)
        return uid

    # ───────────────────────── liveness ─────────────────────────
    async def mark_dead(self, x_window_id: int, now: float | None = None) -> None:
        """Window left _NET_CLIENT_LIST → close its open row (02 §3)."""
        now = time.time() if now is None else now
        uid = self._alive.pop(x_window_id, None)
        await self.store.execute(
            "UPDATE window SET alive=0, closed_at=? "
            "WHERE session_key=? AND x_window_id=? AND alive=1",
            (now, self.session_key, x_window_id),
        )
        if uid is not None:
            log.debug("mark_dead window_uid=%d x=0x%08x", uid, x_window_id)
        if uid is not None and self.current_focus_window_uid == uid:
            self.current_focus_window_uid = None

    async def reconcile(self, current_ids, wm_class_of=None,
                        now: float | None = None) -> None:
        """Sync registry to the live _NET_CLIENT_LIST (startup + each tick, 02 §3).

        - ids present now but with no open row → mint (and record WM_CLASS).
        - rows alive=1 in this session but no longer present → mark dead.
        ``wm_class_of`` (optional) maps x_window_id → WM_CLASS string.
        """
        now = time.time() if now is None else now
        current = set(current_ids)

        # rows we believe are alive in this session (DB is source of truth at startup)
        rows = await self.store.fetchall(
            "SELECT x_window_id, window_uid FROM window "
            "WHERE session_key=? AND alive=1", (self.session_key,))
        db_alive = {r[0]: r[1] for r in rows}
        self._alive = {xid: uid for xid, uid in db_alive.items() if xid in current}

        # closed while we were down (or since last tick)
        for xid in set(db_alive) - current:
            await self.mark_dead(xid, now)

        # appeared (or never recorded)
        for xid in current:
            wm = wm_class_of(xid) if wm_class_of else None
            await self.ensure_window(xid, wm, now)
        log.debug("reconcile done: %d alive, %d current", len(self._alive), len(current))

    # ───────────────────────── activity ─────────────────────────
    async def bump_last_seen(self, window_uid: int | None, now: float | None = None) -> None:
        """Update 'last access time' on any associated activity (02 §4)."""
        if window_uid is None:
            return
        now = time.time() if now is None else now
        await self.store.execute(
            "UPDATE window SET last_seen=? WHERE window_uid=?", (now, window_uid))

    def set_focus(self, window_uid: int | None) -> None:
        """Focus collector sets the attribution pointer (02 §5)."""
        self.current_focus_window_uid = window_uid
