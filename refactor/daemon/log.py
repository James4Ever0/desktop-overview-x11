"""daemon/log.py — central logging setup (stdout + rotating file).

Previously the daemon only created named loggers (``logging.getLogger("dovw.*")``)
but never configured a handler, so nothing was emitted.  This wires a single
root configuration used by ``__main__`` at startup:

  * a **stream handler** to stdout (so ``journalctl``/terminal shows live activity)
  * a **rotating file handler** at ``<data_dir>/logs/daemon.log`` (5 files × 2 MB)

Every module keeps using ``logging.getLogger("dovw.<area>")``; this just gives
those loggers somewhere to go.  Level comes from ``Settings.log_level``
(``DESKTOP_OVERVIEW_LOG_LEVEL``).  Call :func:`setup_logging` exactly once.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)-22s %(filename)s:%(lineno)d %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup_logging(level: str = "info", log_dir: Path | None = None,
                  *, to_file: bool = True, filename: str = "daemon.log") -> Path | None:
    """Configure the ``dovw`` logger tree once. Returns the log file path (or None)."""
    global _configured
    root = logging.getLogger("dovw")
    if _configured:
        return getattr(root, "_logfile", None)

    root.setLevel(_coerce_level(level))
    root.propagate = False
    fmt = logging.Formatter(_FMT, _DATEFMT)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    logfile = None
    if to_file and log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logfile = log_dir / filename
        fileh = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)

    root._logfile = logfile   # type: ignore[attr-defined]
    _configured = True
    root.info("logging initialized (level=%s, file=%s)", level, logfile)
    return logfile


def _coerce_level(level: str) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)
