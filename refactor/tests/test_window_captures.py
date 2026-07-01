#!/usr/bin/env python3
"""tests/test_window_captures.py — step 4: capture scheduler, file save & GC.

The blocking X subprocess calls (wmctrl/import/xprop/xdotool) are stubbed with a
synthetic PIL image so the test is deterministic and needs no live windows.

Run:  python -m tests.test_window_captures   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-window_capture-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image                       # noqa: E402
from daemon.config import Settings          # noqa: E402
from daemon.db.store import Store            # noqa: E402
from daemon.windows import WindowRegistry    # noqa: E402
from daemon.runtime import Runtime           # noqa: E402
from daemon import capture                   # noqa: E402
from daemon.window_captures import WindowCaptureScheduler  # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


# ── stub the blocking X ops ──
_WINDOWS = [("0x111", 0, "Firefox — inbox"), ("0x222", 0, "Konsole"), ("0x333", 1, "VS Code")]
capture.get_window_list = lambda: list(_WINDOWS)
capture.get_active_window_id = lambda: capture.normalize_win_id("0x111")
capture.get_app_name = lambda wid: "app"
capture.window_exists = lambda wid: True
capture.capture_window = lambda wid, title="", max_dim=None, timeout_s=5.0: Image.new("RGB", (12, 9), "blue")


async def main():
    s = Settings().with_overrides(refresh_batch_size=1, window_capture_keep_per_window=2)
    store = Store(s)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at)"
        " VALUES('bootABC','sess',1.0)")
    reg = WindowRegistry(store, "sess", run_id)
    rt = Runtime(store, s)
    # Set the loop for run_in_executor WITHOUT starting writer_loop: this test
    # commits deterministically via store.flush() (in production, shutdown stops
    # the writer before flushing, so there is no drain race — runtime.stop()).
    rt.loop = asyncio.get_running_loop()
    sched = WindowCaptureScheduler(rt, store, reg, s, "bootABC")

    # full_sweep filters out windows with no vdesktop unless filter_no_vdesktop=False;
    # seed focus events so the synthetic windows are capturable.
    for xid, _desktop, _title in _WINDOWS:
        uid = await reg.ensure_window(capture.normalize_win_id(xid), "app", 1.0)
        await store.execute(
            "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
            " VALUES(?,?,?,?)", (uid, 0, "Web", 1.0))

    try:
        n = await sched.full_sweep(1000.0)
        await store.flush()
        check("full sweep captured all 3 windows", n == 3)
        rows = await store.fetchall("SELECT window_uid, rel_path, width, height FROM window_capture")
        check("3 window_capture rows written", len(rows) == 3)
        rel0 = rows[0]["rel_path"]
        check("rel_path under window_captures/<boot>/<uid>/", rel0.startswith("window_captures/bootABC/"))
        check("rel_path ends .png", rel0.endswith(".png"))
        check("captured size recorded", rows[0]["width"] == 12 and rows[0]["height"] == 9)
        on_disk = all((s.data_dir / r["rel_path"]).exists() for r in rows)
        check("window_capture files exist on disk", on_disk)

        # ── power-efficient tick: focused (0x111) always + up to Y=1 background ──
        n2 = await sched.tick(1001.0)
        await store.flush()
        check("tick captured focused + <=1 background", n2 == 2)
        foc = await store.fetchall(
            "SELECT t.is_focused FROM window_capture t JOIN window w ON t.window_uid=w.window_uid "
            "WHERE w.x_window_id=? AND t.captured_at=?", (capture.normalize_win_id("0x111"), 1001.0))
        check("focused window flagged is_focused=1", len(foc) == 1 and foc[0][0] == 1)

        # ── GC: capture firefox repeatedly, keep only N=2 latest ──
        ff_xid = capture.normalize_win_id("0x111")
        for t in (1002.0, 1003.0, 1004.0):
            await sched._capture_and_save("0x111", "Firefox", True, t)
        await store.flush()
        ffrows = await store.fetchall(
            "SELECT rel_path, captured_at FROM window_capture WHERE window_uid="
            "(SELECT window_uid FROM window WHERE x_window_id=? AND alive=1) "
            "ORDER BY captured_at DESC", (ff_xid,))
        check("GC keeps only N=2 latest rows", len(ffrows) == 2)
        check("GC kept the two newest", {r["captured_at"] for r in ffrows} == {1004.0, 1003.0})
        kept_exist = all((s.data_dir / r["rel_path"]).exists() for r in ffrows)
        check("kept window_capture files still on disk", kept_exist)

        # latest-window_capture view returns the newest per window
        latest = await store.fetchone(
            "SELECT captured_at FROM window_capture_latest WHERE window_uid="
            "(SELECT window_uid FROM window WHERE x_window_id=? AND alive=1)", (ff_xid,))
        check("window_capture_latest view returns newest", latest[0] == 1004.0)

        print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
        return 1 if _fails else 0
    finally:
        await rt.stop()
        await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
