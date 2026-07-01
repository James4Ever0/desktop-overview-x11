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
    "bg": "#0a0a0a",
    "fg": "#e0e0e0",
    "tile_bg": "#111111",
    "tile_border": "#222222",
    "accent": "#4a9eff",
    "muted": "#888888",
    "mark_bg": "#5a4a00",
    "alive": "#4caf50",
    "dead": "#888888",
    "indicator": "#ff4444",
    "event_title": "#4caf50",
    "event_clipboard": "#ffcc00",
    "event_selection": "#ffcc00",
    "event_keyboard": "#2a5a8a",
}


@dataclass
class FrontendSettings:
    socket_path: Path = field(default_factory=_default_socket_path)
    use_tcp: bool = False                  # CLI --tcp
    tcp_endpoint: str = "127.0.0.1:8765"   # CLI --tcp HOST:PORT
    request_timeout_s: float = 5.0
    request_worker_threads: int = 4
    search_debounce_ms: int = 200
    grid_auto_refresh_s: int = 2           # 0 = off
    grid_columns: int | None = None        # None = auto-fit width
    grid_tile_width: int = 260
    grid_tile_height: int = 260
    window_capture_display_dim: int = 240
    hover_preview_delay_ms: int = 400
    hover_preview_max_dim: int = 900
    history_stack_depth: int = 20
    show_back_button: bool = False       # 08 §5: navigation history button
    theme: dict = field(default_factory=lambda: dict(DEFAULT_THEME))
    font_family: str = "TkDefaultFont"
    font_size: int = 10
    filter_no_vdesktop: bool = False       # hide windows without vdesktop in search/timeline
    hide_self: bool = True                # hide the GUI's own window from results
    hide_self_method: str = "id"          # "id" (x_window_id) or "title_prefix"

    def __post_init__(self) -> None:
        env_refresh = os.environ.get("DESKTOP_OVERVIEW_REFRESH_INTERVAL_S")
        if env_refresh:
            try:
                self.grid_auto_refresh_s = int(env_refresh)
            except ValueError:
                pass

    def with_overrides(self, **kw) -> "FrontendSettings":
        return replace(self, **kw)


settings = FrontendSettings()
