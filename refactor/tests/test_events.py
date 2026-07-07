#!/usr/bin/env python3
"""tests/test_events.py — Events tab API and query logic.

Run: python -m tests.test_events
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-events-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP
os.environ["DESKTOP_OVERVIEW_UDS"] = os.path.join(_TMP, "daemon.sock")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings                          # noqa: E402
from daemon.db.store import Store                            # noqa: E402
from daemon.db import events as events_mod                   # noqa: E402
from daemon.windows import WindowRegistry                    # noqa: E402
from daemon.api.app import DaemonContext                     # noqa: E402
from daemon.api.server import ApiServer                      # noqa: E402
from frontend.apiclient import ApiClient, GlobalEvent        # noqa: E402
from frontend.config import FrontendSettings                 # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def _seed(store):
    rid = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at) VALUES('b','sess',1.0)")

    async def win(xid, sess, alive, wm, vidx=0, vname="Main"):
        return await store.execute(
            "INSERT INTO window(session_key, first_daemon_run_id, last_daemon_run_id,"
            " x_window_id, wm_class, vdesktop_index, vdesktop_name, first_seen, last_seen, alive)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sess, rid, rid, xid, wm, vidx, vname, 1.0, 300.0, alive))

    a = await win(0xA0001, "sess", 1, "firefox")
    b = await win(0xA0002, "sess", 1, "code")

    await store.execute(
        "INSERT INTO title_history(window_uid, title, changed_at) VALUES(?,?,?)",
        (a, "Invoices — Firefox", 110.0))
    await store.execute(
        "INSERT INTO title_history(window_uid, title, changed_at) VALUES(?,?,?)",
        (b, "refactor.py - Code", 120.0))
    await store.execute(
        "INSERT INTO app_name_history(window_uid, app_name, changed_at) VALUES(?,?,?)",
        (a, "Mozilla Firefox", 115.0))
    await store.execute(
        "INSERT INTO clipboard_event(window_uid, kind, text, created_at) VALUES(?,?,?,?)",
        (a, "TEXT", "invoice number 2026", 130.0))
    await store.execute(
        "INSERT INTO selection_event(window_uid, text, created_at) VALUES(?,?,?)",
        (b, "def search_events", 140.0))
    await store.execute(
        "INSERT INTO kbd_segment(window_uid, text, started_at, ended_at) VALUES(?,?,?,?)",
        (b, "hello world", 150.0, 151.0))
    await store.execute(
        "INSERT INTO focus_event(window_uid, focused_at, vdesktop_index, vdesktop_name) VALUES(?,?,?,?)",
        (a, 100.0, 0, "Main"))
    await store.execute(
        "INSERT INTO screen_lock_event(locked, changed_at) VALUES(?,?)",
        (1, 160.0))
    return a, b


async def _run_server():
    s = Settings()
    uds = str(s.uds_path)
    store = Store(s)
    await store.open()
    a, b = await _seed(store)
    reg = WindowRegistry(store, "sess", 1)
    ctx = DaemonContext(store=store, registry=reg, settings=s, runtime=None,
                        identity=None, handlers=None)

    class _H:
        desktop_names = ["Main"]
        vdesktop_index = 0
        vdesktop_name = "Main"
    ctx.handlers = _H()

    class _WindowCaptures:
        async def full_sweep(self, now):
            return 0
    ctx.window_captures = _WindowCaptures()

    server = ApiServer(ctx)
    stop = asyncio.Event()
    server_task = asyncio.create_task(server.serve(stop))
    for _ in range(50):
        if os.path.exists(uds):
            break
        await asyncio.sleep(0.1)
    return uds, store, server_task, stop, a, b


def _client_checks(uds: str, a: int, b: int):
    fe = FrontendSettings().with_overrides(socket_path=Path(uds))
    cli = ApiClient(fe)
    try:
        items, total = cli.events()
        check("events returns GlobalEvent list", all(isinstance(e, GlobalEvent) for e in items))
        check("events total >= 7", total >= 7)
        check("events ordered desc by timestamp", all(items[i].ts >= items[i + 1].ts for i in range(len(items) - 1)))
        types = {e.type for e in items}
        check("events include clipboard/selection/keyboard/title/focus/lock",
              {"clipboard", "selection", "keyboard", "title", "focus", "lock"}.issubset(types))

        items2, total2 = cli.events(type="clipboard,selection")
        check("type filter restricts results", all(e.type in {"clipboard", "selection"} for e in items2))
        check("type filter total smaller", total2 < total)

        items3, total3 = cli.events(q="invoice")
        check("search finds invoice clipboard event", any("invoice" in (e.text or "").lower() for e in items3))
        check("search result has excerpt or text", all((e.excerpt or e.text) for e in items3))

        items4, total4 = cli.events(q="search_events")
        check("search finds selection text", any("search_events" in (e.text or "") for e in items4))

        items5, total5 = cli.events(limit=2)
        check("limit respected", len(items5) == 2)
        check("total unchanged with limit", total5 == total)

        items6, _ = cli.events(limit=2, offset=2)
        check("offset shifts page", (items6[0].type, items6[0].id) != (items5[0].type, items5[0].id))
    finally:
        cli.close()


async def _direct_checks(store, a, b):
    rows, total = await events_mod.search_events(store, current_session_key="sess")
    check("direct search_events returns rows", len(rows) > 0 and total > 0)
    rows2, total2 = await events_mod.search_events(store, q="invoice", current_session_key="sess")
    check("direct q=invoice finds rows", any("invoice" in (r.get("text") or "").lower() for r in rows2))


async def main():
    uds, store, server_task, stop, a, b = await _run_server()
    check("daemon UDS socket created", os.path.exists(uds))

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _client_checks, uds, a, b)
    await _direct_checks(store, a, b)

    stop.set()
    await server_task
    await store.close()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
