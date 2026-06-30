# Plan 11 — focus_score ranking for search results

## Goal

Add a per-window **`focus_score`** that ranks windows by a blend of:

* **Recent focused usage** — how many minutes the window was actually focused in the last 5 / 10 / 30 minutes (from `window_heartbeat`).
* **Recency** — how long ago the window was last focused (from `focus_event.focused_at`).

The score is exposed in API responses and available as a sort key in the search grid, so the most recently *and* heavily used windows bubble to the top.

---

## Why not just `last_access`?

`last_access` only tells us *when* a window was last focused. Two windows focused 30 seconds ago look identical even if one was used for 8 minutes and the other for 2 seconds. `focus_score` combines both dimensions.

---

## Inputs per window

| Symbol | Source | Meaning |
|--------|--------|---------|
| `u5` | `usage_5m` | focused minutes in last 5 min (max 5.0) |
| `u10` | `usage_10m` | focused minutes in last 10 min (max 10.0) |
| `u30` | `usage_30m` | focused minutes in last 30 min (max 30.0) |
| `la` | `last_access` | epoch seconds of last `focus_event` |
| `now` | server time | epoch seconds |

---

## Algorithm

```text
recency_min = (now - la) / 60.0            if la is not None else +inf
recency_score = 1.0 / (1.0 + recency_min)   # 1.0 when just focused, -> 0 when old

usage_score = w5  * (u5  / 5.0)
            + w10 * (u10 / 10.0)
            + w30 * (u30 / 30.0)

focus_score = usage_weight * usage_score
            + recency_weight * recency_score
```

### Properties

* All sub-scores are in `[0, 1]`.
* No division by zero: `recency_min` is added to `1.0` before division.
* Windows never focused (`la` is `None`) get `recency_score = 0` and `usage_score = 0`, so `focus_score = 0`.
* A just-focused window with zero usage gets a non-zero score purely from recency.
* A heavily-used window from 10 minutes ago still scores higher than an idle window focused 10 minutes ago.

### Default tunable weights

```python
focus_score_w5: float = 0.5     # most recent usage is most important
focus_score_w10: float = 0.3
focus_score_w30: float = 0.2
focus_score_usage_weight: float = 0.6
focus_score_recency_weight: float = 0.4
```

All weights are optional; if the usage weights do not sum to `1.0`, normalize them internally so `usage_score` stays bounded.

---

## Implementation sketch

### 1. Config (`daemon/config.py`)

Add the five tunable floats above under a `# --- §3d focus_score ---` section.

### 2. Backend compute (`daemon/db/search.py`)

Add a helper:

```python
def _focus_score(row, now: float, s) -> float:
    la = row["last_access"]
    if not la:
        return 0.0
    recency_min = (now - la) / 60.0
    recency = 1.0 / (1.0 + recency_min)

    total_w = s.focus_score_w5 + s.focus_score_w10 + s.focus_score_w30
    if total_w == 0:
        usage = 0.0
    else:
        usage = (
            s.focus_score_w5  * (row["usage_5m"]  or 0) / 5.0 +
            s.focus_score_w10 * (row["usage_10m"] or 0) / 10.0 +
            s.focus_score_w30 * (row["usage_30m"] or 0) / 30.0
        ) / total_w

    total_mix = s.focus_score_usage_weight + s.focus_score_recency_weight
    if total_mix == 0:
        return 0.0
    return round(
        (s.focus_score_usage_weight * usage + s.focus_score_recency_weight * recency) / total_mix,
        4,
    )
```

Call it inside `assemble_window()` (passing `store.s` and a `now` parameter) and add `focus_score` to the returned dict.

`list_windows`, `search`, `timeline`, and `window_detail` already receive `now` implicitly through `time.time()` inside `usage_rates`; pass the same `now` to `_focus_score` so the two metrics are consistent.

### 3. Sort integration (`daemon/db/search.py`)

Add `focus_score` as a recognized sort key:

```python
def _window_sort_col(sort: str) -> str:
    if sort == "title":        return "LOWER(current_title)"
    if sort == "window_id":    return "w.window_uid"
    if sort == "focus_score":  return "focus_score"   # derived in Python, see below
    return "last_access"
```

Because `focus_score` depends on `now`, implement it as a Python-side sort for `search()` and `timeline()` (like the existing `last_access`/`title` sorts) rather than a SQL expression.

### 4. API models (`daemon/api/models.py`)

Add to `WindowOut` and `TimelineLane`:

```python
focus_score: float | None = None
```

### 5. Frontend (`frontend/apiclient.py`, `frontend/app.py`)

* Add `focus_score` to the `Window` and `TimelineLane` dataclasses.
* Add a sort option:
  ```python
  SORT_OPTIONS = [
      ("last access", "last_access"),
      ("focus score", "focus_score"),
      ("title", "title"),
      ("window id", "window_id"),
  ]
  ```
* Optionally show the score on tiles as a small muted label, e.g. `score: 0.72`.

---

## Testing plan

Add a unit test in `tests/test_api_search.py` or a new `tests/test_focus_score.py`:

1. Two windows focused at the same time, one with 5m usage `5.0` and one with `0.0` → the used one scores higher.
2. Two windows with identical usage, one focused 1 minute ago and one 10 minutes ago → the recent one scores higher.
3. A window with `last_access = None` scores `0.0`.
4. Sorting by `focus_score` desc returns windows in the expected order.

---

## Open decisions

* **Default sort:** keep `last_access` as the default or switch to `focus_score`. Suggested: add `focus_score` as an option first, then switch the default after using it live.
* **Score display:** show raw `focus_score` on tiles, or only use it for sorting. Suggested: show a compact `score: 0.72` label so the user can see why the order changed.
