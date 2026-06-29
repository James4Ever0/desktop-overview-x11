# 03 — Event Collectors

Covers the `schematic.txt` collection responsibilities:

> - tracking for focus changes, virtual desktop names, window titles
> - get a list of opening windows and ids
> - store clipboard content, selection content, selection read events,
>   clipboard read events, associate them with window ids
> - store keyboard events (visible characters only) ... (detail in `04`)

Every collector is a **refactor of an existing `reference_v2` demo**, not a
rewrite. The transformation is mechanical and identical for all of them:

> Replace the demo's `print(...)` / `log(...)` reporting with a call to an
> injected `emit(event: dict)` callback, and replace its `if __name__ ==
> "__main__": main()` with a `run(stop_event, emit)` entry point. The X11
> mechanics (which atoms, which masks, dedup logic) are kept **verbatim**.

`emit` is the thread-safe shim from `daemon/01 §2`.

---

## 1. Focus + Title collector  ← `demo-get-window-title-change.py`

One Xlib connection, one `next_event()` loop, already monitors:
- `_NET_ACTIVE_WINDOW` on root → **focus change**,
- `_NET_CLIENT_LIST(_STACKING)` on root → **window open/close list**,
- `_NET_WM_NAME`/`WM_NAME` on each client → **title change**.

Refactor:
- `report_active()` → `emit({"kind":"focus", "x_window_id":wid, "title":title, "ts":now})`.
  Focus events also drive `current_focus_window_uid` (attribution, `02 §5`) and
  bump `last_seen`.
- `sync_clients()` → for added/gone sets, `emit({"kind":"window_list",
  "added":[...], "gone":[...]})` so `WindowRegistry` updates alive/closed
  (`02 §3`). This also satisfies "get a list of opening windows and ids".
- `handle_title_change()` → `emit({"kind":"title", "x_window_id":wid,
  "old":old, "new":new, "ts":now})`. Titles are stored as a **history**
  (append-only) so the timeline view can show "title or titles displayed"
  (`frontend/08 §3`).

This collector runs on **Thread A** and *also* hosts the virtual-desktop watch
(next section) because both are PropertyNotify-on-root on the same connection.

## 2. Virtual Desktop collector  ← `demo-virtual-desktop-monitoring.py`

Watches root for `_NET_CURRENT_DESKTOP` / `_NET_NUMBER_OF_DESKTOPS` /
`_NET_DESKTOP_NAMES`. We use **only `cmd_monitor`'s logic**; `list`/`switch`
subcommands are dropped (the daemon never switches desktops).

Refactor:
- `_on_current_changed()` → `emit({"kind":"vdesktop_current",
  "index":new, "name":name, "ts":now})`.
- `_on_names_changed()` / `_on_count_changed()` → `emit({"kind":"vdesktop_meta",
  "count":count, "names":[...]})`.

**Merge with §1 onto one connection.** Both `TitleFocusMonitor` and
`VirtualDesktopMonitor` do `root.change_attributes(PropertyChangeMask)` and a
`next_event()` loop. Rather than two threads/connections, the daemon's Thread A
runs a single loop that dispatches PropertyNotify by atom name to whichever
handler cares. Keep the two classes as separate modules (`collectors/focus.py`,
`collectors/vdesktop.py`) but feed them from one shared pump (`collectors/
xpump.py`) that owns the connection. This avoids two redundant X connections.

Desktop association per window: when a focus/title event fires we record the
**current desktop index+name** alongside it, so the frontend can "display ...
current virtual desktop name & id associated with the window" without a join to
live X state.

## 3. Clipboard collector  ← `demo-clipboard-event-listener.py`

XFIXES `SetSelectionOwnerNotify` on **CLIPBOARD** = a *copy/WRITE* event. The
demo already classifies content (TEXT/HTML/IMAGE/FILES/PASSWORD/OTHER), reads it
via `xclip`, and dedups by md5.

Refactor:
- Keep `classify()`, `get_targets()`, the `xclip` helper, md5 dedup **as-is**.
- The blocking `xclip` calls move to the daemon's async-subprocess path
  (`01 §4`) — the collector emits a *lightweight* event
  `{"kind":"clipboard_write", "selection":"CLIPBOARD", "targets":[...],
  "ts":now}` and the **handler** (on the loop) does the `xclip` read async,
  classifies, dedups, and enqueues the DB write. This keeps Thread A from
  blocking on `xclip`.
- `PASSWORD` content is **never read/stored** — store only the fact + redaction
  marker (the demo already short-circuits this; preserve it).
- Store: type, char/byte counts, text content (for TEXT/HTML), file paths (for
  FILES), and for IMAGE a saved file (see `05`/`06`) + WxH. Associate with
  `current_focus_window_uid`.

## 4. PRIMARY-selection collector  ← `demo-selection-event-listener-v1.py`

XFIXES `SetSelectionOwnerNotify` on **PRIMARY** = the user *highlighted* text.
The demo's Strategy C (content-stability lockout: hash dedup + 250 ms enqueue
threshold + drag-overlap detection) is exactly what we want to avoid storing a
row per intermediate drag state.

Refactor:
- Keep Strategy C state machine **verbatim**; replace `_enqueue()`'s
  `print`/buffer with `emit({"kind":"selection_content", "text":..., "chars":...,
  "ts":start_ts})`.
- The XFIXES PRIMARY-owner registration can share Thread A's connection too
  (it's the same `xfixes_select_selection_input` on root). So Thread A handles:
  PropertyNotify (focus/title/vdesktop) **plus** two XFIXES owner-notify
  selections (CLIPBOARD, PRIMARY). One connection, one loop, multiple handlers.

This is **"selection content"** in the schematic.

## 5. Paste / read-event collector  ← `demo-detect-paste-event-xrecord.py`

This is the **"selection read events, clipboard read events"** requirement —
i.e. *paste* detection. Per the existing `plan_detect_paste_event.txt`, X11 has
no broadcast paste event, so we detect **gestures**:
- `Ctrl+V` / `Ctrl+Shift+V` → **CLIPBOARD read** candidate (strong),
- middle-click → **PRIMARY read** candidate (weak/overloaded).

Refactor:
- Keep the XRecord setup + `_handle_event` decode **verbatim**; this is
  **Thread B** (XRecord needs its own connection pair, `01 §3`/`daemon/03`).
- Replace `report_paste()` with `emit({"kind":"read_event",
  "selection":"clipboard"|"primary", "gesture":..., "confidence":...,
  "server_time":..., "ts":now})`.
- Store as events labeled **candidate** (never "confirmed"), with confidence,
  associated with `current_focus_window_uid`. Optionally enrich by reading the
  selection content at paste time (same async `xclip` path) so a "read event"
  can show *what* was likely pasted.

**Thread B shares its RECORD context with the keyboard collector** (next file):
one `record_create_context` capturing `(KeyPress, ButtonPress)` feeds both the
paste detector and the keystroke logger. We do **not** open two RECORD contexts.

## 6. Keyboard collector  ← same XRecord context (Thread B)

Visible-character keystroke capture + aggregation is involved enough to get its
own file: see `daemon/04-keyboard-aggregation.md`. Mechanically it is the
KeyPress branch of the same `_handle_event` already used for paste detection
(XRecord is the **preferred** backend; pynput is a documented alternative —
`04 §1a`). The segment **chunking** logic (idle / focus-change / **title-change**
flush) follows
`demo-keyevent-listener-with-idle-chunking-and-focus-change-chunking.py`; the
aggregator subscribes to this thread's `focus`/`title` emissions rather than
opening its own X connection (`04 §3`).

## 7. Summary: threads vs connections

| Thread | Connection(s) | Collectors hosted |
|--------|---------------|-------------------|
| **A** (Xlib pump) | 1 display | focus, title, window-list, vdesktop, clipboard-owner, PRIMARY-selection-owner |
| **B** (XRecord pump) | 2 displays (context + control) | keyboard (visible chars), paste/read gestures, middle-click |

Two threads total for all six event sources. Both feed the same asyncio queue
via `emit`.

## 8. Collector module layout

```
daemon/collectors/
  xpump.py        # owns Thread A display + next_event loop; routes by atom/xfixes subcode
  focus.py        # focus + title + window-list handlers (from get-window-title-change)
  vdesktop.py     # virtual desktop handlers (from virtual-desktop-monitoring)
  clipboard.py    # CLIPBOARD owner handler + classify/xclip (from clipboard-event-listener)
  selection.py    # PRIMARY Strategy C (from selection-event-listener-v1)
  xrecord.py      # owns Thread B RECORD context; routes KeyPress/ButtonPress
  keyboard.py     # visible-char extraction (see 04)
  paste.py        # gesture → read_event (from detect-paste-event-xrecord)
```
