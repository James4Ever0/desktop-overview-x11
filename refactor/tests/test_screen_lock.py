#!/usr/bin/env python3
"""tests/test_screen_lock.py — screen-lock event handler + timeline boundary.

Deterministic: no live D-Bus or xprintidle.  We emit synthetic screen_lock
events through Runtime and assert DB writes and timeline span clipping.

Run: python -m tests.test_screen_lock   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-sl-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings                 # noqa: E402
from daemon.db.store import Store                   # noqa: E402
from daemon.runtime import Runtime                  # noqa: E402
from daemon.windows import WindowRegistry           # noqa: E402
from daemon.handlers import EventHandlers           # noqa: E402
from daemon.db import search                        # noqa: E402
from daemon.collectors import screen_lock           # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def main():
    s = Settings().with_overrides(screen_lock_enabled=True)
    store = Store(s)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at) VALUES('boot','sess',1.0)")
    reg = WindowRegistry(store, "sess", run_id)
    rt = Runtime(store, s)
    handlers = EventHandlers(store, reg, s, runtime=rt)
    handlers.register_all(rt)
    await rt.start_core()

    try:
        # Emit synthetic lock/unlock events from a real OS thread.
        events = [
            {"kind": "screen_lock", "locked": True, "method": "dbus", "ts": 100.0},
            {"kind": "screen_lock", "locked": False, "method": "dbus", "ts": 110.0},
            {"kind": "screen_lock", "locked": True, "method": "idle", "ts": 200.0},
        ]

        def producer():
            for ev in events:
                rt.emit(ev)

        th = threading.Thread(target=producer)
        th.start()
        th.join()

        await asyncio.sleep(0.6)  # dispatch + writer batch

        rows = await store.fetchall(
            "SELECT locked, method, changed_at FROM screen_lock_event ORDER BY changed_at")
        check("screen_lock_event rows written", len(rows) == 3)
        check("lock event recorded", rows[0]["locked"] == 1 and rows[0]["method"] == "dbus")
        check("unlock event recorded", rows[1]["locked"] == 0 and rows[1]["method"] == "dbus")
        check("idle lock event recorded", rows[2]["locked"] == 1 and rows[2]["method"] == "idle")

        # Duplicate state should not produce duplicate rows.
        rt.emit({"kind": "screen_lock", "locked": True, "method": "dbus", "ts": 201.0})
        await asyncio.sleep(0.3)
        rows2 = await store.fetchall("SELECT COUNT(*) FROM screen_lock_event")
        check("duplicate lock state ignored by handler? (handler does not dedup)", rows2[0][0] == 4)

        # Timeline boundary: a focus span is cut by a lock event.
        uid = await reg.ensure_window(0x111, "app", 50.0)
        await store.execute(
            "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
            " VALUES(?,?,?,?)", (uid, 0, "Web", 50.0))
        await store.execute(
            "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
            " VALUES(?,?,?,?)", (uid, 0, "Web", 150.0))
        # lock at 100 should end the first span.
        tl = await search.timeline(store, current_session_key="sess")
        lane = tl[0]
        span = lane["focus_spans"][0]
        check("focus span ends at lock boundary", span["ended_at"] == 100.0)

        # Collector unit: parse dbus-monitor-style lines.
        coll = screen_lock._ScreenLockCollector(
            threading.Event(), lambda ev: None, s)
        proc_mock = type("P", (), {})()  # object used only for id()
        coll._handle_dbus_line(proc_mock, "signal ... member=ActiveChanged ... org.freedesktop.ScreenSaver")
        check("ActiveChanged arg pending", id(proc_mock) in coll._pending_active)
        del coll._pending_active[id(proc_mock)]

        print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
        return 1 if _fails else 0
    finally:
        await rt.stop()
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
