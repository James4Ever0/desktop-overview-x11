# 10 — Configuration Reference (all daemon + frontend knobs)

Single source of truth for **every configurable parameter**, so nothing is left
implicit in the per-topic plans. Each row lists the **default**, **type**, where
it is **set**, and the originating plan section.

## 0. Conventions

- **Set via** values:
  - `config.py` — a module-level constant / `Settings` field with the default
    shown (daemon: `daemon/config.py`; frontend: `frontend/config.py`).
  - `env` — overridable by an environment variable of the same name.
  - `CLI` — a command-line flag on `python -m daemon` / `python -m frontend`.
  - `API` — changeable at runtime via an endpoint (`api/07 §5`); not persisted.
- **Precedence** (highest wins): `CLI` > `env` > `config.py` default. API
  toggles override the live value for the running process only.
- Defaults are tuned for a single-user Kubuntu/X11 desktop (the reference
  target). Everything here has a working default; none is mandatory to set.

---

## 1. Daemon — paths & transport  (`07 §2`, `09`, `05 §3`)

| Param | Default | Type | Set via | Ref |
|---|---|---|---|---|
| `DATA_DIR` | `$XDG_DATA_HOME/desktop-overview` (→ `~/.local/share/desktop-overview`) | path | config.py, env | 05 §3 |
| `DB_PATH` | `<DATA_DIR>/daemon.sqlite3` | path | derived from `DATA_DIR` | 06 |
| `WINDOW_CAPTURE_DIR` | `<DATA_DIR>/window_captures/` | path | derived from `DATA_DIR` | 05 §3 |
| `UDS_PATH` | `$XDG_RUNTIME_DIR/desktop-overview.sock` (fallback `<DATA_DIR>/daemon.sock`) | path | config.py, env, CLI | 07 §2 |
| `UDS_MODE` | `0o600` | octal mode | config.py | 07 §2 |
| `TCP_ENABLED` | `false` | bool | CLI `--tcp` | 07 §2 |
| `TCP_HOST` | `127.0.0.1` | str | config.py | 07 §2 |
| `TCP_PORT` | `8765` | int | CLI `--tcp 8765` | 07 §2, 09 §5 |
| `LOG_LEVEL` | `info` | enum | config.py, env | 07 §1 |

## 2. Daemon — keyboard capture & aggregation  (`04`)

| Param | Default | Type | Set via | Ref |
|---|---|---|---|---|
| `KBD_BACKEND` | `xrecord` (preferred) | `xrecord`\|`pynput` | config.py | 04 §1, §1a |
| `KBD_ENABLED` | `true` | bool | config.py, **API** `POST /control/keyboard` | 04 §6 |
| `KBD_IDLE_FLUSH_S` | `3.0` | float s | config.py | 04 §3 |
| `KBD_IDLE_CHECK_S` | `0.5` | float s | config.py | 04 §3 |
| `KBD_FLUSH_ON_FOCUS_CHANGE` | `true` | bool | config.py | 04 §3 |
| `KBD_FLUSH_ON_TITLE_CHANGE` | `true` | bool | config.py | 04 §3 |
| `KBD_APPLY_BACKSPACE` | `true` | bool | config.py | 04 §4 |
| `KBD_MIN_SEGMENT_CHARS` | `3` | int (stripped len must exceed) | config.py | 04 §3 |
| `KBD_APP_DENYLIST` | `["keepassxc","bitwarden", lock screen]` | list[WM_CLASS] | config.py | 04 §6 |

> Whitespace (space/tab/enter) is **content**, not a flush delimiter — there is
> intentionally no "flush on Enter" knob (`04 §1`).

## 3. Daemon — window_captures & capture  (`05`)

| Param | Default | Type | Set via | Ref |
|---|---|---|---|---|
| `REFRESH_INTERVAL_S` (X) | `5` | int s | config.py | 05 §2 |
| `REFRESH_BATCH_SIZE` (Y) | `3` | int | config.py | 05 §2 |
| `WINDOW_CAPTURE_KEEP_PER_WINDOW` | `5` | int | config.py | 05 §4 |
| `WINDOW_CAPTURE_RETENTION_DAYS` | `null` (off) | int\|null | config.py | 05 §4 |
| `WINDOW_CAPTURE_MAX_DIM` | `1920` (≤ screen) | int px | config.py | 05 §4, §6 |
| `OCR_ENABLED` | `false` | bool | config.py | 05 §1 |

> First refresh on startup is always a **full** sweep (fixed behavior, not a
> knob). On-demand full sweep via `POST /window_captures/refresh`.

## 4. Daemon — storage, DB & search  (`01 §3`, `06`)

| Param | Default | Type | Set via | Ref |
|---|---|---|---|---|
| `WRITE_QUEUE_MAX_N` | `200` | int | config.py | 01 §3 |
| `WRITE_QUEUE_MAX_WAIT_S` | `0.25` | float s | config.py | 01 §3 |
| `SQLITE_JOURNAL_MODE` | `WAL` | enum | config.py | 06 §6 |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | enum | config.py | 06 §6 |
| `SQLITE_BUSY_TIMEOUT_MS` | `3000` | int ms | config.py | 06 §6 |
| `FTS_TOKENIZER` | `trigram` (CJK-safe) | `trigram`\|`unicode61` | config.py | 06 §5 |
| `SEARCH_DEFAULT_LIMIT` | `100` | int | config.py | 07 §4 |
| `SEARCH_MAX_LIMIT` | `500` | int | config.py | 07 §4 |

> Server-side **request defaults** for `GET /search` (overridable per request):
> `fields`=all four, `alive`=`both`, `sort`=`last_access`, `hits`=`hit_only`
> (`07 §4`). Window liveness is event-driven (`_NET_CLIENT_LIST` notify) — there
> is intentionally **no poll-interval knob** (`02 §3`).

## 5. Frontend  (`08`, `09`)

| Param | Default | Type | Set via | Ref |
|---|---|---|---|---|
| `SOCKET_PATH` | matches daemon `UDS_PATH` | path | config.py, env, CLI | 08 §6 |
| `USE_TCP` / `TCP_ENDPOINT` | off / `127.0.0.1:8765` | bool / str | CLI `--tcp 8765` | 09 §5 |
| `REQUEST_TIMEOUT_S` | `5.0` | float s | config.py | 08 §6 |
| `REQUEST_WORKER_THREADS` | `4` | int | config.py | 08 §6 |
| `SEARCH_DEBOUNCE_MS` | `200` (150–250) | int ms | config.py | 08 §6 |
| `GRID_AUTO_REFRESH_S` | `10` (0 = off) | int s | config.py | 08 §2 |
| `GRID_COLUMNS` | `auto` (fit width) | int\|`auto` | config.py | 08 §2 |
| `WINDOW_CAPTURE_DISPLAY_DIM` | `240` | int px | config.py | 08 §2 |
| `HOVER_PREVIEW_DELAY_MS` | `400` | int ms | config.py | 08 §1 |
| `HOVER_PREVIEW_MAX_DIM` | `900` | int px | config.py | 08 §1 |
| `HISTORY_STACK_DEPTH` | `20` | int | config.py | 08 §5 |
| `THEME` (bg/fg/accent colors) | dark (from demo) | dict | config.py | 08 §1 |
| `FONT_FAMILY` / `FONT_SIZE` | from demo | str / int | config.py | 08 §1 |

## 6. Open items / deliberate non-knobs

- **No password detection** — there is no "skip password fields" toggle because
  X11 gives no reliable signal; the only privacy levers are `KBD_ENABLED` and
  `KBD_APP_DENYLIST` (`04 §6`).
- **No window-liveness poll interval** — liveness is driven by
  `_NET_CLIENT_LIST` PropertyNotify, not polling (`02 §3`).
- **Full-sweep-on-start** and **keep-last-window_capture-when-closed** are fixed
  behaviors, not configurable (`05 §2`, §4).
- If a `config.toml` is later desired over `config.py` constants, this table is
  the schema to generate it from (Python 3.11+ has `tomllib`, `09 §1`).
