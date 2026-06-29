# 00 — Overview & Architecture

Refactor of the "desktop overview with search" tool from a single monolithic
tkinter app (`reference_v2/demo-no-ocr-efficient-refresh.py`, which both
*collects* data and *renders* it) into a **background daemon** that owns all
collection + storage + querying, and a **thin tkinter frontend** that only
renders and asks the daemon questions.

This file is the top-level map. Per-topic detail lives in the sibling files
(see `README.md` for the index).

---

## 1. Goals (from `target.txt` + `schematic.txt`)

- A **background daemon** that:
  - listens for desktop events (focus, titles, virtual desktops, clipboard,
    selection, paste/read events, keyboard),
  - stores them durably (SQLite for text, files for images),
  - exposes a **query API** (HTTP and/or UNIX domain socket).
- Move compute **out of the frontend** and into the daemon. The current app
  does screenshotting, list reconciliation, OCR, and substring search inside
  the Tk main loop; all of that becomes the daemon's job.
- The daemon is **fully async / non-blocking** (see `daemon/01`).
- Frontend = tkinter; daemon API = FastAPI.
- Ship `requirements.txt` + a pinned Python version (see `09`).

## 2. The two questions in `target.txt`, answered

> **"use ... http api ... or unix domain socket?"**

Both, from the *same* server. Serve FastAPI under uvicorn bound to a **UNIX
domain socket** as the primary transport (local-only, no port collisions, no
network exposure of personal activity data), and optionally also a
`127.0.0.1` TCP port for convenience / debugging. Decision detail in
`api/07-api-design.md §2`.

> **"can we put api code in the daemon?"**

**Yes — and we should.** Run the FastAPI app *inside* the daemon process, on
the *same* asyncio event loop that runs the collectors and the DB writer. No
second process, no inter-process queue: the API handlers read the same SQLite
connection pool the collectors write to. This is the recommended design.
Rationale and the rejected alternative (separate API process) are in
`api/07-api-design.md §1`.

## 3. Process & concurrency model (one process, one event loop)

```
                          daemon process (asyncio loop)
  ┌─────────────────────────────────────────────────────────────────────┐
  │  asyncio event loop (main thread)                                     │
  │   ├─ uvicorn/FastAPI server      (serves UDS + optional TCP)          │
  │   ├─ DB writer task              (drains write-queue -> aiosqlite)    │
  │   ├─ window_capture scheduler task    (power-efficient refresh, §05)       │
  │   ├─ keyboard aggregator task    (idle/focus/title flush, §04)       │
  │   └─ event dispatcher            (drains event-queue from threads)    │
  │                                                                       │
  │  asyncio.Queue  <───────────────────────────────┐                    │
  └──────────────────────────────────────────────────┼───────────────────┘
        ▲ call_soon_threadsafe                        │ run_in_executor
        │                                             ▼
  ┌─────┴───────────────┐  ┌──────────────────────┐  ┌────────────────────┐
  │ Thread A: Xlib pump │  │ Thread B: XRecord pump│  │ ThreadPoolExecutor │
  │  PropertyNotify +   │  │  KeyPress (visible)   │  │  blocking subprocs: │
  │  XFIXES owner-change│  │  + middle-click       │  │  import (capture),  │
  │  (focus, title,     │  │  (paste candidates)   │  │  xclip, wmctrl,     │
  │   vdesktop,         │  │                       │  │  xdotool, xprop     │
  │   clipboard, sel)   │  │                       │  │                     │
  └─────────────────────┘  └──────────────────────┘  └────────────────────┘
```

Why threads at all if we're "async"? Because the X11 event sources are
**blocking C-level loops** (`dpy.next_event()`, `record_enable_context()`) that
cannot be awaited. We isolate each blocking source in its own thread and
marshal every event onto the asyncio loop via `loop.call_soon_threadsafe(...)`.
The asyncio loop itself **never blocks** — all heavy/blocking work (screenshot,
xclip, DB) is dispatched to the executor or to aiosqlite. Full rules in
`daemon/01-runtime-and-concurrency.md`.

### Connection grouping (important)
- **One** Xlib display connection can serve focus + title + virtual-desktop +
  clipboard-owner + PRIMARY-selection-owner, because they are all delivered as
  `PropertyNotify` / XFIXES `SetSelectionOwnerNotify` on that one connection's
  event queue (Thread A).
- **XRecord requires its own pair of connections** (one to drive the context,
  one for keysym lookups) — that's Thread B. Keyboard capture and middle-click
  paste candidates both ride this single RECORD context.

## 4. Data storage at a glance

- **SQLite** (WAL mode) for all text + metadata + event rows + FTS index.
- **Files on disk** for window_captures/screenshots; the DB stores a **relative
  path** plus timestamp, never the image bytes.
- Schema, FTS5 design, and the on-disk image layout: `daemon/06-data-model.md`.

## 5. Mapping reference_v2 demos → daemon modules

Per `coding-rules.md` ("copy and rename current scripts ... make it callable
from other code ... write minimal logic in main script"), each demo becomes a
**collector module** with its print-loop replaced by an emit-callback. Nothing
is rewritten from scratch.

| reference_v2 demo                          | becomes                          | covered in |
|--------------------------------------------|----------------------------------|------------|
| `demo-get-window-title-change.py`          | focus + title collector          | daemon/03  |
| `demo-virtual-desktop-monitoring.py`       | virtual-desktop collector        | daemon/03  |
| `demo-clipboard-event-listener.py`         | clipboard collector              | daemon/03  |
| `demo-selection-event-listener-v1.py`      | PRIMARY-selection collector      | daemon/03  |
| `demo-detect-paste-event-xrecord.py`       | paste/read + keyboard capture    | daemon/03,04 |
| `demo-keyevent-listener-...chunking.py`     | keyboard segment aggregator (idle/focus/title chunking) | daemon/04 |
| `demo-no-ocr-efficient-refresh.py` (capture half) | window_capture engine          | daemon/05  |
| `demo-no-ocr-efficient-refresh.py` (UI half) | frontend                       | frontend/08 |
| `login-session-id-v2.py`                   | boot/session identity            | daemon/02  |

## 6. Read next

1. `daemon/01-runtime-and-concurrency.md` — the async/threading contract.
2. `daemon/02-session-and-window-lifecycle.md` — boot id ↔ window id, liveness.
3. `daemon/03-event-collectors.md` — the X11 sources.
4. `daemon/04-keyboard-aggregation.md` — keystroke → searchable text.
5. `daemon/05-window-captures-and-capture.md` — power-efficient screenshots.
6. `daemon/06-data-model.md` — SQLite + FTS5 + image files.
7. `api/07-api-design.md` — FastAPI endpoints (HTTP + UDS).
8. `frontend/08-frontend-ui.md` — tkinter views.
9. `09-tech-stack-and-layout.md` — deps, Python version, final file tree.
