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

from PIL import Image, ImageTk

from .apiclient import ApiClient, DaemonUnavailable, Window
from . import views

log = logging.getLogger("dovw.fe.app")

ALL_FIELDS = ["title", "clipboard", "selection", "keyboard"]
HOVER_POLL_MS = 120
HOVER_MOVE_THRESHOLD_PX = 6


def _palette(theme: dict) -> dict:
    """Expand config's theme into the full set of keys the demo UI used."""
    return {
        "bg": theme["bg"], "fg": theme["fg"],
        "tile_bg": theme["tile_bg"], "tile_border": theme["tile_border"],
        "accent": theme["accent"], "muted": theme["muted"],
        "mark_bg": theme["mark_bg"], "alive": theme["alive"], "dead": theme["dead"],
        "canvas_bg": theme["bg"], "entry_bg": theme["tile_bg"], "entry_fg": theme["fg"],
        "select_bg": theme["accent"], "tip_border": theme["accent"],
        "tip_bg": theme["bg"], "tiptitle_bg": theme["tile_bg"], "tiptitle_fg": theme["fg"],
    }


class WindowPreviewApp(tk.Tk):
    def __init__(self, settings, client: ApiClient):
        super().__init__()
        self.s = settings
        self.api = client
        self.theme = _palette(settings.theme)
        self.title("Desktop Overview — search")
        self.geometry("1200x800")

        self._setup_fonts()
        self._apply_theme()

        # shared view-state (08 §5): the two tabs render the SAME state
        self.view_state = {
            "view": "search",        # 'search' | 'timeline'
            "q": "",
            "fields": list(ALL_FIELDS),
            "alive": "both",         # only | dead | both
            "hits": "hit_only",      # hit_only | all
            "from": None, "to": None,
            "sort": "last_access",
            "selected_window_uid": None,
        }
        self._history_stack: list[dict] = []

        # tiles keyed by window_uid (08 §2: reconcile by uid, not raw x id)
        self.tiles: dict[int, dict] = {}
        self._window_capture_cache: dict[int, ImageTk.PhotoImage] = {}
        self._preview_cache: dict[int, ImageTk.PhotoImage] = {}

        self.pool = ThreadPoolExecutor(max_workers=settings.request_worker_threads,
                                       thread_name_prefix="api")
        self._search_after = None
        self._refresh_after = None
        self._busy = False

        # hover preview state (lifted from the demo)
        self._preview_tip = None
        self._hover_target = None
        self._still_since = 0.0
        self._last_pointer = (-1, -1)

        self._build_controls()
        self._build_grid()
        self._build_banner()

        self.bind_all("<Key>", self.on_global_key)
        self.bind_all("<Button-4>", self.on_mousewheel)
        self.bind_all("<Button-5>", self.on_mousewheel)
        self.bind_all("<MouseWheel>", self.on_mousewheel)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

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
        base = tkfont.nametofont("TkDefaultFont").cget("family")
        self.search_font = tkfont.Font(family=base, size=self.s.font_size + 6)
        self.title_font = tkfont.Font(family=base, size=self.s.font_size + 4, weight="bold")

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
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"])

    # ───────────────────────── controls ─────────────────────────
    def _build_controls(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=6, pady=(6, 0))

        # tabs (08 §5)
        self.tab_search = ttk.Button(bar, text="🔍 Search", command=lambda: self.switch_view("search"))
        self.tab_timeline = ttk.Button(bar, text="🕑 Timeline", command=lambda: self.switch_view("timeline"))
        self.tab_search.pack(side=tk.LEFT)
        self.tab_timeline.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Button(bar, text="◀ Back", command=self.go_back).pack(side=tk.LEFT)

        ttk.Label(bar, text="Search:").pack(side=tk.LEFT, padx=(12, 4))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, font=self.search_font)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=6)
        self.search_entry.bind("<KeyRelease>", self.on_search_key)
        self.search_entry.bind("<Control-a>", self._select_all)

        self.refresh_btn = ttk.Button(bar, text="Refresh", command=self.on_refresh)
        self.refresh_btn.pack(side=tk.RIGHT, padx=4)
        self.status_var = tk.StringVar(value="ready")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.RIGHT, padx=8)

        # filter row (08 §4): fields, liveness, hits toggle, scope reset
        flt = ttk.Frame(self)
        flt.pack(fill=tk.X, padx=6, pady=(2, 6))
        ttk.Label(flt, text="Fields:").pack(side=tk.LEFT)
        self.field_vars = {}
        for f in ALL_FIELDS:
            v = tk.BooleanVar(value=True)
            self.field_vars[f] = v
            ttk.Checkbutton(flt, text=f, variable=v, command=self._apply_filters).pack(side=tk.LEFT, padx=2)

        ttk.Label(flt, text="   Show:").pack(side=tk.LEFT)
        self.alive_var = tk.StringVar(value="both")
        for label, val in [("alive", "only"), ("dead", "dead"), ("both", "both")]:
            ttk.Radiobutton(flt, text=label, value=val, variable=self.alive_var,
                            command=self._apply_filters).pack(side=tk.LEFT, padx=2)

        self.hits_var = tk.StringVar(value="hit_only")
        ttk.Checkbutton(flt, text="all fields", onvalue="all", offvalue="hit_only",
                        variable=self.hits_var, command=self._apply_filters).pack(side=tk.LEFT, padx=(8, 2))

        self.scope_var = tk.StringVar(value="")
        self.scope_label = ttk.Label(flt, textvariable=self.scope_var, style="Muted.TLabel")
        self.scope_label.pack(side=tk.LEFT, padx=8)

    def _build_grid(self):
        self.canvas = tk.Canvas(self, bg=self.theme["canvas_bg"], highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.inner_frame.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def _build_banner(self):
        self.banner = ttk.Label(self, anchor="center", style="Dead.Tile.TLabel")
        # packed only when the daemon is unreachable (08 §7)

    # ───────────────────────── worker plumbing (08 §6) ─────────────────────────
    def _submit(self, fn, on_done, *args):
        """Run ``fn(*args)`` off the Tk thread; deliver result via after(0,…)."""
        def worker():
            try:
                res = fn(*args)
                self.after(0, lambda: on_done(res, None))
            except DaemonUnavailable as exc:
                self.after(0, lambda: on_done(None, exc))
            except Exception as exc:                       # noqa: BLE001
                log.warning("api call failed: %s", exc)
                self.after(0, lambda: on_done(None, exc))
        self.pool.submit(worker)

    def _show_banner(self, msg: str):
        self.banner.configure(text=msg)
        self.banner.pack(fill=tk.X, side=tk.BOTTOM)

    def _hide_banner(self):
        self.banner.pack_forget()

    # ───────────────────────── render dispatch (08 §5) ─────────────────────────
    def render(self):
        if self.view_state["view"] == "timeline":
            self._render_timeline()
        else:
            self._render_search()

    def switch_view(self, view: str):
        if view != self.view_state["view"]:
            self._push_history()
        self.view_state["view"] = view
        log.info("switch view -> %s", view)
        self.render()

    def _render_search(self):
        st = self.view_state
        self.status_var.set("loading…")
        q = st["q"].strip() or None
        scope = st["selected_window_uid"]

        def call():
            if q is None and scope is None and st["from"] is None and st["to"] is None:
                return self.api.windows(sort=st["sort"], alive=st["alive"])
            return self.api.search(q=q, window_uid=scope, fields=st["fields"],
                                   alive=st["alive"], sort=st["sort"], hits=st["hits"],
                                   t_from=st["from"], t_to=st["to"])
        self._submit(call, self._on_search_results)

    def _on_search_results(self, windows, err):
        if err is not None:
            self._on_error(err)
            return
        self._hide_banner()
        self.status_var.set(f"{len(windows)} window(s)")
        self._reconcile_tiles(windows)

    def _render_timeline(self):
        st = self.view_state
        self.status_var.set("loading timeline…")

        def call():
            return self.api.timeline(window_uid=st["selected_window_uid"],
                                     t_from=st["from"], t_to=st["to"])
        self._submit(call, self._on_timeline_results)

    def _on_timeline_results(self, lanes, err):
        if err is not None:
            self._on_error(err)
            return
        self._hide_banner()
        self.status_var.set(f"{len(lanes) if lanes else 0} lane(s)")
        self._clear_grid()
        views.render_timeline(self, lanes or [])

    def _on_error(self, err):
        if isinstance(err, DaemonUnavailable):
            sock = self.s.tcp_endpoint if self.s.use_tcp else self.s.socket_path
            self._show_banner(f"⚠ daemon not running — start it with:  python -m daemon\n({sock})")
            self.status_var.set("daemon unavailable")
        else:
            self.status_var.set(f"error: {err}")

    # ───────────────────────── grid tiles (08 §2) ─────────────────────────
    def _clear_grid(self):
        self._destroy_tip()
        for uid, tile in list(self.tiles.items()):
            tile["frame"].destroy()
        self.tiles.clear()

    def _reconcile_tiles(self, windows: list[Window]):
        """Add/remove/keep tiles keyed by window_uid; render in API order (08 §2)."""
        self._destroy_tip()
        seen = {w.window_uid for w in windows}
        for uid in list(self.tiles):
            if uid not in seen:
                self.tiles[uid]["frame"].destroy()
                self.tiles.pop(uid, None)
        cols = self.s.grid_columns or max(1, self.canvas.winfo_width() // (self.s.window_capture_display_dim + 20) or 4)
        for i, w in enumerate(windows):
            tile = self.tiles.get(w.window_uid)
            if tile is None:
                tile = self._create_tile(w)
                self.tiles[w.window_uid] = tile
            else:
                self._update_tile(tile, w)
            r, c = divmod(i, cols)
            tile["frame"].grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
        for c in range(cols):
            self.inner_frame.columnconfigure(c, weight=1)
        self.inner_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _create_tile(self, w: Window) -> dict:
        c = self.theme
        frame = ttk.Frame(self.inner_frame, style="Tile.TFrame", relief=tk.RAISED, borderwidth=1)
        img_label = ttk.Label(frame, style="Tile.TLabel")
        img_label.pack()
        app_label = ttk.Label(frame, style="Muted.TLabel", wraplength=self.s.window_capture_display_dim)
        app_label.pack(anchor="w")
        title_label = ttk.Label(frame, style="Tile.TLabel", wraplength=self.s.window_capture_display_dim)
        title_label.pack(anchor="w")
        badge_label = ttk.Label(frame, style="Muted.TLabel")
        badge_label.pack(anchor="w")
        status_label = ttk.Label(frame, style="Tile.TLabel")
        status_label.pack(anchor="w")
        hits_box = tk.Text(frame, height=3, width=34, wrap="word", bd=0,
                           bg=c["tile_bg"], fg=c["fg"], highlightthickness=0)
        hits_box.tag_configure("mark", background=c["mark_bg"])
        hits_box.tag_configure("field", foreground=c["accent"])

        tile = {"frame": frame, "img_label": img_label, "app_label": app_label,
                "title_label": title_label, "badge_label": badge_label,
                "status_label": status_label, "hits_box": hits_box, "win": w}

        def jump(_e=None, uid=w.window_uid):
            self.jump_to(uid)
        def detail(_e=None, uid=w.window_uid):
            self.open_window_scope(uid)
        for widget in (frame, img_label, title_label, app_label):
            widget.bind("<Button-1>", jump)
            widget.bind("<Button-3>", detail)        # right-click → "search this window"
        self._update_tile(tile, w)
        self._load_window_capture(tile, w)
        return tile

    def _update_tile(self, tile: dict, w: Window):
        tile["win"] = w
        tile["app_label"].configure(text=w.wm_class or "")
        title = w.current_title or "(no title)"
        tile["title_label"].configure(text=(title[:48] + "…") if len(title) > 48 else title)
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
            return

        def fetch():
            return self.api.get_bytes(w.window_capture_url)

        def done(data, err):
            if not data or tile["frame"].winfo_exists() == 0:
                return
            try:
                img = Image.open(io.BytesIO(data))
                img.window_capture((self.s.window_capture_display_dim, self.s.window_capture_display_dim), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            except Exception as exc:                       # noqa: BLE001
                log.debug("window_capture decode failed uid=%s: %s", w.window_uid, exc)
                return
            self._window_capture_cache[w.window_uid] = photo
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
        self.status_var.set("refreshing window_captures…")
        self._window_capture_cache.clear()

        def done(res, err):
            if err is not None:
                self._on_error(err)
                return
            self.status_var.set(f"captured {res.get('captured')}")
            self.render()
        self._submit(self.api.refresh_window_captures, done)

    # ───────────────────────── search box / filters ─────────────────────────
    def on_search_key(self, _event):
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(self.s.search_debounce_ms, self._commit_search)

    def _commit_search(self):
        self._search_after = None
        self.view_state["q"] = self.search_var.get()
        if self.view_state["view"] != "search":
            self.view_state["view"] = "search"
        self.render()

    def _apply_filters(self):
        self.view_state["fields"] = [f for f, v in self.field_vars.items() if v.get()] or list(ALL_FIELDS)
        self.view_state["alive"] = self.alive_var.get()
        self.view_state["hits"] = self.hits_var.get()
        self.render()

    def _select_all(self, _event):
        self.search_entry.selection_range(0, tk.END)
        return "break"

    # ───────────────────────── history stack (08 §5) ─────────────────────────
    def _push_history(self):
        self._history_stack.append(dict(self.view_state))
        if len(self._history_stack) > self.s.history_stack_depth:
            self._history_stack.pop(0)

    def go_back(self):
        if not self._history_stack:
            return
        self.view_state = self._history_stack.pop()
        self.search_var.set(self.view_state.get("q", ""))
        uid = self.view_state.get("selected_window_uid")
        self.scope_var.set(f"scoped to window #{uid}  (clear ✕)" if uid else "")
        self.render()

    # ───────────────────────── global key + mousewheel (from demo) ─────────────────────────
    def on_global_key(self, event):
        if self._preview_tip is not None:
            self._destroy_tip()
        self._hover_target = None
        if event.widget is self.search_entry:
            return
        if event.char and len(event.char) == 1 and (event.char.isprintable() or event.char == " "):
            self.search_entry.focus_set()
            self.search_entry.insert(tk.INSERT, event.char)
            self.on_search_key(event)
            return "break"

    def on_mousewheel(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        elif event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ───────────────────────── hover preview (from demo) ─────────────────────────
    def _poll_hover(self):
        try:
            px, py = self.winfo_pointerx(), self.winfo_pointery()
            lx, ly = self._last_pointer
            moved = (abs(px - lx) > HOVER_MOVE_THRESHOLD_PX or abs(py - ly) > HOVER_MOVE_THRESHOLD_PX)
            self._last_pointer = (px, py)
            if moved:
                if self._preview_tip is not None:
                    self._destroy_tip()
                self._hover_target = self._tile_under_pointer(px, py)
                self._still_since = time.perf_counter()
            elif (self._hover_target is not None and self._preview_tip is None
                  and time.perf_counter() - self._still_since >= self.s.hover_preview_delay_ms / 1000):
                self._show_preview(self._hover_target, px, py)
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
        for tile in self.tiles.values():
            if widget is tile["img_label"]:
                return tile
        return None

    def _show_preview(self, tile, px, py):
        w: Window = tile["win"]
        if w.window_capture_url is None:
            return
        photo = self._preview_cache.get(w.window_uid)
        if photo is None:
            data = self.api.get_bytes(w.window_capture_url)   # hover is rare; sync is fine
            if not data:
                return
            try:
                img = Image.open(io.BytesIO(data))
                m = self.s.hover_preview_max_dim
                if img.width > m or img.height > m:
                    img.window_capture((m, m), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            except Exception:                              # noqa: BLE001
                return
            self._preview_cache[w.window_uid] = photo

        self._destroy_tip()
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.configure(bg=self.theme["tip_border"])
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        title = w.current_title or ""
        if w.wm_class:
            title = f"[{w.wm_class}] {title}"
        tk.Label(tip, text=title, anchor="w", justify="left", bg=self.theme["tiptitle_bg"],
                 fg=self.theme["tiptitle_fg"], font=self.title_font, padx=8, pady=4,
                 wraplength=photo.width()).pack(fill=tk.X, padx=1, pady=(1, 0))
        lbl = tk.Label(tip, image=photo, bg=self.theme["tip_bg"], borderwidth=0)
        lbl.image = photo
        lbl.pack(padx=1, pady=(0, 1))
        tip.update_idletasks()
        tw, th = tip.winfo_reqwidth(), tip.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = px + 24
        if x + tw > sw:
            x = px - tw - 24
        if x < 0:
            x = max(0, (sw - tw) // 2)
        y = max(0, min(py - th // 2, sh - th))
        tip.geometry("+%d+%d" % (int(x), int(y)))
        self._preview_tip = tip

    def _destroy_tip(self):
        if self._preview_tip is not None:
            self._preview_tip.destroy()
            self._preview_tip = None

    # ───────────────────────── auto-refresh + close ─────────────────────────
    def _schedule_auto_refresh(self):
        def tick():
            if self.view_state["view"] == "search":
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
