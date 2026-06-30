"""daemon/collectors/selection.py — PRIMARY highlight collector (plan 03 §4).

Refactor of ``reference_v2/demo-selection-event-listener-v1.py`` (Strategy C:
content-stability lockout).  Split like the clipboard collector:

* ``PrimarySelectionCollector`` runs on Thread A's shared display and only emits
  a lightweight ``selection_owner`` event on each XFIXES PRIMARY owner-notify.
* ``read_primary_text`` (blocking ``xclip``, verbatim from the demo) is run by
  the handler through the executor.
* ``SelectionStrategyC`` is the demo's state machine, kept **verbatim** but made
  pure: it takes the already-read ``text`` plus the event ``now`` and returns a
  finished segment dict (to enqueue) or ``None``.  No X, no xclip, no globals →
  unit-testable.  ``_enqueue``'s print/ring-buffer is replaced by *returning* the
  entry; the 250 ms lockout/overlap/hash-dedup logic is unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import time

from Xlib.ext import xfixes

log = logging.getLogger("dovw.selection")

# Strategy C tunables (demo §config)
ENQUEUE_THRESHOLD_S = 0.25        # gap must exceed this to enqueue
READ_RETRY_MAX = 3
READ_RETRY_DELAY_S = 0.02


# ───────────────────── blocking PRIMARY reader (verbatim) ─────────────────────
def read_primary_text():
    """Read PRIMARY as UTF-8 text.  Returns (text|None, bytes_len)."""
    for attempt in range(READ_RETRY_MAX):
        try:
            proc = subprocess.run(
                ["xclip", "-selection", "primary", "-o", "-t", "UTF8_STRING"],
                capture_output=True, timeout=2.0)
        except FileNotFoundError:
            return None, 0
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode == 0:
            raw = proc.stdout
            return raw.decode("utf-8", "replace"), len(raw)
        if attempt < READ_RETRY_MAX - 1:
            time.sleep(READ_RETRY_DELAY_S)
    return None, 0


def _lcp(a, b, min_chars=5):
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return i if i >= min_chars else 0


def _lcs(a, b, min_chars=5):
    i = 0
    while i < len(a) and i < len(b) and a[-(i + 1)] == b[-(i + 1)]:
        i += 1
    return i if i >= min_chars else 0


class SelectionStrategyC:
    """Content-stability lockout state machine (demo Strategy C), made pure.

    ``feed(text, now)`` returns ``{"text","chars","bytes","start_ts"}`` when a
    stable selection should be enqueued, else ``None``.  ``now`` is injected (the
    event timestamp) instead of ``time.time()`` so the daemon — and tests — drive
    the clock deterministically.
    """

    def __init__(self, threshold_s: float = ENQUEUE_THRESHOLD_S):
        self.threshold_s = threshold_s
        self._candidate = None          # {"text","hash","start_ts"}
        self._last_enqueue_ts = None
        self._last_seen_hash = None
        self._prev_text = None

    def feed(self, text: str, now: float):
        if text is None or not text.strip():
            return None

        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if self._last_seen_hash == h:       # 2. hash dedup (owner re-assertion)
            return None
        self._last_seen_hash = h

        # 3. overlap detection — same continuous drag; restart gap clock only.
        if self._prev_text is not None and (_lcp(self._prev_text, text)
                                            or _lcs(self._prev_text, text)):
            if self._candidate is not None:
                self._candidate["text"] = text
                self._candidate["hash"] = h
            else:
                self._candidate = {"text": text, "hash": h, "start_ts": now}
            self._prev_text = text
            return None
        self._prev_text = text

        # 4. gap calculation
        if self._last_enqueue_ts is None:    # first selection ever — record only
            self._last_enqueue_ts = now
            self._candidate = {"text": text, "hash": h, "start_ts": now}
            return None
        if self._candidate is None:          # fresh selection start — record only
            self._candidate = {"text": text, "hash": h, "start_ts": now}
            return None

        gap = now - self._last_enqueue_ts
        if gap >= self.threshold_s:
            out = self._candidate
            entry = {"text": out["text"], "chars": len(out["text"]),
                     "bytes": len(out["text"].encode("utf-8")),
                     "start_ts": out["start_ts"]}
            self._last_enqueue_ts = now
            # this event begins a new candidate (drag start)
            self._candidate = {"text": text, "hash": h, "start_ts": now}
            self._prev_text = text
            return entry
        # within the lockout window — update candidate, do not enqueue
        self._candidate["text"] = text
        self._candidate["hash"] = h
        return None


# ───────────────────────── collector (Thread A / xpump) ─────────────────────────
class PrimarySelectionCollector:
    def __init__(self, dpy, root, emit):
        self.dpy = dpy
        self.root = root
        self.emit = emit
        self.A_PRIMARY = dpy.intern_atom("PRIMARY")

    def select_input(self):
        self.dpy.xfixes_select_selection_input(
            self.root, self.A_PRIMARY, xfixes.XFixesSetSelectionOwnerNotifyMask)

    def on_selection_owner(self, ev) -> bool:
        if getattr(ev, "selection", None) != self.A_PRIMARY:
            return False
        log.debug("selection owner (PRIMARY) changed")
        self.emit({"kind": "selection_owner", "selection": "PRIMARY",
                   "ts": time.time()})
        return True
