"""frontend/_font_setup.py — bundle local Noto fonts for Tk on Linux.

Tk on X11 uses fontconfig.  We ship Noto Sans / Noto Sans Mono in
frontend/fonts/ so the UI renders consistently even when the system lacks
those fonts or has broken fallbacks.  Before any Tk widget is created we copy
those files into the user's fonts directory and refresh the fontconfig cache,
which is the most reliable way to make them discoverable by Tk/Xft.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("dovw.fe.fonts")


_BUNDLE_DIR = Path(__file__).resolve().parent / "fonts"
_USER_FONT_DIR = Path.home() / ".local" / "share" / "fonts" / "desktop-overview"


def _fonts_available() -> bool:
    return _BUNDLE_DIR.is_dir() and any(_BUNDLE_DIR.glob("*.ttf"))


def _install_fonts() -> None:
    """Copy bundled fonts to the user font directory so fontconfig finds them."""
    _USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in _BUNDLE_DIR.glob("*.ttf"):
        dst = _USER_FONT_DIR / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
            copied += 1
    if copied:
        log.info("installed %d bundled font(s) to %s", copied, _USER_FONT_DIR)
    else:
        log.debug("bundled fonts already present in %s", _USER_FONT_DIR)


def _refresh_cache() -> None:
    try:
        subprocess.run(
            ["fc-cache", "-f", str(_USER_FONT_DIR)],
            check=False, capture_output=True, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("fc-cache for bundled fonts failed: %s", exc)


def setup_fonts() -> None:
    """Make bundled fonts discoverable by fontconfig before Tk initializes."""
    if not _fonts_available():
        log.debug("no bundled fonts in %s", _BUNDLE_DIR)
        return
    _install_fonts()
    _refresh_cache()
