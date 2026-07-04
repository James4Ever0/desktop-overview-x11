#!/usr/bin/env python3
"""tests/test_windows.py — WindowRegistry liveness + reboot guard.

Deterministic: no live X.  We seed the DB directly and drive reconcile.

Run: python -m tests.test_windows   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-win-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings                       # noqa: E402
from daemon.db.store import Store                         # noqa: E402
from daemon.windows import WindowRegistry                 # noqa: E402

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
    rid = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at) VALUES('boot','sess',1.0)")

    # Seed a window that is alive in the DB from a "previous" daemon run.
    uid = await store.execute(
        "INSERT INTO window(session_key, first_daemon_run_id, last_daemon_run_id,"
        " x_window_id, wm_class, app_name, first_seen, last_seen, closed_at, alive)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("sess", rid, rid, 0x111, "app", "app", 10.0, 100.0, None, 1))

    reg = WindowRegistry(store, "sess", rid)

    # First reconcile: window is missing from the current X client list.
    # It must NOT be marked dead, because this may simply be a startup gap.
    await reg.reconcile([], now=1000.0)
    row = await store.fetchone("SELECT alive, closed_at FROM window WHERE window_uid=?", (uid,))
    check("first reconcile keeps missing pre-existing window alive",
          row["alive"] == 1 and row["closed_at"] is None)

    # Second reconcile: still missing → now we record the death timestamp.
    await reg.reconcile([], now=1010.0)
    row = await store.fetchone("SELECT alive, closed_at FROM window WHERE window_uid=?", (uid,))
    check("second reconcile marks persistently missing window dead",
          row["alive"] == 0 and row["closed_at"] is not None)

    # A window that appears and then disappears during the current run is marked
    # dead once the grace period has elapsed.
    uid2 = await store.execute(
        "INSERT INTO window(session_key, first_daemon_run_id, last_daemon_run_id,"
        " x_window_id, wm_class, app_name, first_seen, last_seen, closed_at, alive)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("sess", rid, rid, 0x222, "app2", "app2", 10.0, 100.0, None, 1))
    await reg.reconcile([0x222], now=2000.0)
    await reg.reconcile([], now=2000.1)
    await reg.reconcile([], now=2010.0)
    row = await store.fetchone("SELECT alive, closed_at FROM window WHERE window_uid=?", (uid2,))
    check("window seen then removed is marked dead after grace",
          row["alive"] == 0 and row["closed_at"] is not None)

    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    await store.close()
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
