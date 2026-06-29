#!/usr/bin/env python3
"""tests/test_frontend.py — step 7: the thin Tk client, headless.

Validates the frontend's two real responsibilities (everything else is pure Tk
painting, which needs a display):

  1. ``frontend/__main__.parse_args`` — socket/--tcp/--columns wiring.
  2. ``frontend/apiclient.ApiClient`` over a **real UNIX socket** served by the
     daemon's own ``ApiServer`` — the exact transport the GUI uses (08 §6).  We
     seed a DB, bring the API up on a temp UDS, then drive the *sync* client from
     a worker thread (as ``app.py`` does) and assert the typed dataclasses.
  3. ``DaemonUnavailable`` when the socket is missing (08 §7 degraded mode).

If a DISPLAY is reachable we also build the Tk app once to catch import/wiring
regressions; otherwise that check is skipped (not failed).

Run:  python -m tests.test_frontend   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-fe-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP
os.environ["DESKTOP_OVERVIEW_UDS"] = os.path.join(_TMP, "daemon.sock")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image                                       # noqa: E402

from daemon.config import Settings                          # noqa: E402
from daemon.db.store import Store                            # noqa: E402
from daemon.windows import WindowRegistry                    # noqa: E402
from daemon import capture as capture_mod                    # noqa: E402
from daemon.api.app import DaemonContext                     # noqa: E402
from daemon.api.server import ApiServer                      # noqa: E402

from frontend.config import FrontendSettings                 # noqa: E402
from frontend.apiclient import ApiClient, DaemonUnavailable, Window  # noqa: E402
from frontend.__main__ import parse_args                     # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def _seed(store):
    rid = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at) VALUES('b','sess',1.0)")

    async def win(xid, sess, alive, last_seen, wm):
        return await store.execute(
            "INSERT INTO window(session_key, first_daemon_run_id, last_daemon_run_id,"
            " x_window_id, wm_class, first_seen, last_seen, alive) VALUES(?,?,?,?,?,?,?,?)",
            (sess, rid, rid, xid, wm, 1.0, last_seen, alive))

    a = await win(0xABCDE, "sess", 1, 300.0, "firefox")
    b = await win(0xBBBBB, "sess", 0, 200.0, "code")
    for uid, title, t in [(a, "Inbox — invoice 2026", 110.0), (b, "draft document", 120.0)]:
        await store.execute(
            "INSERT INTO title_history(window_uid, title, changed_at) VALUES(?,?,?)", (uid, title, t))
        await store.execute(
            "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
            " VALUES(?,?,?,?)", (uid, 1, "Web", t))
    await store.execute(
        "INSERT INTO clipboard_event(window_uid, kind, text, n_chars, n_bytes, created_at)"
        " VALUES(?,?,?,?,?,?)", (a, "TEXT", "copied an invoice number", 24, 24, 140.0))
    rel = os.path.join("window_captures", "b", str(a), "300000.png")
    abs_path = Path(_TMP) / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(abs_path, "PNG")
    await store.execute(
        "INSERT INTO window_capture(window_uid, rel_path, width, height, captured_at, is_focused)"
        " VALUES(?,?,?,?,?,?)", (a, rel, 8, 8, 300.0, 1))
    return a, b


def test_parse_args():
    print("[parse_args] CLI wiring")
    s = parse_args([])
    check("default uses UDS (not tcp)", s.use_tcp is False)
    s2 = parse_args(["--tcp"])
    check("--tcp flips use_tcp + default endpoint",
          s2.use_tcp is True and s2.tcp_endpoint == "127.0.0.1:8765")
    s3 = parse_args(["--tcp", "10.0.0.5:9000", "--columns", "5", "--socket", "/x/y.sock"])
    check("--tcp HOST:PORT honored", s3.tcp_endpoint == "10.0.0.5:9000")
    check("--columns honored", s3.grid_columns == 5)
    check("--socket honored", str(s3.socket_path) == "/x/y.sock")
    check("log level stashed for setup_logging", getattr(s3, "_log_level", None) == "info")


def _client_checks(uds: str, a: int, b: int):
    """Runs in a worker thread while the asyncio API server handles requests."""
    fe = FrontendSettings().with_overrides(socket_path=Path(uds))
    cli = ApiClient(fe)
    try:
        wins = cli.windows()
        check("client.windows returns typed Window list",
              wins and all(isinstance(w, Window) for w in wins))
        check("client.windows finds both windows", {w.window_uid for w in wins} == {a, b})
        wa = next(w for w in wins if w.window_uid == a)
        check("alive+session window is jumpable", wa.jumpable is True)
        check("desktop_badge formats vdesktop", wa.desktop_badge == "[1: Web]")
        check("window_capture_url present on window a", wa.window_capture_url is not None)

        found = cli.search(q="invoice")
        check("client.search(q) finds window a", a in {w.window_uid for w in found})
        fa = next(w for w in found if w.window_uid == a)
        check("hit excerpt carries <mark>",
              any("<mark>" in (h.excerpt or "") for h in fa.hits))

        png = cli.get_bytes(wa.window_capture_url)
        check("client.get_bytes pulls PNG window_capture",
              png is not None and png[:8] == b"\x89PNG\r\n\x1a\n")

        lanes = cli.timeline()
        check("client.timeline returns lanes", {l.window_uid for l in lanes} == {a, b})

        # activate (monkeypatch desktop side effects on the daemon side)
        capture_mod.get_window_list = lambda: [("0x000abcde", "Inbox")]
        capture_mod.activate_window = lambda wid: True
        check("client.activate alive window -> ok", cli.activate(a).get("ok") is True)
        check("client.activate dead window -> reason dead", cli.activate(b).get("reason") == "dead")

        check("client.refresh_window_captures round-trips", "captured" in cli.refresh_window_captures())
        check("client.health ok", cli.health().get("ok") is True)
    finally:
        cli.close()


def _bad_socket_check():
    fe = FrontendSettings().with_overrides(socket_path=Path(_TMP) / "nope.sock")
    cli = ApiClient(fe)
    raised = False
    try:
        cli.windows()
    except DaemonUnavailable:
        raised = True
    finally:
        cli.close()
    check("missing socket raises DaemonUnavailable (degraded mode)", raised)


def _maybe_build_gui(fe_settings):
    """Construct the Tk app once if a display is reachable; skip cleanly if not."""
    try:
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            print(f"  SKIP  Tk app construction (no display: {exc})")
            return
        root.destroy()
    except Exception as exc:                                # noqa: BLE001
        print(f"  SKIP  Tk app construction ({exc})")
        return
    from frontend.app import WindowPreviewApp
    cli = ApiClient(fe_settings)
    try:
        app = WindowPreviewApp(fe_settings, cli)
        app.update_idletasks()
        check("Tk WindowPreviewApp builds without error", True)
        app.on_close()
    except Exception as exc:                                # noqa: BLE001
        check(f"Tk WindowPreviewApp builds without error ({exc})", False)
        cli.close()


async def main():
    test_parse_args()

    s = Settings()
    uds = str(s.uds_path)
    store = Store(s)
    await store.open()
    a, b = await _seed(store)

    reg = WindowRegistry(store, "sess", 1)
    ctx = DaemonContext(store=store, registry=reg, settings=s, runtime=None,
                        identity=None, handlers=None)

    class _H:
        desktop_names = ["Web", "Code"]
        vdesktop_index = 0
        vdesktop_name = "Web"
    ctx.handlers = _H()

    # a minimal window_captures stand-in so /window_captures/refresh round-trips
    class _WindowCaptures:
        async def full_sweep(self, now):
            return 0
    ctx.window_captures = _WindowCaptures()

    server = ApiServer(ctx)
    stop = asyncio.Event()
    print("[api] serving over real UDS:", uds)
    server_task = asyncio.create_task(server.serve(stop))
    for _ in range(50):
        if os.path.exists(uds):
            break
        await asyncio.sleep(0.1)
    check("daemon UDS socket created", os.path.exists(uds))

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _client_checks, uds, a, b)
    await loop.run_in_executor(None, _bad_socket_check)

    stop.set()
    await server_task

    _maybe_build_gui(FrontendSettings().with_overrides(socket_path=Path(uds)))

    await store.close()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
