"""frontend/views.py — render builders for non-grid views (plan 08 §5).

The grid (Search/History results) lives in ``app.py`` next to its hover/tile
machinery; this module holds the **Timeline** renderer.  The new
``TimelineView`` is an interactive Tk canvas: zoom/pan, a red time indicator,
and per-focus-span hover detail with instantaneous events + a lazy thumbnail.
"""
from __future__ import annotations

import datetime
import io
import logging
import math
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

log = logging.getLogger("dovw.fe.views")

LANE_H = 34
LABEL_W = 230
BAR_H = 18
PAD = 6
SCALE_H = 30


def _parse_lane_filter(query: str) -> list[str]:
    """Return non-empty lower-case tokens for AND matching."""
    return [t.lower() for t in query.split() if t.strip()]


def _lane_title_matches(lane, tokens: list[str]) -> bool:
    """True when every token is a substring of the lane's lower-cased current title."""
    if not tokens:
        return True
    title = (lane.current_title or "").lower()
    return all(token in title for token in tokens)


def render_timeline(app, lanes: list, parent=None) -> TimelineView | None:
    """Render or refresh the interactive timeline view inside ``app.timeline_frame``."""
    parent = parent or getattr(app, "inner_frame", None)
    if parent is None:
        return None
    parent.rowconfigure(0, weight=1)
    parent.columnconfigure(0, weight=1)

    view = getattr(app, "_timeline_view", None)
    if view is None or not getattr(view, "frame", None) or not view.frame.winfo_exists():
        # Defensive: wipe any stale grid/pack slaves left by a previous failed render.
        for child in list(parent.winfo_children()):
            child.destroy()
        view = TimelineView(parent, app, app.theme, app.api,
                            scale=app.view_state.get("t_scale"),
                            indicator_time=app.view_state.get("t_indicator"))
        app._timeline_view = view

    view.set_lanes(lanes)
    return view


def _fmt_range(t_min, t_max):
    a = datetime.datetime.fromtimestamp(t_min).strftime("%Y-%m-%d %H:%M")
    b = datetime.datetime.fromtimestamp(t_max).strftime("%Y-%m-%d %H:%M")
    return f"{a} → {b}"


def _fmt_ts(ts: float | None) -> str:
    if ts is None or ts <= 0:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _truncate(text: str | None, n: int = 120) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _fmt_minutes(minutes: float | None) -> str:
    """Convert minutes into 'X d X h X m', dropping zero units except minutes."""
    if minutes is None or minutes <= 0:
        return "0 m"
    total = int(round(minutes))
    days, rem = divmod(total, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    parts.append(f"{mins} m")
    return " ".join(parts)


def _fmt_usage_lane(lane) -> str:
    """Compact usage line for a timeline lane (5m/10m/30m/1d + total + focus score)."""
    labels = ("5m", "10m", "30m", "1d")
    vals = [getattr(lane, f"usage_{lbl}", None) for lbl in labels]
    if all(v is None for v in vals):
        base = ""
    else:
        base = " ".join(f"{lbl}:{v if v is not None else '—'}" for lbl, v in zip(labels, vals))
    total = getattr(lane, "usage_total", None)
    score = getattr(lane, "focus_score", None)
    parts = []
    if base:
        parts.append(base)
    if total is not None:
        parts.append(f"tot:{_fmt_minutes(total)}")
    if score is not None:
        parts.append(f"score:{score:.2f}")
    return "  |  ".join(parts)


class TimelineView:
    """Interactive timeline: zoom, pan, red indicator, focus-span hover detail."""

    SCALE_MIN = 0.02
    SCALE_MAX = 5000.0
    ZOOM_FACTOR = 1.25
    DEFAULT_INITIAL_SCALE = 14.754
    ARROW_STEP_PX = 10
    ARROW_SHIFT_STEP_PX = 100
    MAX_EVENTS_IN_HOVER = 20
    HOVER_MAX_LINES = 14
    STICKY_MARGIN = LANE_H // 4

    def __init__(self, parent, app, theme, api, *, scale=None, indicator_time=None):
        self.app = app
        self.api = api
        self.theme = theme
        self.parent = parent

        self.scale = max(self.SCALE_MIN, min(self.SCALE_MAX, scale if scale is not None else 1.0))
        self.start_time = 0.0
        self.indicator_time = indicator_time or 0.0
        self.lanes: list = []
        self._all_lanes: list = []
        self._title_filter_query: str = ""
        self.t_min = 0.0
        self.t_max = 1.0

        self._span_items: dict[int, tuple[int, int]] = {}
        self._label_items: dict[int, int] = {}
        self._indicator_id: int | None = None
        self._indicator_id_scale: int | None = None
        self._indicator_text_id: int | None = None
        self._indicator_text_bg_id: int | None = None
        self._active_span: dict | None = None
        self._active_lane = None
        self._drag_mode: str | None = None
        self._pan_start_x = 0.0
        self._pan_start_time = 0.0
        self._click_lane = None

        self._hover_after: str | None = None
        self._hover_tip: tk.Toplevel | None = None
        self._preview_image_label: tk.Label | None = None
        self._hover_span: tuple[int, int, float, float] | None = None
        self._hover_lane_idx: int | None = None
        self._hover_pos = (-1, -1)
        self._hover_threshold = 6

        self.scale_callback = None
        self.indicator_callback = None
        self.range_callback = None

        self._build_widgets()
        self._bind_events()

    # ───────────────────────── construction ─────────────────────────
    def _build_widgets(self):
        self.frame = ttk.Frame(self.parent, style="Tile.TFrame")
        self.frame.grid(row=0, column=0, sticky="nsew")

        # top scale strip stays fixed; lanes canvas scrolls vertically underneath
        self.scale_canvas = tk.Canvas(self.frame, bg=self.theme["tile_bg"],
                                      highlightthickness=0, height=SCALE_H)
        self.canvas = tk.Canvas(self.frame, bg=self.theme["tile_bg"], highlightthickness=0)
        self.vbar = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_canvas_scroll)

        # sticky indicator for the active lane when it is scrolled off-screen
        self._sticky_frame = tk.Frame(self.canvas, bg=self.theme["tile_bg"],
                                      highlightthickness=1,
                                      highlightbackground=self.theme["indicator"])
        self._sticky_text = tk.Label(self._sticky_frame, text="", bg=self.theme["tile_bg"],
                                     fg=self.theme["fg"], anchor="w",
                                     font=("TkDefaultFont", 9))
        self._sticky_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self._sticky_frame.place_forget()

        self.scale_canvas.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.vbar.grid(row=1, column=1, sticky="ns")

        # white 1 px split line between the lanes and the sticky details panel
        self.sep_line = tk.Frame(self.frame, bg="#ffffff", height=1)
        self.sep_line.grid(row=2, column=0, columnspan=2, sticky="ew")

        # bottom details panel: 1/4 of vertical space, lanes get the other 3/4
        self.details_frame = ttk.Frame(self.frame, style="Tile.TFrame")
        self.details_frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.details_text = tk.Text(self.details_frame, wrap="word", bd=0, height=1,
                                    bg=self.theme["tile_bg"], fg=self.theme["fg"],
                                    highlightthickness=0, padx=8, pady=6,
                                    font=("TkDefaultFont", 9))
        self.details_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.details_text.tag_configure("header", foreground=self.theme["accent"], font=("TkDefaultFont", 9, "bold"))
        self.details_text.tag_configure("time", foreground=self.theme["muted"])
        self.details_text.tag_configure("field", foreground=self.theme["accent"])
        self.details_text.tag_configure("mark", background=self.theme["mark_bg"])
        self.details_scroll = ttk.Scrollbar(self.details_frame, orient=tk.VERTICAL,
                                            command=self.details_text.yview)
        self.details_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.details_text.configure(yscrollcommand=self.details_scroll.set)
        self._bind_text_scroll(self.details_text)

        self.frame.rowconfigure(1, weight=3)
        self.frame.rowconfigure(3, weight=1)
        self.frame.columnconfigure(0, weight=1)

    def _bind_text_scroll(self, text: tk.Text):
        """Bind wheel events on a disabled/small Text so it scrolls itself."""
        def _scroll(event):
            if event.num == 4:
                text.yview_scroll(-1, "units")
            elif event.num == 5:
                text.yview_scroll(1, "units")
            elif event.delta:
                text.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        text.bind("<MouseWheel>", _scroll)
        text.bind("<Button-4>", _scroll)
        text.bind("<Button-5>", _scroll)

    def _bind_events(self):
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", self._on_wheel)
        self.canvas.bind("<Button-5>", self._on_wheel)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_pan_start)
        self.canvas.bind("<B3-Motion>", self._on_pan)
        self.canvas.bind("<ButtonRelease-3>", self._on_pan_end)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Left>", self._on_left)
        self.canvas.bind("<Right>", self._on_right)
        self.canvas.bind("<Up>", self._on_up)
        self.canvas.bind("<Down>", self._on_down)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.focus_set())

        # the fixed scale strip should also drive zoom / indicator placement
        self.scale_canvas.bind("<MouseWheel>", self._on_wheel)
        self.scale_canvas.bind("<Button-4>", self._on_wheel)
        self.scale_canvas.bind("<Button-5>", self._on_wheel)
        self.scale_canvas.bind("<Button-1>", self._on_press)
        self.scale_canvas.bind("<B1-Motion>", self._on_drag)
        self.scale_canvas.bind("<ButtonRelease-1>", self._on_release)
        self.scale_canvas.bind("<Enter>", lambda _e: self.canvas.focus_set())

    # ───────────────────────── public state ─────────────────────────
    def set_lanes(self, lanes: list):
        self._cancel_hover()
        self._all_lanes = lanes or []
        self._apply_title_filter()
        self.indicator_time = max(self.t_min, min(self.indicator_time, self.t_max))

        if not self.lanes:
            self.scale_canvas.delete("all")
            self.canvas.delete("all")
            self._draw_empty_message()
            self._notify_range()
            return

        # First load: current time at the right edge, fixed initial zoom.
        if self.start_time == 0.0 and abs(self.scale - 1.0) < 1e-9:
            now = time.time()
            self.indicator_time = now
            self.scale = max(self.SCALE_MIN, min(self.SCALE_MAX, self.DEFAULT_INITIAL_SCALE))
            timeline_w = max(1, self._canvas_width() - self._x_offset() - PAD)
            self.start_time = now - timeline_w * self.scale
            # Allow the right edge to extend past the data so "now" can sit at the right.
            min_start = self.t_min - PAD * self.scale
            max_start = max(self.t_max, now) + PAD * self.scale - timeline_w * self.scale
            self.start_time = max(min_start, min(max_start, self.start_time))
            self._draw()
            self._sync_callbacks()
        else:
            self._clamp_view()
            self._draw()
        self._sync_callbacks()

    def set_title_filter(self, query: str):
        """Filter visible lanes by current title (frontend-only, AND tokens); keep zoom + red line."""
        self._title_filter_query = query
        self._apply_title_filter()
        self._draw()
        self._sync_callbacks()
        self._update_sticky()
        self._update_details_panel()

    def _apply_title_filter(self):
        tokens = _parse_lane_filter(self._title_filter_query)
        self.lanes = [lane for lane in self._all_lanes if _lane_title_matches(lane, tokens)]
        self.t_min, self.t_max = self._calc_bounds()
        if self.t_max <= self.t_min:
            self.t_max = self.t_min + 1.0

    def _draw_empty_message(self):
        """Draw a helpful message when filtering leaves no lanes."""
        x = max(50, self._canvas_width() // 2)
        y = max(50, self.canvas.winfo_height() // 2)
        self.canvas.create_text(
            x, y,
            text="No lanes match the title filter.",
            fill=self.theme.get("muted", "#888888"),
            font=("TkDefaultFont", 12),
            anchor="center",
            tags=("empty_message",)
        )

    def fit(self):
        """Scale and scroll so the whole time range fits the visible width."""
        timeline_w = max(1, self._canvas_width() - self._x_offset() - PAD)
        self.scale = max(self.SCALE_MIN, min(self.SCALE_MAX, (self.t_max - self.t_min) / timeline_w))
        self.start_time = self.t_min - PAD * self.scale
        self.indicator_time = (self.t_min + self.t_max) / 2.0
        self._draw()
        self._sync_callbacks()

    def fit_recent_1d(self):
        """Default view: the most recent 24 h ending at now, clipped to data bounds."""
        now = datetime.datetime.now().timestamp()
        end = min(now, self.t_max)
        start = max(self.t_min, end - 86400.0)
        duration = max(1.0, end - start)
        timeline_w = max(1, self._canvas_width() - self._x_offset() - PAD)
        self.scale = max(self.SCALE_MIN, min(self.SCALE_MAX, duration / timeline_w))
        self.start_time = end - timeline_w * self.scale
        self.indicator_time = end
        self._clamp_view()
        self._draw()
        self._sync_callbacks()

    def jump_to_now(self):
        """Move the red indicator to now and shift the viewport there, keeping zoom."""
        now = time.time()
        self.indicator_time = max(self.t_min, min(self.t_max, now))
        timeline_w = max(1, self._canvas_width() - self._x_offset() - PAD)
        self.start_time = self.indicator_time - timeline_w * self.scale
        self._clamp_view()
        self._draw()
        self._sync_callbacks()

    def set_scale(self, scale: float):
        """Set seconds-per-pixel, keeping the viewport centre fixed."""
        new_scale = max(self.SCALE_MIN, min(self.SCALE_MAX, scale))
        if abs(new_scale - self.scale) < 1e-9:
            return
        centre_t = self._t_of(self._canvas_width() / 2)
        self._zoom_to(new_scale, centre_t)

    def set_start_time(self, t: float):
        self.start_time = t
        self._clamp_view()
        self._draw()

    def set_indicator_time(self, t: float):
        self.indicator_time = max(self.t_min, min(self.t_max, t))
        self._update_indicator()
        if self.indicator_callback:
            self.indicator_callback(self.indicator_time)

    def parse_indicator_str(self, s: str) -> float | None:
        """Parse ``YYYY-MM-DD HH:MM:SS`` to a timestamp, or None."""
        try:
            dt = datetime.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        except ValueError:
            return None

    # ───────────────────────── coordinate math ─────────────────────────
    def _x_offset(self) -> int:
        return LABEL_W + PAD

    def _canvas_width(self) -> int:
        return max(1, self.canvas.winfo_width())

    def _x_of(self, t: float) -> float:
        return self._x_offset() + (t - self.start_time) / self.scale

    def _t_of(self, x: float) -> float:
        return self.start_time + (x - self._x_offset()) * self.scale

    def _calc_bounds(self) -> tuple[float, float]:
        lo, hi = None, None
        for lane in self.lanes:
            for sp in lane.focus_spans:
                for key in ("focused_at", "ended_at"):
                    v = sp.get(key)
                    if v is None:
                        continue
                    lo = v if lo is None else min(lo, v)
                    hi = v if hi is None else max(hi, v)
            for e in lane.events:
                if e.ts is not None:
                    lo = e.ts if lo is None else min(lo, e.ts)
                    hi = e.ts if hi is None else max(hi, e.ts)
        if lo is None:
            return 0.0, 1.0
        return lo, hi

    def _clamp_view(self):
        timeline_w = max(1, self._canvas_width() - self._x_offset() - PAD)
        min_start = self.t_min - PAD * self.scale
        max_start = self.t_max + PAD * self.scale - timeline_w * self.scale
        self.start_time = max(min_start, min(max_start, self.start_time))

    def _visible_time_range(self) -> tuple[float, float]:
        w = self._canvas_width()
        return self._t_of(0), self._t_of(w)

    def _notify_range(self):
        if self.range_callback:
            self.range_callback(*self._visible_time_range())

    def _on_canvas_scroll(self, *args):
        """Forward vertical scroll to the scrollbar and update the sticky hit indicator."""
        self.vbar.set(*args)
        self._update_sticky()

    def _update_sticky(self):
        """Show a sticky label at the top/bottom of the lane viewport when the active lane is off-screen."""
        if self._active_lane is None or not self.lanes:
            self._sticky_frame.place_forget()
            return
        try:
            idx = self.lanes.index(self._active_lane)
        except ValueError:
            self._sticky_frame.place_forget()
            return

        lane_top = PAD + idx * (LANE_H + PAD)
        lane_center = lane_top + LANE_H / 2
        vis_top = self.canvas.canvasy(0)
        vis_bottom = self.canvas.canvasy(self.canvas.winfo_height())
        M = self.STICKY_MARGIN

        if lane_center < vis_top + M:
            pos = "top"
        elif lane_center > vis_bottom - M:
            pos = "bottom"
        else:
            self._sticky_frame.place_forget()
            return

        lane = self._active_lane
        label = f"{lane.app_name or lane.wm_class or '?'} — {lane.current_title or '(no title)'}"
        self._sticky_text.configure(text=label[:80])

        base_x = self._x_offset()
        width = max(1, self.canvas.winfo_width() - base_x)
        self._sticky_frame.configure(width=width)

        if pos == "top":
            self._sticky_frame.place(x=base_x, y=0, anchor="nw", width=width)
        else:
            self._sticky_frame.place(x=base_x, y=self.canvas.winfo_height(), anchor="sw", width=width)
        self._sticky_frame.lift()

    # ───────────────────────── rendering ─────────────────────────
    def _on_configure(self, _event=None):
        self._draw()

    def _draw(self):
        self.scale_canvas.delete("all")
        self.canvas.delete("all")
        self._span_items.clear()
        self._label_items.clear()
        old_span, old_lane = self._active_span, self._active_lane
        self._active_span, self._active_lane = self._span_at_indicator()
        if self._active_key(self._active_span, self._active_lane) != self._active_key(old_span, old_lane):
            self._update_details_panel()
        if not self.lanes:
            self._draw_empty_message()
            self._notify_range()
            return
        self._draw_scale()
        self._draw_lanes()
        self._draw_indicator()
        virtual_w = self._x_offset() + (self.t_max - self.t_min) / self.scale + PAD
        lanes_h = len(self.lanes) * (LANE_H + PAD) + PAD
        self.scale_canvas.configure(scrollregion=(0, 0, virtual_w, SCALE_H))
        self.canvas.configure(scrollregion=(0, 0, virtual_w, lanes_h))
        self._notify_range()
        self._update_sticky()

    def _draw_scale(self):
        t0, t1 = self._visible_time_range()
        major = self._pick_step(min_px=100)
        minor = self._pick_step(min_px=30)
        x_left = self._x_offset()

        # minor ticks (only over the timeline area, not the label column)
        t = math.floor(t0 / minor) * minor
        while t <= t1:
            x = self._x_of(t)
            if x >= x_left - 1:
                self.scale_canvas.create_line(x, SCALE_H - 8, x, SCALE_H, fill=self.theme["muted"])
            t += minor

        # major ticks + labels
        t = math.floor(t0 / major) * major
        while t <= t1:
            x = self._x_of(t)
            if x >= x_left - 1:
                self.scale_canvas.create_line(x, 0, x, SCALE_H, fill=self.theme["fg"])
                label = self._scale_label(t, major)
                self.scale_canvas.create_text(x + 4, 2, text=label, anchor="nw",
                                        fill=self.theme["fg"])
            t += major

        # baseline under the scale
        self.scale_canvas.create_line(x_left, SCALE_H,
                                max(x_left, self._x_offset() + (self.t_max - self.t_min) / self.scale + PAD),
                                SCALE_H,
                                fill=self.theme["tile_border"])

        # hit/miss badge in the leftmost blank space
        hit = self._active_span is not None
        self.scale_canvas.create_text(PAD, 2,
                                      text="hit" if hit else "miss",
                                      anchor="nw",
                                      fill=self.theme["indicator"] if hit else self.theme["muted"])

    def _pick_step(self, min_px: int) -> float:
        candidates = [
            1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600,
            7200, 21600, 43200, 86400, 172800, 604800, 2592000,
            7776000, 15552000, 31536000,
        ]
        for step in candidates:
            if step / self.scale >= min_px:
                return float(step)
        return float(candidates[-1])

    def _scale_label(self, t: float, step: float) -> str:
        dt = datetime.datetime.fromtimestamp(t)
        if step < 60:
            return dt.strftime("%H:%M:%S")
        if step < 86400:
            return dt.strftime("%a %H:%M")
        if step < 2592000:
            return dt.strftime("%a %d %b")
        return dt.strftime("%b %Y")

    def _draw_lanes(self):
        y = PAD
        for i, lane in enumerate(self.lanes):
            self._draw_lane(i, lane, y, active=(lane is self._active_lane))
            y += LANE_H + PAD

    def _draw_lane(self, idx: int, lane, y: int, active: bool = False):
        x_left = self._x_offset()

        # lane separator / axis
        self.canvas.create_line(x_left, y + LANE_H // 2,
                                x_left + (self.t_max - self.t_min) / self.scale + PAD,
                                y + LANE_H // 2,
                                fill=self.theme["tile_border"])

        # window lifespan line (behind the focus-span rectangles)
        if lane.created_since is not None:
            life_start = lane.created_since
            life_end = lane.dead_at if lane.dead_at is not None else self.t_max
            lx0 = max(x_left, self._x_of(life_start))
            lx1 = self._x_of(life_end)
            mid = y + LANE_H // 2
            self.canvas.create_line(lx0, mid, lx1, mid,
                                    fill=self.theme.get("lifespan", "#333333"), width=2)

        # focus spans (clipped so they never draw over the label column)
        bar_y = y + (LANE_H - BAR_H) // 2
        for si, sp in enumerate(lane.focus_spans):
            start = sp.get("focused_at")
            end = sp.get("ended_at") or start
            if start is None:
                continue
            x0 = max(x_left, self._x_of(start))
            x1 = max(x0 + 2, self._x_of(end))
            item = self.canvas.create_rectangle(
                x0, bar_y, x1, bar_y + BAR_H,
                fill=self.theme["accent"], outline="", tags=("span",))
            self._span_items[item] = (idx, si)

        # title-change ticks (clipped to the timeline area)
        for tt in lane.titles:
            t = tt.get("changed_at")
            if t is None:
                continue
            x = max(x_left, self._x_of(t))
            self.canvas.create_line(x, y + 2, x, y + LANE_H - 2,
                                    fill=self.theme["event_title"])

        # instantaneous event markers (clipboard/selection yellow, keyboard dark blue)
        for e in lane.events:
            if e.type == "title" or e.ts is None:
                continue
            x = max(x_left, self._x_of(e.ts))
            if e.type == "clipboard":
                color = self.theme["event_clipboard"]
                y0, y1 = y + 2, y + 8
            elif e.type == "selection":
                color = self.theme["event_selection"]
                y0, y1 = y + LANE_H - 8, y + LANE_H - 2
            elif e.type == "keyboard":
                color = self.theme["event_keyboard"]
                mid = y + LANE_H // 2
                y0, y1 = mid - 3, mid + 3
            else:
                continue
            self.canvas.create_line(x, y0, x, y1, fill=color, width=2)

        # solid background for the label column, on top of any bars/ticks
        label_bg = self.theme["active_lane_bg"] if active else self.theme["tile_bg"]
        mask_id = self.canvas.create_rectangle(0, y, x_left, y + LANE_H,
                                               fill=label_bg, outline="",
                                               tags=("mask", "lane_label"), state=tk.DISABLED)
        self._label_items[mask_id] = idx

        # window title text on top of everything; highlight if this lane is active
        label = f"{lane.app_name or lane.wm_class or '?'} — {lane.current_title or '(no title)'}"
        if active:
            colour = "#ffffff"
            font = ("TkDefaultFont", 9, "bold")
        else:
            colour = self.theme["alive"] if lane.alive and lane.jumpable else self.theme["dead"]
            font = ("TkDefaultFont", 9)
        label_id = self.canvas.create_text(PAD, y + LANE_H // 2, text=label[:48], anchor="w",
                                fill=colour, font=font, width=LABEL_W - PAD, tags=("lane_label",))
        self._label_items[label_id] = idx
        log.debug("drew lane %d label_id=%s text=%r active=%s", idx, label_id, label[:48], active)

    def _draw_indicator(self):
        self._delete_indicator()
        x_left = self._x_offset()
        x = max(x_left, self._x_of(self.indicator_time))
        lanes_h = len(self.lanes) * (LANE_H + PAD) + PAD
        self._indicator_id = self.canvas.create_line(
            x, 0, x, lanes_h, fill=self.theme["indicator"], width=1,
            dash=(4, 4), tags=("indicator",))
        self._indicator_id_scale = self.scale_canvas.create_line(
            x, 0, x, SCALE_H, fill=self.theme["indicator"], width=1,
            dash=(4, 4), tags=("indicator",))
        if x >= x_left:
            text = _fmt_ts(self.indicator_time)
            tx = x - 4
            self._indicator_text_id = self.scale_canvas.create_text(
                tx, 2, text=text, anchor="ne",
                fill=self.theme["indicator"], tags=("indicator",))
            self.scale_canvas.update_idletasks()
            bbox = self.scale_canvas.bbox(self._indicator_text_id)
            if bbox:
                pad = 2
                self._indicator_text_bg_id = self.scale_canvas.create_rectangle(
                    bbox[0] - pad, bbox[1] - pad,
                    bbox[2] + pad, bbox[3] + pad,
                    fill=self.theme["bg"], outline="#ffffff", width=1,
                    tags=("indicator",))
                self.scale_canvas.lower(self._indicator_text_bg_id, self._indicator_text_id)

    def _delete_indicator(self):
        items = ((self.canvas, "_indicator_id"),
                 (self.scale_canvas, "_indicator_id_scale"),
                 (self.scale_canvas, "_indicator_text_id"),
                 (self.scale_canvas, "_indicator_text_bg_id"))
        for canvas, attr in items:
            item = getattr(self, attr, None)
            if item is not None:
                canvas.delete(item)
                setattr(self, attr, None)

    def _update_indicator(self):
        x_left = self._x_offset()
        x = max(x_left, self._x_of(self.indicator_time))
        if self._indicator_id is not None:
            coords = self.canvas.coords(self._indicator_id)
            if len(coords) == 4:
                self.canvas.coords(self._indicator_id, x, coords[1], x, coords[3])
        if self._indicator_id_scale is not None:
            coords = self.scale_canvas.coords(self._indicator_id_scale)
            if len(coords) == 4:
                self.scale_canvas.coords(self._indicator_id_scale, x, coords[1], x, coords[3])
        if self._indicator_text_id is not None:
            tx = x - 4
            self.scale_canvas.coords(self._indicator_text_id, tx, 2)
            self.scale_canvas.itemconfigure(self._indicator_text_id, text=_fmt_ts(self.indicator_time))
            self.scale_canvas.update_idletasks()
            bbox = self.scale_canvas.bbox(self._indicator_text_id)
            if bbox:
                pad = 2
                if self._indicator_text_bg_id is None:
                    self._indicator_text_bg_id = self.scale_canvas.create_rectangle(
                        bbox[0] - pad, bbox[1] - pad,
                        bbox[2] + pad, bbox[3] + pad,
                        fill=self.theme["bg"], outline="#ffffff", width=1,
                        tags=("indicator",))
                    self.scale_canvas.lower(self._indicator_text_bg_id, self._indicator_text_id)
                else:
                    self.scale_canvas.coords(self._indicator_text_bg_id,
                                       bbox[0] - pad, bbox[1] - pad,
                                       bbox[2] + pad, bbox[3] + pad)
        # Red line may have moved into/out of a focus span; refresh details + highlight only when the active span changes.
        old_key = self._active_key(self._active_span, self._active_lane)
        new_span, new_lane = self._span_at_indicator()
        new_key = self._active_key(new_span, new_lane)
        self._active_span, self._active_lane = new_span, new_lane
        if new_key != old_key:
            self._update_details_panel()
            self._draw()

    def _active_key(self, span, lane):
        """Stable value-based key for the currently highlighted focus span/lane."""
        if span is None or lane is None:
            return None
        return (getattr(lane, "window_uid", None), span.get("focused_at"), span.get("ended_at"))

    def _span_at_indicator(self):
        """Return the focus span (and its lane) that contains the red-line time, if any."""
        for lane in self.lanes:
            for sp in lane.focus_spans:
                start = sp.get("focused_at")
                end = sp.get("ended_at")
                if start is None:
                    continue
                if end is None:
                    end = start
                if start <= self.indicator_time <= end:
                    return sp, lane
        return None, None

    def _update_details_panel(self):
        """Fill the sticky bottom panel with details for the active focus span."""
        top = self.details_text.yview()[0]
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", tk.END)
        span, lane = self._active_span, self._active_lane
        if span is None or lane is None:
            self.details_text.insert(tk.END, "No focused window event at the red line.", "time")
        else:
            self._fill_details(span, lane)
        self.details_text.configure(state="disabled")
        self.details_text.yview_moveto(top)

    def _fill_details(self, span: dict, lane):
        box = self.details_text
        start = span.get("focused_at")
        end = span.get("ended_at") or start
        app = lane.app_name or lane.wm_class or "?"
        title = lane.current_title or "(no title)"
        box.insert(tk.END, f"{app} — {title}\n", "header")

        status = "accessible" if lane.alive and lane.jumpable else ("dead" if not lane.alive else "other session")
        box.insert(tk.END, f"status: {status}\n")

        vd_index = span.get("vdesktop_index")
        vd_name = span.get("vdesktop_name")
        if vd_index is not None:
            box.insert(tk.END, f"desktop: [{vd_index}: {vd_name or '?'}]\n")

        box.insert(tk.END, "focus span: ", "field")
        box.insert(tk.END, f"{_fmt_ts(start)}  →  {_fmt_ts(end)}")
        if start is not None and end is not None:
            box.insert(tk.END, f"  ({_fmt_minutes((end - start) / 60.0)})\n")
        else:
            box.insert(tk.END, "\n")

        usage = _fmt_usage_lane(lane)
        if usage:
            box.insert(tk.END, f"{usage}\n")

        def in_span(ts: float | None) -> bool:
            return ts is not None and start is not None and end is not None and start <= ts <= end

        # Title changes within the span
        titles = [t for t in lane.titles if in_span(t.get("changed_at"))]
        if titles:
            box.insert(tk.END, "\nTitle changes\n", "header")
            for t in titles[:20]:
                box.insert(tk.END, f"[{_fmt_ts(t.get('changed_at'))}] ", "time")
                box.insert(tk.END, f"{_truncate(t.get('title') or '')}\n")

        # Grouped instantaneous events within the span
        for typ, label in (("keyboard", "Keyboard events"),
                           ("clipboard", "Clipboard events"),
                           ("selection", "Selection events")):
            evs = [e for e in lane.events if e.type == typ and in_span(e.ts)]
            if evs:
                box.insert(tk.END, f"\n{label}\n", "header")
                for e in evs[:20]:
                    box.insert(tk.END, f"[{_fmt_ts(e.ts)}] ", "time")
                    if e.kind:
                        box.insert(tk.END, f"{e.kind}: ", "field")
                    body = _truncate(e.text or "") or "—"
                    box.insert(tk.END, f"{body}\n")

    # ───────────────────────── zoom / pan ─────────────────────────
    def _zoom_to(self, new_scale: float, centre_t: float):
        new_scale = max(self.SCALE_MIN, min(self.SCALE_MAX, new_scale))
        if abs(new_scale - self.scale) < 1e-9:
            return
        centre_x = self._x_of(centre_t)
        new_start = centre_t - (centre_x - self._x_offset()) * new_scale
        self.scale = new_scale
        self.start_time = new_start
        self._clamp_view()
        self._draw()
        self._sync_callbacks()

    def _on_wheel(self, event):
        if not self.app._pointer_in_tip(self._hover_tip):
            self._destroy_hover_tip()
        cx = self.canvas.canvasx(event.x)
        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        delta = event.delta if event.delta else (-120 if event.num == 5 else 120)

        if ctrl:
            factor = self.ZOOM_FACTOR if delta < 0 else 1 / self.ZOOM_FACTOR
            self._zoom_to(self.scale * factor, self._t_of(cx))
            return "break"
        if shift:
            self.set_start_time(self.start_time - delta / 120 * self._canvas_width() / 5 * self.scale)
            return "break"
        # plain wheel scrolls vertically
        self.canvas.yview_scroll(-int(delta / 120), "units")
        return "break"

    def _on_pan_start(self, event):
        if not self.app._pointer_in_tip(self._hover_tip):
            self._destroy_hover_tip()
        self.canvas.config(cursor="fleur")
        self._pan_start_x = self.canvas.canvasx(event.x)
        self._pan_start_time = self.start_time

    def _on_pan(self, event):
        dx = self._pan_start_x - self.canvas.canvasx(event.x)
        self.set_start_time(self._pan_start_time + dx * self.scale)

    def _on_pan_end(self, _event):
        self.canvas.config(cursor="")

    # ───────────────────────── pointer / selection ─────────────────────────
    def _on_press(self, event):
        if not self.app._pointer_in_tip(self._hover_tip):
            self._destroy_hover_tip()
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        item = self.canvas.find_closest(cx, cy)[0]
        tags = self.canvas.gettags(item)
        log.debug("_on_press item=%s tags=%s", item, tags)
        if "indicator" in tags:
            self._drag_mode = "indicator"
        elif "lane_label" in tags:
            lane_idx = self._label_items.get(item)
            if lane_idx is not None and 0 <= lane_idx < len(self.lanes):
                uid = self.lanes[lane_idx].window_uid
                log.debug("left-click lane label: jumping to window_uid=%d", uid)
                self.app.jump_to(uid)
            self._drag_mode = "none"
        else:
            # Left-click anywhere else (including a focus span) just positions the red line.
            self._drag_mode = "none"
            self.set_indicator_time(self._t_of(cx))

    def _on_drag(self, event):
        if self._drag_mode == "indicator":
            self.set_indicator_time(self._t_of(self.canvas.canvasx(event.x)))

    def _on_release(self, event):
        self._drag_mode = None

    def _on_left(self, event):
        step = (self.ARROW_SHIFT_STEP_PX if event.state & 0x1 else self.ARROW_STEP_PX) * self.scale
        self.set_indicator_time(self.indicator_time - step)
        return "break"

    def _on_right(self, event):
        step = (self.ARROW_SHIFT_STEP_PX if event.state & 0x1 else self.ARROW_STEP_PX) * self.scale
        self.set_indicator_time(self.indicator_time + step)
        return "break"

    def _on_up(self, _event):
        self._zoom_to(self.scale / self.ZOOM_FACTOR, self.indicator_time)
        return "break"

    def _on_down(self, _event):
        self._zoom_to(self.scale * self.ZOOM_FACTOR, self.indicator_time)
        return "break"

    # ───────────────────────── callbacks ─────────────────────────
    def _sync_callbacks(self):
        if self.scale_callback:
            self.scale_callback(self.scale)
        if self.indicator_callback:
            self.indicator_callback(self.indicator_time)

    # ───────────────────────── hover detail ─────────────────────────
    def _on_motion(self, event):
        px, py = event.x_root, event.y_root

        # While Shift is held, ignore all mouse movement and never hide the hover interface.
        if self.app._is_shift_held():
            self._hover_pos = (px, py)
            return

        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        log.debug("<Motion> widget=%s serial=%s canvas=(%.1f,%.1f) root=(%d,%d)",
                  event.widget, event.serial, cx, cy, px, py)

        # Don't hide the hover tip if the pointer is currently inside it.
        if self._hover_tip is not None and self.app._pointer_in_tip(self._hover_tip):
            self._hover_pos = (px, py)
            return

        # Hide the hover tip if the pointer has moved away from where it appeared.
        if self._hover_tip is not None:
            ox, oy = self._hover_pos
            if abs(px - ox) > self._hover_threshold or abs(py - oy) > self._hover_threshold:
                log.debug("pointer moved away from hover origin; destroying tip")
                self._destroy_hover_tip()

        try:
            item = self.canvas.find_closest(cx, cy)[0]
            tags = self.canvas.gettags(item)
        except Exception as exc:  # noqa: BLE001
            log.debug("find_closest failed: %s", exc)
            self._cancel_hover()
            return
        log.debug("closest item=%s tags=%s label_items=%s span_items=%s",
                  item, tags, list(self._label_items.keys()), list(self._span_items.keys()))

        self._hover_span = None
        self._hover_lane_idx = None
        if "span" in tags:
            lane_idx, span_idx = self._span_items[item]
            self._hover_span = (lane_idx, span_idx, cx, cy)
            log.debug("hover target is span lane=%d span=%d", lane_idx, span_idx)
        elif "lane_label" in tags:
            lane_idx = self._label_items.get(item)
            if lane_idx is not None:
                self._hover_lane_idx = lane_idx
                log.debug("hover target is lane_label lane=%d", lane_idx)
            else:
                log.debug("lane_label tag found but item not in mapping")
        else:
            log.debug("closest item is not a hover target; cancelling")
            self._cancel_hover()
            return
        self._hover_pos = (px, py)
        if self._hover_after is not None:
            self.canvas.after_cancel(self._hover_after)
        delay = getattr(self.app.s, "hover_preview_delay_ms", 400)
        log.debug("scheduling hover preview in %d ms", delay)
        self._hover_after = self.canvas.after(delay, self._show_hover)

    def _on_leave(self, event):
        log.debug("<Leave> widget=%s serial=%s", event.widget, event.serial)
        self._cancel_hover()

    def _cancel_hover(self):
        if self._hover_after is not None:
            self.canvas.after_cancel(self._hover_after)
            self._hover_after = None
        self._hover_span = None
        self._hover_lane_idx = None

    def _show_hover(self):
        log.debug("_show_hover called span=%s lane=%s lanes=%d",
                  self._hover_span is not None, self._hover_lane_idx, len(self.lanes))
        if self._hover_span is not None:
            lane_idx, span_idx, cx, cy = self._hover_span
            if not (0 <= lane_idx < len(self.lanes) and
                    0 <= span_idx < len(self.lanes[lane_idx].focus_spans)):
                log.debug("stale span hover indices; cancelling")
                self._cancel_hover()
                return
            lane = self.lanes[lane_idx]
            span = lane.focus_spans[span_idx]
            px = self.canvas.winfo_rootx() + int(cx)
            py = self.canvas.winfo_rooty() + int(cy)
            log.debug("showing span preview lane=%d span=%d", lane_idx, span_idx)
            self._show_span_preview(lane, span, px, py)
        elif self._hover_lane_idx is not None:
            if not (0 <= self._hover_lane_idx < len(self.lanes)):
                log.debug("stale lane hover index; cancelling")
                self._cancel_hover()
                return
            lane = self.lanes[self._hover_lane_idx]
            px = self.canvas.winfo_rootx() + int(self.canvas.canvasx(0))
            py = self.canvas.winfo_rooty() + int(self.canvas.canvasy(0))
            log.debug("showing lane preview lane=%d uid=%d", self._hover_lane_idx, lane.window_uid)
            self._show_lane_preview(lane, px, py)
        else:
            log.debug("_show_hover called with nothing to show")
        self._hover_span = None
        self._hover_lane_idx = None

    def _show_span_preview(self, lane, span, px, py):
        self._destroy_hover_tip()
        tip = tk.Toplevel(self.canvas)
        tip.wm_overrideredirect(True)
        tip.configure(bg=self.theme["tip_border"])
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass

        title = f"#{lane.window_uid}  {lane.app_name or lane.wm_class or '?'} — {lane.current_title or '(no title)'}"
        title_lbl = tk.Label(tip, text=title, anchor="w", justify="left",
                             bg=self.theme["tiptitle_bg"], fg=self.theme["tiptitle_fg"],
                             font=self.app.title_font, padx=8, pady=4, wraplength=500)
        title_lbl.pack(fill=tk.X, padx=1, pady=(1, 0))

        meta = tk.Text(tip, height=1, width=55, wrap="word", bd=0,
                       bg=self.theme["tip_bg"], fg=self.theme["fg"],
                       highlightthickness=0, padx=8, pady=4,
                       font=self.app.meta_font)
        meta.tag_configure("field", foreground=self.theme["accent"])
        meta.tag_configure("time", foreground=self.theme["muted"])
        meta.pack(fill=tk.X, padx=1, pady=(0, 1))
        self._fill_span_meta(meta, lane, span)
        meta.configure(state="disabled")
        meta.update_idletasks()
        try:
            lines = int(float(meta.index("end-1c")))
        except Exception:  # noqa: BLE001
            lines = 6
        meta.configure(height=min(max(lines, 3), self.HOVER_MAX_LINES))

        self._hover_tip = tip
        self._preview_image_label = None
        self._position_hover_tip(px, py)

    def _show_lane_preview(self, lane, px, py):
        log.debug("_show_lane_preview lane=%d uid=%d", self._hover_lane_idx, lane.window_uid)
        self._destroy_hover_tip()
        tip = tk.Toplevel(self.canvas)
        tip.wm_overrideredirect(True)
        tip.configure(bg=self.theme["tip_border"])
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass

        title = f"#{lane.window_uid}  {lane.app_name or lane.wm_class or '?'} — {lane.current_title or '(no title)'}"
        title_lbl = tk.Label(tip, text=title, anchor="w", justify="left",
                             bg=self.theme["tiptitle_bg"], fg=self.theme["tiptitle_fg"],
                             font=self.app.title_font, padx=8, pady=4, wraplength=500)
        title_lbl.pack(fill=tk.X, padx=1, pady=(1, 0))

        meta = tk.Text(tip, height=1, width=55, wrap="word", bd=0,
                       bg=self.theme["tip_bg"], fg=self.theme["fg"],
                       highlightthickness=0, padx=8, pady=4,
                       font=self.app.meta_font)
        meta.tag_configure("field", foreground=self.theme["accent"])
        meta.tag_configure("time", foreground=self.theme["muted"])
        meta.pack(fill=tk.X, padx=1, pady=(0, 1))
        self._fill_lane_meta(meta, lane)
        meta.configure(state="disabled")
        meta.update_idletasks()
        try:
            lines = int(float(meta.index("end-1c")))
        except Exception:  # noqa: BLE001
            lines = 6
        meta.configure(height=min(max(lines, 3), self.HOVER_MAX_LINES))

        img_lbl = tk.Label(tip, text="…", bg=self.theme["tip_bg"], fg=self.theme["fg"])
        img_lbl.pack(padx=1, pady=(0, 1))

        self._hover_tip = tip
        self._preview_image_label = img_lbl
        self._position_hover_tip(px, py)

        ts = self.indicator_time if self.t_min <= self.indicator_time <= self.t_max else datetime.datetime.now().timestamp()
        ts = max(self.t_min, min(self.t_max, ts))
        self.app._submit(
            lambda: self.api.get_bytes(f"/windows/{lane.window_uid}/window_capture/{ts}"),
            lambda data, err: self._on_hover_image(data, err, tip, img_lbl, px, py),
        )

    def _fill_lane_meta(self, box: tk.Text, lane):
        box.configure(state="normal")
        box.delete("1.0", tk.END)
        box.insert(tk.END, f"window #{lane.window_uid}\n", "field")
        box.insert(tk.END, f"app: {lane.app_name or '-'}\n")
        box.insert(tk.END, f"class: {lane.wm_class or '-'}\n")
        box.insert(tk.END, f"alive: {'yes' if lane.alive else 'no'}, jumpable: {'yes' if lane.jumpable else 'no'}\n")
        box.insert(tk.END, f"focus spans: {len(lane.focus_spans)}\n")
        if lane.titles:
            box.insert(tk.END, f"title changes: {len(lane.titles)}\n")
        if lane.events:
            recent = sorted(lane.events, key=lambda e: e.ts, reverse=True)[:self.MAX_EVENTS_IN_HOVER]
            box.insert(tk.END, "\nRecent events\n", "field")
            for e in reversed(recent):
                label = e.type if e.type != "clipboard" or not e.kind else f"{e.type}/{e.kind}"
                box.insert(tk.END, f"[{_fmt_ts(e.ts)}] {label}: ", "time")
                box.insert(tk.END, _truncate(e.text or "") + "\n")

    def _fill_span_meta(self, box: tk.Text, lane, span):
        box.configure(state="normal")
        box.delete("1.0", tk.END)
        box.insert(tk.END, f"window #{lane.window_uid}\n", "field")
        start = span.get("focused_at")
        end = span.get("ended_at")
        box.insert(tk.END, f"focus: {_fmt_ts(start)} → {_fmt_ts(end)}\n")

        if lane.events:
            start_t = start or 0
            end_t = end or start_t
            events = [e for e in lane.events if start_t <= e.ts <= end_t]
            if events:
                box.insert(tk.END, "\nEvents\n", "field")
                for e in events[:self.MAX_EVENTS_IN_HOVER]:
                    label = e.type if e.type != "clipboard" or not e.kind else f"{e.type}/{e.kind}"
                    box.insert(tk.END, f"[{_fmt_ts(e.ts)}] {label}: ", "time")
                    box.insert(tk.END, _truncate(e.text or "") + "\n")

    def _on_hover_image(self, data, err, tip, lbl, px, py):
        if err or not data or tip is None or not tip.winfo_exists():
            return
        try:
            img = Image.open(io.BytesIO(data))
            m = getattr(self.app.s, "hover_preview_max_dim", 900)
            if img.width > m or img.height > m:
                img.thumbnail((m, m), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001
            return
        lbl.configure(image=photo, text="")
        lbl.image = photo
        self._position_hover_tip(px, py)

    def _position_hover_tip(self, px, py):
        if self._hover_tip is None:
            return
        self._hover_tip.update_idletasks()
        tw, th = self._hover_tip.winfo_reqwidth(), self._hover_tip.winfo_reqheight()
        sw, sh = self.canvas.winfo_screenwidth(), self.canvas.winfo_screenheight()
        x = px + 20
        if x + tw > sw:
            x = px - tw - 20
        if x < 0:
            x = 0
        y = py + 20
        if y + th > sh:
            y = py - th - 20
        if y < 0:
            y = 0
        self._hover_tip.geometry(f"+{int(x)}+{int(y)}")

    def _destroy_hover_tip(self):
        self._cancel_hover()
        if self._hover_tip is not None:
            self._hover_tip.destroy()
            self._hover_tip = None
        self._preview_image_label = None

    def destroy(self):
        self._destroy_hover_tip()
        if self.frame is not None:
            self.frame.destroy()
