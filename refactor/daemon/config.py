"""daemon/config.py — all daemon-side configurable knobs.

Single Python source of the defaults catalogued in
``refactor/plans/10-configuration.md``.  Precedence (highest wins):
CLI flag > environment variable > the default here.  Only a subset is wired to
env/CLI (noted per field); everything has a working default.

Import ``settings`` (a module-level ``Settings`` instance) everywhere; never
read os.environ directly elsewhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "desktop-overview"


def _default_uds_path(data_dir: Path) -> Path:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        return Path(rt) / "desktop-overview.sock"
    return data_dir / "daemon.sock"   # fallback (07 §2)


@dataclass
class Settings:
    # --- §1 paths & transport ---
    data_dir: Path = field(default_factory=_default_data_dir)
    uds_path: Path | None = None           # filled in __post_init__ from data_dir
    uds_mode: int = 0o600
    tcp_enabled: bool = False              # CLI --tcp
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 8765                   # CLI --tcp PORT
    log_level: str = "info"

    # --- §2 keyboard ---
    kbd_backend: str = "xrecord"           # 'xrecord' (preferred) | 'pynput'
    kbd_enabled: bool = True               # API-toggleable at runtime
    kbd_idle_flush_s: float = 3.0
    kbd_idle_check_s: float = 0.5
    kbd_flush_on_focus_change: bool = True
    kbd_flush_on_title_change: bool = True
    kbd_apply_backspace: bool = True
    kbd_min_segment_chars: int = 3         # store only if stripped len > this (04 §3)
    kbd_app_denylist: tuple[str, ...] = ("keepassxc", "bitwarden", "ksshaskpass")

    # --- §2b clipboard / selection / paste ---
    read_event_capture_content: bool = True   # read the selection at paste time (03 §5)

    # --- §3 window_captures & capture ---
    refresh_interval_s: int = 5            # X
    refresh_batch_size: int = 3            # Y
    window_capture_keep_per_window: int = 5
    window_capture_retention_days: int | None = None
    window_capture_max_dim: int = 1920
    ocr_enabled: bool = False
    filter_no_vdesktop: bool = True       # skip windows without a vdesktop
    capture_on_focus: bool = True         # capture on focus change
    capture_on_title: bool = True         # capture on focused-window title change

    # --- §3b title denylist ---
    window_title_denylist: tuple[str, ...] = (
        "Desktop Overview — search",
        "Desktop Overview — timeline",
        "Desktop - Plasma",
    )

    # --- §4 storage, db & search ---
    write_queue_max_n: int = 200
    write_queue_max_wait_s: float = 0.05   # batch window: lower = fresher, higher = fewer fsyncs
    write_queue_maxsize: int = 10_000      # back-pressure bound (01 §2)
    event_queue_maxsize: int = 10_000      # dispatch back-pressure bound (01 §2)
    sqlite_journal_mode: str = "WAL"
    sqlite_synchronous: str = "NORMAL"
    sqlite_busy_timeout_ms: int = 3000
    fts_tokenizer: str = "trigram"         # CJK-safe (06 §5)
    search_default_limit: int = 100
    search_max_limit: int = 500

    # --- executor sizing (01 §4) ---
    executor_max_workers: int = 8

    def __post_init__(self) -> None:
        self.data_dir = _env_path("DESKTOP_OVERVIEW_DATA_DIR", self.data_dir)
        if self.uds_path is None:
            env_uds = os.environ.get("DESKTOP_OVERVIEW_UDS")
            self.uds_path = Path(env_uds) if env_uds else _default_uds_path(self.data_dir)
        self.log_level = os.environ.get("DESKTOP_OVERVIEW_LOG_LEVEL", self.log_level)

    # derived paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "daemon.sqlite3"

    @property
    def window_capture_dir(self) -> Path:
        return self.data_dir / "window_captures"

    def with_overrides(self, **kw) -> "Settings":
        """Return a copy with CLI overrides applied (e.g. tcp_enabled/tcp_port)."""
        return replace(self, **kw)


settings = Settings()
