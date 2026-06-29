# 07 — API Design (FastAPI: HTTP + UNIX domain socket)

This is where `target.txt`'s two questions are decided:

> can we put api code in the daemon? — **yes, in-process (see §1)**
> http api or unix domain socket? — **both from one uvicorn server (see §2)**

---

## 1. The API lives *inside* the daemon process

The FastAPI app runs on the **same asyncio event loop** as the collectors, DB
writer, and window_capture scheduler. We mount uvicorn programmatically as a loop
task rather than as a separate process:

```python
config = uvicorn.Config(app, uds=UDS_PATH, log_level="info", lifespan="on")
server = uvicorn.Server(config)
await server.serve()          # one task among the daemon's gather(...)
```

**Why in-process (recommended):**
- API read handlers open their own **read-only** aiosqlite connections to the
  same WAL DB the writer uses — no IPC, no second copy of the data, reads and
  writes coexist (WAL, `daemon/01 §3`).
- Shared in-memory state (current focus, collector health, last full-sweep time)
  is directly readable by handlers — no serialization across a process
  boundary.
- One thing to start/stop/supervise.

**Rejected alternative — separate API process** reading the same SQLite file:
adds a process to manage, needs the DB as the *only* coordination channel
(can't read live in-memory daemon state like collector health), and gains
nothing here because the API is local and low-traffic. Keep it in the daemon.

**Concurrency caveat:** FastAPI handlers must be `async def` and must not block
the loop (no sync DB calls, no `time.sleep`). Any unavoidable blocking work goes
through the same executor (`daemon/01 §4`). A pathological query is bounded by
`PRAGMA busy_timeout` + `query_only` read connections so it can't stall the
writer.

## 2. Transport: UDS primary, optional localhost TCP

Default bind: **UNIX domain socket** at
`$XDG_RUNTIME_DIR/desktop-overview.sock` (fallback `<data_dir>/daemon.sock`).

Why UDS as primary:
- The data is the user's entire desktop activity (keystrokes, clipboard) — it
  must **not** be reachable over the network. A UDS is filesystem-scoped;
  `chmod 600` + owner-only dir means only the logging-in user can reach it.
- No TCP port to collide or be firewalled; lifecycle tied to the socket file.

Optional `127.0.0.1:<port>` (off by default, enabled by config/flag
`--tcp 8765`) for quick `curl`/browser debugging. uvicorn can't bind both UDS
and TCP in one `Config`; if both are requested, run **two `uvicorn.Server`
tasks** sharing the same `app`.

Frontend client (`frontend/08 §5`) talks UDS via `httpx` with a
`httpx.AsyncHTTPTransport(uds=...)` (or `requests-unixsocket` for sync). Same
JSON API regardless of transport.

Config knobs (this section): `UDS_PATH`, `UDS_MODE` (0o600), `TCP_ENABLED`
(false), `TCP_HOST`, `TCP_PORT` (8765), `LOG_LEVEL`. Search request defaults
(`§4`) and limits live in `10-configuration.md §4`; full transport table →
**`10-configuration.md §1`**.

## 3. Read endpoints (frontend offload — all compute server-side)

Everything the current Tk app computes client-side moves behind these. Each
returns assembled, ready-to-render objects (current title, current desktop,
liveness, last-access, window_capture URL) so the frontend never joins or scans.

| Method & path | Purpose | Maps to frontend feature |
|---|---|---|
| `GET /windows` | live windows: uid, x_id, wm_class, current title, current vdesktop (id+name), alive, last_seen, latest-window_capture url | grid view, sort by last access |
| `GET /windows/{uid}` | one window + title history + recent events | detail / hover |
| `GET /windows/{uid}/window_capture/latest` | latest window_capture bytes (or 404) | grid tile, hover preview |
| `GET /windows/{uid}/window_capture/{ts}` | a specific historical capture | timeline |
| `GET /search` | the big one — see §4 | search results |
| `GET /timeline` | windows active in `[from,to]` with title-change history; `?window_uid=` scopes to **one window's** history (per-window timeline) | timeline view |
| `GET /history` | windows in `[from,to]` + liveness filter + hit fields | history query |
| `GET /vdesktops` | desktops: id, name, current | desktop labels |
| `GET /health` | collector status, queue depth, last sweep, db size | status bar / diagnostics |

`GET /windows` supports `?sort=last_access|title&order=desc` and
`?alive=only|dead|both` so the **sort and liveness filter are server-side**
(`schematic` frontend asks for sort-by-last-access and the liveness filter).

## 4. `GET /search` — the core query

Query params:
- `q` — the search string. **Optional**: with no `q` but a `window_uid`, this
  degenerates to "show this window's fields" (scope-only, no text match).
- `window_uid` — **optional scope to a single window** (§4a). When present, the
  FTS match is constrained to that window's rows, so "search this window" =
  `window_uid` filter, *not* copying its title into `q`.
- `fields` — CSV subset of `{title,clipboard,selection,keyboard}`; default =
  all four ("combine all fields", `schematic`).
- `alive` — `only|dead|both` (default `both`).
- `from`,`to` — optional epoch range.
- `sort` — `last_access` (default) | `relevance` | `recency`.
- `hits` — `hit_only` (default) | `all` — whether result fields include only the
  fields that matched or every field (`schematic`: *"can be configured to show
  all fields or just the hit fields"*).

Server pipeline (in `daemon/db/search.py`, `daemon/06 §5`):
1. For each selected field, run its FTS5 `MATCH`, pulling `rowid` +
   `snippet()/highlight()` excerpt **with match markers already inserted**.
2. Map each hit → `window_uid`.
3. Group by `window_uid`; attach the set of `{field, excerpt}` hits.
4. Join window metadata (current title, current vdesktop, alive, last_seen,
   latest window_capture).
5. Apply `alive` filter + time range; sort; paginate (`limit`,`offset`).

Response (one result):
```json
{
  "window_uid": 42,
  "x_window_id": "0x03a00003",
  "wm_class": "firefox",
  "current_title": "Inbox — Mail",
  "vdesktop": {"index": 1, "name": "Web"},
  "alive": true,
  "last_access": 1751200000.0,
  "window_capture_url": "/windows/42/window_capture/latest",
  "hits": [
    {"field": "keyboard", "excerpt": "...draft about <mark>invoice 2026</mark>..."},
    {"field": "title",    "excerpt": "<mark>Inbox</mark> — Mail"}
  ]
}
```

The frontend just renders `hits[*].excerpt` (already highlighted) for the fields
present — satisfying "display hit fields and highlight the search excerpt" with
**zero** client-side text processing.

### 4a. "Search this window" = scope by `window_uid`, not title-copy

There are two **orthogonal** axes the frontend composes:

| Axis | Param | Meaning |
|---|---|---|
| keyword search | `q` (FTS) | find windows by *content*, across all windows |
| window scoping | `window_uid` | restrict the view to *one* window |

- "**Search this window**" (the timeline→search cross-nav, `frontend/08 §5`)
  sets `window_uid` and **leaves `q` empty** → returns that window's own fields
  (title history, clipboard, selection, keyboard) as a detail view. It does
  **not** copy the window's title into `q` — a title query would both over-match
  (other windows with the same title) and under-match (only the title field).
- "**Search *within* this window**" = `q` **+** `window_uid` together → FTS
  match constrained to that window's rows. Pipeline step 1 adds
  `AND window_uid = ?` after mapping FTS rowids to windows (step 2), so scoping
  is a cheap filter, not a separate code path.
- A bare `GET /windows/{uid}` (§3) is the no-FTS shortcut for the same detail
  payload (window + title history + recent events).

## 5. Control / write endpoints

| Method & path | Purpose |
|---|---|
| `POST /window_captures/refresh` | trigger a full capture sweep now (Refresh button) |
| `POST /control/keyboard` `{enabled}` | pause/resume keyboard capture (`04 §6`) |
| `POST /windows/{uid}/activate` | **jump to window** — `xdotool windowactivate` after the liveness check (`daemon/02 §6`). Returns `{ok, reason}` where `reason ∈ {ok, dead, different-session, vanished}` |

`activate` is the **core "jump to window" action** from `objective.txt`, not an
optional extra. It is the one endpoint with a desktop side effect; the daemon
**re-verifies liveness** (current `session_key` + `x_window_id` still in
`_NET_CLIENT_LIST`) before acting, and returns a typed `reason` when the jump
isn't possible so the frontend can grey-out and explain. Search/timeline results
include an `alive`/`jumpable` flag so the UI knows up front whether the jump is
available.

## 6. Schemas & app structure

Pydantic models for every response; FastAPI generates OpenAPI docs at `/docs`
(reachable over the optional TCP port for development).

```
daemon/api/app.py        # FastAPI() instance, dependency wiring (db read pool, daemon state)
daemon/api/routes.py     # the endpoints above (thin: call db/search + db/store)
daemon/api/models.py     # pydantic request/response models
daemon/api/server.py     # uvicorn.Config/Server construction (UDS + optional TCP)
```

Handlers stay **thin** — they call `db/search.py` / `db/store.py` / read daemon
state and serialize. No business logic in routes.
