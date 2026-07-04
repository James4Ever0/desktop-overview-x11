# Timeline lane title filter

## Context

The timeline currently shows every lane returned by `/timeline`.  When many
windows are open the list becomes long and hard to scan.  A lightweight,
frontend-only filter that keeps only lanes whose **current title** matches a
few typed keywords would make the timeline much easier to use.

This plan adds a small search box to the timeline filter row.  The filter is
applied locally to the lanes already loaded in the frontend; no new API call or
backend change is needed.

## Goal

Add a text box in the timeline view that filters lanes by current title using
**AND** semantics:

1. Split the query on whitespace.
2. Strip each token.
3. Lower-case both tokens and the lane's current title.
4. Keep the lane only if **every** token is a substring of the title.
5. Empty query shows all lanes.

The existing zoom factor, pan position and red-line time must be preserved when
the filter changes.

## Proposed approach

### 1. UI: add a filter entry to the timeline filter row

In `frontend/app.py::_build_filter_bar()` (the row that already builds the
zoom/red-line timeline controls), add a new entry widget after the **Now**
button:

```python
self.lane_filter_var = tk.StringVar(value="")
self.lane_filter_var.trace_add("write", self._on_lane_filter_changed)

lane_filter_lbl = ttk.Label(flt, text="Lanes:")
self._timeline_filters.append(lane_filter_lbl)
self.lane_filter_entry = ttk.Entry(flt, textvariable=self.lane_filter_var, width=24)
self._timeline_filters.append(self.lane_filter_entry)
```

The label and entry are appended to `_timeline_filters` so `_show_search_filters()`
already shows them only in timeline view and hides them in search view.

Add a small "×" button to clear the filter quickly:

```python
self._lane_filter_clear = ttk.Button(flt, text="×", width=2,
                                     command=self._clear_lane_filter)
self._timeline_filters.append(self._lane_filter_clear)
```

### 2. State

Add one key to the shared `view_state` dict:

```python
"lane_title_filter": ""
```

No backend, history, or URL serialization is needed; the filter is purely a
local view preference.

### 3. Filter parsing helper

Add a small pure function, e.g. in `frontend/views.py` or `frontend/app.py`:

```python
def _parse_lane_filter(query: str) -> list[str]:
    """Return non-empty lower-case tokens for AND matching."""
    return [t.lower() for t in query.split() if t.strip()]


def _lane_matches(lane, tokens: list[str]) -> bool:
    if not tokens:
        return True
    title = (lane.current_title or "").lower()
    return all(token in title for token in tokens)
```

### 4. Apply the filter inside `TimelineView`

`TimelineView` already owns `self.lanes`.  Introduce a second list that always
holds the **full** lanes returned by the API:

```python
self._all_lanes: list = []
self.lanes: list = []
```

When `set_lanes(all_lanes)` is called, store the full list, then apply the
current filter before drawing:

```python
def set_lanes(self, lanes: list):
    self._cancel_hover()
    self._all_lanes = lanes or []
    self._apply_title_filter()
    # ... existing bounds/time setup now uses self.lanes ...
```

Add:

```python
def set_title_filter(self, query: str):
    self._title_filter_query = query
    self._apply_title_filter()
    self._draw()
    self._update_sticky()
    self._update_details_panel()

def _apply_title_filter(self):
    tokens = _parse_lane_filter(getattr(self, "_title_filter_query", ""))
    self.lanes = [lane for lane in self._all_lanes if _lane_matches(lane, tokens)]
    self.t_min, self.t_max = self._calc_bounds()
    if self.t_max <= self.t_min:
        self.t_max = self.t_min + 1.0
```

Important: `_draw()` must keep the existing `self.scale` and `self.start_time`
(as it already does on refreshes).  Only the lane list and scroll region
change, so the zoom is preserved.

### 5. Connect the entry to the view

In `frontend/app.py`:

```python
def _on_lane_filter_changed(self, *_args):
    query = self.lane_filter_var.get()
    self.view_state["lane_title_filter"] = query
    view = getattr(self, "_timeline_view", None)
    if view is not None:
        view.set_title_filter(query)
    self.status_var.set(
        f"{len(view.lanes) if view else 0} lane(s)"
    )

def _clear_lane_filter(self):
    self.lane_filter_var.set("")
```

Bind the clear button and the `trace_add` once during construction.

### 6. Preserve active lane / red line

When the filter changes:

* If the currently active lane is filtered out, set `self._active_lane = None`
  and update the details panel to the "no event" state.
* The red indicator (`indicator_time`) must stay exactly where it was.
* If the active lane remains visible, keep it highlighted and keep the sticky
  indicator logic unchanged.

This is handled automatically if `_apply_title_filter()` is followed by a full
`_draw()` and `_update_details_panel()`, because `_draw()` recomputes
`_active_span`/`_active_lane` from the new `self.lanes` at the current
indicator time.

### 7. Restore the filter on timeline (re-)render

When `_on_timeline_results()` creates or refreshes the timeline view, push the
stored query into the view and the entry widget:

```python
view = views.render_timeline(self, lanes or [], parent=self.timeline_frame)
if view is not None:
    # ... existing callbacks ...
    self.lane_filter_var.set(self.view_state.get("lane_title_filter", ""))
    view.set_title_filter(self.lane_filter_var.get())
```

Because `render_timeline()` calls `view.set_lanes(lanes)`, the filter is
re-applied and the visible lanes are correct.

### 8. Edge cases

* **All lanes filtered out**: `self.lanes` becomes empty.  Draw a message in the
  canvas such as *"No lanes match the title filter."* instead of the lanes.
  `set_lanes()` already has an early return for empty `self.lanes` that clears
  the canvas; extend it to draw the message.
* **Case sensitivity**: all matching is lower-case.
* **Whitespace-only query**: treated as empty after `strip()`, so all lanes are
  shown.
* **Unicode titles**: Python's `str.lower()` and `in` operator handle Unicode
  correctly for the simple substring match required.

## Critical files to modify

* `frontend/app.py` — add the entry/clear button, `view_state` key, trace
  callback, and wire the filter into `_on_timeline_results()`.
* `frontend/views.py` — add `_all_lanes`, `_title_filter_query`,
  `_parse_lane_filter()`, `_lane_matches()`, `_apply_title_filter()`,
  `set_title_filter()`, and update `set_lanes()` / `_draw()` to handle the
  filtered list.

## Out of scope

* Filtering by app name, wm_class, or event content.
* Regex / fuzzy / OR matching.
* Persisting the filter across application restarts.
* A server-side lane filter (the daemon already supports `window_uid` scope;
  this is a pure-frontend convenience).

## Verification

1. Manual test:
   * Open Timeline.
   * Type a word that appears in one or more current titles.
   * Confirm only matching lanes remain.
   * Add a second word; confirm only lanes containing **both** words remain.
   * Clear the box; confirm all lanes return.
   * Confirm the zoom scale and red-line time did not change while filtering.
2. Unit test (optional):
   * Extract `_parse_lane_filter()` and `_lane_matches()` as pure functions and
     test tokenization plus AND logic in `tests/test_frontend.py`.
