#!/usr/bin/env python3
"""tests/test_collectors.py — step 3: collectors + dispatch handlers.

Part A (deterministic, headless): feed synthetic collector events through
EventHandlers + a real DB + WindowRegistry and assert the resulting rows.
Part B (live X smoke): if DISPLAY is set and python-xlib imports, run the real
XPump briefly and assert it emits a baseline.  Part B is skipped (not failed)
when X is unavailable.

Run:  python -m tests.test_collectors   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-coll-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings        # noqa: E402
from daemon.db.store import Store          # noqa: E402
from daemon.windows import WindowRegistry  # noqa: E402
from daemon.handlers import EventHandlers   # noqa: E402
from daemon.runtime import Runtime          # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def part_a():
    print("[part A] dispatch handlers")
    s = Settings()
    store = Store(s)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at)"
        " VALUES('boot','sess',1.0)")
    reg = WindowRegistry(store, "sess", run_id)
    h = EventHandlers(store, reg, s)

    flushed = []
    h.on_focus_change = lambda uid: flushed.append(("focus", uid)) or _noop()
    h.on_title_change = lambda uid: flushed.append(("title", uid)) or _noop()

    rt = Runtime(store, s)
    h.register_all(rt)
    await rt.start_core()

    def emit(ev):
        rt.emit(ev)

    # desktop baseline first, so focus rows get stamped
    emit({"kind": "vdesktop_meta", "count": 2, "names": ["Web", "Code"], "ts": 1.0})
    emit({"kind": "vdesktop_current", "index": 1, "name": "Code", "ts": 1.1})
    # window appears, gets focus, title changes
    emit({"kind": "window_list", "added": [0x111], "gone": [], "ts": 2.0})
    emit({"kind": "focus", "x_window_id": 0x111, "title": "draft", "ts": 2.1})
    emit({"kind": "title", "x_window_id": 0x111, "old": "draft",
          "new": "draft — invoice", "ts": 2.2})
    emit({"kind": "window_list", "added": [], "gone": [0x111], "ts": 3.0})

    await asyncio.sleep(0.6)
    await rt.stop()

    win = await store.fetchone(
        "SELECT window_uid, alive, last_seen FROM window WHERE x_window_id=?", (0x111,))
    check("window minted from window_list/focus", win is not None)
    uid = win[0]
    check("window marked dead after gone", win[1] == 0)
    check("last_seen bumped to latest activity", win[2] == 2.2)

    fe = await store.fetchone(
        "SELECT vdesktop_index, vdesktop_name FROM focus_event WHERE window_uid=?", (uid,))
    check("focus_event stamped with current desktop", fe[0] == 1 and fe[1] == "Code")

    th = await store.fetchall(
        "SELECT title FROM title_history WHERE window_uid=? ORDER BY changed_at", (uid,))
    check("title history appended", len(th) == 1 and th[0][0] == "draft — invoice")

    ti = await store.fetchall("SELECT rowid FROM fts_title WHERE fts_title MATCH 'invoice'")
    check("title is FTS-searchable", len(ti) == 1)

    vd = await store.fetchall("SELECT idx, name FROM vdesktop_state ORDER BY id")
    check("vdesktop_state recorded", any(r[0] == 1 and r[1] == "Code" for r in vd))

    check("aggregator focus-flush hook fired", ("focus", uid) in flushed)
    check("aggregator title-flush hook fired", ("title", uid) in flushed)

    await store.close()


async def _noop():
    return None


def part_b():
    print("[part B] live X pump smoke")
    if not os.environ.get("DISPLAY"):
        print("  SKIP  no DISPLAY")
        return
    try:
        from daemon.collectors.xpump import XPump
    except Exception as exc:
        print(f"  SKIP  xlib import failed: {exc}")
        return
    try:
        events = []
        pump = XPump(events.append)
    except Exception as exc:
        print(f"  SKIP  cannot open X display: {exc}")
        return

    stop = threading.Event()
    t = threading.Thread(target=pump.run, args=(stop,))
    t.start()
    # prime() runs synchronously at loop start and emits baselines; give it a moment
    deadline_iters = 40
    while not events and deadline_iters > 0:
        threading.Event().wait(0.05)
        deadline_iters -= 1
    stop.set()
    t.join(timeout=3)
    kinds = {e["kind"] for e in events}
    check("pump emitted a baseline (window_list/vdesktop_meta)",
          bool(kinds & {"window_list", "vdesktop_meta", "focus", "vdesktop_current"}))
    check("pump thread stopped cleanly", not t.is_alive())


async def main():
    await part_a()
    part_b()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
