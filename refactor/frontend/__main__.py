"""frontend/__main__.py — launch the thin Tk client (plan 08 §1, 10 §5).

Parses the few frontend knobs (socket path / --tcp / log level), wires central
logging to **stdout + a rotating file** (``frontend/log.py``), opens the API
client, and runs the Tk mainloop.  No daemon logic here.

Run:  python -m frontend            (UDS auto-discovered from XDG)
      python -m frontend --tcp      (talk to 127.0.0.1:8765)
      python -m frontend --socket /path/to/daemon.sock
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from .config import FrontendSettings
from .log import setup_logging
from .apiclient import ApiClient
from ._font_setup import setup_fonts


def parse_args(argv=None) -> FrontendSettings:
    p = argparse.ArgumentParser(prog="frontend", description="Desktop Overview — search UI")
    p.add_argument("--socket", metavar="PATH", help="daemon UDS path (overrides XDG default)")
    p.add_argument("--tcp", nargs="?", const="127.0.0.1:8765", metavar="HOST:PORT",
                   help="connect over TCP instead of the UNIX socket")
    p.add_argument("--columns", type=int, help="fixed grid column count (default: auto-fit)")
    p.add_argument("--refresh-interval", type=int, metavar="SEC",
                   help="auto-refresh interval in seconds (0 = off, default 2)")
    p.add_argument("--show-back-button", action="store_true",
                   help="show the Back navigation button (hidden by default)")
    p.add_argument("--hide-self", dest="hide_self", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="hide the GUI's own window from results (default: on)")
    p.add_argument("--hide-self-method", choices=["id", "title_prefix"], default="id",
                   help="how to identify the GUI window: id (default) or title_prefix")
    p.add_argument("--resizable", dest="resizable", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="allow the main window to be resized (default: on)")
    p.add_argument("--log-level", default="info",
                   choices=["debug", "info", "warning", "error"])
    args = p.parse_args(argv)

    s = FrontendSettings()
    over = {}
    if args.socket:
        over["socket_path"] = Path(args.socket)
    if args.tcp is not None:
        over["use_tcp"] = True
        over["tcp_endpoint"] = args.tcp
    if args.columns:
        over["grid_columns"] = args.columns
    if args.refresh_interval is not None:
        over["grid_auto_refresh_s"] = args.refresh_interval
    if args.show_back_button:
        over["show_back_button"] = True
    if args.hide_self is not None:
        over["hide_self"] = args.hide_self
    if args.hide_self_method:
        over["hide_self_method"] = args.hide_self_method
    if args.resizable is not None:
        over["resizable"] = args.resizable
    s = s.with_overrides(**over) if over else s
    s._log_level = args.log_level   # type: ignore[attr-defined]
    return s


def main(argv=None) -> int:
    settings = parse_args(argv)
    logfile = setup_logging(getattr(settings, "_log_level", "info"))
    log = logging.getLogger("dovw.fe")
    log.info("starting frontend (uds=%s tcp=%s) logfile=%s",
             settings.socket_path, settings.use_tcp, logfile)

    # Register bundled Noto fonts with fontconfig before Tk loads.
    setup_fonts()

    # import Tk lazily so --help works headless and import errors are logged
    from .app import WindowPreviewApp

    client = ApiClient(settings)
    try:
        app = WindowPreviewApp(settings, client)
    except Exception:
        log.exception("failed to build GUI")
        client.close()
        return 1

    # SIGINT → clean shutdown in the Tk event loop (Tk swallows KeyboardInterrupt
    # inside C-level callbacks, so we route it through the event queue instead).
    signal.signal(signal.SIGINT, lambda sig, frame: app.after(0, app.on_close))

    try:
        app.mainloop()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
