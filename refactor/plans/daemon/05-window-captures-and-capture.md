# 05 — WindowCaptures & Screenshot Capture

Covers:

> store window captures along with timestamp. can get latest window
> window_capture.

…plus the power-efficiency scheme from `reference_v2/power_efficient_refresh.txt`.
This is the half of `demo-no-ocr-efficient-refresh.py` that stays in the daemon
(the UI half goes to the frontend, `frontend/08`).

---

## 1. What we lift from the demo (unchanged logic)

Keep these functions essentially verbatim, called through the executor
(`daemon/01 §4`):
- `get_window_list()` — `wmctrl -l` → `[(win_id, title)]`.
- `capture_window(win_id)` — `import -window <id> png:-` → PNG bytes / PIL image.
- `get_active_window_id()` / `normalize_win_id()` — focus detection for "always
  refresh the focused window".
- `get_app_name()` — `xprop WM_CLASS` (also used by `WindowRegistry`, `02`).
- The `_select_background_batch()` shuffled-rotation queue logic — the heart of
  power-efficient refresh.

OCR (`ocr_image`) is **out** for v1 (the chosen reference is the *no-ocr*
variant; `OCR_ENABLED=False`). The schema leaves room to add an OCR-text field
later (`06`), but no OCR runs in the daemon now.

## 2. Power-efficient refresh, as a daemon task

From `power_efficient_refresh.txt`:

> first refresh on program start is always full refresh. Then every X seconds:
> always update the focused window + up to Y background windows drawn from a
> shuffled queue; when the queue runs out, recollect all background ids,
> shuffle, refill.

As an asyncio task (`window_capture_scheduler`):

```
on startup:      FULL sweep — capture every window (fills DB immediately)
every X seconds: tick:
    windows = await executor(get_window_list)        # cheap
    reconcile with WindowRegistry (alive/closed)     # 02 §3
    focused = await executor(get_active_window_id)
    targets = [focused] + select_background_batch(Y) # shuffled rotation
    await capture_stream(targets)  -> per result, save file + DB row
```

- `X = REFRESH_INTERVAL_S` (demo default 5), `Y = REFRESH_BATCH_SIZE` (demo
  default 3). Both config-driven.
- Capture concurrency reuses the demo's `capture_stream` (asyncio +
  `run_in_executor`), so a tick captures `focused + ≤Y` windows in parallel.
- A `POST /window_captures/refresh` API call (or the frontend's "Refresh" button)
  triggers a **full** sweep on demand (`api/07 §5`).

The scheduler runs **whether or not** the frontend is open — that's the whole
point of moving it into the daemon: window_captures stay fresh for later search even
when no UI is up.

## 3. On-disk storage layout (images = files, DB = paths)

`schematic.txt`: *"file for storing images. reference those images with relative
file path in the database."*

```
<data_dir>/                         # e.g. ~/.local/share/desktop-overview/
  daemon.sqlite3
  window_captures/
    <daemon_boot_id>/
      <window_uid>/
        <ts_millis>.png             # one file per capture
        ...
```

- DB stores the path **relative to `<data_dir>`**
  (`window_captures/<daemon_boot_id>/<window_uid>/<ts>.png`), so the data dir can be
  moved/backed up wholesale. API resolves relative→absolute at serve time.
- Saving: write PNG bytes the executor already produced; we do **not** re-encode
  on the loop. Use the demo's `_cap_large` cap (≤ screen size) before saving to
  bound file size; the frontend downsizes to its own window_capture dimensions.
- "**Latest** window capture" = the newest row in `window_capture` for a
  `window_uid` (indexed by `(window_uid, captured_at DESC)`), or
  `window_capture_latest` view. API: `GET /windows/{uid}/window_capture/latest`.

## 4. Retention / disk hygiene

Captures accumulate quickly (every window, every few seconds). Bound growth:
- Keep **only the latest N** window_captures per `window_uid` (e.g. N=5) for history,
  plus the single latest for the grid — older files pruned by a periodic
  `window_capture_gc` task that deletes both the file and its DB row.
- Or time-based: drop captures older than `WINDOW_CAPTURE_RETENTION_DAYS`.
- When a window goes `closed`, keep its **last** window_capture (so dead windows
  still render in history/timeline) but prune the rest.
- All GC is a low-priority asyncio task; deletes run through the executor.

Config knobs (this section): `REFRESH_INTERVAL_S`, `REFRESH_BATCH_SIZE`,
`WINDOW_CAPTURE_KEEP_PER_WINDOW`, `WINDOW_CAPTURE_RETENTION_DAYS`, `WINDOW_CAPTURE_MAX_DIM`, `OCR_ENABLED`.
Defaults, types, and set-via → **`10-configuration.md §3`**.

## 5. WindowCapture table (see `06` for full schema)

```
window_capture(
  id INTEGER PK,
  window_uid INTEGER FK,
  rel_path TEXT,        -- window_captures/<boot>/<uid>/<ts>.png
  width INTEGER, height INTEGER,
  captured_at REAL,
  is_focused INTEGER    -- captured because it was the focused window this tick
)
```

## 6. Module shape

```
daemon/capture.py     # get_window_list/capture_window/... (from demo, made importable)
daemon/window_captures.py  # the scheduler task + background-batch rotation + GC + file save
```

Refactor note: in the demo these functions are module-level and already
side-effect-free except for logging — lifting them is a near-verbatim copy
(`coding-rules.md`: "simple file copy" over re-dumping). The Tk-specific bits
(`_make_window_capture_photo`, `_cap_large` using `winfo_screenwidth`) move to the
frontend; the daemon caps using a fixed `WINDOW_CAPTURE_MAX_DIM` instead of the live
screen size.
