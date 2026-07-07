# Events tab — paginated, searchable event log

## Context

The application already records many timestamped events in SQLite:
title changes, app-name changes, focus changes, clipboard/selection events,
keyboard segments, paste-read events and screen-lock events.  Today the user
sees these only indirectly (inside a window's hover preview, or as small markers
on a timeline lane).  A dedicated **Events** tab would give a readable,
paginated, searchable event stream.

## Goal

Add a third notebook tab called **Events** that lets the user:

1. Browse the latest events in reverse chronological order.
2. Page through results with Prev / Next controls.
3. Run a hybrid search (FTS5 + substring) over event text.
4. See results as plain text lines with timestamp, event type, window identity
   and a short text excerpt.

## Event sources

| type | table | text column | timestamp column | notes |
|---|---|---|---|---|
| `title` | `title_history` | `title` | `changed_at` | FTS table `fts_title` |
| `app_name` | `app_name_history` | `app_name` | `changed_at` | FTS table `fts_appname` |
| `clipboard` | `clipboard_event` | `text` | `created_at` | FTS table `fts_clip` |
| `selection` | `selection_event` | `text` | `created_at` | FTS table `fts_sel` |
| `keyboard` | `kbd_segment` | `text` | `started_at` | FTS table `fts_kbd` |
| `read` | `read_event` | `text` | `created_at` | substring only (no FTS yet) |
| `focus` | `focus_event` | — | `focused_at` | no text, included only for browsing |
| `lock` | `screen_lock_event` | `locked` boolean | `changed_at` | no text, included only for browsing |

The first implementation can include all rows above.  The searchable subset is
anything with a text column; `focus` and `lock` are browse-only unless we later
add a synthetic description.

## Proposed approach

### 1. Backend: new `daemon/db/events.py` module

Create a focused query builder so `search.py` does not grow further.

Core function signature:

```python
async def search_events(
    store,
    *,
    q: str | None = None,
    types: list[str] | None = None,
    t_from: float | None = None,
    t_to: float | None = None,
    sort: str = "ts_desc",          # "ts_desc" | "ts_asc" | "rank"
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    ...
```

Returns `(events, total)`.

#### 1.1 No-query mode (latest events)

Build a single SQLite `UNION ALL` query over all requested event tables, each
contributing:

```sql
SELECT 'clipboard' AS type, id, window_uid, kind, text, created_at AS ts
FROM clipboard_event
```

Add `WHERE` clauses for `t_from`, `t_to`, and event type.  Order by `ts DESC`
and apply `LIMIT ? OFFSET ?`.  Also run a matching `COUNT(*)` query for the
`total`.

#### 1.2 Hybrid search mode

For each text-bearing table:

1. **FTS branch**: run `fts_table MATCH ?` to get matching `rowid`s, join the
   content table, compute FTS `rank`.
2. **Substring branch**: if the query is short or FTS returns nothing, fall back
   to `LOWER(text) LIKE '%' || ? || '%'`.

Merge both branches per source, then merge across sources.  Sort by rank
(ascending) and then `ts DESC`, apply limit/offset, and count total hits.

This mirrors the existing `search.search()` pipeline but produces **one row per
event** instead of one row per window.

#### 1.3 Enrichment

Each result needs a human-readable window identity.  Use the existing
`_WINDOW_COLS` helper / `assemble_window()` or a lightweight join to get:

* `app_name` / `wm_class`
* `current_title` (latest title for that window)
* `vdesktop_index` / `vdesktop_name`
* `alive`

Because event volume can be high, batch enrichment by collecting unique
`window_uid`s and fetching their metadata in one query.

### 2. API endpoint

`daemon/api/routes.py`:

```python
@router.get("/events", response_model=models.EventListOut)
async def get_events(
    ctx: DaemonContext = Depends(get_ctx),
    q: str | None = Query(None),
    type: str | None = Query(None),          # comma-separated list
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
    sort: str = Query("ts_desc", pattern="^(ts_desc|ts_asc|rank)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    ...
```

New Pydantic model `daemon/api/models.py`:

```python
class GlobalEvent(BaseModel):
    id: int
    type: str
    kind: str | None = None
    ts: float
    window_uid: int
    app_name: str | None = None
    wm_class: str | None = None
    current_title: str | None = None
    vdesktop: VDesktopRef | None = None
    text: str | None = None
    alive: bool | None = None


class EventListOut(BaseModel):
    total: int
    items: list[GlobalEvent]
```

No new database tables or migrations are required.

### 3. Frontend API client

`frontend/apiclient.py`:

```python
@dataclass
class GlobalEvent:
    id: int
    type: str
    kind: str | None
    ts: float
    window_uid: int
    app_name: str | None
    wm_class: str | None
    current_title: str | None
    vdesktop: VDesktopRef | None
    text: str | None
    alive: bool | None

class ApiClient:
    def events(self, *, q=None, type=None, t_from=None, t_to=None,
               sort="ts_desc", limit=100, offset=0) -> tuple[list[GlobalEvent], int]:
        data = self._get("/events", q=q, type=type, from=t_from, to=t_to,
                         sort=sort, limit=limit, offset=offset)
        items = [GlobalEvent.from_json(e) for e in data.get("items", [])]
        return items, data.get("total", 0)
```

### 4. Frontend UI

#### 4.1 New notebook tab

In `frontend/app.py::_build_controls()`:

```python
self.events_frame = ttk.Frame(self.notebook)
self.notebook.add(self.events_frame, text="Events")
```

Extend `view_state["view"]` to allow `"events"` and bind tab changes.

#### 4.2 Events view layout

Create a new module or helper `frontend/views.py::render_events(parent, app,
events, total, offset, limit)` returning a frame with:

* Top bar:
  * search Entry (`events_search_var`)
  * type filter Checkbuttons: clipboard, selection, keyboard, title, app_name,
    read, focus, lock
  * Prev / Next buttons
  * page label: `1-100 of 1,247`
* Main area: a `tk.Text` widget (read-only) showing one plain-text line per event.

#### 4.3 Plain-text line format

```
2026-07-07 10:23:45  clipboard  [Firefox] Inbox — invoice #2026
2026-07-07 10:21:12  keyboard   [Code] refactor.py — def search_events(
2026-07-07 10:20:01  title      [Code] refactor.py — ktransformer_ds_v4_deploy...
```

Use Text tags to colour the event type:

* `clipboard` / `selection` → yellow
* `keyboard` → dark blue
* `title` / `app_name` → green
* `read` / `focus` / `lock` → muted

#### 4.4 Pagination state

Store in `view_state`:

```python
"events_q": "",
"events_types": ["clipboard", "selection", "keyboard", "title", "app_name", "read", "focus", "lock"],
"events_offset": 0,
"events_limit": 100,
```

On search, reset `events_offset = 0` and render.  Next/Prev adjust the offset
and re-fetch.

#### 4.5 Render dispatch

In `frontend/app.py::render()`:

```python
if self.view_state["view"] == "events":
    self._render_events()
```

`_render_events()` calls `self.api.events(...)` and passes the result to
`views.render_events()`.

### 5. Filter bar visibility

Extend `_show_search_filters()` so that in the Events view:

* hide the search-grid-only controls (search box, fields, hits toggle, scope)
* hide the timeline-only controls
* show a small Events-specific top bar inside the tab itself, or reuse the
  existing filter row for the type checkboxes and pagination buttons.

A simple approach is to put all Events controls inside `events_frame` so the
shared filter row only needs to hide everything.

## Critical files to modify

* `daemon/api/models.py` — add `GlobalEvent` and `EventListOut`.
* `daemon/api/routes.py` — add `GET /events`.
* `daemon/db/events.py` — new module for paginated event queries and hybrid
  search.
* `frontend/apiclient.py` — add `GlobalEvent` dataclass and `events()` method.
* `frontend/app.py` — add Events tab, view state, render dispatch, tab-switch
  wiring.
* `frontend/views.py` — add `render_events()` builder.

## Out of scope

* Jumping from an event result to the timeline (can reuse plan 11 later).
* Rich formatting / inline images / screenshots in event rows.
* Server-sent live updates for the event stream; initial version re-renders on
  tab switch or manual refresh.
* Full-text search for `focus`/`lock` events.

## Verification

1. Backend tests:
   * `tests/test_events.py` —
     * latest events returns rows from all tables ordered by `ts DESC`,
     * `type=` filter restricts to requested tables,
     * `q=` returns matching clipboard/selection/keyboard/title rows with
       `<mark>` highlight snippets,
     * pagination `limit`/`offset` works and `total` is correct.
2. Frontend tests:
   * `tests/test_frontend.py` — `ApiClient.events()` round-trips.
3. Manual test:
   * Open Events tab, see latest 100 events.
   * Type a known clipboard string, see matching rows.
   * Check/uncheck event types, see results update.
   * Click Next / Prev, see new pages.
