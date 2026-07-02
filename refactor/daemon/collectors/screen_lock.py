"""daemon/collectors/screen_lock.py — detect screen lock/unlock and emit boundaries.

Two independent mechanisms:

  1. Primary: D-Bus signals.
     - Session bus: org.freedesktop.ScreenSaver.ActiveChanged
     - System bus: org.freedesktop.login1.Session.Lock / .Unlock
     Implemented by spawning ``dbus-monitor`` subprocesses and parsing their
     stdout, matching the existing pattern of shelling out to X11 tools.

  2. Fallback: idle time via ``xprintidle``.
     If D-Bus is unavailable or disabled, poll xprintidle.  Sustained idle
     longer than ``screen_lock_idle_threshold_s`` is treated as locked.

Emits:

    {"kind": "screen_lock", "locked": True|False, "method": "dbus"|"idle", "ts": epoch}
"""
from __future__ import annotations

import logging
import select
import subprocess
import threading
import time

log = logging.getLogger("dovw.screen_lock")


def run(stop: threading.Event, emit, settings) -> None:
    """Thread entry point.  Blocks until ``stop`` is set."""
    if not getattr(settings, "screen_lock_enabled", True):
        return
    _ScreenLockCollector(stop, emit, settings).run()


class _ScreenLockCollector:
    def __init__(self, stop: threading.Event, emit, settings):
        self.stop = stop
        self.emit = emit
        self.s = settings
        self._last_state: bool | None = None
        self._last_idle_check = 0.0
        # Per-process parsing state for ActiveChanged boolean argument.
        self._pending_active: dict[int, bool] = {}

    # ───────────────────────── public loop ─────────────────────────
    def run(self) -> None:
        log.debug("screen_lock collector starting")
        procs = self._start_dbus_monitors()
        if not procs and not getattr(self.s, "screen_lock_idle_enabled", True):
            log.warning("screen_lock enabled but no detector available")
            return

        idle_interval = getattr(self.s, "screen_lock_idle_poll_s", 10.0)
        while not self.stop.is_set():
            # Clean up dead monitor processes and respawn if needed.
            procs = [p for p in procs if p.poll() is None]
            if self.s.screen_lock_dbus_enabled and not procs:
                procs = self._start_dbus_monitors()

            fds = {p.stdout.fileno(): p for p in procs if p.stdout}
            if fds:
                try:
                    readable, _, _ = select.select(list(fds.keys()), [], [], idle_interval)
                except (ValueError, OSError):
                    readable = []
                for fd in readable:
                    proc = fds[fd]
                    line = proc.stdout.readline()
                    if line:
                        self._handle_dbus_line(proc, line)
                    else:
                        # EOF: close and respawn next loop.
                        proc.poll()
            else:
                self.stop.wait(idle_interval)

            self._maybe_check_idle()

        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=1.0)
            except Exception:                               # noqa: BLE001
                pass
        log.debug("screen_lock collector stopped")

    # ───────────────────────── D-Bus detection ─────────────────────────
    def _start_dbus_monitors(self) -> list[subprocess.Popen]:
        procs = []
        if not getattr(self.s, "screen_lock_dbus_enabled", True):
            return procs

        # KDE/GNOME session-bus screensaver signal.
        try:
            p = subprocess.Popen(
                ["dbus-monitor", "--session",
                 "type='signal',interface='org.freedesktop.ScreenSaver',"
                 "member='ActiveChanged'"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                bufsize=1)
            procs.append(p)
            log.debug("started dbus-monitor session ScreenSaver")
        except Exception as exc:                            # noqa: BLE001
            log.debug("dbus-monitor session ScreenSaver failed: %s", exc)

        # systemd-logind system-bus session lock/unlock.
        try:
            p = subprocess.Popen(
                ["dbus-monitor", "--system",
                 "type='signal',interface='org.freedesktop.login1.Session',"
                 "member='Lock'",
                 "type='signal',interface='org.freedesktop.login1.Session',"
                 "member='Unlock'"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                bufsize=1)
            procs.append(p)
            log.debug("started dbus-monitor system login1.Session")
        except Exception as exc:                            # noqa: BLE001
            log.debug("dbus-monitor system login1.Session failed: %s", exc)

        return procs

    def _handle_dbus_line(self, proc: subprocess.Popen, line: str) -> None:
        text = line.strip()
        if not text:
            return

        if "member=ActiveChanged" in text and "org.freedesktop.ScreenSaver" in text:
            self._pending_active[id(proc)] = True
            return

        if id(proc) in self._pending_active:
            self._pending_active.pop(id(proc), None)
            if "boolean true" in text:
                self._emit_if_changed(True, "dbus")
            elif "boolean false" in text:
                self._emit_if_changed(False, "dbus")
            return

        if "member=Lock" in text and "org.freedesktop.login1.Session" in text:
            self._emit_if_changed(True, "dbus")
        elif "member=Unlock" in text and "org.freedesktop.login1.Session" in text:
            self._emit_if_changed(False, "dbus")

    # ───────────────────────── idle fallback ─────────────────────────
    def _maybe_check_idle(self) -> None:
        if not getattr(self.s, "screen_lock_idle_enabled", True):
            return
        now = time.monotonic()
        interval = getattr(self.s, "screen_lock_idle_poll_s", 10.0)
        if now - self._last_idle_check < interval:
            return
        self._last_idle_check = now
        idle_ms = self._xprintidle()
        if idle_ms is None:
            return
        threshold_ms = getattr(self.s, "screen_lock_idle_threshold_s", 300.0) * 1000
        self._emit_if_changed(idle_ms > threshold_ms, "idle")

    @staticmethod
    def _xprintidle() -> int | None:
        try:
            proc = subprocess.run(["xprintidle"], capture_output=True, text=True,
                                  timeout=2)
            if proc.returncode != 0:
                return None
            return int(proc.stdout.strip())
        except Exception as exc:                            # noqa: BLE001
            log.debug("xprintidle failed: %s", exc)
            return None

    # ───────────────────────── emission ─────────────────────────
    def _emit_if_changed(self, locked: bool, method: str) -> None:
        if locked == self._last_state:
            return
        self._last_state = locked
        log.debug("screen lock state changed: locked=%s method=%s", locked, method)
        self.emit({"kind": "screen_lock", "locked": locked,
                   "method": method, "ts": time.time()})
