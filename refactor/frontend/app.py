"""frontend/app.py — WindowPreviewApp: the thin Tk client (plan 08 §1-6).

Keeps the *look & interaction model* of
``reference_v2/demo-no-ocr-efficient-refresh.py`` — dark theme, scrollable grid
of tiles, hover-zoom preview, type-to-search — but **deletes all collection /
compute** (08 §1).  Every datum comes from the daemon API:

  * grid tiles ← ``GET /windows`` (no query) or ``GET /search`` (with query)
  * window_captures ← ``GET /windows/{uid}/window_capture/latest`` (bytes, lazy)
  * tile click → **jump to window** via ``POST /windows/{uid}/activate`` (08 §2)
  * Search ⇄ Timeline tabs over one **shared view-state** (08 §5)

Network calls run in a worker pool and marshal results back with
``self.after(0, …)`` — exactly the demo's capture pattern — so the Tk thread
never blocks (08 §6).  Search is debounced (~200 ms).
"""
from __future__ import annotations

import io
import logging
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import font as tkfont
from tkinter import ttk
import functools

from PIL import Image, ImageOps, ImageTk

from .apiclient import ApiClient, DaemonUnavailable, Window
from . import views
from ._font_setup import setup_fonts

# Register bundled Noto fonts with fontconfig before any Tk widget is created.
setup_fonts()

log = logging.getLogger("dovw.fe.app")


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert a '#RRGGBB' or '#RGB' hex colour to an (r,g,b) tuple."""
    c = hex_color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _fmt_last_access(ts: float | None) -> str:
    """Human-readable last-focus/access label for a tile."""
    if ts is None or ts <= 0:
        return "last: —"
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    if dt.date() == now.date():
        return f"last: {dt.strftime('%a %H:%M')}"
    if dt.year == now.year:
        return f"last: {dt.strftime('%a %d %b %H:%M')}"
    return f"last: {dt.strftime('%a %d %b %Y %H:%M')}"


def _fmt_duration(minutes: float | None) -> str:
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


def _fmt_usage(w: Window) -> str:
    """Compact short-window active-minutes label (5m/10m/30m/1d)."""
    vals = [getattr(w, label, None) for label in ("usage_5m", "usage_10m", "usage_30m", "usage_1d")]
    if all(v is None for v in vals):
        return "use: —"
    parts = []
    for label, v in zip(("5m", "10m", "30m", "1d"), vals):
        if v is None:
            parts.append(f"{label}:—")
        else:
            parts.append(f"{label}:{v:.1f}")
    return "use: " + " ".join(parts)


def _fmt_usage_summary(w: Window) -> str:
    """Total focused time + focus score on a separate line."""
    total = getattr(w, "usage_total", None)
    score = getattr(w, "focus_score", None)
    if total is None and score is None:
        return ""
    parts = []
    if total is not None:
        parts.append(f"tot:{_fmt_duration(total)}")
    if score is not None:
        parts.append(f"score:{score:.2f}")
    return " | ".join(parts)


def _fmt_ts(ts: float | None) -> str:
    """ISO-style timestamp for event/keyboard chunks in the hover detail view."""
    if ts is None or ts <= 0:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _truncate(text: str | None, n: int = 140) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[:n].rstrip() + "…"


ALL_FIELDS = ["title", "app_name", "clipboard", "selection", "keyboard"]
SORT_OPTIONS = [
    ("last access", "last_access"),
    ("focus score", "focus_score"),
    ("title", "title"),
    ("window id", "window_id"),
    ("5m usage", "usage_5m"),
    ("10m usage", "usage_10m"),
    ("30m usage", "usage_30m"),
    ("1d usage", "usage_1d"),
    ("total usage", "usage_total"),
]
SORT_LABELS = {v: k for k, v in SORT_OPTIONS}
HOVER_POLL_MS = 120
HOVER_MOVE_THRESHOLD_PX = 6

HELP_TEXT = """\
Desktop Overview — Keyboard & Mouse Shortcuts

Global
  ?               Open this help window.
  Shift           Hold Shift to "freeze" a hover preview so you can move the
                  mouse into it without it disappearing.

Search view
  Type            Focus the search box and search instantly.
  Enter           Apply the search.
  Ctrl+A          Select all text in the search box.
  Left-click tile Jump to / activate that window.
  Hover image     Show a larger screenshot preview.
  Hover metadata  Show window details, usage, and recent hits.

Timeline view
  Left / Right         Move the red indicator line backward / forward.
  Shift + Left / Right Move the indicator 10x faster.
  Up / Down            Zoom in / out around the red line.
  Ctrl + wheel         Zoom in / out at the cursor position.
  Shift + wheel        Pan the time axis left / right.
  Wheel                Scroll lanes up / down.
  Right-drag           Pan the time axis.
  Left-drag red line   Move the indicator to a chosen time.
  Left-click lane label
                       Jump to / activate that window.
  Hover focus span     Show details + thumbnail for that focus period.
  Hover lane label     Show a window summary.

Control knobs
  Fields          Which fields the search checks (title, app, clipboard,
                  selection, keyboard).
  Show            alive only / dead only / both.
  all fields      Show every window, or only windows with search hits.
  current desktop Hide windows not on the current virtual desktop.
  hide self       Hide this Desktop Overview window from results.
  current boot    Only show windows from the current boot session.
  Sort / Order    Order the grid or timeline lanes.
  Zoom (s/px)     Timeline scale: seconds per pixel.
  Red line        Exact timestamp of the vertical indicator; editable.
  Fit             Scale the timeline so the full data range fits the window.
  Now             Jump the red line to the current time.

Status colors
  ● accessible    Window is alive and can be jumped to.
  ○ dead          Window is no longer alive.
  ○ other session Window exists but is not jumpable from here.
"""


def _palette(theme: dict) -> dict:
    """Expand config's theme into the full set of keys the demo UI used."""
    return {
        "bg": theme["bg"], "fg": theme["fg"],
        "tile_bg": theme["tile_bg"], "tile_border": theme["tile_border"],
        "accent": theme["accent"], "muted": theme["muted"],
        "mark_bg": theme["mark_bg"], "alive": theme["alive"], "dead": theme["dead"],
        "indicator": theme.get("indicator", "#ff4444"),
        "event_title": theme.get("event_title", "#4caf50"),
        "event_clipboard": theme.get("event_clipboard", "#ffcc00"),
        "event_selection": theme.get("event_selection", "#ffcc00"),
        "event_keyboard": theme.get("event_keyboard", "#2a5a8a"),
        "lifespan": theme.get("lifespan", "#333333"),
        "active_lane_bg": theme.get("active_lane_bg", "#331111"),
        "canvas_bg": theme["bg"], "entry_bg": theme["tile_bg"], "entry_fg": theme["fg"],
        "select_bg": theme["accent"], "tip_border": theme["accent"],
        "tip_bg": theme["bg"], "tiptitle_bg": theme["tile_bg"], "tiptitle_fg": theme["fg"],
    }


APP_GEOMETRY="1500x800"

class WindowPreviewApp(tk.Tk):
    def __init__(self, settings, client: ApiClient):
        super().__init__()
        self.s = settings
        self.api = client
        self.theme = _palette(settings.theme)
        self.title("Desktop Overview — search")
        self.geometry(APP_GEOMETRY)
        self.resizable(self.s.resizable, self.s.resizable)

        self._setup_fonts()
        self._apply_theme()

        # shared view-state (08 §5): the two tabs render the SAME state
        self.view_state = {
            "view": "search",        # 'search' | 'timeline'
            "q": "",
            "fields": list(ALL_FIELDS),
            "alive": "only",         # only | dead | both
            "hits": "hit_only",      # hit_only | all
            "from": None, "to": None,
            "sort": "last_access",
            "order": "desc",         # asc | desc
            "selected_window_uid": None,
            "filter_no_vdesktop": False,
            "hide_self": self.s.hide_self,
            "current_boot_only": True,
            "lane_title_filter": "",
        }
        # identify our own X window for the hide-self filter (Tk-only, no Xlib)
        self._self_xid = self._get_self_xid()
        self._self_title_prefix = self.title()
        self._history_stack: list[dict] = []
        self._timeline_view = None
        self._syncing_timeline = False
        self._resize_after = None
        self._last_size = (1200, 800)

        # tiles keyed by window_uid (08 §2: reconcile by uid, not raw x id)
        self.tiles: dict[int, dict] = {}
        self._last_order_uids: tuple[int, ...] = ()
        self._last_grid_cols: int = 0
        self._window_capture_cache: dict[int, ImageTk.PhotoImage] = {}
        self._preview_cache: dict[int, ImageTk.PhotoImage] = {}

        self.pool = ThreadPoolExecutor(max_workers=settings.request_worker_threads,
                                       thread_name_prefix="api")
        self._search_after = None
        self._refresh_after = None
        self._busy = False
        self._render_generation = 0

        # hover preview state (lifted from the demo)
        self._preview_tip = None
        self._preview_title_label = None
        self._preview_image_label = None
        self._preview_text = None
        self._preview_kind = None
        self._hover_uid = None
        self._still_since = 0.0
        self._last_pointer = (-1, -1)

        # image-hover screenshot navigation state
        self._preview_capture_ts: float | None = None

        # focus tracking: hide hover tips when the main UI loses focus
        self._focus_out_after: str | None = None

        # shift state for temporarily locking hover previews
        self._shift_count = 0

        self._build_controls()
        self._build_grid()
        self._build_banner()

        self.bind_all("<Key>", self.on_global_key)
        self.bind_all("<KeyPress-Shift_L>", self._on_shift_press)
        self.bind_all("<KeyPress-Shift_R>", self._on_shift_press)
        self.bind_all("<KeyRelease-Shift_L>", self._on_shift_release)
        self.bind_all("<KeyRelease-Shift_R>", self._on_shift_release)
        self.bind_all("<Button-4>", self.on_mousewheel)
        self.bind_all("<Button-5>", self.on_mousewheel)
        self.bind_all("<MouseWheel>", self.on_mousewheel)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._on_root_configure)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<FocusIn>", self._on_focus_in)

        self.after(100, self.render)
        self._poll_hover()
        if settings.grid_auto_refresh_s:
            self._schedule_auto_refresh()
        log.info("GUI initialized")

    # ───────────────────────── appearance (from demo) ─────────────────────────
    def _setup_fonts(self):
        fam = self.s.font_family
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
            except tk.TclError:
                continue
            f.configure(size=self.s.font_size)
            if fam and fam != "TkDefaultFont":
                f.configure(family=fam)
        # Use the bundled monospace font for fixed-width widgets.
        try:
            fixed = tkfont.nametofont("TkFixedFont")
            fixed.configure(family="Noto Sans Mono", size=self.s.font_size)
        except tk.TclError:
            pass
        base = tkfont.nametofont("TkDefaultFont").cget("family")
        self.search_font = tkfont.Font(family=base, size=self.s.font_size + 6)
        self.title_font = tkfont.Font(family=base, size=self.s.font_size + 4, weight="bold")
        self.meta_font = tkfont.Font(family=base, size=max(8, self.s.font_size - 1))

    def _apply_theme(self):
        c = self.theme
        self.configure(bg=c["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=c["bg"], foreground=c["fg"],
                        fieldbackground=c["entry_bg"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Tile.TFrame", background=c["tile_bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("Tile.TLabel", background=c["tile_bg"], foreground=c["fg"])
        style.configure("Muted.TLabel", background=c["tile_bg"], foreground=c["muted"])
        style.configure("Alive.Tile.TLabel", background=c["tile_bg"], foreground=c["alive"])
        style.configure("Dead.Tile.TLabel", background=c["tile_bg"], foreground=c["dead"])
        style.configure("TButton", background=c["tile_bg"], foreground=c["fg"])
        style.map("TButton", background=[("active", c["select_bg"])])
        style.configure("Accent.TButton", background=c["accent"], foreground="#ffffff")
        style.configure("TEntry", fieldbackground=c["entry_bg"], foreground=c["entry_fg"],
                        insertcolor=c["fg"])
        style.configure("TCombobox", fieldbackground=c["entry_bg"], foreground=c["entry_fg"],
                        background=c["tile_bg"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["entry_bg"]), ("active", c["entry_bg"])],
                  foreground=[("readonly", c["entry_fg"]), ("active", c["entry_fg"])],
                  selectbackground=[("readonly", c["select_bg"])])
        # the popped listbox is a plain Listbox widget; set its colors via option db
        self.option_add("*TCombobox*Listbox.background", c["entry_bg"])
        self.option_add("*TCombobox*Listbox.foreground", c["entry_fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", c["select_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", c["entry_fg"])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"])
        # native notebook tabs: dark tiles with white text, selected matches page bg
        style.configure("TNotebook", background=c["bg"], tabmargins=[0, 0, 0, 0])
        style.configure("TNotebook.Tab", background=c["tile_bg"], foreground="#ffffff",
                        padding=[10, 2])
        style.map("TNotebook.Tab",
                  background=[("selected", c["bg"]), ("active", c["select_bg"])],
                  foreground=[("selected", "#ffffff"), ("active", "#ffffff")])

    def _on_focus_out(self, _event=None):
        self._focus_out_after = self.after(50, self._check_app_focus_lost)

    def _on_focus_in(self, _event=None):
        if self._focus_out_after is not None:
            self.after_cancel(self._focus_out_after)
            self._focus_out_after = None

    def _check_app_focus_lost(self):
        self._focus_out_after = None
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            # Combobox/menu popdowns can make focus_get() fail transiently.
            return
        if focused is None:
            log.debug("main UI lost focus; destroying hover tips")
            self._destroy_tip()
            view = getattr(self, "_timeline_view", None)
            if view is not None:
                view._destroy_hover_tip()

    def _is_shift_held(self) -> bool:
        return self._shift_count > 0

    def _on_shift_press(self, _event=None):
        self._shift_count += 1

    def _on_shift_release(self, _event=None):
        self._shift_count = max(0, self._shift_count - 1)
        # Record the current pointer location so releasing Shift does not count
        # as a sudden mouse movement.
        try:
            self._last_pointer = (self.winfo_pointerx(), self.winfo_pointery())
        except Exception:                               # noqa: BLE001
            pass

    def _pointer_in_tip(self, tip: tk.Toplevel | None) -> bool:
        """True if the pointer is currently inside the given hover Toplevel."""
        if tip is None or not tip.winfo_exists() or not tip.winfo_viewable():
            return False
        px, py = self.winfo_pointerx(), self.winfo_pointery()
        x1 = tip.winfo_rootx()
        y1 = tip.winfo_rooty()
        x2 = x1 + tip.winfo_width()
        y2 = y1 + tip.winfo_height()
        return x1 <= px < x2 and y1 <= py < y2

    def _get_self_xid(self) -> str | None:
        """Return this Tk window's X11 id as '0xXXXXXXXX', or None if unknown."""
        try:
            # ensure the window is realized before reading its X id
            self.update_idletasks()
            wid = self.winfo_id()
            if wid:
                return f"0x{int(wid):08x}"
        except Exception as exc:                           # noqa: BLE001
            log.debug("could not get own X window id: %s", exc)
        return None

    def _refresh_self_xid(self):
        """Re-read our own X id once the window is mapped; called before first render."""
        if self._self_xid is None:
            self._self_xid = self._get_self_xid()
            log.debug("self x_window_id=%s title=%r", self._self_xid, self.title())

    def _is_self(self, w) -> bool:
        """True if the given Window/TimelineLane is this GUI's own alive window."""
        if not self.view_state.get("hide_self", True):
            return False
        if not getattr(w, "alive", False):
            return False
        xid = getattr(w, "x_window_id", None)
        if xid and self._self_xid and xid.lower() == self._self_xid.lower():
            return True
        return False

    # ───────────────────────── controls ─────────────────────────
    def _build_controls(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=6, pady=(6, 0))

        self.back_btn = ttk.Button(bar, text="◀ Back", command=self.go_back)
        if self.s.show_back_button:
            self.back_btn.pack(side=tk.LEFT)

        self._search_label = ttk.Label(bar, text="Search:")
        self._search_label.pack(side=tk.LEFT, padx=(12, 4))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, font=self.search_font)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=6)
        self.search_entry.bind("<KeyRelease>", self.on_search_key)
        self.search_entry.bind("<Control-a>", self._select_all)

        # non-editable timeline viewable range label, shown in place of the search box
        self._timeline_range_var = tk.StringVar(value="")
        self._timeline_range_lbl = ttk.Label(
            bar, textvariable=self._timeline_range_var, font=self.search_font)

        # lane title filter (timeline-only, frontend-only AND search), placed like the search box
        self._timeline_top_filters: list[tk.Widget] = []
        lane_filter_lbl = ttk.Label(bar, text="Lanes:")
        self._timeline_top_filters.append(lane_filter_lbl)
        self.lane_filter_var = tk.StringVar(value=self.view_state.get("lane_title_filter", ""))
        self.lane_filter_var.trace_add("write", self._on_lane_filter_changed)
        self.lane_filter_entry = ttk.Entry(bar, textvariable=self.lane_filter_var,
                                           font=self.search_font, width=30)
        self.lane_filter_entry.bind("<Control-a>", self._select_all)
        self._timeline_top_filters.append(self.lane_filter_entry)
        self.lane_filter_clear_btn = ttk.Button(bar, text="×", width=2,
                                                command=self._clear_lane_filter)
        self._timeline_top_filters.append(self.lane_filter_clear_btn)

        self.help_btn = ttk.Button(bar, text="?", width=2, command=self._show_help)
        self.help_btn.pack(side=tk.RIGHT, padx=4)
        self.refresh_btn = ttk.Button(bar, text="Refresh", command=self.on_refresh)
        self.refresh_btn.pack(side=tk.RIGHT, padx=4)
        self.status_var = tk.StringVar(value="ready")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.RIGHT, padx=8)

        # filter row (08 §4): fields (search-only), liveness, hits toggle, scope reset
        flt = ttk.Frame(self)
        flt.pack(fill=tk.X, padx=6, pady=(2, 6))
        self._flt = flt
        self._search_only_filters: list[tk.Widget] = []

        self._fields_label = ttk.Label(flt, text="Fields:")
        self._fields_label.pack(side=tk.LEFT)
        self.field_vars = {}
        self._field_cbs = []
        for f in ALL_FIELDS:
            v = tk.BooleanVar(value=True)
            self.field_vars[f] = v
            cb = ttk.Checkbutton(flt, text=f, variable=v, command=self._apply_filters)
            cb.pack(side=tk.LEFT, padx=2)
            self._field_cbs.append(cb)

        ttk.Label(flt, text="   Show:").pack(side=tk.LEFT)
        self.alive_var = tk.StringVar(value="only")
        for label, val in [("alive", "only"), ("dead", "dead"), ("both", "both")]:
            ttk.Radiobutton(flt, text=label, value=val, variable=self.alive_var,
                            command=self._apply_filters).pack(side=tk.LEFT, padx=2)
        self._show_alive = self.alive_var.get

        self.hits_var = tk.StringVar(value="hit_only")
        self._hits_cb = ttk.Checkbutton(flt, text="all fields", onvalue="all", offvalue="hit_only",
                                        variable=self.hits_var, command=self._apply_filters)
        self._hits_cb.pack(side=tk.LEFT, padx=(8, 2))

        self.filter_no_vdesktop_var = tk.BooleanVar(value=self.s.filter_no_vdesktop)
        self._vdesktop_cb = ttk.Checkbutton(flt, text="current desktop",
                                            variable=self.filter_no_vdesktop_var,
                                            command=self._apply_filters)
        self._vdesktop_cb.pack(side=tk.LEFT, padx=(8, 2))

        self.scope_var = tk.StringVar(value="")
        self.scope_label = ttk.Label(flt, textvariable=self.scope_var, style="Muted.TLabel")
        self.scope_label.pack(side=tk.LEFT, padx=8)

        # hide self (kept in frontend so it works regardless of daemon title capture)
        self.hide_self_var = tk.BooleanVar(value=self.view_state["hide_self"])
        self._hide_self_cb = ttk.Checkbutton(flt, text="hide self", variable=self.hide_self_var,
                                             command=self._apply_filters)
        self._hide_self_cb.pack(side=tk.LEFT, padx=(8, 2))

        # current boot session filter (search + timeline)
        self.current_boot_var = tk.BooleanVar(value=self.view_state.get("current_boot_only", True))
        self._current_boot_cb = ttk.Checkbutton(flt, text="current boot",
                                                variable=self.current_boot_var,
                                                command=self._apply_filters)
        self._current_boot_cb.pack(side=tk.LEFT, padx=(8, 2))

        # sort / order (applies to search grid and timeline lanes)
        ttk.Label(flt, text="   Sort:").pack(side=tk.LEFT)
        self.sort_var = tk.StringVar(value="last_access")
        self.sort_combo = ttk.Combobox(flt, textvariable=self.sort_var, state="readonly",
                                       values=[v for _l, v in SORT_OPTIONS], width=14)
        self.sort_combo.bind("<<ComboboxSelected>>", self._on_sort_changed)
        self.sort_combo.pack(side=tk.LEFT, padx=2)
        self.order_var = tk.StringVar(value="desc")
        self.order_combo = ttk.Combobox(flt, textvariable=self.order_var, state="readonly",
                                        values=["asc", "desc"], width=6)
        self.order_combo.bind("<<ComboboxSelected>>", self._on_sort_changed)
        self.order_combo.pack(side=tk.LEFT, padx=2)

        # timeline-specific controls (zoom + red-line time); shown only in timeline view, on the same row
        self._timeline_filters: list[tk.Widget] = []
        tl_pad = ttk.Label(flt, text="   ")
        self._timeline_filters.append(tl_pad)
        tl_zoom_lbl = ttk.Label(flt, text="Zoom (s/px):")
        self._timeline_filters.append(tl_zoom_lbl)
        self.scale_var = tk.StringVar(value="1")
        self.scale_entry = ttk.Entry(flt, textvariable=self.scale_var, width=12, justify="right")
        self.scale_entry.bind("<Return>", self._on_scale_entry)
        self.scale_entry.bind("<FocusOut>", self._on_scale_entry)
        self._timeline_filters.append(self.scale_entry)
        tl_time_lbl = ttk.Label(flt, text="Red line:")
        self._timeline_filters.append(tl_time_lbl)
        self.indicator_var = tk.StringVar(value="")
        self.indicator_entry = ttk.Entry(flt, textvariable=self.indicator_var, width=20)
        self.indicator_entry.bind("<Return>", self._on_indicator_entry)
        self.indicator_entry.bind("<FocusOut>", self._on_indicator_entry)
        self._timeline_filters.append(self.indicator_entry)
        self.fit_btn = ttk.Button(flt, text="Fit", command=self._fit_timeline)
        self._timeline_filters.append(self.fit_btn)
        self.now_btn = ttk.Button(flt, text="Now", command=self._now_timeline)
        self._timeline_filters.append(self.now_btn)

        # native tabbed container: each view keeps its own canvas and scroll position
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.search_frame = ttk.Frame(self.notebook)
        self.timeline_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.search_frame, text="Search")
        self.notebook.add(self.timeline_frame, text="Timeline")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

    def _build_grid(self):
        self.canvas = tk.Canvas(self.search_frame, bg=self.theme["canvas_bg"], highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.search_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner_frame = ttk.Frame(self.canvas)
        self._inner_window = self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.inner_frame.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_canvas_configure(self, event=None):
        """Keep the search inner_frame sized to the viewport width."""
        width = event.width if event else self.canvas.winfo_width()
        self.canvas.itemconfig(self._inner_window, width=width)

    def _build_banner(self):
        self.banner = ttk.Label(self, anchor="center", style="Dead.Tile.TLabel")
        # packed only when the daemon is unreachable (08 §7)

    # ───────────────────────── worker plumbing (08 §6) ─────────────────────────
    def _submit(self, fn, on_done, *args):
        """Run ``fn(*args)`` off the Tk thread; deliver result via after(0,…)."""
        def worker():
            try:
                res = fn(*args)
                # self.after(0, lambda: on_done(res, None))
                self.after(0, functools.partial(on_done, res, None))
            except DaemonUnavailable as exc:
                # self.after(0, lambda: on_done(None, exc))
                self.after(0, functools.partial(on_done, None, exc))
            except Exception as exc:                       # noqa: BLE001
                log.warning("api call failed: %s", exc)
                # self.after(0, lambda: on_done(None, exc))
                self.after(0, functools.partial(on_done, None, exc))
        self.pool.submit(worker)

    def _show_banner(self, msg: str):
        self.banner.configure(text=msg)
        self.banner.pack(fill=tk.X, side=tk.BOTTOM)

    def _hide_banner(self):
        self.banner.pack_forget()

    # ───────────────────────── render dispatch (08 §5) ─────────────────────────
    def render(self):
        self._refresh_self_xid()
        self._render_generation += 1
        if self.view_state["view"] == "timeline":
            self._render_timeline()
        else:
            self._render_search()

    def _on_root_configure(self, event):
        if event.widget is not self:
            return
        size = (event.width, event.height)
        if size == self._last_size:
            return
        self._last_size = size
        if self._resize_after is not None:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(300, self.render)

    def switch_view(self, view: str):
        """Public alias used by external callers (kept for compatibility)."""
        self._switch_to_view(view, push_history=True)

    def _switch_to_view(self, view: str, push_history: bool = True, clear_scope: bool = True):
        """Change the active view, clearing any stale hover/render state."""
        if view == self.view_state["view"]:
            return
        if push_history:
            self._push_history()
        # Bump the generation so any in-flight render result for the old view is ignored.
        self._render_generation += 1
        self.view_state["view"] = view
        # Clear scope when switching via tabs (only meaningful when set explicitly).
        if clear_scope:
            self.view_state["selected_window_uid"] = None
            self.scope_var.set("")
        self.title(f"Desktop Overview — {view}")
        self._self_title_prefix = self.title()
        self._show_search_filters(view == "search")
        self._sync_notebook_tab()
        self.focus()
        if view == "search":
            self._on_canvas_configure()
        self._destroy_tip()
        view_obj = getattr(self, "_timeline_view", None)
        if view_obj is not None:
            view_obj._destroy_hover_tip()
        log.info("switch view -> %s", view)
        self.render()

    def _on_notebook_tab_changed(self, _event=None):
        idx = self.notebook.index(self.notebook.select())
        view = "timeline" if idx == 1 else "search"
        if view == self.view_state["view"]:
            return
        self._switch_to_view(view, push_history=True)

    def _sync_notebook_tab(self):
        if not getattr(self, "notebook", None):
            return
        if self.view_state["view"] == "timeline":
            self.notebook.select(self.timeline_frame)
        else:
            self.notebook.select(self.search_frame)

    def _wrap_callback(self, generation: int, view: str, callback):
        """Drop results from a render that was superseded or switched away from."""
        def wrapped(res, err):
            if not self.winfo_exists():
                return
            if generation != self._render_generation or self.view_state.get("view") != view:
                log.debug("stale render result ignored gen=%d view=%s current=%s",
                          generation, view, self.view_state.get("view"))
                return
            callback(res, err)
        return wrapped

    def _render_search(self):
        st = self.view_state
        self.status_var.set("loading…")
        q = st["q"].strip() or None
        scope = st["selected_window_uid"]
        self_xid = self._self_xid if st.get("hide_self") else None
        log.debug("render_search q=%r scope=%s t_from=%s t_to=%s", q, scope, st["from"], st["to"])

        def call():
            if q is None and scope is None and st["from"] is None and st["to"] is None:
                return self.api.windows(sort=st["sort"], order=st["order"], alive=st["alive"],
                                        self_xid=self_xid,
                                        current_boot_only=st.get("current_boot_only"),
                                        current_vdesktop_only=st.get("filter_no_vdesktop"))
            return self.api.search(q=q, window_uid=scope, fields=st["fields"],
                                   alive=st["alive"], sort=st["sort"], order=st["order"],
                                   hits=st["hits"], t_from=st["from"], t_to=st["to"],
                                   self_xid=self_xid, mode="mixed",
                                   current_boot_only=st.get("current_boot_only"),
                                   current_vdesktop_only=st.get("filter_no_vdesktop"))
        gen = self._render_generation
        self._submit(call, self._wrap_callback(gen, "search", self._on_search_results))

    def _on_search_results(self, windows, err):
        if err is not None:
            self._on_error(err)
            return
        windows = [w for w in windows if not self._is_self(w)]
        self._hide_banner()
        self.status_var.set(f"{len(windows)} window(s)")
        log.debug("search returned %d windows", len(windows))
        self._reconcile_tiles(windows)

    def _render_timeline(self):
        st = self.view_state
        self.status_var.set("loading timeline…")
        self_xid = self._self_xid if st.get("hide_self") else None

        def call():
            return self.api.timeline(window_uid=st["selected_window_uid"],
                                     sort=st["sort"], order=st["order"],
                                     t_from=st["from"], t_to=st["to"],
                                     self_xid=self_xid,
                                     current_boot_only=st.get("current_boot_only"),
                                     current_vdesktop_only=st.get("filter_no_vdesktop"))
        gen = self._render_generation
        self._submit(call, self._wrap_callback(gen, "timeline", self._on_timeline_results))

    def _on_timeline_results(self, lanes, err):
        if err is not None:
            self._on_error(err)
            return
        n_before = len(lanes)
        alive_mode = self.view_state.get("alive", "only")
        if alive_mode == "only":
            lanes = [l for l in lanes if l.alive]
        elif alive_mode == "dead":
            lanes = [l for l in lanes if not l.alive]
        removed = n_before - len(lanes)
        if removed:
            log.debug("timeline filters removed %d lane(s)", removed)
        lanes = [l for l in lanes if not self._is_self(l)]
        self._hide_banner()
        self.status_var.set(f"{len(lanes) if lanes else 0} lane(s)")
        view = views.render_timeline(self, lanes or [], parent=self.timeline_frame)
        if view is not None:
            view.scale_callback = self._on_timeline_scale
            view.indicator_callback = self._on_timeline_indicator
            view.range_callback = self._on_timeline_range
            query = self.view_state.get("lane_title_filter", "")
            if query:
                self.lane_filter_var.set(query)
                view.set_title_filter(query)
            self.status_var.set(f"{len(view.lanes)} lane(s)")

    def _on_timeline_range(self, t0: float, t1: float):
        self._timeline_range_var.set(f"{_fmt_ts(t0)}  →  {_fmt_ts(t1)}")

    def _on_timeline_scale(self, scale: float):
        self.view_state["t_scale"] = scale
        if self._syncing_timeline:
            return
        self._syncing_timeline = True
        self.scale_var.set(f"{scale:.3f}".rstrip("0").rstrip("."))
        self._syncing_timeline = False

    def _on_timeline_indicator(self, ts: float):
        self.view_state["t_indicator"] = ts
        if self._syncing_timeline:
            return
        self._syncing_timeline = True
        self.indicator_var.set(_fmt_ts(ts))
        self._syncing_timeline = False

    def _on_scale_entry(self, _event=None):
        if self._syncing_timeline:
            return
        view = getattr(self, "_timeline_view", None)
        if view is None:
            return
        try:
            scale = float(self.scale_var.get().strip())
        except ValueError:
            self._flash_entry(self.scale_entry)
            self._syncing_timeline = True
            self.scale_var.set(f"{view.scale:.3f}".rstrip("0").rstrip("."))
            self._syncing_timeline = False
            return
        view.set_scale(scale)

    def _on_indicator_entry(self, _event=None):
        if self._syncing_timeline:
            return
        view = getattr(self, "_timeline_view", None)
        if view is None:
            return
        ts = view.parse_indicator_str(self.indicator_var.get())
        if ts is None:
            self._flash_entry(self.indicator_entry)
            self._syncing_timeline = True
            self.indicator_var.set(_fmt_ts(view.indicator_time))
            self._syncing_timeline = False
            return
        view.set_indicator_time(ts)

    def _fit_timeline(self):
        view = getattr(self, "_timeline_view", None)
        if view is not None:
            view.fit()

    def _now_timeline(self):
        view = getattr(self, "_timeline_view", None)
        if view is not None:
            view.jump_to_now()

    def _on_lane_filter_changed(self, *_args):
        """Apply the frontend-only lane title filter, preserving zoom and red line."""
        query = self.lane_filter_var.get()
        self.view_state["lane_title_filter"] = query
        view = getattr(self, "_timeline_view", None)
        if view is not None:
            view.set_title_filter(query)
            self.status_var.set(f"{len(view.lanes)} lane(s)")

    def _clear_lane_filter(self):
        self.lane_filter_var.set("")

    def _flash_entry(self, entry):
        old = entry.cget("foreground")
        entry.configure(foreground="#ff0000")
        self.after(300, lambda: entry.configure(foreground=old))

    def _on_error(self, err):
        if isinstance(err, DaemonUnavailable):
            sock = self.s.tcp_endpoint if self.s.use_tcp else self.s.socket_path
            self._show_banner(f"⚠ daemon not running — start it with:  python -m daemon\n({sock})")
            self.status_var.set("daemon unavailable")
        else:
            self.status_var.set(f"error: {err}")

    # ───────────────────────── grid tiles (08 §2) ─────────────────────────
    def _clear_view(self):
        """Destroy every child widget in inner_frame (tiles, timeline lanes, etc)."""
        for child in list(self.inner_frame.winfo_children()):
            child.destroy()
        self.tiles.clear()
        self._last_order_uids = ()
        self._timeline_view = None
        # Reset grid weights so a later timeline view gets the full width
        # instead of sharing columns with a now-destroyed search grid.
        for c in range(self._last_grid_cols):
            self.inner_frame.columnconfigure(c, weight=0)
        self._last_grid_cols = 0

    def _clear_grid(self):
        """Legacy — kept for callers that predate _clear_view."""
        self._clear_view()

    def _reconcile_tiles(self, windows: list[Window]):
        """Add/remove/keep tiles keyed by window_uid; render in API order (08 §2).

        To avoid visual shuffling on every refresh, existing tiles are only
        re-gridded when the desired UID order actually changes.  Content is still
        updated every render.  The hover preview is closed only if its window
        disappears; otherwise its image is refreshed in place.
        """
        seen = {w.window_uid for w in windows}
        order_uids = tuple(w.window_uid for w in windows)
        reorder = order_uids != self._last_order_uids

        # Close hover preview only if the hovered window is no longer in results.
        if self._hover_uid is not None and self._hover_uid not in seen:
            self._destroy_tip()
            self._hover_uid = None

        for uid in list(self.tiles):
            if uid not in seen:
                self.tiles[uid]["frame"].destroy()
                self.tiles.pop(uid, None)

        for w in windows:
            tile = self.tiles.get(w.window_uid)
            if tile is None:
                tile = self._create_tile(w)
                self.tiles[w.window_uid] = tile
                reorder = True   # a new tile has no grid position yet
            else:
                self._update_tile(tile, w)
                old_ts = tile.get("capture_ts")
                if w.window_capture_ts is not None and w.window_capture_ts != old_ts:
                    log.debug("reconcile: uid=%d capture_ts changed %s -> %s, re-fetch",
                              w.window_uid, old_ts, w.window_capture_ts)
                    self._window_capture_cache.pop(w.window_uid, None)
                    self._preview_cache.pop(w.window_uid, None)
                    tile.pop("capture_ts", None)
                    self._load_window_capture(tile, w)
                    if self._hover_uid == w.window_uid:
                        self._update_preview_image(w)

        if reorder and windows:
            cols = self.s.grid_columns or max(1, self.canvas.winfo_width() // (self.s.grid_tile_width + 20) or 4)
            self._last_grid_cols = cols
            for i, w in enumerate(windows):
                tile = self.tiles[w.window_uid]
                r, c = divmod(i, cols)
                tile["frame"].grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
            for c in range(cols):
                self.inner_frame.columnconfigure(c, weight=1)
            self._last_order_uids = order_uids
        elif not windows:
            self._last_order_uids = ()

        self.inner_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _create_tile(self, w: Window) -> dict:
        c = self.theme
        tw = self.s.grid_tile_width
        th = self.s.grid_tile_height
        frame = ttk.Frame(self.inner_frame, style="Tile.TFrame", relief=tk.RAISED, borderwidth=1,
                          width=tw)
        img_label = ttk.Label(frame, style="Tile.TLabel")
        img_label.pack()
        app_label = ttk.Label(frame, style="Muted.TLabel", wraplength=tw - 12)
        app_label.pack(anchor="w")
        title_label = ttk.Label(frame, style="Tile.TLabel", wraplength=tw - 12)
        title_label.pack(anchor="w")
        access_label = ttk.Label(frame, style="Muted.TLabel")
        access_label.pack(anchor="w")
        usage_label = ttk.Label(frame, style="Muted.TLabel")
        usage_label.pack(anchor="w")
        total_score_label = ttk.Label(frame, style="Muted.TLabel")
        total_score_label.pack(anchor="w")
        badge_label = ttk.Label(frame, style="Muted.TLabel")
        badge_label.pack(anchor="w")
        status_label = ttk.Label(frame, style="Tile.TLabel")
        status_label.pack(anchor="w")
        hits_box = tk.Text(frame, height=3, width=34, wrap="word", bd=0,
                           bg=c["tile_bg"], fg=c["fg"], highlightthickness=0)
        hits_box.tag_configure("mark", background=c["mark_bg"])
        hits_box.tag_configure("field", foreground=c["accent"])

        tile = {"frame": frame, "img_label": img_label,
                "app_label": app_label, "title_label": title_label,
                "access_label": access_label, "usage_label": usage_label,
                "total_score_label": total_score_label,
                "badge_label": badge_label, "status_label": status_label,
                "hits_box": hits_box, "win": w}

        def jump(_e=None, uid=w.window_uid):
            self.jump_to(uid)
        for widget in (frame, img_label, title_label, app_label):
            widget.bind("<Button-1>", jump)
        self._update_tile(tile, w)
        self._load_window_capture(tile, w)
        return tile

    def _update_tile(self, tile: dict, w: Window):
        tile["win"] = w
        tile["app_label"].configure(text=w.app_name or w.wm_class or "")
        title = w.current_title or "(no title)"
        tile["title_label"].configure(text=(title[:48] + "…") if len(title) > 48 else title)
        tile["access_label"].configure(text=_fmt_last_access(w.last_access))
        tile["usage_label"].configure(text=_fmt_usage(w))
        tile["total_score_label"].configure(text=_fmt_usage_summary(w))
        tile["badge_label"].configure(text=w.desktop_badge)
        if w.alive and w.jumpable:
            tile["status_label"].configure(text="● accessible", style="Alive.Tile.TLabel")
        else:
            reason = "dead" if not w.alive else "other session"
            tile["status_label"].configure(text=f"○ {reason}", style="Dead.Tile.TLabel")
        self._fill_hits(tile, w)

    def _fill_hits(self, tile: dict, w: Window):
        box = tile["hits_box"]
        box.configure(state="normal")
        box.delete("1.0", tk.END)
        if w.hits:
            for h in w.hits:
                box.insert(tk.END, f"{h.field}: ", "field")
                self._insert_marked(box, (h.excerpt or "").replace("\n", " "))
                box.insert(tk.END, "\n")
            if not box.winfo_ismapped():
                box.pack(anchor="w", fill=tk.X, pady=(2, 0))
        else:
            box.pack_forget()
        box.configure(state="disabled")

    def _insert_marked(self, box: tk.Text, text: str):
        """Render daemon-provided <mark>…</mark> excerpts as a highlight tag (08 §4)."""
        i = 0
        while True:
            start = text.find("<mark>", i)
            if start < 0:
                box.insert(tk.END, text[i:])
                break
            box.insert(tk.END, text[i:start])
            end = text.find("</mark>", start)
            if end < 0:
                box.insert(tk.END, text[start + 6:])
                break
            box.insert(tk.END, text[start + 6:end], "mark")
            i = end + 7

    def _load_window_capture(self, tile: dict, w: Window):
        if w.window_capture_url is None:
            return
        cached = self._window_capture_cache.get(w.window_uid)
        if cached is not None:
            tile["img_label"].configure(image=cached)
            tile["img_label"].image = cached
            tile["capture_ts"] = w.window_capture_ts
            return

        def fetch():
            return self.api.get_bytes(w.window_capture_url)

        def done(data, err):
            if not data or tile["frame"].winfo_exists() == 0:
                return
            try:
                img = Image.open(io.BytesIO(data))
                size = (self.s.grid_tile_width, self.s.grid_tile_height)
                bg = _hex_to_rgb(self.theme["tile_bg"])
                img = ImageOps.pad(img, size, color=bg)
                photo = ImageTk.PhotoImage(img)
            except Exception as exc:                       # noqa: BLE001
                log.debug("window_capture decode failed uid=%s: %s", w.window_uid, exc)
                return
            self._window_capture_cache[w.window_uid] = photo
            tile["capture_ts"] = w.window_capture_ts
            tile["img_label"].configure(image=photo)
            tile["img_label"].image = photo
        self._submit(fetch, done)

    # ───────────────────────── actions ─────────────────────────
    def jump_to(self, uid: int):
        log.info("jump to window_uid=%s", uid)
        self.status_var.set("jumping…")

        def done(res, err):
            if err is not None:
                self._on_error(err)
                return
            if res.get("ok"):
                self.status_var.set("jumped")
            else:
                self.status_var.set(f"can't jump: {res.get('reason')}")
        self._submit(lambda: self.api.activate(uid), done)

    def open_window_scope(self, uid: int):
        """Right-click → 'search this window': scope by window_uid, do NOT copy title (08 §5)."""
        self._push_history()
        self.view_state["selected_window_uid"] = uid
        self.view_state["q"] = ""
        self.search_var.set("")
        self.scope_var.set(f"scoped to window #{uid}  (clear ✕)")
        self.scope_label.bind("<Button-1>", lambda e: self.clear_scope())
        self.render()

    def clear_scope(self):
        self.view_state["selected_window_uid"] = None
        self.scope_var.set("")
        self.render()

    def on_refresh(self):
        log.debug("manual refresh triggered")
        self.status_var.set("refreshing window_captures…")
        self._window_capture_cache.clear()

        def done(res, err):
            if err is not None:
                self._on_error(err)
                return
            self.status_var.set(f"captured {res.get('captured')}")
            self.render()
        self._submit(self.api.refresh_window_captures, done)

    def _show_help(self):
        """Open a non-modal help window with operation shortcuts and knob meanings."""
        if getattr(self, "_help_window", None) and self._help_window.winfo_exists():
            self._help_window.lift()
            self._help_window.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("Desktop Overview — Help")
        win.configure(bg=self.theme["bg"])
        win.transient(self)
        win.geometry("620x520")
        win.resizable(True, True)

        frame = ttk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        text = tk.Text(frame, wrap="word", bd=0,
                       bg=self.theme["tile_bg"], fg=self.theme["fg"],
                       highlightthickness=0, padx=8, pady=6,
                       font=("TkDefaultFont", 10))
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.tag_configure("heading", foreground=self.theme["accent"],
                           font=("TkDefaultFont", 10, "bold"))

        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        text.configure(yscrollcommand=sb.set)
        self._bind_text_scroll(text)
        self._block_wheel_on(win)

        for raw in HELP_TEXT.splitlines():
            line = raw.rstrip()
            if not line:
                text.insert(tk.END, "\n")
            elif not line.startswith(" "):
                text.insert(tk.END, line + "\n", "heading")
            else:
                text.insert(tk.END, line + "\n")
        text.configure(state="disabled")

        btn = ttk.Button(win, text="Close", command=win.destroy)
        btn.pack(side=tk.BOTTOM, pady=(0, 8))

        def _on_close(_event=None):
            win.destroy()
            return "break"

        win.bind("<Escape>", _on_close)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        self._help_window = win
        btn.focus_set()

    # ───────────────────────── search box / filters ─────────────────────────
    def on_search_key(self, _event):
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(self.s.search_debounce_ms, self._commit_search)

    def _commit_search(self):
        self._search_after = None
        self.view_state["q"] = self.search_var.get()
        if self.view_state["view"] != "search":
            self._switch_to_view("search", push_history=False, clear_scope=False)
        else:
            self.render()

    def _apply_filters(self):
        self.view_state["fields"] = [f for f, v in self.field_vars.items() if v.get()] or list(ALL_FIELDS)
        self.view_state["alive"] = self.alive_var.get()
        self.view_state["hits"] = self.hits_var.get()
        self.view_state["filter_no_vdesktop"] = self.filter_no_vdesktop_var.get()
        self.view_state["hide_self"] = self.hide_self_var.get()
        self.view_state["current_boot_only"] = self.current_boot_var.get()
        self.render()

    def _on_sort_changed(self, _event=None):
        self.view_state["sort"] = self.sort_var.get()
        self.view_state["order"] = self.order_var.get()
        self.render()

    def _select_all(self, event):
        try:
            event.widget.selection_range(0, tk.END)
        except tk.TclError:
            pass
        return "break"

    def _show_search_filters(self, visible: bool):
        """Show/hide knobs that only apply to the current view."""
        if visible:
            self._search_label.pack(side=tk.LEFT, padx=(12, 4))
            self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=6)
            self._fields_label.pack(side=tk.LEFT)
            for cb in self._field_cbs:
                cb.pack(side=tk.LEFT, padx=2)
            self._hits_cb.pack(side=tk.LEFT, padx=(8, 2))
            self.scope_label.pack(side=tk.LEFT, padx=8)
            self._timeline_range_lbl.pack_forget()
            for w in self._timeline_top_filters:
                w.pack_forget()
            for w in self._timeline_filters:
                w.pack_forget()
        else:
            self._search_label.pack_forget()
            self.search_entry.pack_forget()
            self._fields_label.pack_forget()
            for cb in self._field_cbs:
                cb.pack_forget()
            self._hits_cb.pack_forget()
            self.scope_label.pack_forget()
            # lane filter sits at the left, range indicator expands to fill the rest
            for w in self._timeline_top_filters:
                w.pack(side=tk.LEFT, padx=2)
            self._timeline_range_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
            for w in self._timeline_filters:
                w.pack(side=tk.LEFT, padx=2)

    # ───────────────────────── history stack (08 §5) ─────────────────────────
    def _sync_filter_widgets(self):
        """Set filter widget variables from the current view_state."""
        for f, v in self.field_vars.items():
            v.set(f in self.view_state.get("fields", ALL_FIELDS))
        self.alive_var.set(self.view_state.get("alive", "only"))
        self.hits_var.set(self.view_state.get("hits", "hit_only"))
        self.filter_no_vdesktop_var.set(self.view_state.get("filter_no_vdesktop", False))
        self.sort_var.set(self.view_state.get("sort", "last_access"))
        self.order_var.set(self.view_state.get("order", "desc"))
        self.hide_self_var.set(self.view_state.get("hide_self", self.s.hide_self))
        self.current_boot_var.set(self.view_state.get("current_boot_only", True))

    def _push_history(self):
        self._history_stack.append(dict(self.view_state))
        if len(self._history_stack) > self.s.history_stack_depth:
            self._history_stack.pop(0)

    def go_back(self):
        if not self._history_stack:
            return
        prev_view = self.view_state.get("view")
        self.view_state = self._history_stack.pop()
        self.search_var.set(self.view_state.get("q", ""))
        uid = self.view_state.get("selected_window_uid")
        self.scope_var.set(f"scoped to window #{uid}  (clear ✕)" if uid else "")
        self._sync_filter_widgets()
        self._show_search_filters(self.view_state["view"] == "search")
        self._sync_notebook_tab()
        if self.view_state.get("view") == "search":
            self._on_canvas_configure()
        self.render()

    # ───────────────────────── global key + mousewheel (from demo) ─────────────────────────
    def on_global_key(self, event):
        px, py = self.winfo_pointerx(), self.winfo_pointery()
        in_tip = self._pointer_in_tip(self._preview_tip)
        log.debug("on_global_key keysym=%s char=%r widget=%s view=%s kind=%s pointer=(%d,%d) in_tip=%s",
                  event.keysym, event.char, event.widget, self.view_state.get("view"),
                  self._preview_kind, px, py, in_tip)
        # Don't hide a hover preview if the pointer is currently inside it.
        if self._preview_tip is not None and not in_tip:
            self._destroy_tip()
            self._hover_uid = None
        # Hide any open timeline hover preview on keyboard input unless pointer is inside it.
        view = getattr(self, "_timeline_view", None)
        if view is not None and view._hover_tip is not None and not self._pointer_in_tip(view._hover_tip):
            view._destroy_hover_tip()
        if event.widget is self.search_entry:
            log.debug("key event in search entry; returning")
            return
        if self.view_state.get("view") == "timeline" and event.widget not in (
            self.scale_entry, self.indicator_entry
        ):
            view = getattr(self, "_timeline_view", None)
            if view is not None:
                if event.keysym == "Left":
                    view.set_indicator_time(view.indicator_time - view.ARROW_STEP_PX * view.scale)
                    return "break"
                if event.keysym == "Right":
                    view.set_indicator_time(view.indicator_time + view.ARROW_STEP_PX * view.scale)
                    return "break"
                if event.keysym == "Up":
                    view._zoom_to(view.scale / view.ZOOM_FACTOR, view.indicator_time)
                    return "break"
                if event.keysym == "Down":
                    view._zoom_to(view.scale * view.ZOOM_FACTOR, view.indicator_time)
                    return "break"
        # Image hover navigation: Left/Right steps through screenshots while pointer is inside the tip.
        if (self._preview_tip is not None and self._preview_kind == "image"
                and in_tip):
            if event.keysym == "Left":
                log.debug("image hover Left arrow recognised -> previous capture")
                self._navigate_preview(1)   # previous (older) capture
                return "break"
            if event.keysym == "Right":
                log.debug("image hover Right arrow recognised -> next capture")
                self._navigate_preview(-1)  # next (newer) capture
                return "break"
        if (self.view_state.get("view") == "search"
                and event.char and len(event.char) == 1
                and (event.char.isprintable() or event.char == " ")):
            self.search_entry.focus_set()
            self.search_entry.insert(tk.INSERT, event.char)
            self.on_search_key(event)
            return "break"

    def on_mousewheel(self, event):
        if self.view_state.get("view") == "timeline":
            return
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        elif event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ───────────────────────── hover preview (from demo) ─────────────────────────
    def _poll_hover(self):
        try:
            # Hover previews only make sense in the search grid.
            if self.view_state.get("view") != "search":
                self._last_pointer = (self.winfo_pointerx(), self.winfo_pointery())
                return
            px, py = self.winfo_pointerx(), self.winfo_pointery()
            # While Shift is held, or while the pointer is inside the preview tip,
            # ignore mouse movement completely and never hide the hover interface.
            if self._is_shift_held() or self._pointer_in_tip(self._preview_tip):
                self._last_pointer = (px, py)
                return
            lx, ly = self._last_pointer
            moved = (abs(px - lx) > HOVER_MOVE_THRESHOLD_PX or abs(py - ly) > HOVER_MOVE_THRESHOLD_PX)
            self._last_pointer = (px, py)
            if moved:
                if self._preview_tip is not None:
                    self._destroy_tip()
                tile = self._tile_under_pointer(px, py)
                self._hover_uid = tile["win"].window_uid if tile else None
                self._still_since = time.perf_counter()
            elif (self._hover_uid is not None and self._preview_tip is None
                  and time.perf_counter() - self._still_since >= self.s.hover_preview_delay_ms / 1000):
                tile = self.tiles.get(self._hover_uid)
                if tile is not None:
                    self._show_preview(tile, px, py)
        except Exception as exc:                           # noqa: BLE001
            log.debug("hover poll: %s", exc)
        finally:
            self.after(HOVER_POLL_MS, self._poll_hover)

    def _tile_under_pointer(self, px, py):
        try:
            widget = self.winfo_containing(px, py)
        except KeyError:
            widget = None
        if widget is None:
            return None
        path = str(widget)
        for tile in self.tiles.values():
            frame = tile["frame"]
            fpath = str(frame)
            if widget is frame or path == fpath or path.startswith(fpath + "."):
                return tile
        return None

    def _hover_kind(self, px: int, py: int, tile: dict) -> str:
        """Return 'image' if the pointer is over the tile's thumbnail, else 'metadata'."""
        img = tile["img_label"]
        if img.winfo_ismapped():
            top = img.winfo_rooty()
            bottom = top + img.winfo_height()
            if top <= py < bottom:
                return "image"
        return "metadata"

    def _show_preview(self, tile, px, py):
        w: Window = tile["win"]
        kind = self._hover_kind(px, py, tile)

        photo = None
        if kind == "image":
            photo = self._preview_cache.get(w.window_uid)
            if photo is None and w.window_capture_url is not None:
                data = self.api.get_bytes(w.window_capture_url)   # hover is rare; sync is fine
                if data:
                    try:
                        img = Image.open(io.BytesIO(data))
                        m = self.s.hover_preview_max_dim
                        if img.width > m or img.height > m:
                            img.thumbnail((m, m), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img)
                    except Exception:                              # noqa: BLE001
                        photo = None
                    if photo is not None:
                        self._preview_cache[w.window_uid] = photo
            if photo is None:
                kind = "metadata"

        detail = None
        if kind == "metadata":
            try:
                detail = self.api.window(w.window_uid)
            except Exception as exc:                               # noqa: BLE001
                log.debug("detail fetch failed uid=%s: %s", w.window_uid, exc)

        self._destroy_tip()
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.configure(bg=self.theme["tip_border"])
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        title = w.display_label
        self._preview_display_label = w.display_label

        if kind == "image":
            wrap_w = photo.width()
            title_lbl = tk.Label(tip, text=title, anchor="w", justify="left",
                                 bg=self.theme["tiptitle_bg"], fg=self.theme["tiptitle_fg"],
                                 font=self.title_font, padx=8, pady=4, wraplength=wrap_w)
            title_lbl.pack(fill=tk.X, padx=1, pady=(1, 0))
            lbl = tk.Label(tip, image=photo, bg=self.theme["tip_bg"], borderwidth=0)
            lbl.image = photo
            lbl.pack(padx=1, pady=(0, 1))
            self._preview_tip = tip
            self._preview_title_label = title_lbl
            self._preview_image_label = lbl
            self._preview_text = None
            self._preview_kind = "image"
            self._preview_capture_ts = w.window_capture_ts
            self._update_preview_title(w.window_capture_ts)
            self._block_wheel_on(tip)
            self._block_wheel_on(title_lbl)
            self._block_wheel_on(lbl)
        else:
            title_lbl = tk.Label(tip, text=title, anchor="w", justify="left",
                                 bg=self.theme["tiptitle_bg"], fg=self.theme["tiptitle_fg"],
                                 font=self.title_font, padx=8, pady=4, wraplength=500)
            title_lbl.pack(fill=tk.X, padx=1, pady=(1, 0))
            meta_frame = ttk.Frame(tip)
            meta_frame.pack(fill=tk.BOTH, expand=False, padx=1, pady=(0, 1))
            meta = tk.Text(meta_frame, height=1, width=60, wrap="word", bd=0,
                           bg=self.theme["tip_bg"], fg=self.theme["fg"],
                           highlightthickness=0, padx=8, pady=4,
                           font=self.meta_font)
            meta.tag_configure("field", foreground=self.theme["accent"])
            meta.tag_configure("mark", background=self.theme["mark_bg"])
            meta.tag_configure("time", foreground=self.theme["muted"])
            meta.tag_configure("section", foreground=self.theme["accent"])
            meta.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._fill_preview_metadata(meta, w, detail)
            meta.configure(state="disabled")
            meta.update_idletasks()
            try:
                lines = int(float(meta.index("end-1c")))
            except Exception:                                      # noqa: BLE001
                lines = 6
            height = min(max(lines, 3), 16)
            meta.configure(height=height)
            if lines > height:
                sb = ttk.Scrollbar(meta_frame, orient=tk.VERTICAL, command=meta.yview)
                sb.pack(side=tk.RIGHT, fill=tk.Y)
                meta.configure(yscrollcommand=sb.set)
            self._bind_text_scroll(meta)
            self._preview_tip = tip
            self._preview_title_label = title_lbl
            self._preview_image_label = None
            self._preview_text = meta
            self._preview_kind = "metadata"
            self._block_wheel_on(tip)
            self._block_wheel_on(title_lbl)
            self._block_wheel_on(meta_frame)

        self._hover_uid = w.window_uid
        self._position_preview_tip()
        log.debug("created hover tip kind=%s uid=%d ts=%s",
                  self._preview_kind, w.window_uid, _fmt_ts(self._preview_capture_ts))

    def _fill_preview_metadata(self, box: tk.Text, w: Window, detail: dict | None):
        """Populate the hover detail Text with hits, usage, recent events, titles."""
        box.configure(state="normal")
        box.delete("1.0", tk.END)

        box.insert(tk.END, f"window #{w.window_uid}\n", "section")
        if w.app_name or w.wm_class:
            box.insert(tk.END, f"app: {w.app_name or w.wm_class}\n")
        if w.current_title:
            box.insert(tk.END, f"title: {w.current_title}\n")
        if w.vdesktop and w.vdesktop.index is not None:
            box.insert(tk.END, f"desktop: {w.desktop_badge}\n")
        status = "accessible" if w.alive and w.jumpable else ("dead" if not w.alive else "other session")
        box.insert(tk.END, f"status: {status}\n")
        box.insert(tk.END, f"last access: {_fmt_last_access(w.last_access)}\n")
        usage_line = f"{_fmt_usage(w)}  |  {_fmt_usage_summary(w)}".strip()
        if usage_line:
            box.insert(tk.END, f"{usage_line}\n")

        if w.hits:
            box.insert(tk.END, "\nSearch hits\n", "section")
            for h in w.hits:
                box.insert(tk.END, f"{h.field}: ", "field")
                self._insert_marked(box, _truncate(h.excerpt or "").replace("\n", " "))
                box.insert(tk.END, "\n")

        events = detail.get("events", []) if detail else []
        if events:
            box.insert(tk.END, "\nRecent events\n", "section")
            for e in events:
                typ = e.get("type", "?")
                kind = e.get("kind")
                label = f"{typ}/{kind}" if typ == "clipboard" and kind else typ
                ts = _fmt_ts(e.get("ts"))
                body = _truncate(e.get("text") or "").replace("\n", " ")
                box.insert(tk.END, f"[{ts}] {label}: ", "time")
                self._insert_marked(box, body)
                box.insert(tk.END, "\n")

        titles = detail.get("title_history", []) if detail else []
        if len(titles) > 1:
            box.insert(tk.END, "\nTitle history\n", "section")
            for t in titles[:5]:
                ts = _fmt_ts(t.get("changed_at"))
                body = _truncate(t.get("title") or "").replace("\n", " ")
                box.insert(tk.END, f"[{ts}] ", "time")
                self._insert_marked(box, body)
                box.insert(tk.END, "\n")

    def _block_wheel_on(self, widget):
        """Stop mouse-wheel events on a hover tip from scrolling the main grid."""
        widget.bind("<MouseWheel>", lambda _e: "break")
        widget.bind("<Button-4>", lambda _e: "break")
        widget.bind("<Button-5>", lambda _e: "break")

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

    def _update_preview_title(self, ts: float | None):
        """Refresh the hover title label to show the screenshot timestamp."""
        if self._preview_title_label is None:
            return
        text = self._preview_display_label or ""
        if ts is not None and ts > 0:
            text = f"{text}\n{_fmt_ts(ts)}"
        self._preview_title_label.configure(text=text)

    def _navigate_preview(self, delta: int):
        """Step to the previous/next screenshot using cursor-style queries."""
        if self._hover_uid is None or self._preview_kind != "image":
            return
        if delta == 0:
            return
        current_ts = self._preview_capture_ts
        if current_ts is None or current_ts <= 0:
            return
        # delta > 0 → previous (older) capture; delta < 0 → next (newer) capture.
        log.debug("navigate preview uid=%d delta=%d current_ts=%s", self._hover_uid, delta, _fmt_ts(current_ts))
        if delta > 0:
            self._submit(
                lambda: self.api.window_captures(
                    self._hover_uid, before=current_ts, limit=1),
                lambda data, err: self._on_navigate_result(data, err, direction="prev"),
            )
        else:
            self._submit(
                lambda: self.api.window_captures(
                    self._hover_uid, after=current_ts, limit=1),
                lambda data, err: self._on_navigate_result(data, err, direction="next"),
            )

    def _on_navigate_result(self, data, err, direction: str):
        if err or not data or self._preview_tip is None or self._preview_kind != "image":
            log.debug("navigate result ignored direction=%s err=%s tip=%s kind=%s",
                      direction, err is not None, self._preview_tip is not None, self._preview_kind)
            return
        cap = data[0]
        ts = cap.get("captured_at")
        url = cap.get("url")
        if not ts or not url:
            log.debug("navigate result empty direction=%s", direction)
            return
        log.debug("navigate result direction=%s ts=%s url=%s", direction, _fmt_ts(ts), url)
        self._preview_capture_ts = ts
        self._update_preview_title(ts)
        self._submit(
            lambda: self.api.get_bytes(url),
            lambda data2, err2: self._on_navigated_image(data2, err2),
        )

    def _on_navigated_image(self, data, err):
        if err or not data or self._preview_tip is None or self._preview_kind != "image":
            log.debug("navigated image ignored err=%s tip=%s kind=%s",
                      err is not None, self._preview_tip is not None, self._preview_kind)
            return
        try:
            img = Image.open(io.BytesIO(data))
            m = self.s.hover_preview_max_dim
            if img.width > m or img.height > m:
                img.thumbnail((m, m), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception as exc:                                  # noqa: BLE001
            log.debug("navigated image decode failed: %s", exc)
            return
        log.debug("navigated image loaded %dx%d; keeping existing tip position", photo.width(), photo.height())
        if self._preview_image_label is not None:
            self._preview_image_label.configure(image=photo, text="")
            self._preview_image_label.image = photo
        if self._preview_title_label is not None:
            wrap_w = photo.width()
            self._preview_title_label.configure(wraplength=wrap_w)

    def _update_preview_image(self, w: Window):
        """Refresh the currently-open image hover preview with a new capture/title."""
        if self._preview_tip is None or self._hover_uid != w.window_uid:
            return
        if getattr(self, "_preview_kind", None) != "image":
            return
        # If the user has navigated away from the latest capture, don't auto-refresh.
        if (self._preview_capture_ts is not None
                and self._preview_capture_ts != w.window_capture_ts):
            return
        if w.window_capture_url is None:
            self._destroy_tip()
            return

        def fetch():
            return self.api.get_bytes(w.window_capture_url)

        def done(data, err):
            if self._preview_tip is None or self._hover_uid != w.window_uid:
                return
            if getattr(self, "_preview_kind", None) != "image":
                return
            if not data:
                return
            try:
                img = Image.open(io.BytesIO(data))
                m = self.s.hover_preview_max_dim
                if img.width > m or img.height > m:
                    img.thumbnail((m, m), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            except Exception:                           # noqa: BLE001
                return
            log.debug("auto-refresh image loaded %dx%d for uid=%d; keeping tip position",
                      photo.width(), photo.height(), w.window_uid)
            self._preview_cache[w.window_uid] = photo
            if self._preview_image_label is not None:
                self._preview_image_label.configure(image=photo)
                self._preview_image_label.image = photo
            self._preview_capture_ts = w.window_capture_ts
            self._update_preview_title(w.window_capture_ts)
            if photo is not None:
                self._preview_title_label.configure(wraplength=photo.width())

        self._submit(fetch, done)

    def _position_preview_tip(self):
        """Reposition the hover Toplevel based on the last known pointer location."""
        if self._preview_tip is None:
            return
        px, py = self._last_pointer
        self._preview_tip.update_idletasks()
        tw, th = self._preview_tip.winfo_reqwidth(), self._preview_tip.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = px + 24
        if x + tw > sw:
            x = px - tw - 24
        if x < 0:
            x = max(0, (sw - tw) // 2)
        y = max(0, min(py - th // 2, sh - th))
        log.debug("positioning hover tip pointer=(%d,%d) tip=%dx%d pos=(%d,%d) screen=%dx%d",
                  px, py, tw, th, x, y, sw, sh)
        self._preview_tip.geometry("+%d+%d" % (int(x), int(y)))

    def _destroy_tip(self):
        if self._preview_tip is not None:
            log.debug("destroying hover tip")
            self._preview_tip.destroy()
            self._preview_tip = None
        self._preview_title_label = None
        self._preview_image_label = None
        self._preview_text = None
        self._preview_kind = None
        self._preview_display_label = ""
        self._preview_capture_ts = None

    # ───────────────────────── auto-refresh + close ─────────────────────────
    def _schedule_auto_refresh(self):
        def tick():
            self.render()
            self._refresh_after = self.after(self.s.grid_auto_refresh_s * 1000, tick)
        self._refresh_after = self.after(self.s.grid_auto_refresh_s * 1000, tick)

    def on_close(self):
        log.info("shutting down GUI")
        for aid in (self._search_after, self._refresh_after):
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
        self._destroy_tip()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.api.close()
        self.destroy()
