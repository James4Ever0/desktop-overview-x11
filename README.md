# Desktop Overview — X11 Activity Recorder & Search

**Watch your desktop like a security camera, but for window focus, clipboard, typed text, and window captures — all fully offline.**

A background daemon silently records everything associated with the currently focused X11 window: clipboard reads/writes, text selection, typed characters (flushed into searchable segments on idle or focus change), window title history, virtual desktop switches, and periodic screenshots. A Tk frontend lets you **search by keyword across all these fields**, view a **timeline**, preview captured windows, and **jump back to a live window** with one click.

```
                          ┌─────────────────────────────┐
  X11 events ──────────▶  │  Daemon (single asyncio     │
  (XFIXES, XRecord,       │  loop + 2 OS threads)       │
   xdotool, wmctrl)       │  ├─ Xlib event pump         │
                          │  ├─ XRecord keyboard tap     │
                          │  ├─ idb-chunked aggregator   │
                          │  ├─ FTS5 search (trigram!)   │
                          │  └─ FastAPI over UNIX socket │
                          └──────────┬──────────────────┘
                                     │ HTTP/JSON
                                     │ (chmod 600 UDS)
                          ┌──────────▼──────────────────┐
                          │  Tk Frontend                 │
                          │  grid, hover-zoom, search,   │
                          │  timeline, jump-to-window    │
                          └─────────────────────────────┘
```

> **Platform:** X11/Linux only. Python 3.11–3.12.

---

## What makes this different

| Capability | What it does |
|---|---|
| **XRecord keyboard tap** | Hooks XRecord (not `/dev/input` or pynput) to capture typed text per-window without polling — no root needed, works over SSH |
| **Idle-chunking aggregator** | Concatenates keystrokes into segments, flushed on idle timeout *or* window focus change — noise below 3 chars is dropped |
| **Boot-ID window liveness** | Tags every window with daemon boot_id + X window ID, so you can tell if a search hit is still alive and jump to it |
| **Virtual desktop tracking** | Monitors KDE/other WM virtual desktop switches per window via `_NET_CURRENT_DESKTOP` |
| **FTS5 trigram tokenizer** | Full-text search that works with CJK, no spaces, and mixed languages |
| **Password redaction** | Clipboard content flagged as a password is never stored — only a redaction marker |
| **Zero-network API** | FastAPI bound to a `chmod 600` UNIX socket by default, TCP opt-in for debugging |

---

## Project layout

```
.
├── demo-*.py                     # Standalone prototypes (proven concepts before refactor)
├── plan-*.txt                    # Design docs for each prototype
├── *.sh                          # Launch & test scripts
│
├── refactor/                     # ◀─ ACTIVE DEVELOPMENT
│   ├── daemon/                   #   Background collector + FastAPI server
│   │   ├── collectors/           #     xpump (Xlib), xrecord, clipboard, selection, focus, vdesktop
│   │   ├── db/                   #     SQLite schema, async store, FTS5 search
│   │   ├── api/                  #     FastAPI routes, models, UDS server
│   │   ├── aggregator.py         #     Keyboard idle-chunking engine
│   │   ├── identity.py           #     Boot/session ID generation
│   │   ├── windows.py            #     Window registry + liveness tracking
│   │   ├── handlers.py           #     Event → DB dispatch
│   │   └── capture.py            #     xdotool screenshot, wmctrl list
│   ├── frontend/                 #   Tk UI (search grid, timeline, hover-zoom)
│   └── tests/                    #   Headless tests per module
│
├── reference/                    # Versioned snapshots of working prototypes
│   ├── 2026_6_26/
│   └── 2026_6_29/
├── reference_v2/                 # Refined reference versions
├── password_hash_reference/      # Research on secure password handling
├── rewrite_plans/                # Future rewrite ideas (Rust/Go backend)
└── similar_projects/             # Related work research
```

---

## Active development (refactor/)

The `refactor/` folder is where the integrated daemon + frontend lives — most files updated `Jun 29`. Recent work:

- `daemon/collectors/` — all collectors (xpump, xrecord, clipboard, selection, paste, focus, vdesktop)
- `daemon/aggregator.py` — keyboard idle-chunking with denylist support
- `tests/` — 8 test modules covering identity, runtime, collectors, DB, search, aggregator, captures, frontend
- `frontend/app.py` — Tk grid view with type-to-search, hover zoom, timeline, jump-to-window
- `db/schema.sql` + `store.py` + `search.py` — SQLite FTS5 with trigram tokenizer, async read/write

### Run it

```bash
cd refactor
pip install -r requirements.txt
./run.sh          # daemon (background) + UI (foreground)
```

---

## License

[Unlicense](LICENSE) — public domain. Do what you want.
