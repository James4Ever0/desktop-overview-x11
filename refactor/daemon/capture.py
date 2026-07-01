"""daemon/capture.py — window enumeration & screenshot capture (plan 05 §1).

Lifted near-verbatim from ``reference_v2/demo-no-ocr-efficient-refresh.py``
(coding-rules.md: copy over re-dump).  All functions are blocking subprocess
calls — the daemon runs them through its executor (01 §4), never on the loop.
The Tk-specific bits (`_cap_large` using screen size, `_make_window_capture_photo`) stay
in the frontend; here we cap to a fixed ``WINDOW_CAPTURE_MAX_DIM`` instead.

OCR is intentionally absent for v1 (the no-ocr reference; OCR_ENABLED=False).
"""
from __future__ import annotations

import io
import logging
import subprocess
import time

from PIL import Image

log = logging.getLogger("dovw.capture")


def get_window_list() -> list[tuple[str, int, str]]:
    """``wmctrl -l`` → [(win_id_hex, desktop_index, title)].

    Windows with a negative desktop index (sticky / "on all desktops") are
    discarded — they do not belong to a real virtual desktop.
    Empty list on any failure.
    """
    try:
        proc = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=4)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("wmctrl unavailable: %s", exc)
        return []
    if proc.returncode != 0:
        log.warning("wmctrl exited %d: %s", proc.returncode, (proc.stderr or "").strip())
        return []
    windows = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.strip().split(maxsplit=3)
        if len(parts) >= 4:
            try:
                desktop = int(parts[1])
            except ValueError:
                continue
            if desktop < 0:
                continue
            windows.append((parts[0], desktop, parts[3]))   # (0x03a00003, 0, title)
    return windows


def capture_window(win_id: str, title: str = "", max_dim: int | None = None,
                   timeout_s: float = 5.0):
    """``import -window <id> png:-`` → PIL Image (decoded), or None on failure.

    Blocking; runs in the executor.  Optionally downsizes so neither side exceeds
    ``max_dim`` (daemon-side cap replacing the demo's screen-relative cap).
    ``timeout_s`` caps the subprocess so a destroyed window cannot hang forever.
    """
    cmd = ["import", "-window", win_id, "png:-"]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning("capture TIMEOUT %s (%s) %.0fms", win_id, title[:40], elapsed)
        return None
    elapsed = (time.perf_counter() - t0) * 1000
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        log.debug("capture FAILED %s (%s) %.0fms exit=%d: %s",
                  win_id, title[:40], elapsed, proc.returncode, stderr or "<empty>")
        return None
    try:
        img = Image.open(io.BytesIO(proc.stdout))
        img.load()   # force decode off the loop thread
    except Exception as exc:
        log.debug("decode FAILED %s: %s", win_id, exc)
        return None
    if max_dim and (img.width > max_dim or img.height > max_dim):
        img.window_capture((max_dim, max_dim), Image.LANCZOS)
    return img


def window_exists(win_id: str) -> bool:
    """Cheap check that an X window id is still mapped (``xprop -id <id>``).

    Returns False on any error so that a closing window is treated as gone.
    """
    try:
        proc = subprocess.run(["xprop", "-id", win_id],
                              capture_output=True, timeout=2)
        return proc.returncode == 0
    except Exception as exc:
        log.debug("window_exists check failed for %s: %s", win_id, exc)
        return False


def get_app_name(win_id: str) -> str:
    """``xprop WM_CLASS`` → lowercase instance name, or '' on failure."""
    try:
        proc = subprocess.run(["xprop", "-id", win_id, "WM_CLASS"],
                              capture_output=True, text=True, timeout=2)
        if proc.returncode != 0 or "=" not in proc.stdout:
            return ""
        val = proc.stdout.strip().split("=", 1)[1].strip()
        if val.startswith('"'):
            return val.split('"')[1].lower()
    except Exception:
        return ""
    return ""


def get_active_window_id() -> int | None:
    """``xdotool getactivewindow`` → decimal int id, or None."""
    try:
        proc = subprocess.run(["xdotool", "getactivewindow"],
                              capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def activate_window(win_id: str) -> bool:
    """``xdotool windowactivate <id>`` — raise+focus the window (02 §6 jump)."""
    try:
        proc = subprocess.run(["xdotool", "windowactivate", win_id],
                              capture_output=True, text=True, timeout=2)
    except Exception as exc:
        log.warning("activate_window failed: %s", exc)
        return False
    return proc.returncode == 0


def get_desktop_names() -> list[str]:
    """``wmctrl -d`` → [name_for_index_0, name_for_index_1, ...]. Empty list on failure."""
    try:
        proc = subprocess.run(["wmctrl", "-d"], capture_output=True, text=True, timeout=4)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("wmctrl -d unavailable: %s", exc)
        return []
    if proc.returncode != 0:
        log.warning("wmctrl -d exited %d: %s", proc.returncode, (proc.stderr or "").strip())
        return []
    names = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.strip().split(maxsplit=5)
        if len(parts) >= 6:
            names.append(parts[5])
        elif len(parts) >= 1:
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            while len(names) <= idx:
                names.append(f"Desktop {len(names) + 1}")
    return names


def normalize_win_id(win_id) -> int | None:
    """wmctrl hex id (0x03a00003) → decimal int; None on failure."""
    try:
        return int(win_id, 16)
    except (ValueError, TypeError):
        return None
