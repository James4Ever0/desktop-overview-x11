"""frontend/config.py — all frontend-side configurable knobs.

Mirrors the frontend section of ``refactor/plans/10-configuration.md §5``.
Precedence: CLI flag > env var > default here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path


def _default_socket_path() -> Path:
    env_uds = os.environ.get("DESKTOP_OVERVIEW_UDS")
    if env_uds:
        return Path(env_uds)
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        return Path(rt) / "desktop-overview.sock"
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "desktop-overview" / "daemon.sock"


# Dark theme lifted from the no-ocr demo UI.
DEFAULT_THEME = {
    "bg": "#1e1e1e",
    "fg": "#e0e0e0",
    "tile_bg": "#2a2a2a",
    "tile_border": "#3a3a3a",
    "accent": "#4a9eff",
    "muted": "#888888",
    "mark_bg": "#5a4a00",
    "alive": "#4caf50",
    "dead": "#888888",
}


@dataclass
class FrontendSettings:
    socket_path: Path = field(default_factory=_default_socket_path)
    use_tcp: bool = False                  # CLI --tcp
    tcp_endpoint: str = "127.0.0.1:8765"   # CLI --tcp HOST:PORT
    request_timeout_s: float = 5.0
    request_worker_threads: int = 4
    search_debounce_ms: int = 200
    grid_auto_refresh_s: int = 10          # 0 = off
    grid_columns: int | None = None        # None = auto-fit width
    window_capture_display_dim: int = 240
    hover_preview_delay_ms: int = 400
    hover_preview_max_dim: int = 900
    history_stack_depth: int = 20
    theme: dict = field(default_factory=lambda: dict(DEFAULT_THEME))
    font_family: str = "TkDefaultFont"
    font_size: int = 10

    def with_overrides(self, **kw) -> "FrontendSettings":
        return replace(self, **kw)


settings = FrontendSettings()
