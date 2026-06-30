"""frontend/views.py — render builders for non-grid views (plan 08 §5).

The grid (Search/History results) lives in ``app.py`` next to its hover/tile
machinery; this module holds the **Timeline** renderer.  Like everything in the
frontend it is *pure presentation* — it draws the lanes the daemon already
computed (``GET /timeline``) and never derives spans/titles itself (08 §1).
"""
from __future__ import annotations

import logging

import tkinter as tk
from tkinter import ttk

log = logging.getLogger("dovw.fe.views")

LANE_H = 34
LABEL_W = 230
BAR_H = 18
PAD = 6


def render_timeline(app, lanes: list) -> None:
    """Draw one horizontal lane per window into ``app.inner_frame``.

    ``app`` is the WindowPreviewApp (we borrow its theme + jump_to + scope).
    Each lane shows focus spans as accent bars on a shared time axis, with title
    changes as tick marks; clicking a lane scopes search to that window.
    """
    theme = app.theme
    parent = app.inner_frame

    if not lanes:
        ttk.Label(parent, text="No activity in this time range.",
                  style="Muted.TLabel").grid(row=0, column=0, padx=12, pady=12, sticky="w")
        return

    # shared time axis across all lanes
    t_min, t_max = _time_bounds(lanes)
    span = max(1e-6, t_max - t_min)

    header = ttk.Frame(parent)
    header.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))
    ttk.Label(header, text=_fmt_range(t_min, t_max), style="Muted.TLabel").pack(side=tk.LEFT)

    width = max(600, app.canvas.winfo_width() - LABEL_W - 40)
    for i, lane in enumerate(lanes):
        _render_lane(app, parent, lane, i + 1, t_min, span, width, theme)

    parent.update_idletasks()
    app.canvas.configure(scrollregion=app.canvas.bbox("all"))


def _render_lane(app, parent, lane, row, t_min, span, width, theme):
    frame = ttk.Frame(parent, style="Tile.TFrame")
    frame.grid(row=row, column=0, sticky="ew", padx=PAD, pady=2)

    title = lane.current_title or "(no title)"
    app = lane.app_name or lane.wm_class or "?"
    label = f"{app} — {title}"
    if len(label) > 40:
        label = label[:40] + "…"
    accessible = bool(lane.alive) and bool(lane.jumpable)
    lab = ttk.Label(frame, text=label, width=34, anchor="w",
                    style="Alive.Tile.TLabel" if accessible else "Dead.Tile.TLabel")
    lab.pack(side=tk.LEFT, padx=(4, 6))

    canvas = tk.Canvas(frame, height=LANE_H, width=width, bg=theme["tile_bg"],
                       highlightthickness=0)
    canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def x_of(t):
        return PAD + (t - t_min) / span * (width - 2 * PAD)

    y = (LANE_H - BAR_H) // 2
    canvas.create_line(PAD, LANE_H // 2, width - PAD, LANE_H // 2, fill=theme["tile_border"])
    for sp in lane.focus_spans:
        start = sp.get("start") or sp.get("focused_at")
        end = sp.get("end") or sp.get("ended_at") or start
        if start is None:
            continue
        x0, x1 = x_of(start), x_of(max(end, start))
        canvas.create_rectangle(x0, y, max(x1, x0 + 2), y + BAR_H,
                                fill=theme["accent"], outline="")
    for tt in lane.titles:
        t = tt.get("changed_at")
        if t is None:
            continue
        x = x_of(t)
        canvas.create_line(x, y - 3, x, y + BAR_H + 3, fill=theme["mark_bg"])

    # click anywhere on the lane → scope search to this window (08 §5)
    def scope(_e=None, uid=lane.window_uid):
        app.open_window_scope(uid)
    def jump(_e=None, uid=lane.window_uid):
        app.jump_to(uid)
    canvas.bind("<Button-1>", scope)
    lab.bind("<Button-1>", scope)
    canvas.bind("<Button-3>", jump)
    lab.bind("<Button-3>", jump)


def _time_bounds(lanes):
    lo, hi = None, None
    for lane in lanes:
        for sp in lane.focus_spans:
            for key in ("start", "focused_at", "end", "ended_at"):
                v = sp.get(key)
                if v is None:
                    continue
                lo = v if lo is None else min(lo, v)
                hi = v if hi is None else max(hi, v)
        for tt in lane.titles:
            v = tt.get("changed_at")
            if v is None:
                continue
            lo = v if lo is None else min(lo, v)
            hi = v if hi is None else max(hi, v)
    if lo is None:
        lo, hi = 0.0, 1.0
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _fmt_range(t_min, t_max):
    import datetime
    a = datetime.datetime.fromtimestamp(t_min).strftime("%Y-%m-%d %H:%M")
    b = datetime.datetime.fromtimestamp(t_max).strftime("%H:%M")
    return f"{a} → {b}"
