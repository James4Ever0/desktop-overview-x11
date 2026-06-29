# 04 — Keyboard Capture & Aggregation

Covers the most involved `schematic.txt` requirement:

> store keyboard events (visible characters only), associate them with window
> ids. prepare those events for full text search by concatenation using idle
> time threshold or triggered by window focus change.

---

## 1. Capture: visible characters only (XRecord backend — preferred)

Source = the shared XRecord context on **Thread B** (`daemon/03 §5`). For each
`KeyPress` we already have `event.detail` (keycode) and `event.state` (cooked
modifiers). To get a *character*:

```python
keysym = local_dpy.keycode_to_keysym(event.detail, index)   # index from Shift state
ch = keysym_to_unicode(keysym)     # Xlib has XK keysym->string; map to printable
```

XRecord is the **preferred** backend: keystrokes and paste/middle-click gestures
ride **one** RECORD context (no extra thread, no extra dependency), and
`event.state` gives cooked modifiers directly. The
`reference_v2/demo-keyevent-listener-with-idle-chunking-and-focus-change-chunking.py`
demo is used as the reference for the **chunking/flush logic** (§3), which is
backend-agnostic; its pynput capture is the documented *alternative* (§1a).

"Visible characters only" filtering rules:
- **Keep**: printable Unicode produced by the key (letters, digits, punctuation),
  plus space/tab/enter recorded as the whitespace chars `' '`, `'\t'`, `'\n'`.
  Respect Shift/AltGr via the keysym index so we record `A` vs `a`, `@` vs `2`.
- **Drop**: pure modifiers (Ctrl/Alt/Super/Shift alone), navigation/function
  keys (arrows, F-keys, Home/End).
- **Whitespace is content, not a delimiter.** Enter/Tab/Space are appended to
  the segment text (`\n`/`\t`/` `). Segment *cuts* are driven only by
  idle/focus/title (§3) — Enter no longer flushes. (This revises the earlier
  draft, which dropped Enter/Tab and used them as delimiters; the chunking demo
  treats them as content.)
- **Drop shortcut chords**: skip any keypress while **Ctrl or Alt or Super is
  held** (read straight from `event.state`) — those are shortcuts, not text
  (this also avoids logging `Ctrl+V` as a "v"; the paste detector handles that
  chord separately).
- This is a keylogger of *content the user typed into windows*; it is the
  user's own machine and their own data, but we still **never special-case
  passwords** — we have no way to know a field is a password from XRecord.
  Mitigations (skip when the focused window class is a known password manager;
  honor a pause toggle) are in §6.

### 1a. Alternative backend: pynput (not preferred)

`reference_v2/demo-keyevent-listener-...py` captures via a
`pynput.keyboard.Listener` on its own daemon thread and decodes chars with
`_key_to_repr` (no manual keysym work — `KeyCode.char` is already printable;
`Key.space/tab/enter/backspace` map to whitespace/`\b`). It is a viable
fallback when XRecord is unavailable, selectable via `KBD_BACKEND=pynput`
(`10-configuration.md`). Trade-offs vs XRecord:
- **(+)** simpler char decoding; works without the RECORD extension.
- **(−)** adds the `pynput` dependency and a **separate listener thread**
  (Thread C) — keystrokes no longer share the paste detector's RECORD context.
- **(−)** pynput gives **no cooked modifier state** per event, so the collector
  must track held modifiers itself (a small set updated in `on_press`/
  `on_release`) to drop shortcut chords — work XRecord gets for free via
  `event.state`.

Either backend feeds the *same* aggregator (§3); only the capture front-end
differs.

## 2. Why aggregate? (don't store one row per keystroke for search)

Storing each keystroke as its own row makes full-text search useless ("find the
window where I typed `invoice 2026`" can't match across 12 single-char rows).
So we **concatenate** consecutive keystrokes belonging to the same window into a
**text segment**, then index the segment for FTS.

Raw keystrokes *may* still be kept in a low-level table for debugging/timeline,
but the **searchable unit is the aggregated segment**.

## 3. Aggregation state machine (runs as an asyncio task)

Per `schematic.txt` plus the chunking reference demo, a segment is flushed
(finalized + indexed) when **any** of **three** triggers fires:

1. **Idle timeout** — no visible keystroke for `KBD_IDLE_FLUSH_S` (default 3 s),
   checked by a tick every `KBD_IDLE_CHECK_S` (default 0.5 s).
2. **Focus change** — the active window changed. The focus collector emits
   `focus`; the aggregator subscribes and flushes the *previous* window's open
   segment, because subsequent typing belongs to a new window.
3. **Title change** — the *focused* window's title changed even though focus did
   **not** (the demo's third cut condition: `_NET_WM_NAME`/`WM_NAME` change). The
   title collector already emits `title`; the aggregator subscribes to it too
   and flushes. Rationale: "same window, same title" is one coherent typing
   context (e.g. one chat message, one doc); a title change usually means a new
   document/tab/conversation, so it should start a new segment.

> The demo opens its **own** `FocusTitleMonitor` X connection to detect 2 and 3.
> In the daemon we do **not** — Thread A's focus+title collector (`03 §1`)
> already emits both `focus` and `title` events; the aggregator just subscribes
> to those two event kinds. No third X connection.

State (in-memory, one open segment at a time since typing goes to the focused
window):

```python
open_segment = {
  "window_uid": <focused window at first keystroke>,
  "buf": [chars...],
  "started_at": ts,          # time of the segment's FIRST key (demo's start_ts)
  "last_key_at": ts,         # time of the most recent key (drives idle + ended_at)
  "vdesktop_index": ..., "vdesktop_name": ...,   # snapshot for context
}
```

Transitions:
- **keystroke (visible)**:
  - if no open segment, or `window_uid` differs from current focus → flush the
    open one (if any), start a new segment attributed to current focus.
  - append char (whitespace included), update `last_key_at`, reset idle timer.
- **backspace** → pop last char from `buf` (best-effort, `APPLY_BACKSPACE`, §4).
- **focus-change / title-change / idle-timeout** → **flush**. (Enter/Tab are
  *not* flush triggers — they are content, §1.)
- **flush**: if `buf` non-empty, build `text = "".join(buf)`, **strip it**, and
  apply a **minimum-length threshold**: the stripped text must be **longer than
  `KBD_MIN_SEGMENT_CHARS`** (default 3, i.e. ≥ 4 chars) to be persisted.
  Sub-threshold segments (stray keys, a lone Enter/Tab, accidental focus taps)
  are **dropped** — they never enter the write buffer, `kbd_segment`, or its FTS
  row, and do **not** bump `window.last_seen`. Otherwise emit a `kbd_segment`
  write: `{window_uid, text=<stripped>, started_at, ended_at=last_key_at,
  vdesktop...}` → inserted into `kbd_segment` + its FTS row (`daemon/06`). Clear
  the buffer either way. On daemon shutdown, flush the final open segment (same
  threshold applies).

Idle timer implementation: either a single asyncio task `await asyncio.sleep` on
the pending deadline (rescheduled per keystroke), or the demo's simpler approach
— a `KBD_IDLE_CHECK_S` tick that flushes when
`now - last_key_at >= KBD_IDLE_FLUSH_S`. Either is non-blocking on the loop.

## 4. Editing fidelity (pragmatic stance)

We are reconstructing typed text from key *presses*, not reading the widget's
buffer, so:
- **Backspace**: pop the last char from `buf` (best-effort). Other editing
  (arrow-key navigation, mouse-positioned insertion, paste) is **not** modeled —
  the segment is an *approximation* of typed text, which is sufficient for
  "find the window where I typed X". Document this clearly.
- Paste content is captured separately by the clipboard/selection collectors, so
  the keyboard segment intentionally does **not** try to include pasted text.

## 5. Association & timeline value

- Each segment carries `window_uid` (attribution per `02 §5`) and bumps
  `window.last_seen`.
- Segments are time-bounded (`started_at`/`ended_at`), so the **timeline view**
  can show "what was typed into which window when", and **history queries** can
  return keyboard hits within a time range (`frontend/08 §3-4`).

## 6. Privacy controls (recommended, not strictly required by schematic)

- **Pause hotkey** / API toggle to stop keyboard capture (`POST
  /control/keyboard {enabled:false}`); the collector checks a shared flag.
- **App denylist** by `WM_CLASS` (e.g. `keepassxc`, `bitwarden`, lock screen):
  drop keystrokes while such a window is focused.
- **At-rest**: the DB lives under the user's home; consider documenting
  filesystem perms (`chmod 700` the data dir). These are noted so the
  implementer makes a deliberate choice; defaults can be "capture on, denylist
  known password managers".

## 7. Module shape

```
daemon/collectors/keyboard.py   # preferred: XRecord KeyPress -> visible char (keysym decode,
                                #   modifier filter via event.state). Alt backend pynput (§1a),
                                #   selected by KBD_BACKEND; both emit the same `kbd_char` events.
daemon/aggregator.py            # segment state machine: idle/focus/title flush -> kbd_segment;
                                #   subscribes to `focus` + `title` events (no own X connection)
```

## 8. Config knobs (this section)

`KBD_BACKEND` (`xrecord`), `KBD_ENABLED` (true, API-toggleable), `KBD_IDLE_FLUSH_S`
(3.0), `KBD_IDLE_CHECK_S` (0.5), `KBD_FLUSH_ON_FOCUS_CHANGE` (true),
`KBD_FLUSH_ON_TITLE_CHANGE` (true), `KBD_APPLY_BACKSPACE` (true),
`KBD_MIN_SEGMENT_CHARS` (3 — stripped text must exceed this to be stored),
`KBD_APP_DENYLIST`. Defaults, types, and set-via → **`10-configuration.md §2`**.
