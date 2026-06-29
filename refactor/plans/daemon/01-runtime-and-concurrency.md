# 01 — Daemon Runtime & Concurrency

**Hard requirement (`schematic.txt`): the daemon is fully async and
non-blocking.** This file defines exactly how we honor that given that every
X11 event source is a *blocking* loop, and `import`/`xclip`/`wmctrl` are
*blocking* subprocesses.

---

## 1. The contract

> The asyncio event loop thread must never make a blocking call.

Everything that can block is pushed off the loop:

| Blocking thing                         | Where it runs                              |
|----------------------------------------|--------------------------------------------|
| `dpy.next_event()` (Xlib pump)         | dedicated **Thread A**                     |
| `record_enable_context()` (XRecord)    | dedicated **Thread B**                     |
| `import -window` screenshot capture    | `ThreadPoolExecutor` via `run_in_executor` |
| `xclip` selection reads                | executor, or `asyncio.create_subprocess_exec` |
| `wmctrl -l` / `xdotool` / `xprop`      | executor / async subprocess                |
| SQLite reads/writes                    | `aiosqlite` (its own thread internally)    |

## 2. Thread → loop marshaling

Collector threads never touch asyncio objects or the DB directly. They call a
single thread-safe shim:

```python
def emit(self, event: dict) -> None:
    # called from Thread A / Thread B
    self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
```

The loop runs an **event dispatcher** task:

```python
async def dispatch_loop(self):
    while True:
        ev = await self._queue.get()
        handler = self._handlers[ev["kind"]]
        await handler(ev)        # validates, enriches, enqueues a DB write
```

`asyncio.Queue` is **not** thread-safe for `put` from another thread — that's
why threads use `call_soon_threadsafe(queue.put_nowait, ev)`, which schedules
the put *on the loop thread*. This is the canonical pattern; do not
`put_nowait` directly from a collector thread.

Back-pressure: use a bounded queue (`maxsize≈10_000`). If it ever fills (e.g.
a paste storm), drop **lowest-value** events first (keyboard repeats) and log a
counter; never block the producer thread on a full queue.

## 3. The DB writer (single writer, WAL)

SQLite tolerates many concurrent readers but only one writer. We therefore
funnel **all** writes through one task:

```python
async def db_writer(self):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        while True:
            batch = await self._drain_write_queue(max_n=200, max_wait=0.25)
            await db.executemany(...)   # grouped by statement
            await db.commit()
```

- **WAL mode** lets the API's read connections run concurrently with the
  writer — essential since the API and collectors share the process.
- **Batch + periodic commit** (every ≤250 ms or 200 rows) keeps fsync cost off
  the hot path; keyboard events especially are high-rate.
- API handlers get **read-only** connections (separate `aiosqlite.connect`,
  `PRAGMA query_only=ON`), so a slow query can never stall the writer.

Knobs (this section): `WRITE_QUEUE_MAX_N` (200), `WRITE_QUEUE_MAX_WAIT_S`
(0.25). Full table → `10-configuration.md §4`.

## 4. Subprocess discipline

Two acceptable patterns; pick per call-site:

- **Executor** (simplest, reuses the existing sync functions verbatim from
  `reference_v2`):
  ```python
  img = await loop.run_in_executor(pool, capture_window, win_id, title)
  ```
  The capture half of `demo-no-ocr-efficient-refresh.py` already does exactly
  this — we keep `capture_window`/`get_window_list` unchanged and just call
  them through the executor.
- **Native async subprocess** (preferred for many small `xclip` calls):
  ```python
  proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
  out, err = await proc.communicate()
  ```

Executor sizing: `max_workers = min(8, cpu_count())` (same as the demo). The
executor is for *capture/OCR*; tiny `xclip` reads should prefer the async
subprocess path so they don't starve capture slots.

## 5. Startup / shutdown sequence

**Startup**
1. Resolve identity (boot id, session id, daemon boot id — `daemon/02`).
2. Open DB, run migrations / `CREATE TABLE IF NOT EXISTS`, enable WAL.
3. Start core loop tasks: `db_writer`, `dispatch_loop`, `kbd_aggregator`,
   `window_capture_scheduler`.
4. Start collector threads (A: Xlib pump, B: XRecord pump), handing each the
   loop ref + `emit` shim.
5. Do **one full window_capture sweep** (the demo's "first refresh is always full").
6. Start uvicorn server task (binds UDS + optional TCP).
7. `await asyncio.gather(...)` / run forever.

**Shutdown** (SIGINT/SIGTERM via `loop.add_signal_handler`)
1. Stop accepting API requests (uvicorn graceful).
2. Signal collector threads to stop (set an `Event`; for Thread B call
   `record_disable_context` from the *control* connection — see `daemon/03`).
3. Flush the write queue, final `commit()`, close DB.
4. Shut down executor (`cancel_futures=True`), close X connections.

## 6. Why not run X event loops *in* asyncio?

`python-xlib` exposes the connection's fd, so in principle one could
`loop.add_reader(dpy.fileno(), ...)` and pump with
`dpy.pending_events()`. That works for the **PropertyNotify/XFIXES**
connection and is a valid optimization (removes Thread A). But **XRecord**'s
`record_enable_context` is a blocking call that owns its connection, so Thread
B is unavoidable regardless. To keep one uniform mental model we start with
**both sources in threads**; migrating Thread A to `add_reader` later is a
drop-in change behind the same `emit` shim. Noted as a future optimization,
not required for v1.

## 7. Failure isolation

- Each collector thread wraps its loop in try/except and an Xlib
  `set_error_handler` (windows vanish constantly → `BadWindow`; ignore, never
  crash — the demos already do this).
- A crashed collector thread is **restarted** by a supervisor task (max N
  restarts/min, then mark that collector "degraded" and surface via an API
  health field) — the rest of the daemon keeps running.
- DB writer errors are logged and retried; a poisoned row is quarantined, not
  fatal.
