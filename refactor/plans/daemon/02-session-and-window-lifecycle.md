# 02 — Session & Window Lifecycle

Covers two `schematic.txt` daemon responsibilities:

> - generate boot id per daemon process, and associate the boot id with window
>   ids, to ensure the window is actually opening, instead of closed.
> - track window lifespan, alive or not.

The core problem: **X11 window ids are reused.** `0x03a00003` today may be a
Firefox window; after both close and reopen, the same numeric id can belong to
something else. Stored rows that reference a bare window id become ambiguous
across time. We fix this with a layered identity scheme.

---

## 1. Three identity layers

| Layer | Source | Lifetime | Purpose |
|-------|--------|----------|---------|
| **boot_id** (machine) | `/proc/sys/kernel/random/boot_id` | until reboot | distinguishes data from different OS boots |
| **session_key** | md5(boot_id + ":" + session_start) | login session | distinguishes logins (from `login-session-id-v2.py`) |
| **daemon_boot_id** | generated at daemon startup (uuid4) | one daemon process | distinguishes daemon runs; *this* is the "boot id per daemon process" the schematic asks for |

> ⚠️ **Which id governs liveness & "jump to window"?** The X server hands out
> window ids that stay valid for the lifetime of the **X session** (≈ until
> logout/reboot), *not* the lifetime of our daemon process. So an x_window_id
> observed before a daemon restart is **still the same live window** after the
> restart, as long as the session didn't end. Therefore **liveness and
> jump-to-window key on `session_key` (the X session), NOT on
> `daemon_boot_id`.** The daemon process uuid is for diagnostics/grouping
> captures only — never for deciding whether a window is still reachable.
> This directly serves `objective.txt`: *"jump to the window if ... it is
> alive, through boot_id and window_id combination."* (`objective`'s "boot_id"
> = the session/machine identity, the stable one — see `§6`.)

`login-session-id-v2.py` already computes boot_id, boot time, session start,
user, and the md5 — we **lift it into a module** (`identity.py`) and call it
once at startup, storing the result in a `daemon_run` row. The `session_key` it
produces is the stable cross-restart key used everywhere liveness matters.

```
daemon_run(
  id INTEGER PK,
  daemon_boot_id TEXT,      -- uuid4, unique per process
  machine_boot_id TEXT,     -- /proc boot_id
  session_key TEXT,         -- md5 from login-session-id-v2
  user_name TEXT, uid TEXT,
  started_at REAL, stopped_at REAL NULL
)
```

## 2. The window identity record

A logical window instance is **(session_key, x_window_id, open-interval)**, not
the bare x_window_id and not the daemon process. We mint a surrogate key
`window_uid` (autoincrement row id) the first time we see an x_window_id that
has no currently-open row **within this session**, and *all* event tables
foreign-key to `window_uid`, never to the raw X id.

```
window(
  window_uid INTEGER PK,        -- stable surrogate, referenced everywhere
  session_key TEXT,             -- X session this window belongs to (liveness scope)
  first_daemon_run_id INTEGER,  -- which daemon run first saw it (diagnostics)
  last_daemon_run_id  INTEGER,  -- which daemon run last touched it (diagnostics)
  x_window_id INTEGER,          -- the raw 0x... id (decimal)
  wm_class TEXT,                -- app name (xprop WM_CLASS), for grouping
  first_seen REAL,
  last_seen REAL,               -- updated on any activity; = "last access time"
  closed_at REAL NULL,          -- set when the window leaves the client list
  alive INTEGER                 -- 1 = currently in _NET_CLIENT_LIST, 0 = gone
)
```

Why scope by `session_key` and not `daemon_run_id`: a window observed before a
daemon restart must keep the **same** `window_uid` after the restart so its
history is continuous and it stays jumpable. On startup the daemon reconciles
(see `§3`) — for each x_window_id currently in `_NET_CLIENT_LIST` that already
has an open row in this `session_key`, it **reuses** that `window_uid` instead
of minting a new one. A reused X id *after a genuine close/reopen* (its row was
marked `closed`) produces a **new** `window_uid`, so history never collides.

## 3. Liveness tracking (alive / dead)

Liveness is driven by `_NET_CLIENT_LIST` on the root window — the same property
`demo-get-window-title-change.py::sync_clients()` already watches.

- **Window appears** in `_NET_CLIENT_LIST` and we have no open `window` row for
  that x_window_id **in this session** → INSERT a `window` row (`alive=1`,
  `first_seen=now`), read `WM_CLASS` once, start watching its title
  (`PropertyChangeMask`).
- **Window disappears** from `_NET_CLIENT_LIST` → mark the open row
  `alive=0, closed_at=now`. (The demo already computes `gone = watched -
  current` per reconcile tick — we hook the DB update there.)
- **Periodic reconcile** (cheap, event-driven via the existing
  `_NET_CLIENT_LIST` PropertyNotify) keeps this correct without polling.
  *(Deliberate non-knob: there is no liveness poll-interval setting — see
  `10-configuration.md §6`.)*
- **Startup reconcile (handles daemon restart mid-session):** on boot, read the
  current `_NET_CLIENT_LIST`. For each x_window_id:
  - open row exists in this `session_key` → **reuse** `window_uid` (still alive).
  - no row, or only a `closed` row → mint a new `window_uid`.
  Any window row that was `alive=1` for this session but is **no longer** in the
  client list (closed while the daemon was down) → mark `alive=0, closed_at` =
  now (best-effort; exact close time unknown).

Liveness for *history queries* (`schematic.txt` frontend: "show whether those
windows are accessible or not ... filter ... only alive / only dead / both"):
- `alive=1 AND session_key == current session` → **accessible now / jumpable**.
- everything else (`alive=0`, or a row from a previous session / OS boot) →
  **dead / inaccessible** (cannot be jumped to — its x_window_id no longer
  refers to that window).
- This single derivation feeds both the frontend liveness filter and the
  jump-to-window precondition; see `§6`, `frontend/08 §4`, `api/07 §4`.

## 4. "last access time"

`window.last_seen` is bumped on **any** associated event: focus gained, title
change, clipboard/selection/keyboard event attributed to it, or a window_capture
capture. The frontend's "sort by last access time" reads this column directly
(no client-side compute). Focus-gain is the strongest signal and is always
recorded (see `daemon/03 §1`).

## 5. Attribution: which window owns an event?

Clipboard/selection/keyboard/paste events don't carry a window id. We attribute
them to **the currently focused window** at event time, which the focus
collector keeps hot in memory (`current_focus_window_uid`). This is the same
assumption the schematic makes ("associate them with window ids"). Edge cases:
- Focus unknown (e.g. our own daemon has no window) → attribute to a sentinel
  `window_uid = NULL`/"(unknown)" rather than dropping the event.
- A small race (event lands in the ~ms between focus change and property
  update) is acceptable; the focus collector updates synchronously on
  `_NET_ACTIVE_WINDOW` so the window is fresh.

## 6. Jump to window (objective's core action)

`objective.txt`: *"jump to the window if we can find it and it is alive,
through boot_id and window_id combination."* This is a **first-class feature**,
not optional.

Resolution algorithm, given a stored `window_uid` (from a search/timeline
result):
1. Load its `window` row → `(session_key, x_window_id, alive)`.
2. **Find + liveness check:** jumpable **iff** `session_key == current session`
   **and** `alive == 1` **and** `x_window_id` is in the live `_NET_CLIENT_LIST`
   (re-verify, don't trust a stale flag).
3. **Jump:** `xdotool windowactivate <x_window_id>` (the demo's
   `activate_window`), which raises + focuses it, switching virtual desktops if
   needed (KWin follows). Confirm via `_NET_ACTIVE_WINDOW`.
4. **Not jumpable:** return a typed reason (`dead` / `different-session` /
   `vanished`) so the frontend can grey-out the jump action and explain why
   (`frontend/08 §4`).

The `(boot_id/session, window_id)` combination from the objective is exactly
the precondition in step 2: a window is only reachable if it belongs to the
**current** session and is still open. Cross-session matches are surfaced as
history (with their last window_capture) but are **never** jumpable.

API surface: `POST /windows/{uid}/activate` → `{ok, reason}` (`api/07 §5`).

## 7. Module shape

```
daemon/identity.py      # from login-session-id-v2.py, made importable (no print/sys.exit)
daemon/windows.py       # WindowRegistry: id<->window_uid, alive/closed, last_seen bumps
```

`identity.py` refactor note: the current script `sys.exit()`s on error and
prints. Make each getter return a value or raise a typed exception; move the
printing into a `__main__` block so it still runs standalone (per
`coding-rules.md`).
