# Refactor Plans — Index

Planning docs for splitting the monolithic tkinter tool into a **background
daemon** (collect + store + serve) and a **thin tkinter frontend** (render +
query). Source requirements: `../target.txt`, `../schematic.txt`,
`../objective.txt`. Reference implementation being refactored:
`../../reference_v2/`.

Read in order; `00` is the map.

| # | File | Topic |
|---|------|-------|
| 00 | [00-overview-and-architecture.md](00-overview-and-architecture.md) | Big picture, process/threading model, answers to `target.txt`'s two questions, demo→module mapping |
| 01 | [daemon/01-runtime-and-concurrency.md](daemon/01-runtime-and-concurrency.md) | Async/non-blocking contract, thread→loop marshaling, DB writer, startup/shutdown |
| 02 | [daemon/02-session-and-window-lifecycle.md](daemon/02-session-and-window-lifecycle.md) | boot id / session / daemon-run, `window_uid`, alive/dead, last-access, attribution |
| 03 | [daemon/03-event-collectors.md](daemon/03-event-collectors.md) | The six X11 collectors (focus, title, vdesktop, clipboard, selection, paste) as demo refactors |
| 04 | [daemon/04-keyboard-aggregation.md](daemon/04-keyboard-aggregation.md) | Visible-char capture (XRecord, pynput alt), idle/focus/title flush, FTS prep, privacy controls |
| 05 | [daemon/05-window-captures-and-capture.md](daemon/05-window-captures-and-capture.md) | Power-efficient refresh, image files on disk, latest window_capture, retention |
| 06 | [daemon/06-data-model.md](daemon/06-data-model.md) | SQLite schema, FTS5 per-source design, image file layout |
| 07 | [api/07-api-design.md](api/07-api-design.md) | FastAPI in-process, UDS + optional TCP, endpoints, `/search` pipeline |
| 08 | [frontend/08-frontend-ui.md](frontend/08-frontend-ui.md) | tkinter grid / timeline / history views, daemon client, what compute was removed |
| 09 | [09-tech-stack-and-layout.md](09-tech-stack-and-layout.md) | requirements.txt, Python version, final file tree, run + build order |
| 10 | [10-configuration.md](10-configuration.md) | **All** configurable params (daemon + frontend): defaults, types, set-via, plan refs |

## Key decisions at a glance

- **API runs inside the daemon process**, on the same asyncio loop (no second
  process). → `00 §2`, `07 §1`.
- **UNIX domain socket** is the primary transport (local-only, private);
  localhost TCP is optional/off-by-default. → `07 §2`.
- **Two collector threads** (Xlib property/XFIXES pump; XRecord pump) feed one
  asyncio queue; the loop never blocks. → `01`, `03 §7`.
- **`window_uid` surrogate key**, scoped to the **X session (`session_key`)** —
  not the daemon process — so windows survive a daemon restart, solving X11
  window-id reuse and powering alive/dead history + **jump-to-window**. → `02`.
- **Jump to a window** (`objective.txt`) is a core action: activate iff it's in
  the current session and still live; otherwise surfaced as non-jumpable
  history. → `02 §6`, `07 §5`.
- **Search ⇄ timeline ⇄ jump** is one shared-state workflow in the frontend. →
  `08 §5`.
- **SQLite (WAL) + per-source FTS5** for text; **PNG files on disk** referenced
  by relative path for images. → `06`.
- **Keyboard segments chunk on idle / focus-change / title-change** (the third
  trigger from `demo-keyevent-listener-...chunking.py`); whitespace is content,
  not a delimiter. Capture backend is **XRecord (preferred)**, with **pynput as
  a documented alternative** (`KBD_BACKEND`). → `04 §1`, `§1a`, `§3`.
- **All tunables are catalogued in one place** (`10-configuration.md`) for both
  daemon and frontend — defaults, types, and where each is set. → `10`.
- **Every collector is a copy+rename of a `reference_v2` demo** with its print
  loop replaced by an `emit` callback (per `../../coding-rules.md`). → `03`.
