# Plan 14 — Search/History keyword search UX: pagination, event cap, hybrid ranking, hit count

## Context

The **Search** tab (and the time-scoped **History** tab) both call `GET /search` / `GET /history`, which delegate to `daemon/db/search.py::search()`.

Current flow:

1. For every requested field, run FTS5 `MATCH` **or** substring `INSTR` over the whole event table.
2. Each field returns up to `limit * 4` rows (today `limit` is the window page size, default 100).
3. Results are grouped by `window_uid` in Python.
4. A metadata query fetches **all** matched windows plus `alive`/`boot` filters.
5. Every matched window is enriched with usage + focus score.
6. The full list is sorted in Python and sliced to one page.

Why it hangs on "dead" / "both":

- A broad query can match events in thousands of historical (dead) windows.
- Even though each field is `LIMIT`-ed, the per-field cap is tied to the small page size, and the metadata/enrichment step still touches **every** candidate window before sorting.
- `INSTR` substring fallback does a full table scan when FTS5 is bypassed (malformed query or short tokens).
- Python sorting/hit merging over a large candidate set blocks the async loop.

## Goal

Make keyword search on the **Search** and **History** tabs feel instant and useful for large histories:

1. Cap the raw event scan to a configurable top **N** (default 1000) latest matching events per query.
2. Build the candidate window set **only** from those top events.
3. Run a hybrid relevance/recency/usage score on the candidate windows.
4. Support stable pagination (`limit`/`offset`) on the final window list and return a total window count.
5. Return and display a **total hit event count** (matching event rows) alongside the window list.
6. Add sort keys: `last_access` (visit time), `recency` (latest matching event), `relevance` (hybrid), plus usage intervals including a new `usage_1m`.
7. Do not break existing API consumers; keep the current list response as the default.

## Non-goals

- Rewriting the Events tab search (plan 13 already covers that).
- Changing the Timeline lane query.
- Implementing real-time streaming or infinite scroll in this plan.
- Replacing SQLite with an external engine as the first step (alternatives are discussed at the end).

## Proposed design

### 1. Backend: cap the event scan (`daemon/db/search.py`)

Introduce a new internal helper:

```python
async def _event_candidates(store, q, fields, window_uid, t_from, t_to, max_events=1000)
```

Responsibilities:

- Tokenize the query exactly like today (`_safe_fts_query` for FTS, lower-case tokens for substring).
- For each requested field, run **two** bounded queries in parallel:
  - **FTS branch**: `fts_X MATCH ? ORDER BY rank LIMIT per_field_cap`.
  - **Substring branch**: `INSTR(LOWER(text), LOWER(token)) > 0 ORDER BY ts DESC LIMIT per_field_cap`.
- `per_field_cap` can be `max_events // len(fields)` with a floor (e.g. 100), so no single field can starve the others.
- If the query has multiple tokens, run the per-token substring scan with the same cap and intersect by `window_uid` **inside** the bounded result sets.
- Merge all hits into `{window_uid: {"hits": [...], "best_rank": ..., "max_ts": ..., "hit_count": ...}}`.
- Trim the merged map to the top `max_events` rows by `(max_ts DESC, best_rank ASC, window_uid)`.
- Return the candidate map plus a `total_hits_estimate` (sum of per-field FTS `COUNT(*)` when FTS was used; otherwise the capped count with a `+` indicator).

Why this fixes the hang:

- The database never sorts/returns more than a few thousand event rows, regardless of how many dead windows exist.
- Window metadata, usage, and focus-score enrichment only runs on the small candidate set.
- Python sorting operates on at most `max_events` items.

### 2. Backend: metadata join with early filters

After candidate extraction:

```python
uids = list(candidates)
placeholders = ...
where = [f"w.window_uid IN ({placeholders})"]
params = list(uids)
# apply alive / boot filters in SQL
sql = f"SELECT {_WINDOW_COLS} FROM window w WHERE " + " AND ".join(where)
```

- Keep the existing `alive` and `current_boot_id` filters so dead-window rows are dropped before enrichment.
- Apply the time-range filter (`t_from`/`t_to`) against the candidate's `max_ts`/`min_ts` in Python (fast, small set).
- Assemble windows and attach their hit lists.

### 3. Backend: hybrid scoring and sorting

For each candidate window compute:

- `hit_count`: number of matching events.
- `best_rank`: best FTS rank among hits (`None` if only substring matches).
- `max_ts`: timestamp of the latest matching event.
- `last_access`: from `_WINDOW_COLS`.
- `usage_*`: enriched after candidate filter.

A default hybrid score when `sort=relevance`:

```python
def _hybrid_score(window, now, settings):
    recency = 1.0 / (1.0 + (now - (window.max_ts or window.last_access or now)) / 60.0)
    usage = (window.usage_5m or 0) / 5.0 * 0.5 + \
            (window.usage_10m or 0) / 10.0 * 0.3 + \
            (window.usage_30m or 0) / 30.0 * 0.2
    rank_component = 1.0 / (1.0 + (window.best_rank or 1000.0))
    return rank_component * 0.4 + recency * 0.4 + usage * 0.2
```

Expose sort keys:

- `relevance` — `_hybrid_score` DESC.
- `recency` — `max_ts` DESC (latest matching event).
- `last_access` — `last_access` DESC (visit time).
- `usage_1m`, `usage_5m`, `usage_10m`, `usage_30m`, `usage_1d`, `usage_total`.
- `title`, `window_id` (existing).

Implementation note: sort keys that depend on usage still need Python sort, but now over ≤1000 items. For `last_access`, `title`, `window_id`, `recency`, SQL can sort the metadata result directly if we prefer; Python is acceptable given the cap.

### 4. Backend: paginated response shape

Current consumers expect a JSON list. Add an opt-in wrapper to avoid breaking them.

In `daemon/api/routes.py`:

- Add query parameter `wrap: bool = Query(False)` to `/search` and `/history`.
- When `wrap=false` (default), return `list[WindowOut]` exactly as today.
- When `wrap=true`, return a new model:

```python
class SearchResultOut(BaseModel):
    windows: list[WindowOut]
    total: int            # total windows matching the query & filters
    total_hits: int       # total matching event rows (or capped estimate)
    capped: bool          # true if total_hits was limited by max_events
    limit: int
    offset: int
```

The frontend will use `wrap=true` for paginated search. Old callers / tests continue to work unchanged.

### 5. Frontend: paginated grid and status (`frontend/app.py`, `frontend/apiclient.py`)

`frontend/apiclient.py`:

- Add `search_paginated(...)` returning `(list[Window], total, total_hits, capped)`.
- Keep the existing `search()` method for compatibility.

`frontend/app.py`:

- Add view-state keys:
  - `search_limit`: default 100 (same as today).
  - `search_offset`: default 0.
- Update `_render_search` to call `search_paginated(...)` and pass `limit`/`offset`.
- `_on_search_results` receives `(windows, total, total_hits, capped)`.
- Update the status label to show:
  - `"42 windows (1,247 hits)"` when exact.
  - `"42 windows (1,000+ hits)"` when `capped`.
- Add Prev/Next buttons under the grid (similar to the Events tab):
  - Disable Prev when `offset == 0`.
  - Disable Next when `offset + len(windows) >= total`.
- Reset `search_offset` to 0 whenever `q`, `fields`, `alive`, `t_from`, `t_to`, or filters change.
- Keep the existing debounce (`search_debounce_ms`).

### 6. Optional: per-tile hit badge

Each tile already shows title, last access, and usage. Optionally show a small badge:

```
[3 hits] [title]
```

This is low priority and can be added after the core pagination lands.

### 7. Add `usage_1m` sort key

`daemon/heartbeat.py` currently exposes `usage_5m`, `usage_10m`, `usage_30m`, `usage_1d`.

To support "usage_1m":

- Add `"usage_1m": 60` to `USAGE_INTERVALS_S`.
- No schema change is needed because `usage_rates` computes these from `window_heartbeat` on demand.
- Add `usage_1m` to `WindowOut`, `Window` dataclass, frontend `SORT_OPTIONS`, and `SORT_RE` in `daemon/api/routes.py`.

This is bundled here because the new search ranking will expose short-term usage as a first-class sort key.

## Alternative designs and libraries

### Option A: keep SQLite FTS5, just add the event cap (recommended)

Pros: no new dependencies, uses existing schema, minimal code change.
Cons: substring fallback still scans text tables; very broad substring queries on huge history can still be slow, but the cap limits damage.

### Option B: replace substring fallback with a trigram-like token index

SQLite FTS5 already uses the `trigram` tokenizer, so most substring searches are already served by FTS5. The substring fallback is mainly for:

- Very short tokens (1–2 chars).
- Malformed queries.

We can keep the fallback but explicitly refuse to run it without a `window_uid` scope when `q` is shorter than 3 characters, returning an empty list or a "type more" prompt. This removes the worst full-scan case.

### Option C: in-memory inverted index

On daemon startup (or on first search), build a Python inverted index:

```python
{token: {window_uid: [event_refs]}}
```

Search becomes set intersection. Queries are instant, but:

- Memory usage grows with history.
- Index must be kept in sync with new events; easiest if built from the event queue, but adds complexity.
- Schema changes require a rebuild.

Defer this until SQLite FTS5 proves insufficient.

### Option D: dedicated local search engine (`tantivy-py`, `whoosh`, `xapian`)

- **Tantivy** (Rust, `tantivy-py`): fast BM25, incremental indexing, small binary.
- **Whoosh**: pure Python, easy to embed, but slower and effectively unmaintained.
- **Xapian**: mature, but Python bindings are heavier and less common.

Recommended only if plan A still feels slow after deployment. If we later adopt Tantivy, the event-cap / pagination shape designed here still applies; only the `_event_candidates` implementation changes.

### Option E: PostgreSQL with `pg_trgm` + `tsvector`

Would give fast substring and full-text search, but introduces a server process and migration burden. Not appropriate for a single-user desktop daemon.

## API changes

`GET /search` and `GET /history` gain:

- `limit` (already present internally but not exposed by the route) — default from settings.
- `offset` — default 0.
- `wrap=true|false` — default false.

Response when `wrap=true`:

```json
{
  "windows": [ /* WindowOut objects */ ],
  "total": 42,
  "total_hits": 1247,
  "capped": false,
  "limit": 100,
  "offset": 0
}
```

No change to `GET /search` default list response.

## UI changes

Search tab only:

- Status label shows windows + hit count.
- Prev/Next pager below the grid.
- Sort dropdown extended with `relevance`, `recency`, `usage_1m`.
- Search offset reset on any filter/query change.

## Configuration changes

`daemon/config.py`:

- `search_event_cap: int = 1000` — max matching events examined per keyword search.
- `search_min_substring_chars: int = 3` — refuse unscoped substring fallback for shorter queries.

`frontend/config.py`:

- `search_page_size: int = 100` (or reuse existing defaults).

`daemon/heartbeat.py`:

- Add `usage_1m: 60` to `USAGE_INTERVALS_S`.

## Files to modify

- `daemon/db/search.py` — `_event_candidates`, capped collection, hybrid score, pagination, total counts.
- `daemon/api/routes.py` — expose `limit`/`offset`/`wrap`; route wrapper.
- `daemon/api/models.py` — add `SearchResultOut`.
- `frontend/apiclient.py` — add `search_paginated`.
- `frontend/app.py` — paginated search render, status text, Prev/Next.
- `daemon/config.py` — new search knobs.
- `frontend/config.py` — page size.
- `daemon/heartbeat.py` — `usage_1m` interval.
- `tests/test_api_search.py` — add wrapped response + pagination + hit-count tests.
- `tests/test_frontend.py` — verify client wrapper returns counts.

## Tests to add

- `search_event_cap` is respected: a query that would match >cap events returns at most cap candidate windows.
- `wrap=true` returns `total`, `total_hits`, `capped`, `limit`, `offset`.
- Pagination `offset` returns a different page of windows for the same query.
- `sort=usage_1m` orders results correctly.
- Total hit count is exact for FTS queries and capped/estimated for substring fallback.
- Default `wrap=false` still returns a plain list.

## Backward compatibility

- Default `/search` and `/history` responses remain `list[WindowOut]`.
- Existing `limit`/`offset` are not currently exposed, so adding them is safe.
- `frontend/app.py` switches to `wrap=true`; old manual callers that use `ApiClient.search()` keep working.

## Verification

1. Run unit tests:
   ```bash
   python -m tests.test_api_search
   python -m tests.test_frontend
   python -m tests.test_heartbeat
   ```
2. Manual smoke test:
   - Open the Search tab.
   - Select "both" (alive + dead).
   - Type a common 3+ character keyword.
   - Result should return in <500 ms.
   - Status shows "N windows (M hits)".
   - Prev/Next pages navigate correctly.
   - Sort by "recency", "last access", and "1m usage" reorder the grid.
