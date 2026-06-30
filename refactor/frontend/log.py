"""frontend/log.py — central logging for the GUI (stdout + rotating file).

Mirrors ``daemon/log.py`` so the frontend also logs in detail to both the
terminal and a rotating file (``<data_dir>/logs/frontend.log``).  Tk callbacks
swallow exceptions silently otherwise, so logging here is the only window into
what the UI is doing.  Call :func:`setup_logging` once at launch.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)-20s %(filename)s:%(lineno)d %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def _default_log_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "desktop-overview" / "logs"


def setup_logging(level: str = "info", log_dir: Path | None = None) -> Path | None:
    global _configured
    root = logging.getLogger("dovw.fe")
    if _configured:
        return getattr(root, "_logfile", None)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False
    fmt = logging.Formatter(_FMT, _DATEFMT)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_dir = Path(log_dir) if log_dir else _default_log_dir()
    logfile = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logfile = log_dir / "frontend.log"
        fileh = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError:
        logfile = None   # read-only home etc. — stdout still works

    root._logfile = logfile   # type: ignore[attr-defined]
    _configured = True
    root.info("frontend logging initialized (level=%s, file=%s)", level, logfile)
    return logfile
