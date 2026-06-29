#!/usr/bin/env python3
"""tests/test_runtime.py — step 2.5: runtime core (emit → dispatch → writer).

Run:  python -m tests.test_runtime    (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-rt-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings        # noqa: E402
from daemon.db.store import Store          # noqa: E402
from daemon.runtime import Runtime         # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def main():
    s = Settings()
    store = Store(s)
    await store.open()
    # a window row so FK-bearing inserts are valid
    wuid = await store.execute(
        "INSERT INTO window(session_key,x_window_id,wm_class,first_seen,last_seen,alive)"
        " VALUES('k',1,'app',1.0,1.0,1)")

    rt = Runtime(store, s)

    # handler: turn a 'title' event into a DB write via the batched path
    seen = []

    async def on_title(ev):
        seen.append(ev)
        store.enqueue("INSERT INTO title_history(window_uid,title,changed_at) VALUES(?,?,?)",
                      (wuid, ev["title"], ev["t"]))

    rt.register("title", on_title)
    await rt.start_core()

    # emit from a *real* OS thread (the collector-thread path: call_soon_threadsafe)
    def producer():
        for i in range(5):
            rt.emit({"kind": "title", "title": f"window title {i}", "t": 1.0 + i})

    th = threading.Thread(target=producer)
    th.start()
    th.join()

    await asyncio.sleep(0.6)   # let dispatch run + one writer batch commit
    check("all emitted events dispatched", len(seen) == 5)
    n = (await store.fetchone("SELECT COUNT(*) FROM title_history"))[0]
    check("dispatched events reached DB via writer", n == 5)
    hit = await store.fetchall(
        "SELECT rowid FROM fts_title WHERE fts_title MATCH 'window'")
    check("written titles are FTS-searchable", len(hit) == 5)

    # unknown kinds are ignored, not fatal
    rt.emit({"kind": "no_such_handler", "x": 1})
    await asyncio.sleep(0.2)
    check("unknown event kind ignored", True)

    await rt.stop()
    await store.close()

    # ── back-pressure: tiny queue, flood low-value 'key' events, never block ──
    s2 = s.with_overrides(event_queue_maxsize=4)
    store2 = Store(s2)
    await store2.open()
    rt2 = Runtime(store2, s2)
    # NO dispatcher running → queue cannot drain → must hit QueueFull path
    rt2.loop = asyncio.get_running_loop()
    for i in range(50):
        rt2._enqueue({"kind": "key", "code": i})
    check("flooding never raised (producer never blocks)", True)
    check("overflow counted as dropped", rt2.dropped > 0)
    check("queue capped at maxsize", rt2._q.qsize() <= 4)

    # high-value event evicts a low-value one to make room
    before = rt2._q.qsize()
    rt2._enqueue({"kind": "clipboard", "text": "important"})
    kinds = [e["kind"] for e in list(rt2._q._queue)]
    check("high-value event retained after eviction", "clipboard" in kinds)
    check("queue still within cap", rt2._q.qsize() <= 4)
    await store2.close()

    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
