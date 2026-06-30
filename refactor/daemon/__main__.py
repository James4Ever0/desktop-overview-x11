"""daemon/__main__.py — the daemon entrypoint (plan 01 §5, 09 §6).

Wires every piece built in steps 1-6 into one asyncio process:

  identity → store → runtime core (writer + dispatch) → window registry →
  event handlers (focus/title/vdesktop + clipboard/selection/paste IO) →
  keyboard aggregator (+ idle loop) → Thread A (Xlib pump) + Thread B (XRecord)
  → window_capture scheduler → in-process API server (UDS + optional TCP).

The loop thread never blocks (01 §1): the two X sources run in OS threads and
marshal events back through ``Runtime.emit``; all blocking IO (xclip, capture,
xdotool) goes through the executor.  Shutdown is graceful — SIGINT/SIGTERM set a
single stop event, the aggregator's last open segment is flushed, and the writer
drains before the DB closes (01 §5).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time

from .aggregator import KeyboardAggregator
from .api.app import DaemonContext
from .api.server import ApiServer
from .collectors import xpump, xrecord
from .config import Settings
from .db.store import Store
from .handlers import EventHandlers
from .identity import resolve_identity
from .log import setup_logging
from .runtime import Runtime
from .window_captures import WindowCaptureScheduler
from .windows import WindowRegistry

log = logging.getLogger("dovw.main")


def parse_args(argv=None) -> Settings:
    p = argparse.ArgumentParser(prog="desktop-overview-daemon",
                                description="background desktop activity collector + API")
    p.add_argument("--tcp", nargs="?", const=8765, type=int, metavar="PORT",
                   help="also serve on 127.0.0.1:PORT (default 8765) for debugging")
    p.add_argument("--data-dir", help="override data dir (else $DESKTOP_OVERVIEW_DATA_DIR)")
    p.add_argument("--log-level", help="debug|info|warning|error")
    p.add_argument("--no-keyboard", action="store_true", help="start with keyboard capture off")
    args = p.parse_args(argv)

    s = Settings()
    over = {}
    if args.data_dir:
        from pathlib import Path
        over["data_dir"] = Path(args.data_dir).expanduser()
    if args.tcp is not None:
        over["tcp_enabled"] = True
        over["tcp_port"] = args.tcp
    if args.log_level:
        over["log_level"] = args.log_level
    if args.no_keyboard:
        over["kbd_enabled"] = False
    return s.with_overrides(**over) if over else s


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level, settings.data_dir / "logs")
    ident = resolve_identity()
    log.info("identity: session_key=%s user=%s daemon_boot_id=%s",
             ident.session_key, ident.user_name, ident.daemon_boot_id)

    # ── storage + identity row ──────────────────────────────────────────────
    store = Store(settings)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, machine_boot_id, session_key, "
        "user_name, uid, started_at) VALUES(?,?,?,?,?,?)",
        (ident.daemon_boot_id, ident.boot_id, ident.session_key,
         ident.user_name, ident.uid, time.time()))
    log.info("daemon_run id=%s db=%s", run_id, settings.db_path)

    # ── core ────────────────────────────────────────────────────────────────
    runtime = Runtime(store, settings)
    await runtime.start_core()
    registry = WindowRegistry(store, ident.session_key, run_id)

    handlers = EventHandlers(store, registry, settings, runtime=runtime)
    handlers.register_all(runtime)
    handlers.register_io(runtime)

    aggregator = KeyboardAggregator(
        store, registry, settings,
        vdesktop_provider=lambda: (handlers.vdesktop_index, handlers.vdesktop_name))
    aggregator.register(runtime)
    # keyboard segment cuts on focus/title change (04 §3)
    handlers.add_focus_hook(aggregator.on_focus_change)
    handlers.add_title_hook(aggregator.on_title_change)

    window_captures = WindowCaptureScheduler(runtime, store, registry, settings, ident.daemon_boot_id)
    # immediate capture on focus/title change (config: capture_on_focus / capture_on_title)
    handlers.add_focus_hook(window_captures.on_focus_change)
    handlers.add_title_hook(window_captures.on_title_change)

    ctx = DaemonContext(store=store, registry=registry, settings=settings,
                        runtime=runtime, identity=ident, window_captures=window_captures,
                        handlers=handlers)
    api = ApiServer(ctx)

    # ── stop plumbing: one event, threads + tasks honor it (01 §5) ───────────
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    thread_stop = threading.Event()

    def _request_stop(*_):
        log.info("shutdown requested")
        thread_stop.set()
        loop.call_soon_threadsafe(stop.set)

    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, _request_stop)

    # ── Thread A (Xlib pump) + Thread B (XRecord) ────────────────────────────
    tA = threading.Thread(target=xpump.run, args=(thread_stop, runtime.emit),
                          name="xpump", daemon=True)
    tB = threading.Thread(target=xrecord.run, args=(thread_stop, runtime.emit, settings),
                          name="xrecord", daemon=True)
    tA.start()
    tB.start()
    log.info("collector threads started (xpump, xrecord)")

    # ── loop tasks: window_captures, idle flush, API ──────────────────────────────
    async def _window_capture_task():
        ctx.stats["last_full_sweep"] = time.time()
        await window_captures.run(stop, time.time)

    runtime.add_task(_window_capture_task(), name="window_captures")
    runtime.add_task(aggregator.idle_loop(stop, time.time), name="kbd_idle")
    api_task = asyncio.create_task(api.serve(stop), name="api")

    log.info("daemon up")
    await stop.wait()

    # ── graceful shutdown (01 §5) ────────────────────────────────────────────
    log.info("stopping…")
    await aggregator.flush("shutdown")          # finalize the last open segment
    await api_task                              # uvicorn drains + unlinks socket
    await runtime.stop()                        # cancel tasks, drain writer
    await store.execute("UPDATE daemon_run SET stopped_at=? WHERE id=?",
                        (time.time(), run_id))
    await store.close()
    for t in (tA, tB):
        t.join(timeout=2.0)
    log.info("daemon stopped cleanly")


def main(argv=None) -> int:
    settings = parse_args(argv)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
