# Timeline jump from search hits

## Context

Search tiles and the hover preview already show matching text excerpts, but they
do not expose **when** the match happened, and there is no way to open the
surrounding context in the **Timeline** tab.  The timeline already has a red
indicator line and zoom/pan, but switching tabs from search currently loses the
user's zoom factor on the first load.

This plan wires search hits and recent events to the timeline:

* show a timestamp on every search-hit / event line,
* right-click → **"Show in timeline"** jumps to that window's lane and places
the red line on the event timestamp,
* preserve the current zoom factor across the jump.

## What already has a timestamp?

| Source | Timestamp column(s) | Currently shown in search? | Currently shown in timeline? |
|---|---|---|---|
| `focus_event` | `focused_at`, derived `ended_at` | via `last_access` | as blue focus spans + red-line hit |
| `title_history` | `changed_at` | yes (field `title`) | yes, as green ticks |
| `app_name_history` | `changed_at` | yes (field `app_name`) | no (only current app name) |
| `clipboard_event` | `created_at` | yes (field `clipboard`) | yes, as yellow marker |
| `selection_event` | `created_at` | yes (field `selection`) | yes, as yellow marker |
| `kbd_segment` | `started_at`, `ended_at` | yes (field `keyboard`) | yes, as dark-blue marker |
| `read_event` | `created_at` | **no** | **no** |
| `window_capture` | `captured_at` | yes (screenshot hover) | yes, via capture endpoint |
| `window` lifecycle | `first_seen`, `last_seen`, `closed_at` | no | yes, as lifespan line |
| `screen_lock_event` | `changed_at` | no | yes, ends focus spans |
| `vdesktop_state` | `changed_at` | no | no |
| `window_heartbeat` | `ts` | no | no (used for usage rates) |
| `daemon_run` | `started_at` | no | no |

The immediate scope is the fields already returned by `/search`: **title,
app_name, clipboard, selection, keyboard**.  `read_event` and `window_capture`
are listed as follow-up opportunities.

## Goals

1. Every search-hit line carries its timestamp from the DB.
2. The hover preview (and optionally the tile hits box) displays the timestamp.
3. Right-clicking a hit or recent-event line opens a context menu with **"Show in timeline"**.
4. Choosing it:
   * destroys the hover preview,
   * switches to the **Timeline** tab,
   * scrolls vertically to the window's lane,
   * centres the viewport horizontally on the event time,
   * places the red indicator line exactly on that timestamp,
   * keeps the zoom factor the user had last set (or a sensible default).

## Proposed approach

### 1. Add `ts` to search-hit responses

* `daemon/api/models.py`:
  ```python
  class Hit(BaseModel):
      field: str
      excerpt: str | None = None
      ts: float | None = None
  ```
* `daemon/db/search.py`:
  `_merge_hit()` already receives a `ts`; change the appended dict to include it:
  ```python
  agg["hits"].append({"field": field, "excerpt": excerpt, "ts": ts})
  ```
  FTS and substring SQL already selects the timestamp column, so no query change
  is required.

### 2. Frontend dataclass

* `frontend/apiclient.py`:
  ```python
  @dataclass
  class Hit:
      field: str
      excerpt: str | None = None
      ts: float | None = None
  ```
  Update `Window.from_json()` to copy `d.get("ts")` into each `Hit`.

### 3. Display timestamps

* Tile hits box (`frontend/app.py::_fill_hits`):
  ```python
  for h in w.hits:
      ts = _fmt_ts(h.ts) if h.ts else "—"
      box.insert(tk.END, f"[{ts}] ", "time")
      box.insert(tk.END, f"{h.field}: ", "field")
      self._insert_marked(box, (h.excerpt or "").replace("\n", " "))
      box.insert(tk.END, "\n")
  ```
  Add a `"time"` tag to the hits box (same style as the preview: muted colour).

* Hover preview metadata (`frontend/app.py::_fill_preview_metadata`):
  Prefix every search-hit line and every recent-event line with its formatted
  timestamp, using the same `"time"` tag already configured in the preview Text.

### 4. Right-click context menu in the hover preview

The preview Text lines are static; we bind a context menu per-line using Tk
Text tags.

* For each search-hit line, create a tag `hit_<uid>_<ts>_<field>` covering the
  whole line.
* For each recent-event line, create a tag `event_<uid>_<ts>_<type>` covering
  the whole line.
* Bind `<Button-3>` on each tag to a helper that builds a small `tk.Menu`:
  * **Show in timeline** → `_jump_to_timeline(uid, ts)`
  * **Search this window** → `open_window_scope(uid)`
  * **Jump to window** → `jump_to(uid)` (greyed out if not jumpable)
* Post the menu at `event.x_root, event.y_root` and return `"break"` so the
  click does not also dismiss the hover tip.

Because the action closes the hover tip, the helper should call
`self._destroy_tip()` before switching tabs.

### 5. Jump to timeline

New method in `frontend/app.py`:

```python
def _jump_to_timeline(self, uid: int, ts: float | None = None):
    """Switch to timeline, focus on window uid and timestamp ts."""
    self._destroy_tip()

    # Preserve current zoom, or use a reasonable default for a single event.
    current_scale = getattr(self._timeline_view, "scale", None)
    fallback_scale = 5.0  # seconds per pixel
    self.view_state["t_scale"] = current_scale or self.view_state.get("t_scale") or fallback_scale

    # Stash where we want the view to land.
    self.view_state["focus_uid"] = uid
    self.view_state["focus_ts"] = ts
    self.view_state["selected_window_uid"] = None  # show all lanes, not a single-window scope
    self.view_state["view"] = "timeline"

    self.notebook.select(self.timeline_frame)
    self.render()
```

Notes:

* `selected_window_uid` is intentionally cleared so the timeline shows **all**
  lanes and we can scroll to the correct one.  If the user prefers a single-lane
  view, this can be changed to `uid` and the scrolling step below becomes
  unnecessary.
* `t_scale` is pre-set so `render_timeline()` → `render_timeline()` passes the
  preserved scale into the new `TimelineView` constructor.

### 6. Scroll to lane and centre on time

Add a method to `TimelineView` (`frontend/views.py`):

```python
def focus_on(self, uid: int, ts: float | None = None):
    """Scroll vertically to the lane for uid and, if given, centre horizontally on ts."""
    if not self.lanes:
        return
    try:
        idx = next(i for i, lane in enumerate(self.lanes) if lane.window_uid == uid)
    except StopIteration:
        return

    self._active_lane = self.lanes[idx]

    if ts is not None:
        self.indicator_time = max(self.t_min, min(self.t_max, ts))
        timeline_w = max(1, self._canvas_width() - self._x_offset() - PAD)
        self.start_time = self.indicator_time - (timeline_w / 2.0) * self.scale
        self._clamp_view()

    # Vertical scroll: put the lane in the middle of the viewport.
    lane_top = PAD + idx * (LANE_H + PAD)
    canvas_h = self.canvas.winfo_height()
    scroll_h = len(self.lanes) * (LANE_H + PAD) + PAD
    visible_h = max(1, scroll_h - canvas_h)
    fraction = (lane_top - canvas_h / 2 + LANE_H / 2) / visible_h
    fraction = max(0.0, min(1.0, fraction))
    self.canvas.yview_moveto(fraction)

    self._draw()
    self._sync_callbacks()
    self._update_sticky()
    self._update_details_panel()
```

After `_on_timeline_results()` creates/refreshes `self._timeline_view`, consume
the transient focus request:

```python
focus_uid = self.view_state.pop("focus_uid", None)
focus_ts = self.view_state.pop("focus_ts", None)
if focus_uid is not None:
    view.focus_on(focus_uid, focus_ts)
```

### 7. Preserve zoom factor

* `TimelineView.set_lanes()` already keeps `self.scale` on refreshes when it is
  not the first load.
* `render_timeline()` already passes `scale=app.view_state.get("t_scale")` to
  the `TimelineView` constructor.
* `_on_timeline_scale()` already writes every scale change back into
  `view_state["t_scale"]`.
* The only missing piece is the first load from search: by setting
  `view_state["t_scale"]` before `render()` in `_jump_to_timeline()`, the new
  `TimelineView` is created with the preserved scale.
* To prevent `set_lanes()` from overriding the pre-set view, also set
  `start_time` before `set_lanes()` is called.  `render_timeline()` can do this
  by pre-configuring the view after construction but before `set_lanes()`, or by
  having `focus_on()` set `start_time` and then calling `_draw()` directly.

A clean way: modify `render_timeline()` so that when `focus_uid` is present it
constructs the view, calls `set_lanes(lanes)`, then calls `view.focus_on(...)`.
`focus_on()` recomputes `start_time` and re-draws, overriding any defaulting
that `set_lanes()` did.

### 8. Right-click on search-tile hits box (optional)

The same tags + menu can be added to `_fill_hits()` so the small hits box on
each tile also supports "Show in timeline".  This is a small follow-up to step 4.

## Critical files to modify

* `daemon/api/models.py` — add `ts` to `Hit`.
* `daemon/db/search.py` — propagate `ts` through `_merge_hit()`.
* `frontend/apiclient.py` — add `ts` to `Hit` dataclass and `Window.from_json()`.
* `frontend/app.py` —
  * display timestamps in `_fill_hits()` and `_fill_preview_metadata()`;
  * add right-click tags/menu helpers;
  * add `_jump_to_timeline()`.
* `frontend/views.py` — add `TimelineView.focus_on()` and wire it from
  `_on_timeline_results()`.

## Out of scope

* Jumping from `read_event` data (not exposed in search/timeline yet).
* Jumping from a `window_capture` screenshot timestamp.
* A context menu inside the timeline canvas itself.
* Persisting the timeline viewport across application restarts.

## Verification

1. Unit tests:
   * `tests/test_api_search.py` — assert that search-hit dicts include `ts` for
     title, clipboard, selection and keyboard matches.
   * `tests/test_frontend.py` — assert that `Hit.ts` round-trips through the
     `Window.from_json()` path.
2. Manual test:
   * Search for text that appears in a clipboard or keyboard event.
   * Confirm the hover preview shows the timestamp.
   * Right-click → **Show in timeline**.
   * Confirm:
     * the Timeline tab becomes active,
     * the window lane is visible and highlighted,
     * the red indicator sits on the event time,
     * the zoom level matches what the timeline had before (or the default if
       this is the first timeline visit).
