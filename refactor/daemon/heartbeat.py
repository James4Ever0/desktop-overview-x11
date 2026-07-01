"""daemon/heartbeat.py — periodic focus-window snapshot + usage-rate queries.

Every ``heartbeat_interval_s`` the daemon writes one ``window_heartbeat`` row for
the currently **focused** window only.  Aggregating these rows gives per-window
"active minutes" over arbitrary look-back windows (5m, 10m, 30m, …) — this is
*focused* usage, not mere presence, so a background window that is alive but
never focused will report zero usage.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("dovw.heartbeat")

# Usage-rate look-back windows exposed by the API and frontend.
USAGE_INTERVALS_S = {
    "usage_5m": 300,
    "usage_10m": 600,
    "usage_30m": 1800,
    "usage_1d": 86400,
}


class HeartbeatRecorder:
    def __init__(self, store, registry, settings, daemon_boot_id: str):
        self.store = store
        self.reg = registry
        self.s = settings
        self.boot = daemon_boot_id

    async def record(self, now: float) -> int:
        """Write one heartbeat row for the currently focused window.  Returns rows written."""
        uid = self.reg.current_focus_window_uid
        if uid is None:
            log.debug("heartbeat skipped: no focused window")
            return 0
        xid = self.reg.xid_for_uid(uid)
        if xid is None:
            # focused window not in alive cache (shouldn't happen), fall back to DB
            row = await self.store.fetchone(
                "SELECT x_window_id FROM window WHERE window_uid=?", (uid,))
            xid = row[0] if row else 0
        self.store.enqueue(
            "INSERT INTO window_heartbeat(daemon_boot_id, window_uid, x_window_id, ts)"
            " VALUES(?,?,?,?)",
            (self.boot, uid, xid, now))
        log.debug("heartbeat recorded focused window_uid=%d x=0x%08x", uid, xid)
        return 1

    async def run(self, stop: asyncio.Event, clock) -> None:
        """Long-lived task: write a snapshot every ``heartbeat_interval_s``."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.heartbeat_interval_s)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                await self.record(clock())
            except Exception:                         # a bad beat never kills the task
                log.exception("heartbeat record failed")


async def usage_rates(store, window_uids: list[int], now: float | None = None) -> dict[int, dict]:
    """Return {window_uid: {"usage_5m": minutes, ...}} from heartbeat rows.

    Active minutes = beat_count * heartbeat_interval_s / 60.0, capped at the
    length of the look-back window.  Because heartbeats are only written for the
    focused window, this measures focused attention, not background liveness.
    """
    if not window_uids:
        return {}
    now = time.time() if now is None else now
    interval = getattr(store.s, "heartbeat_interval_s", 10.0)
    max_age = max(USAGE_INTERVALS_S.values())
    placeholders = ",".join("?" * len(window_uids))
    # One aggregate query covering all requested windows and the largest window.
    cols = ", ".join(
        f"SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS {label}"
        for label in USAGE_INTERVALS_S
    )
    params = [now - v for v in USAGE_INTERVALS_S.values()]
    params.extend(window_uids)
    params.append(now - max_age)
    sql = (f"SELECT window_uid, {cols} FROM window_heartbeat "
           f"WHERE window_uid IN ({placeholders}) AND ts >= ? "
           "GROUP BY window_uid")
    rows = await store.fetchall(sql, tuple(params))
    out: dict[int, dict] = {uid: {label: 0.0 for label in USAGE_INTERVALS_S} for uid in window_uids}
    for row in rows:
        uid = row["window_uid"]
        for label, seconds in USAGE_INTERVALS_S.items():
            count = row[label] or 0
            minutes = min(count * interval / 60.0, seconds / 60.0)
            out[uid][label] = round(minutes, 1)

    # total focused minutes across all recorded heartbeats (no cap)
    total_sql = (f"SELECT window_uid, COUNT(*) AS n FROM window_heartbeat "
                 f"WHERE window_uid IN ({placeholders}) GROUP BY window_uid")
    total_rows = await store.fetchall(total_sql, tuple(window_uids))
    for row in total_rows:
        out[row["window_uid"]]["usage_total"] = round(row["n"] * interval / 60.0, 1)
    for uid in window_uids:
        out[uid].setdefault("usage_total", 0.0)
    return out
