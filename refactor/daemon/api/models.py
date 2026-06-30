"""daemon/api/models.py — pydantic response models (plan 07 §6).

Thin serialization contracts shared by routes; the assembly happens in
``db/search.py`` (which returns plain dicts), so these mostly validate/shape the
JSON.  Fields mirror the response objects documented in 07 §3-5.
"""
from __future__ import annotations

from pydantic import BaseModel


class VDesktopRef(BaseModel):
    index: int | None = None
    name: str | None = None


class Hit(BaseModel):
    field: str
    excerpt: str | None = None


class WindowOut(BaseModel):
    window_uid: int
    x_window_id: str
    wm_class: str | None = None
    app_name: str | None = None
    current_title: str | None = None
    vdesktop: VDesktopRef | None = None
    alive: bool
    jumpable: bool
    last_access: float | None = None
    usage_5m: float | None = None
    usage_10m: float | None = None
    usage_30m: float | None = None
    usage_total: float | None = None
    focus_score: float | None = None
    window_capture_url: str | None = None
    window_capture_ts: int | None = None
    hits: list[Hit] = []
    hit_fields: list[str] | None = None


class TitleChange(BaseModel):
    title: str | None = None
    changed_at: float


class EventOut(BaseModel):
    type: str
    kind: str | None = None
    text: str | None = None
    ts: float | None = None


class WindowDetail(WindowOut):
    title_history: list[TitleChange] = []
    events: list[EventOut] = []


class FocusSpan(BaseModel):
    focused_at: float
    vdesktop_index: int | None = None
    vdesktop_name: str | None = None


class TimelineLane(BaseModel):
    window_uid: int
    x_window_id: str | None = None
    wm_class: str | None = None
    app_name: str | None = None
    current_title: str | None = None
    alive: bool | None = None
    jumpable: bool | None = None
    usage_5m: float | None = None
    usage_10m: float | None = None
    usage_30m: float | None = None
    usage_total: float | None = None
    focus_score: float | None = None
    focus_spans: list[FocusSpan] = []
    titles: list[TitleChange] = []


class VDesktopOut(BaseModel):
    index: int | None = None
    name: str | None = None
    current: bool = False


class HealthOut(BaseModel):
    ok: bool
    session_key: str | None = None
    daemon_boot_id: str | None = None
    kbd_enabled: bool
    event_queue_depth: int
    events_dropped: int
    writes_dropped: int
    db_size_bytes: int | None = None
    last_full_sweep: float | None = None
    window_count: int | None = None


class KeyboardToggleIn(BaseModel):
    enabled: bool


class KeyboardToggleOut(BaseModel):
    enabled: bool


class ActivateOut(BaseModel):
    ok: bool
    reason: str   # ok | dead | different-session | vanished


class RefreshOut(BaseModel):
    ok: bool
    captured: int | None = None
