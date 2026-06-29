# Desktop Overview with Search

A background **daemon** records what happens on your X11 desktop — window focus,
title changes, virtual-desktop moves, clipboard/selection/paste, typed-text
segments, and periodic window captures — into a local SQLite database with
full-text search. A thin **Tk frontend** lets you search that history, preview
windows, and jump straight back to a live window.

Everything stays on your machine: the database lives under your home directory,
the daemon's API is served over a `chmod 600` UNIX socket (not the network), and
TCP is **off by default**.

> **Platform:** X11 only. Python 3.11–3.12.

---

## Quick start

```bash
# 1. system deps (Debian/Ubuntu)
sudo apt install python3-tk wmctrl imagemagick xdotool x11-utils xclip

# 2. python deps
pip install -r requirements.txt

# 3. run daemon + UI together (daemon in background, UI in foreground)
./run.sh
```

Or run the two halves separately (two terminals):

```bash
./run-daemon.sh          # terminal 1: collector + API
./run-frontend.sh        # terminal 2: search UI
```

> The launch scripts use whatever `python` is on your `PATH`. To pin an
> interpreter (e.g. a conda env): `PYTHON=/path/to/python ./run-daemon.sh`.

---

## Launch scripts

| Script | What it does |
|---|---|
| `run-daemon.sh`   | Starts the daemon (`python -m daemon`). Passes args through. |
| `run-frontend.sh` | Starts the search UI (`python -m frontend`). Passes args through. |
| `run.sh`          | Boots the daemon in the background, waits for its socket, then opens the UI. Reuses an already-running daemon. Stops the daemon it started when the UI exits. |

### Daemon flags (`run-daemon.sh …`)

```
--tcp [PORT]        also serve 127.0.0.1:PORT (default 8765) for curl/debugging
--data-dir PATH     override the data directory
--log-level LEVEL   debug | info | warning | error
--no-keyboard       start with keyboard capture disabled
```

### Frontend flags (`run-frontend.sh …`)

```
--socket PATH       daemon UNIX socket (default: auto-discovered from XDG)
--tcp [HOST:PORT]   connect over TCP instead of the socket (default 127.0.0.1:8765)
--columns N         fixed grid column count (default: auto-fit to width)
--log-level LEVEL   debug | info | warning | error
```

---

## Configuration & default paths

**There is no config file.** All settings are plain Python defaults in two
dataclasses, overridable per-run. Precedence (highest wins):

```
CLI flag  >  environment variable  >  default in config.py
```

- Daemon config: [`daemon/config.py`](daemon/config.py) — `Settings`
- Frontend config: [`frontend/config.py`](frontend/config.py) — `FrontendSettings`

### Default paths

All data lives under `$XDG_DATA_HOME/desktop-overview`, falling back to
`~/.local/share/desktop-overview`:

| Path | Default | What |
|---|---|---|
| Data dir   | `~/.local/share/desktop-overview/`             | root of all stored data |
| Database   | `…/desktop-overview/daemon.sqlite3`            | events + FTS5 indexes (WAL) |
| Window captures | `…/desktop-overview/window_captures/`         | captured window PNGs |
| Daemon log | `…/desktop-overview/logs/daemon.log`           | rotating, 2 MB × 5 |
| Frontend log | `…/desktop-overview/logs/frontend.log`       | rotating, 2 MB × 3 |
| API socket | `$XDG_RUNTIME_DIR/desktop-overview.sock`<br>(fallback `…/desktop-overview/daemon.sock`) | `chmod 600` UNIX socket |

> Logs are written to **both stdout and the rotating file** for the daemon and
> the frontend.

### Environment variables

| Variable | Effect |
|---|---|
| `DESKTOP_OVERVIEW_DATA_DIR` | override the data directory (db, window_captures, logs) |
| `DESKTOP_OVERVIEW_UDS`      | override the API socket path (daemon and UI) |
| `DESKTOP_OVERVIEW_LOG_LEVEL`| daemon log level |
| `XDG_DATA_HOME`             | base for the default data dir |
| `XDG_RUNTIME_DIR`           | base for the default socket path |

### Notable defaults (edit in `config.py` to change)

- `refresh_interval_s=5`, `refresh_batch_size=3`, `window_capture_keep_per_window=5`, `window_capture_max_dim=1920`
- `kbd_enabled=True` (toggle live via the API), `kbd_idle_flush_s=3.0`,
  `kbd_min_segment_chars=3` (a typed chunk is stored only if its **stripped**
  length exceeds 3 — shorter noise is dropped before the buffer/db),
  `kbd_app_denylist=("keepassxc","bitwarden","ksshaskpass")`
- `fts_tokenizer="trigram"` (CJK-safe), `search_default_limit=100`, `search_max_limit=500`
- `tcp_enabled=False` — the API is socket-only unless you pass `--tcp`.

#### Privacy guarantees (by design, not configuration)
- Keyboard capture **never special-cases passwords**; instead use `kbd_enabled`
  (runtime toggle) and `kbd_app_denylist` to exclude sensitive apps entirely.
- Clipboard content marked as a **password** is **never read or stored** — only
  the fact of the event plus a redaction hint.

---

## Code structure

```
refactor/
├── run.sh, run-daemon.sh, run-frontend.sh   launch scripts
├── requirements.txt
│
├── daemon/                 background collector + in-process API (single asyncio loop)
│   ├── __main__.py         entrypoint: wires everything, handles signals/shutdown
│   ├── config.py           Settings (all daemon knobs + default paths)
│   ├── log.py              central logging → stdout + rotating file
│   ├── identity.py         session_key (boot_id:session_start) + daemon_boot_id
│   ├── runtime.py          event queue, dispatch, async DB writer, executor
│   ├── windows.py          WindowRegistry (window_uid surrogate, liveness)
│   ├── handlers.py         focus/title/vdesktop + clipboard/selection/paste handlers
│   ├── aggregator.py       KeyboardAggregator (typed-text → idle-flushed segments)
│   ├── capture.py          window list / activate / screenshot (xdotool, etc.)
│   ├── window_captures.py       WindowCaptureScheduler (periodic batched captures + retention)
│   ├── collectors/
│   │   ├── xpump.py        Thread A: Xlib event pump (focus/title/vdesktop/XFIXES)
│   │   └── xrecord.py      Thread B: XRecord keyboard tap
│   ├── db/
│   │   ├── schema.sql      tables + FTS5 external-content indexes + triggers
│   │   ├── store.py        async aiosqlite Store (WAL; read + write conns)
│   │   └── search.py       multi-field FTS search, window assembly, timeline
│   └── api/
│       ├── app.py          FastAPI app factory + DaemonContext
│       ├── routes.py       endpoints (windows/search/timeline/window_capture/activate/health/…)
│       ├── models.py       pydantic response models
│       └── server.py       uvicorn on UDS (+ optional TCP), chmod 600
│
├── frontend/               thin Tk client — renders + asks the daemon, no collection
│   ├── __main__.py         entrypoint: args + logging + Tk launch
│   ├── config.py           FrontendSettings (theme, debounce, grid, hover…)
│   ├── log.py              central logging → stdout + rotating file
│   ├── apiclient.py        sync httpx-over-UDS client + typed dataclasses
│   ├── app.py              WindowPreviewApp: grid, hover-zoom, type-to-search, tabs, jump
│   └── views.py            Timeline lane renderer
│
├── tests/                  one headless test per build step (run with python -m tests.<name>)
└── plans/                  design docs the implementation follows
```

### How the two halves talk

```
                 UNIX socket (chmod 600, $XDG_RUNTIME_DIR/desktop-overview.sock)
  frontend  ───────────────  HTTP/JSON  ───────────────▶  daemon (FastAPI on its asyncio loop)
  (Tk, sync httpx                                          reads: db/search.py over aiosqlite
   on worker threads)                                      writes/actions: runtime executor
```

The daemon runs one asyncio loop; the two X sources live in OS threads and
marshal events back through `Runtime.emit`. All blocking IO (xclip, screenshot,
xdotool) goes through an executor so the loop never blocks. The frontend does
**no** capture or matching — it renders exactly what the API returns (including
server-side `<mark>` search highlights) and issues actions like *jump to window*
back over the socket.

---

## API (for debugging)

Bring up TCP and poke it with curl:

```bash
./run-daemon.sh --tcp                       # serves 127.0.0.1:8765
curl 127.0.0.1:8765/health
curl '127.0.0.1:8765/search?q=invoice'
curl '127.0.0.1:8765/windows?alive=only'
```

Read endpoints: `/windows`, `/windows/{uid}`, `/windows/{uid}/window_capture/latest`,
`/search`, `/history`, `/timeline`, `/vdesktops`, `/health`.
Actions (POST): `/window_captures/refresh`, `/control/keyboard`, `/windows/{uid}/activate`.

---

## Tests

Each build step has a headless test (no live X needed):

```bash
python -m tests.test_db
python -m tests.test_identity_windows
python -m tests.test_runtime
python -m tests.test_collectors
python -m tests.test_window_captures
python -m tests.test_keyboard_aggregator
python -m tests.test_api_search
python -m tests.test_frontend
```
