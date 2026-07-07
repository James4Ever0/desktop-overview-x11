# Plan 15 — Windows tab: why dead/both is slow, cancellation, and chunked loading

## Context

The **Windows** tab (formerly Search) renders either:

- `GET /windows` with no keyword query.
- `GET /search` when a keyword query or time range is active.

The user reports that clicking the **dead** radio button (or **both**) in the Windows tab is slow, and asks:

1. Why is dead-window retrieval slow?
2. Can ongoing requests be cancelled when the user switches back to **alive**?
3. Can the frontend load results in small pages (10–20 windows at a time) and update incrementally?
4. Can enrichment/scoring run in a background process, update the view when ready, and be cancelled on refresh?
5. Since dead windows are closed, should they hide recent usage / focus score and show only `usage_total`?
6. If no usage/focus-score value is given, how should sorting work?  Treat missing values as zero.

This plan addresses the empty-query / `/windows` path.  Keyword search already has its own performance plan (plan 14) and is out of scope here.

## 1. Diagnosis: why dead/both is slow

Current path for an empty query:

```text
frontend/app.py::_render_search
  → ApiClient.windows(alive=..., sort=..., order=...)
    → GET /windows
      → daemon/db/search.py::list_windows()
```

`list_windows()` does the following for **every** window that matches the `alive` filter:

1. Runs a metadata query (`_WINDOW_COLS`) that includes correlated subqueries for:
   - `last_access` from `focus_event`
   - `current_title` from `title_history`
   - latest `window_capture` path/timestamp
2. Calls `_enrich_usage()` → one aggregate query over `window_heartbeat` for **all** matched `window_uid`s.
3. Calls `_enrich_focus_score()` → computes focus score from usage in Python for every row.
4. Sorts the full result set in Python if `sort` is `focus_score` or any `usage_*` key.
5. Only then slices to `limit`/`offset`.

The slowness comes from a few specific places:

- **Usage enrichment is unconditional.**  Even when sorting by `last_access`, `title`, or `window_id`, `list_windows` still queries `window_heartbeat` and computes focus scores for every candidate.
- **Dead windows can be numerous.**  A long-lived session can accumulate thousands of dead windows.  With `alive=dead` or `alive=both`, the metadata query and usage query touch all of them before pagination cuts the list down.
- **Usage/focus score sorts are global.**  If the user sorts by `focus_score` or `usage_*`, the backend must score every dead window before it knows which ones are top-N.  These scores are usually zero for dead windows, but the work is still done.
- **The frontend requests up to 200 windows in one synchronous call.**  `ApiClient.windows()` does not expose `limit`/`offset`, so the frontend always asks for the configured default (200).  The backend enriches and sorts a much larger set to satisfy that request.

### Why the enrichment itself is expensive

1. **`_enrich_usage()` does a grouped aggregate over `window_heartbeat`.**
   - It builds one SQL statement with one `SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END)` column per usage interval.
   - The `WHERE window_uid IN (...)` clause can contain thousands of ids for `alive=dead`/`both`.
   - SQLite must scan every matching heartbeat row, group by `window_uid`, and compute the sums.
   - It then runs a second query for `usage_total` that counts *all* heartbeats for those ids.

2. **`_enrich_focus_score()` is pure Python over every returned row.**
   - It computes recency, weighted usage, and the blended score for each window.
   - With thousands of rows this is not huge, but it happens synchronously inside the API handler.

3. **The metadata query (`_WINDOW_COLS`) uses correlated subqueries.**
   - `current_title` is fetched per row via a `title_history` lookup.
   - `last_access` is fetched per row via `focus_event`.
   - These are fine for a page of 20–200 rows, but they still add up for thousands of dead windows.

### Do we have proof today?

No — there are no timing logs yet.  We can prove it by adding `time.perf_counter()` logs around each phase.

### Exact log insertion points

**Backend — `daemon/db/search.py::list_windows`**

Add a helper and log around each phase:

```python
import time

_T = lambda: time.perf_counter() * 1000
```

Inside `list_windows`:

```python
async def list_windows(store, *, alive="both", sort="last_access", order="desc",
                       limit=200, offset=0, current_session_key=None,
                       title_denylist=None, self_xid=None,
                       current_boot_id=None) -> list[dict]:
    t0 = _T()
    # ... existing where / params build ...

    now = time.time()

    if sort == "focus_score" or sort.startswith("usage_"):
        sql = f"SELECT {_WINDOW_COLS} FROM window w {where_clause}"
        rows = await store.fetchall(sql, tuple(params))
        t1 = _T()
        log.info("list_windows metadata alive=%s sort=%s rows=%d ms=%.1f",
                 alive, sort, len(rows), t1 - t0)
        results = [assemble_window(r, current_session_key) for r in rows]
        results = _filter_denied(results)
        results = _filter_self(results, self_xid)
        await _enrich_usage(store, results, now=now)
        t2 = _T()
        log.info("list_windows enrich_usage alive=%s rows=%d ms=%.1f",
                 alive, len(results), t2 - t1)
        await _enrich_focus_score(store, results, now=now)
        t3 = _T()
        log.info("list_windows focus_score alive=%s rows=%d ms=%.1f",
                 alive, len(results), t3 - t2)
        # ... sort and slice ...
        return results[offset:offset + limit]

    sort_col = _window_sort_col(sort)
    direction = "ASC" if order == "asc" else "DESC"
    secondary = "w.window_uid ASC"
    sql = (f"SELECT {_WINDOW_COLS} FROM window w "
           + where_clause
           + f" ORDER BY {sort_col} {direction}, {secondary} LIMIT ? OFFSET ?")
    rows = await store.fetchall(sql, tuple(params + [limit, offset]))
    t1 = _T()
    log.info("list_windows metadata alive=%s sort=%s rows=%d ms=%.1f",
             alive, sort, len(rows), t1 - t0)
    results = [assemble_window(r, current_session_key) for r in rows]
    results = _filter_denied(results)
    results = _filter_self(results, self_xid)
    await _enrich_usage(store, results, now=now)
    t2 = _T()
    log.info("list_windows enrich_usage alive=%s rows=%d ms=%.1f",
             alive, len(results), t2 - t1)
    await _enrich_focus_score(store, results, now=now)
    t3 = _T()
    log.info("list_windows focus_score alive=%s rows=%d ms=%.1f",
             alive, len(results), t3 - t2)
    return results
```

**Backend — `daemon/heartbeat.py::usage_rates`**

Log the size of the input set and the query duration:

```python
async def usage_rates(store, window_uids: list[int], now: float | None = None) -> dict[int, dict]:
    if not window_uids:
        return {}
    now = time.time() if now is None else now
    t0 = _T()
    # ... existing query ...
    rows = await store.fetchall(sql, tuple(params))
    t1 = _T()
    log.info("usage_rates uids=%d rows=%d ms=%.1f", len(window_uids), len(rows), t1 - t0)
    # ... rest unchanged ...
```

**Frontend — `frontend/app.py::_render_search` and `_on_search_results`**

```python
def _render_search(self):
    st = self.view_state
    self.status_var.set("loading…")
    q = st["q"].strip() or None
    scope = st["selected_window_uid"]
    self_xid = self._self_xid if st.get("hide_self") else None
    t0 = time.perf_counter()

    def call():
        # existing call body
        ...

    def on_done(res, err):
        dt = (time.perf_counter() - t0) * 1000
        log.info("_render_search round-trip view=%s alive=%s sort=%s ms=%.1f",
                 st.get("view"), st.get("alive"), st.get("sort"), dt)
        # existing callback body
        ...

    gen = self._render_generation
    self._submit(call, self._wrap_callback(gen, st.get("view"), on_done))
```

Run with `DESKTOP_OVERVIEW_LOG_LEVEL=info` and open the Windows tab with `alive=dead`.  The logs will show which phase dominates.

## 2. Short-term backend optimisation

Before changing the loading model, make the existing `/windows` path cheaper:

### 2a. Skip recent usage / focus-score for dead windows

For **dead** windows the UI should show only `usage_total`; recent intervals (`usage_5m`, `usage_10m`, `usage_30m`, `usage_1d`) and `focus_score` are not useful because the window is closed.

In `daemon/db/search.py::list_windows()`:

- If `alive == "dead"` and the sort key is **not** `focus_score` or a `usage_*` key, do **not** call `_enrich_usage()` or `_enrich_focus_score()`.
- If `alive == "both"` and the sort key is not usage/focus-score, only enrich usage for rows where `alive == 1`.  Set usage fields to zero and `focus_score` to `None` for dead rows without querying `window_heartbeat`.
- If the sort key **is** `focus_score` or `usage_*`, every row needs a value so sorting is stable.  Treat dead rows as `0.0` for those fields (they naturally sink to the bottom when sorted descending).  Do **not** compute focus scores for dead rows.

This makes the default `last_access` sort fast for `alive=dead`, because the metadata query is a simple `SELECT ... FROM window WHERE alive=0 ORDER BY last_access DESC LIMIT 200 OFFSET 0` with no heartbeat aggregation.

The sort dropdown can keep all options; dead rows simply sort as zero for usage/focus-score columns.

### 2b. Always pass `limit`/`offset` to the metadata query

For non-usage sorts, `list_windows` already uses SQL `LIMIT`/`OFFSET`.  Ensure the `WHERE alive` and `current_boot_id` filters are applied in SQL, not in Python after enrichment.

## 3. Frontend cancellation of stale requests

The frontend runs API calls in a `ThreadPoolExecutor` with **synchronous** `httpx`.  A single large in-flight HTTP request cannot be cleanly cancelled mid-flight without closing the underlying connection, which would also abort any other concurrent request.

Therefore the practical cancellation strategy is:

- Give every render a generation number (`self._render_generation`).
- For **chunked** loading (see §4), after each small page returns, check the generation.  If it changed, stop requesting more pages.
- For the current single-request model, the best we can do is **ignore the result** when it finally arrives (the `_wrap_callback` helper already does this for view mismatches).  We should extend it to also drop results when the alive filter has changed since the request started.

To truly cancel mid-flight, we would need to either:

- Switch the Windows tab to short pages (recommended), or
- Run `httpx` in streaming/ASGI mode inside the worker, which adds complexity.

## 4. Chunked / paged loading for empty queries

### 4a. Goal

When there is no keyword query, load windows in pages of 10–20, render each page as soon as it arrives, and stop when the server returns an empty or partial page.

### 4b. Backend changes

`GET /windows` already supports `limit` and `offset` query parameters, but `ApiClient.windows()` does not expose them.

Changes:

- `frontend/apiclient.py::ApiClient.windows()` gains `limit=20` and `offset=0` parameters and forwards them to `/windows`.
- Optionally, `daemon/api/routes.py::get_windows` can return a `X-Total-Count` response header so the UI knows the final total.  This is optional because an empty or under-sized page already signals "no more".
- The backend optimisation in §2a must be in place first; otherwise each small page still triggers a global sort/enrich when the user sorts by usage/focus-score.

### 4c. Frontend changes

`frontend/app.py`:

- Add view-state keys:
  - `windows_page_size`: default 20.
  - `windows_offset`: current chunk offset.
  - `windows_loading`: True while more chunks may exist.
- In `_render_search`, when the query is empty:
  - Reset `windows_offset = 0` and `windows_loading = True`.
  - Clear the grid immediately.
  - Start a worker that calls `api.windows(limit=page_size, offset=0, ...)`.
- On receiving a chunk:
  - If the render generation or alive filter changed, drop the chunk.
  - Otherwise, **append** the new windows to the grid (`_reconcile_tiles` already supports incremental updates by uid).
  - If the chunk size == page_size, schedule the next offset (`offset += page_size`).
  - If the chunk size < page_size or is empty, set `windows_loading = False`.
- Update the status label incrementally, e.g.:
  - `"loading… 20 loaded"`
  - `"47 window(s)"` when done.
- When the user changes `alive` or any filter, increment `self._render_generation`, which makes any in-flight chunk handler drop its result and stops scheduling further chunks.

### 4d. Sorting complications

Chunked loading works cleanly only for sorts that the database can compute globally per page:

- `last_access` (default)
- `title`
- `window_id`

For `focus_score` and `usage_*` sorts, the database cannot produce page N without scoring all windows first.  Options:

1. **Disable usage/focus-score sorts when `alive` is `dead` or `both`.**  Simplest and arguably correct: these scores are only meaningful for windows the user has recently focused.
2. **Keep global scoring but cap the candidate set** (e.g., top 500 by `last_access`, then score and sort those).  Approximate but fast.
3. **Load all windows for usage sorts** with a progress message, falling back to the current behaviour.  Acceptable if the user rarely sorts dead windows by usage.

Recommended: option 1 for the first implementation, with a UI note that usage sorts are only available for "alive only".

## 5. What the user will see

1. Click **dead** → grid clears, first 20 dead windows appear almost immediately, status says "loading…", more rows stream in 20 at a time.
2. Click **alive** while dead rows are still loading → dead loading stops, alive rows start loading from offset 0.
3. With the backend optimisation, switching to **dead only** + default sort should feel instant because no heartbeat aggregation is run.
4. Dead window tiles show only **total usage**; recent usage (5m/10m/30m/1d) and focus score are hidden.  Sorting by those columns still works because dead rows are treated as zero.

## 6. Background enrichment process

The user also asked whether enrichment/scoring can run in a background process, update the view when ready, and be cancelled on refresh.  This is a more robust long-term fix than simply skipping dead-window scores.

### 6a. Goal

- The Windows tab should paint the grid as fast as a plain `SELECT` from the `window` table.
- Usage and focus-score numbers appear shortly afterwards, only for the rows that are actually visible.
- If the user changes a filter while scoring is in progress, the old scoring task is abandoned and a new one starts.
- For dead windows, only `usage_total` is computed and shown; recent usage and focus score are omitted.

### 6b. Design

Make enrichment **optional and cancellable**:

1. Add an `enrich` query parameter to `GET /windows` (and, later, `GET /search`).
   - `enrich=0` → return windows with metadata but **no** `usage_*` or `focus_score` fields.
   - `enrich=1` → existing behaviour (compute scores).
   - Default `enrich=1` for backward compatibility.

2. Add a new endpoint `POST /windows/scores` (or `GET /windows/scores?uids=1,2,3`) that accepts a list of `window_uid`s and returns only the usage/focus-score records for those ids.  For **dead** uids it returns only `usage_total`; recent usage and `focus_score` are omitted.

3. In the daemon, keep a **per-request scoring task**:
   - Each call to `/windows/scores` creates an `asyncio.Task` that runs `_enrich_usage` + `_enrich_focus_score`.
   - Store the task in `DaemonContext` keyed by a client request id (or simply replace the previous task for the same endpoint).
   - When a new `/windows/scores` request arrives, `cancel()` the previous task.
   - The task checks `asyncio.current_task().cancelled()` between rows/batches so it stops promptly.

4. Frontend two-phase loading:
   - **Phase 1 (fast):** call `/windows?enrich=0&limit=20&offset=0` and render the grid immediately.
   - **Phase 2 (background):** call `/windows/scores?uids=uid1,uid2,...` for the visible uids.  Alive rows get full recent usage + focus score; dead rows get only `usage_total`.
   - When scores return, patch the matching tiles in place (`_update_tile`) instead of re-rendering the whole grid.
   - If the user changes `alive`, sort, or query, abandon the pending scores request and start Phase 1 again.

### 6c. Why this works

- Scoring only the visible 20–200 rows avoids the aggregate heartbeat query over thousands of dead windows.
- Because the daemon is async, cancellation is clean: cancelling the `asyncio.Task` stops the query and Python loops.
- The first paint is fast because `enrich=0` skips both `_enrich_usage` and `_enrich_focus_score`.
- Existing callers that do not pass `enrich=0` keep getting full scores, so the API change is backward-compatible.

### 6d. Caveats

- Sorting by `focus_score` or `usage_*` still requires global scores.  Handle this by either:
  - Disabling those sorts when `alive` is `dead`/`both` (recommended first step).
  - Maintaining an in-memory score cache updated incrementally by heartbeat events, then sorting from the cache.
- `POST /windows/scores` should be idempotent and read-only; it must not block the writer.

## 7. API / model changes

- `GET /windows` gains optional `enrich=0|1` query parameter.
- New endpoint `POST /windows/scores` (or `GET /windows/scores?uids=...`) returns usage/focus-score dicts.  Dead uids return only `usage_total`.
- `frontend/apiclient.py::ApiClient.windows()` gains `limit` and `offset`.
- Add `ApiClient.window_scores(uids: list[int]) -> dict[int, dict]`.
- No response model changes for `/windows` if we rely on empty/short pages as the end-of-stream signal.
- Optional: `GET /windows` returns `X-Total-Count` header for a progress indicator.

## 8. Configuration changes

- `frontend/config.py`: add `windows_page_size: int = 20`.

## 9. Files to modify

- `daemon/db/search.py` — optional `enrich` flag; skip usage/focus-score when disabled; add phase logging.
- `daemon/heartbeat.py` — add timing log in `usage_rates`.
- `daemon/api/routes.py` — expose `enrich` on `/windows`; add `/windows/scores` route with cancellable task.
- `daemon/api/app.py` — store the current scoring task in `DaemonContext`.
- `frontend/apiclient.py` — `windows(limit, offset)`, `window_scores(uids)`.
- `frontend/app.py` — two-phase loading, tile patching, cancellation.
- `frontend/config.py` — `windows_page_size`.
- `tests/test_api_search.py` — verify `/windows?enrich=0` omits scores and `/windows/scores` returns them.
- `tests/test_frontend.py` — verify `ApiClient.windows(limit, offset)` and `window_scores()` round-trip.

## 10. Verification

1. Add phase logs, run Windows tab with `alive=dead`, confirm `_enrich_usage` dominates.
2. Apply `enrich=0` fast path: first paint should be under ~100 ms.
3. Verify `/windows/scores` populates scores for visible tiles only, and dead tiles show only `usage_total`.
4. Change `alive` while scores are loading: confirm old scoring task is cancelled (daemon log or task id check).
5. Enable chunked loading, observe incremental rendering.
6. Run `python -m tests.test_api_search` and `python -m tests.test_frontend`.

## 11. Out of scope

- Keyword search performance (covered by plan 14).
- Timeline performance.
- Replacing SQLite with an external search engine.

---

**Where the plans live:** all plans are in `refactor/plans/` as Markdown files.  The current plan is at `plans/15-windows-tab-dead-checkbox-performance.md`.
