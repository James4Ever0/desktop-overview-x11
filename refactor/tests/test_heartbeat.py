#!/usr/bin/env python3
"""tests/test_heartbeat.py — focused-window heartbeat snapshots + usage-rate queries.

Run:  python -m tests.test_heartbeat   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-heartbeat-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings                  # noqa: E402
from daemon.db.store import Store                    # noqa: E402
from daemon.windows import WindowRegistry            # noqa: E402
from daemon.heartbeat import HeartbeatRecorder, usage_rates, USAGE_INTERVALS_S  # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def main():
    s = Settings().with_overrides(heartbeat_interval_s=10.0)
    store = Store(s)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at)"
        " VALUES('bootH','sess',1.0)")
    reg = WindowRegistry(store, "sess", run_id)

    uidA = await reg.ensure_window(0xA1, "appA", 10.0)
    uidB = await reg.ensure_window(0xB2, "appB", 10.0)

    rec = HeartbeatRecorder(store, reg, s, "bootH")

    # No focus yet → no heartbeat rows.
    await rec.record(100.0)
    await store.flush()
    total = await store.fetchone("SELECT COUNT(*) FROM window_heartbeat")
    check("heartbeat skipped when nothing focused", total[0] == 0)

    # Focus window A and record two beats (t=100, t=110).
    reg.set_focus(uidA)
    await rec.record(100.0)
    await rec.record(110.0)
    await store.flush()
    total = await store.fetchone("SELECT COUNT(*) FROM window_heartbeat")
    check("heartbeat wrote 2 rows for focused window A", total[0] == 2)

    rows = await store.fetchall(
        "SELECT daemon_boot_id, window_uid, x_window_id FROM window_heartbeat")
    check("heartbeat stores boot id", all(r["daemon_boot_id"] == "bootH" for r in rows))
    check("heartbeat stores focused x_window_id", {r["x_window_id"] for r in rows} == {0xA1})
    check("heartbeat stores focused window_uid", {r["window_uid"] for r in rows} == {uidA})

    # usage at t=200: 2 beats * 10s / 60 = 0.3 active minutes (capped at 5/10/30).
    rates = await usage_rates(store, [uidA, uidB], now=200.0)
    for label in USAGE_INTERVALS_S:
        check(f"{label} = 0.3 focused minutes for A", rates[uidA][label] == 0.3)
        check(f"{label} = 0.0 for unfocused B", rates[uidB][label] == 0.0)

    # Switch focus to B and add more beats.
    reg.set_focus(uidB)
    for t in (200.0, 210.0, 220.0, 230.0):
        await rec.record(t)
    await store.flush()
    rates2 = await usage_rates(store, [uidA, uidB], now=300.0)
    # For B in last 300s: 4 beats at 10s = 40s -> 0.7 min.
    check("B usage_5m after focused beats", rates2[uidB]["usage_5m"] == 0.7)
    # A only has old beats at t=100/110, outside 5m/10m/30m windows from now=300? 200s ago, inside 300s.
    # For 5m window: A has 0 beats in [0,300]? Actually now=300, 5m=300s → t>=0; A beats at 100,110 count.
    # Wait 300-300=0, so all ts>=0 count. A count=2 -> 0.3 min.
    check("A still has old focused minutes", rates2[uidA]["usage_5m"] == 0.3)

    # unknown window returns zeroes
    zeros = await usage_rates(store, [99999], now=300.0)
    check("unknown window has zero usage", zeros[99999]["usage_5m"] == 0.0)

    # cap at interval length: add enough beats to fill the 5m window.
    reg.set_focus(uidB)
    base = 1000.0
    for i in range(30):
        await rec.record(base + i * 10.0)
    await store.flush()
    rates3 = await usage_rates(store, [uidB], now=base + 300.0)
    # 5m window = 300s → 30 beats at 10s = 300s = 5.0 min.
    check("usage capped by interval", rates3[uidB]["usage_5m"] == 5.0)

    await store.close()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
