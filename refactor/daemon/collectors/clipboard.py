"""daemon/collectors/clipboard.py — CLIPBOARD copy/WRITE collector (plan 03 §3).

Refactor of ``reference_v2/demo-clipboard-event-listener.py``.  Split in two:

* The **collector** (``ClipboardCollector``) runs on Thread A's shared display
  (xpump).  On an XFIXES ``SetSelectionOwnerNotify`` for CLIPBOARD it emits a
  *lightweight* event — it does **not** call ``xclip`` (that would block Thread A
  for up to seconds and starve focus/title detection, 03 §3).
* The blocking ``xclip`` helpers (``get_targets``/``classify``/content read) are
  module functions kept **verbatim** from the demo; the loop-side handler runs
  them through the executor (01 §4) and does dedup + the DB write.

``PASSWORD`` content is never read — only the fact + redaction marker (03 §3).
"""
from __future__ import annotations

import subprocess
import time

from Xlib.ext import xfixes

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:                       # pragma: no cover - Pillow is a dep
    HAS_PIL = False


# ───────────────────── blocking helpers (verbatim from demo) ─────────────────────
def xclip(sel_name: str, target: str, timeout: float = 2.0):
    """Shell out to xclip to fetch selection content. Returns bytes or None."""
    try:
        p = subprocess.run(
            ["xclip", "-selection", sel_name.lower(), "-o", "-t", target],
            capture_output=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def get_targets(sel_name: str):
    """Fetch available target atom names for a selection."""
    raw = xclip(sel_name, "TARGETS")
    if raw is None:
        return []
    return raw.decode("ascii", "replace").split()


PASSWORD_HINTS = {
    "x-kde-passwordManagerHint",
    "application/x-kde-passwordManagerHint",
}
TEXT_ATOMS = {
    "UTF8_STRING", "STRING", "TEXT",
    "text/plain", "text/plain;charset=utf-8",
}
FILE_ATOMS = {
    "text/uri-list",
    "x-special/gnome-copied-files",
    "application/x-kde-cutselection",
}


def classify(targets):
    """Classify clipboard content: PASSWORD|IMAGE|FILES|HTML|TEXT|OTHER."""
    tset = set(targets)
    if tset & PASSWORD_HINTS:
        return "PASSWORD"
    if any(t.startswith("image/") for t in targets):
        return "IMAGE"
    if tset & FILE_ATOMS:
        return "FILES"
    if "text/html" in tset:
        return "HTML"
    if tset & TEXT_ATOMS:
        return "TEXT"
    return "OTHER"


def pick_image_target(targets):
    """Pick the best image-type target from the available list."""
    for pref in ("image/png", "image/jpeg", "image/bmp", "image/tiff"):
        if pref in targets:
            return pref
    for t in targets:
        if t.startswith("image/"):
            return t
    return "image/png"


def read_content_bytes(sel_name: str, kind: str, targets) -> bytes:
    """Fetch the raw selection content for ``kind`` (the demo's dedup/fetch block)."""
    if kind in ("TEXT", "HTML"):
        return (xclip(sel_name, "UTF8_STRING")
                or xclip(sel_name, "text/plain;charset=utf-8")
                or xclip(sel_name, "STRING")
                or xclip(sel_name, "text/plain")
                or b"")
    if kind == "IMAGE":
        return xclip(sel_name, pick_image_target(targets), timeout=5.0) or b""
    if kind == "FILES":
        return xclip(sel_name, "text/uri-list") or b""
    return xclip(sel_name, "UTF8_STRING") or b""


def image_dims(raw: bytes):
    """(width, height) for an encoded image, or (None, None)."""
    if not raw or not HAS_PIL:
        return None, None
    try:
        from io import BytesIO
        img = Image.open(BytesIO(raw))
        return img.width, img.height
    except Exception:
        return None, None


def save_bytes(raw: bytes, abs_path: str) -> None:
    """Write raw clipboard image bytes to ``abs_path`` (run via executor)."""
    import os
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as fh:
        fh.write(raw)


# ───────────────────────── collector (Thread A / xpump) ─────────────────────────
class ClipboardCollector:
    """XFIXES CLIPBOARD owner watcher.  Emits a lightweight write event; the
    handler does the (blocking) xclip read off-thread."""

    def __init__(self, dpy, root, emit):
        self.dpy = dpy
        self.root = root
        self.emit = emit
        self.A_CLIPBOARD = dpy.intern_atom("CLIPBOARD")

    def select_input(self):
        """Register for CLIPBOARD owner-change notifications (call once at start)."""
        self.dpy.xfixes_select_selection_input(
            self.root, self.A_CLIPBOARD, xfixes.XFixesSetSelectionOwnerNotifyMask)

    def on_selection_owner(self, ev) -> bool:
        """Return True if this XFIXES owner-notify was for CLIPBOARD."""
        if getattr(ev, "selection", None) != self.A_CLIPBOARD:
            return False
        self.emit({"kind": "clipboard_write", "selection": "CLIPBOARD",
                   "ts": time.time()})
        return True
