# 09 — Tech Stack, Dependencies & Final Code Layout

Closes `target.txt`: *"provide requirements.txt and associated python
version."*

---

## 1. Python version

**Python 3.12** (the existing env at `/home/jamesbrown/miniforge3/envs/gui_agent`
is 3.12, per `plan_detect_paste_event.txt`). Minimum supported **3.11**
(`asyncio.TaskGroup`, `tomllib`, modern typing). Pin in `pyproject`/docs as
`requires-python = ">=3.11,<3.13"`.

## 2. Python dependencies (`refactor/requirements.txt`)

```
# --- daemon: event capture ---
python-xlib==0.33          # X11: focus/title/vdesktop/XFIXES/XRecord
# pynput>=1.7              # OPTIONAL alt keyboard backend (KBD_BACKEND=pynput); xrecord preferred — see 04 §1a

# --- daemon: API ---
fastapi>=0.111
uvicorn[standard]>=0.30    # serves UDS + optional TCP
pydantic>=2.7

# --- daemon: storage ---
aiosqlite>=0.20            # async SQLite (WAL); FTS5 ships with stdlib sqlite3

# --- imaging (daemon capture + frontend render) ---
Pillow>=10.3

# --- frontend HTTP-over-UDS client ---
httpx>=0.27                # supports UDS transport (sync + async)
```

Notes:
- **FTS5** needs no package — it's compiled into CPython's bundled SQLite
  (verify once: `python -c "import sqlite3;
  sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"`).
  If the chosen tokenizer is `trigram` (`daemon/06 §5`), require **SQLite ≥
  3.34** (CPython 3.11+ bundles newer — verify on target).
- `tkinter` is stdlib but ships separately on Debian/Ubuntu → system package
  `python3-tk` (not pip-installable). List it under system deps.
- `pynput` is **not** required by default — XRecord is the preferred keyboard
  backend. Install it only to use the alternative backend (`KBD_BACKEND=pynput`,
  `04 §1a`); listed commented in `requirements.txt`.

## 3. System dependencies (apt, Kubuntu 22.04 / X11)

```
sudo apt install \
  python3-tk \           # tkinter (frontend)
  wmctrl \               # window list + desktop switch (capture, vdesktop)
  imagemagick \          # `import` window screenshot (capture)
  xdotool \              # active window id, activate window
  x11-utils \            # xprop (WM_CLASS), xdpyinfo (RECORD check)
  xclip                  # clipboard/selection content reads
```

X11 only (no Wayland). XRecord must be enabled (default on Xorg; verify
`xdpyinfo | grep -i record`). Same target as every reference demo.

## 4. Final code layout under `refactor/`

```
refactor/
├── requirements.txt
├── README.md                      # run instructions (start daemon, start frontend)
├── plans/                         # <- these planning docs
│
├── daemon/
│   ├── __main__.py                # thin entry: build app, start tasks+threads, run loop (01 §5)
│   ├── config.py                  # all daemon knobs: paths, intervals, batch sizes, toggles (see 10)
│   ├── identity.py                # boot/session ids (from login-session-id-v2.py)
│   ├── windows.py                 # WindowRegistry: window_uid, alive, last_seen (02)
│   ├── aggregator.py              # keyboard segment state machine (04)
│   ├── capture.py                 # get_window_list/capture_window/... (from no-ocr demo)
│   ├── window_captures.py              # power-efficient scheduler + GC (05)
│   ├── collectors/
│   │   ├── xpump.py               # Thread A: shared PropertyNotify/XFIXES pump (03 §1-2,7)
│   │   ├── focus.py               # focus + title + window-list handlers
│   │   ├── vdesktop.py            # virtual desktop handlers
│   │   ├── clipboard.py           # CLIPBOARD owner -> classify/xclip
│   │   ├── selection.py           # PRIMARY Strategy C
│   │   ├── xrecord.py             # Thread B: shared RECORD context pump (03 §5)
│   │   ├── keyboard.py            # visible-char extraction
│   │   └── paste.py               # gesture -> read_event
│   ├── db/
│   │   ├── schema.sql             # all DDL (06)
│   │   ├── store.py               # aiosqlite open, write-queue, read helpers
│   │   └── search.py              # FTS query builder + result assembly (06 §5, 07 §4)
│   └── api/
│       ├── app.py                 # FastAPI instance + DI wiring
│       ├── routes.py              # endpoints (07)
│       ├── models.py              # pydantic models
│       └── server.py              # uvicorn Config/Server (UDS + optional TCP)
│
└── frontend/
    ├── __main__.py                # parse args, launch Tk
    ├── config.py                  # frontend knobs: socket path, debounce, window_capture dims, grid, hover (see 10)
    ├── apiclient.py               # sync httpx-over-UDS wrapper (08 §5)
    ├── app.py                     # WindowPreviewApp (UI half of the no-ocr demo)
    └── views.py                   # grid / timeline / history builders
```

Per `coding-rules.md`: each `collectors/*.py` and `capture.py`/`identity.py` is a
**copy+rename** of the matching `reference_v2` demo with its print-loop swapped
for `emit`/return values; `__main__.py` (daemon) and `app.py` (frontend) hold
the **minimal wiring**, not re-dumped logic.

## 5. Running

```
# 1. start the daemon (collects + stores + serves API)
python -m daemon                       # binds UDS at $XDG_RUNTIME_DIR/desktop-overview.sock
python -m daemon --tcp 8765            # also expose localhost:8765 for /docs

# 2. start the frontend (renders + queries)
python -m frontend                     # connects to the UDS
python -m frontend --tcp 8765          # or via TCP
```

Optional: a systemd **user** unit (`~/.config/systemd/user/desktop-overview.service`)
to autostart the daemon at login — fits the boot/session identity model
(`daemon/02`) and keeps window_captures/events flowing without the GUI open.

## 6. Build order (suggested implementation sequence)

1. `db/` (schema + store + a couple of fake rows) → prove WAL + FTS work.
2. `identity.py` + `windows.py` → registry with no events yet.
3. `collectors/xpump.py` + `focus.py`/`vdesktop.py` → focus/title/desktop rows.
4. `capture.py` + `window_captures.py` → window_captures on disk + rows.
5. `clipboard.py` + `selection.py` + `xrecord.py`/`paste.py`/`keyboard.py` +
   `aggregator.py` → full event set.
6. `api/` → endpoints over the populated DB.
7. `frontend/` → grid first, then search, then timeline/history.

Each step is independently testable (DB inspectable with `sqlite3`, collectors
runnable standalone via their retained `__main__`).
