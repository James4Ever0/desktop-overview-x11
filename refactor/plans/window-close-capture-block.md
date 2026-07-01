# Window-close capture block — investigation & fix plan

## Symptom

When the user closes a window while the background capture process is still
working on it, the UI occasionally blocks and the mouse cursor changes to a
crosshair-like shape:

```
_| |_
_   _
 | |
```

The user suspects the background daemon is involved.

## Root-cause hypothesis

The daemon uses ImageMagick `import -window <xid> png:-` to capture individual
windows (`daemon/capture.py::capture_window`).  When the target window is
closing/destroyed while `import` is reading its pixels, ImageMagick can fall into
an interactive/selection mode and display its characteristic crosshair cursor.
Because `capture_window` calls `subprocess.run(...)` **without a timeout**, a
hanging `import` process will also block one daemon executor thread until it is
manually killed or the process exits.

Cursor evidence: the shape described matches ImageMagick's `import` crosshair
used for region/window selection.

## Log evidence

A daemon log captured while the symptom occurred shows the exact race:

- `18:23:07` — focus moves to the Desktop Overview main window (`0x09a0000b`,
  title "Desktop Overview — search").
- `18:23:08` — `capture_on_focus window_uid=1134` fires, i.e. the daemon starts
  trying to capture its own UI window.
- `18:23:08` — a help dialog opens (`0x09a03804`, title "Desktop Overview —
  Help") and receives focus.
- `18:23:11` — focus moves back to Konsole.
- `18:23:11` — `capture_on_focus window_uid=1138` fires for the help window
  **after** focus has left it.
- `18:23:11` — `mark_dead window_uid=1138 x=0x09a03804`: the help window is now
  gone, but the capture for it is already in flight.

This confirms the window-lifetime race: `capture_on_focus` is triggered by a
focus-change hook, but the actual `capture_window` call runs asynchronously in
the executor.  By the time it executes, the window can already be destroyed.
`import -window <xid>` on a destroyed window is the likely source of the
blocking crosshair cursor.

Additional observation: the daemon currently captures its **own** windows
("Desktop Overview — search", "Desktop Overview — Help").  The
`window_title_denylist` only applies to keyboard aggregation; it is **not**
consulted by `window_captures.py`.  Capturing the GUI's own windows wastes
executor time and increases the chance of hitting the destruction race.

## Why this is plausible

1. `capture_window` runs `import -window <win_id> png:-` synchronously in the
daemon's executor.
2. The subprocess has **no timeout**.
3. The window list is refreshed at the start of each scheduler tick, but the
actual per-window capture happens later; the window can be destroyed in between.
4. ImageMagick `import` is known to show a crosshair cursor in interactive
selection mode.  If the requested `-window` handle is invalid or disappears
mid-call, behaviour is version/environment-dependent and can include hanging or
falling back to interactive mode.
5. The frontend is mostly async via the API, but a hanging daemon executor thread
reduces/eliminates capture throughput and can make the UI feel blocked if a
synchronous API call (e.g. hover preview, manual refresh) is waiting for the
daemon.

## Investigation steps

### Step 1 — reproduce in isolation

Create a small standalone script that repeatedly captures a window while closing
it, using the exact command the daemon uses:

```bash
import -window 0xXXXXXXXX png:- > /tmp/cap.png
```

or a Python wrapper using `subprocess.run(["import", "-window", wid, "png:-"])`.

- Close the target window at random times while the command is running.
- Observe whether the cursor changes to the crosshair and whether the command
  hangs.
- Test with different ImageMagick versions and window managers/compositors.

### Step 2 — add observability

Temporarily instrument `daemon/capture.py::capture_window`:

- Log the exact command, window id, title, start time, elapsed time, and
  return code.
- Add a defensive timeout to the subprocess call during testing so hung
  `import` processes surface quickly.
- When a hang is observed, inspect the running `import` process:
  - `ps aux | grep import`
  - `strace -p <pid>` to see if it is waiting on an X11 request.
  - `xprop -id <xid>` to confirm whether the window still exists.

### Step 3 — confirm window lifetime gap

Add logging around `daemon/window_captures.py::_capture_and_save`:

- Log when `_capture_and_save` starts for a window id.
- Just before calling `capture_window`, verify the window id is still in
  `capture.get_window_list()`.
- After `capture_window` returns, log success/failure and elapsed time.

This will show whether captures are attempted on windows that have already
closed and whether those attempts correlate with hangs.

### Step 4 — evaluate alternatives locally

For the same window-closing stress test, compare:

1. `import -window <xid>` with a short timeout.
2. `scrot -u` / `scrot` with window selection.
3. `xwd -id <xid> | convert xwd:- png:-`.
4. Python Xlib (`python-xlib`) `XGetImage`.
5. Capturing the full screen / active monitor and cropping by window geometry.

Measure:

- Success rate when the window closes mid-capture.
- Whether any method shows a crosshair cursor.
- Latency and CPU usage.
- Whether the method works under Wayland (if applicable).

## Fix options

### Option A — pre-flight existence check + subprocess timeout (cheap)

Before calling `import`, verify the window still exists with
`capture.get_window_list()` or `xprop -id <xid>`.  Add a timeout to the
`subprocess.run` call (e.g. 5–10 seconds).  If the window is gone or the capture
times out, return `None` gracefully.

Pros: minimal code change; prevents indefinite hangs.
Cons: race remains (window can close between the check and the capture);
does not eliminate the crosshair if `import` still enters interactive mode.

### Option B — replace `import` with `xwd` or `scrot` (medium)

Use `xwd -id <xid>` or `scrot` to capture the window.  These tools generally
fail fast when the window is gone rather than prompting interactively.

Pros: avoids ImageMagick's interactive fallback.
Cons: still X11-specific; may have similar races; adds new dependency.

### Option C — root-window capture + crop (robust)

Capture the entire screen (or the monitor containing the window) using a fast
non-interactive tool, then crop to the window's last-known geometry.  The window
closing mid-capture simply yields stale or empty pixels, but never hangs the
capture pipeline.

Pros: immune to per-window destruction races; no crosshair cursor.
Cons: higher memory/bandwidth; must track window geometry; may capture
overlapping windows; occluded windows become harder.

### Option D — Python Xlib capture (more control)

Use `python-xlib` to call `XGetImage` directly on the window id, with explicit
error handling for `BadWindow` / `BadDrawable`.  This gives fine-grained control
and fast failure on closed windows.

Pros: no subprocess spawn overhead; precise error handling; no interactive
fallback.
Cons: X11-only; requires new dependency; more code.

### Option E — kill-switch for long-running capture subprocesses

Wrap `capture_window` in a wrapper that starts the subprocess with a timeout and
kills it if it exceeds the budget.  Combine with a pre-flight existence check.

Pros: generic safety net regardless of capture backend.
Cons: kills are heavy; does not address the root cause of the crosshair.

## Recommended approach

1. **Immediate mitigation** — add a subprocess timeout and a last-moment
   window-existence check in `capture_window` / `_capture_and_save`.  This
   prevents indefinite hangs even if the race still occurs.
2. **Stop capturing our own windows** — make `window_captures.py` skip windows
   whose titles match `window_title_denylist` (or a new capture-specific
   denylist).  The Desktop Overview main window and help dialog should never be
   captured; this both saves work and removes a known trigger for the race.
3. **If the crosshair still appears**, swap the backend from ImageMagick
   `import` to `xwd` or Python Xlib so invalid windows fail fast instead of
   entering interactive selection mode.

Long-term, **root-window capture + crop** remains the most robust fix for
window-lifetime races, but it is a larger change and should be evaluated after
the cheaper measures above.

## Implementation plan

1. **Timeout**
   - Add `timeout=...` to `subprocess.run` in `daemon/capture.py::capture_window`.
   - Catch `subprocess.TimeoutExpired`, log it, and return `None`.
2. **Existence check**
   - Add a cheap helper `_window_exists(win_id)` using `xprop -id <xid>` or
     `get_window_list()`.
   - Call it immediately before `capture_window` in
     `daemon/window_captures.py::_capture_and_save`.
3. **Skip self windows**
   - In `daemon/window_captures.py`, skip any capture target whose title matches
     `s.window_title_denylist` (extend the list with "Desktop Overview — Help").
   - Alternatively, introduce `s.capture_title_denylist` if the existing list
     should stay keyboard-only.
4. **Logging**
   - Log every capture attempt with xid, title, elapsed ms, and result
     (success / not-found / timeout / decode-error).
5. **Backend evaluation (conditional)**
   - If crosshair/blocking persists after steps 1–3, stress-test `xwd` and
     Python Xlib under the same window-close scenario and replace `import`.
6. **Regression test / checklist**
   - Rapidly open and close the help dialog (and other windows) while the daemon
     is capturing; verify no crosshair cursor and no permanently blocked
     executor threads.

## Out of scope for this fix

- Changing the frontend capture/cache logic.
- OCR-related capture paths (OCR is disabled in v1).
- Wayland-specific capture backends (unless investigation shows the issue is
  Wayland-specific).

## Files to watch

- `daemon/capture.py` — the actual capture subprocess.
- `daemon/window_captures.py` — scheduler that decides which windows to capture.
- `daemon/config.py` — capture-related knobs (timeouts could become config).
- `daemon/runtime.py` — executor usage if we need to detect blocked workers.

## Open questions for investigation

1. Does the crosshair appear on every window-close-during-capture, or only for
   specific window types / compositors?
2. What ImageMagick version is installed?  Does `convert --version` matter?
3. Is the daemon running under X11 or Wayland?
4. Does adding a `subprocess.run(..., timeout=...)` make the symptom disappear
   (replaced by a quick failure) or does the timeout itself hang?
5. Does `xprop -id <xid>` return an error immediately after the window closes,
   confirming a cheap existence check is viable?
6. Does skipping Desktop Overview windows in `window_captures.py` eliminate the
   most common trigger (opening/closing the help dialog)?
