# 08 — Frontend UI (tkinter, thin client)

The frontend is reduced to **render + ask the daemon**. It keeps the *look* and
interaction model of `reference_v2/demo-no-ocr-efficient-refresh.py` (dark
theme, grid of tiles, hover-zoom preview, type-to-search) but **deletes all the
collection/compute**: no `wmctrl`, no `import`, no `xclip`, no asyncio capture,
no substring scanning. Those now live in the daemon.

`schematic.txt` frontend responsibilities map to the views below.

---

## 1. What is removed vs. kept from the demo

**Removed** (now daemon-side):
- `get_window_list`, `capture_window`, `get_app_name`, `get_active_window_id`,
  `capture_stream`, the ThreadPoolExecutor, the `_tick` refresh loop,
  `_select_background_batch`, OCR, the `_relayout` substring filter.

**Kept** (pure UI, lifted largely verbatim):
- Theme/`_apply_theme`/`_setup_fonts`, the scrollable canvas + `inner_frame`
  grid, mousewheel binding.
- Hover-zoom preview machinery (`_poll_hover`, `_show_preview`, `_destroy_tip`)
  — but it requests the full-size image from the API instead of holding a PIL
  image.
- Type-to-search UX (`on_global_key` redirecting keystrokes into the search box)
  — but each keystroke now fires a debounced `GET /search` instead of filtering
  in-memory tiles.

## 2. Grid (search results) view

> search windows ... display windows in a grid view in search results. sort by
> last access time, display current window title and current virtual desktop
> name & id.

- The grid is populated **from `GET /windows`** (no query) or **`GET /search`**
  (with a query). Both already return current title, current vdesktop (id+name),
  alive, last_access, and a `window_capture_url`.
- Each tile: window_capture (lazy-loaded from `window_capture_url`), wm_class, current
  title, and a small `[desktop-id: name]` badge. Sort order comes from the API
  (`?sort=last_access`); the frontend just renders in returned order.
- Tile click → **jump to window**: `POST /windows/{uid}/activate`
  (`api/07 §5`). If the result is not `jumpable` (dead / different session), the
  tile shows a greyed jump affordance with the reason on hover — the objective's
  *"jump to the window if we can find it and it is alive"* (`daemon/02 §6`).
- Tiles reconcile against the returned uid set (reuse the demo's add/remove/keep
  diffing, keyed by `window_uid` instead of raw x id).

## 3. Timeline view

> display windows in timeline view, show which windows were active in the past,
> with what title or titles displayed (title change history).

- Backed by `GET /timeline?from=&to=`. The daemon returns, per window active in
  the range: the `focus_event` spans + the `title_history` entries within the
  range.
- Render as horizontal lanes (one per window) or a vertical time-ordered list;
  each segment labeled with the title(s) shown during it. Clicking a segment can
  fetch the historical window_capture closest to that time
  (`GET /windows/{uid}/window_capture/{ts}`).
- All time math + title-history assembly is server-side; the frontend draws
  rectangles/labels from the JSON.

## 4. History query view

> query window history, given from/end time ... show whether accessible or not.
> filter by liveness (only alive / only dead / both). display hit fields and
> highlight the search excerpt (only show searched & containing fields,
> configurable to show all fields or just hit fields).

- Controls: from/to pickers, a liveness radio (`only alive` / `only dead` /
  `both`), the field checkboxes (title / clipboard / selection / keyboard), and
  a "show: hit fields only / all fields" toggle.
- These map **directly** to `GET /search` (or `/history`) params: `from`,`to`,
  `alive`, `fields`, `hits` (`api/07 §4`).
- Each result row shows the window (title, alive badge — green "accessible" /
  grey "dead") and, beneath it, the **hit fields** with the daemon-provided
  excerpt. The excerpt already contains `<mark>…</mark>` markers; the frontend
  renders them as a highlight tag in a Tk `Text` widget — **no client-side
  matching or excerpting**.

## 5. Switching between search and timeline views (objective)

> `objective.txt`: *"search by keywords, view by timeline, switch back and
> forth from timeline and keyword search view, then jump to the window."*

The two primary views (**Search/grid** and **Timeline**) are tabs over a
**shared query state**, so the user moves between them without losing context:

```
shared state = { q, fields, alive_filter, from, to, selected_window_uid }
```

- A top toggle (`[ Search | Timeline ]`) swaps the view **without** clearing the
  state. Switching simply re-renders the *same* `q`/filters through the other
  view's endpoint:
  - Search tab → `GET /search` → grid of result tiles.
  - Timeline tab → `GET /timeline?from=&to=` (optionally constrained to the
    `window_uid`s that matched the current `q`, so "timeline of what I searched"
    is one click).
- **Cross-navigation actions:**
  - From a **search result** → "show in timeline" jumps to the Timeline tab
    scrolled to that window's activity (`selected_window_uid` carried over).
  - From a **timeline segment** → "search this window" / "show details" flips to
    the Search tab (or detail panel) for that `window_uid`. **This sets
    `window_uid`, it does NOT copy the title into `q`** — see the note below.
  - From **either** view → **jump to the live window** (`§2` tile action /
    a timeline segment's jump button), available iff `jumpable`.
- **What "search this window" means (and doesn't):** it scopes the view to one
  `window_uid` (`api/07 §4a`), opening that window's detail — its title history,
  clipboard, selection, keyboard segments, window_captures. It is **not** "put the
  window title in the search box": a title query would match *other* windows
  with similar titles and miss the window's non-title content. The search box
  (`q`) and the window scope (`window_uid`) are independent — leave `q` empty to
  see everything for the window, or type in `q` to **search within** just that
  window.
- **Per-window timeline:** the same `window_uid` carried into the Timeline tab
  calls `GET /timeline?window_uid=…`, rendering **only that window's** focus
  spans + title-change history — "browse this window's history over time",
  distinct from the all-windows timeline.
- Implementation: a single `WindowPreviewApp` holding `self.view_state`; the
  toggle calls `self._render_search()` or `self._render_timeline()`, each reading
  the same state and repainting the canvas. No data is recomputed client-side —
  each render is one API call. Back/forward is just a small stack of prior
  `view_state` snapshots.

This makes search ⇄ timeline ⇄ jump a single coherent loop, which is the
objective's central workflow.

## 6. Daemon client (the only "logic" left)

A thin `apiclient.py` wrapping the calls. Because Tk's mainloop is synchronous,
use **sync HTTP** to avoid mixing event loops:
- `httpx.Client(transport=httpx.HTTPTransport(uds=SOCK_PATH))` (httpx supports
  UDS for sync too) — or `requests-unixsocket`.
- Keep network calls **off the Tk thread**: run each request in a small
  worker thread (or a `concurrent.futures` pool) and marshal the result back
  with `widget.after(0, ...)` — exactly the pattern the demo already uses for
  captures (`threading.Thread` + `self.after(0, ...)`). This keeps the UI
  responsive without an asyncio loop in the GUI.
- Search is **debounced** (~150–250 ms after last keystroke) before firing
  `GET /search`, so fast typing doesn't spam the daemon.

```
frontend/apiclient.py    # sync httpx-over-UDS wrapper; returns dataclasses
frontend/app.py          # WindowPreviewApp: theme, grid, hover, search box (from demo UI half)
frontend/views.py        # grid / timeline / history view builders
frontend/__main__.py     # parse args (socket path), launch Tk
```

Config knobs (frontend, in `frontend/config.py`): `SOCKET_PATH`,
`USE_TCP`/`TCP_ENDPOINT`, `REQUEST_TIMEOUT_S`, `REQUEST_WORKER_THREADS`,
`SEARCH_DEBOUNCE_MS` (~200), `GRID_AUTO_REFRESH_S`, `GRID_COLUMNS`,
`WINDOW_CAPTURE_DISPLAY_DIM`, `HOVER_PREVIEW_DELAY_MS`, `HOVER_PREVIEW_MAX_DIM`,
`HISTORY_STACK_DEPTH`, `THEME`/`FONT_*`. Defaults, types, and set-via →
**`10-configuration.md §5`**.

## 7. Degraded mode

If the daemon isn't reachable (socket missing), the frontend shows a clear
"daemon not running" banner with the command to start it, rather than trying to
collect anything itself. The frontend has **no fallback collection path** — that
separation is the whole point of the refactor.
