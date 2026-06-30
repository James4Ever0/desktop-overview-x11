"""daemon/identity.py — session & daemon identity (plan daemon/02 §1).

Refactored from ``reference_v2/login-session-id-v2.py``: each getter now returns
a value or raises ``IdentityError`` instead of ``sys.exit``-ing, so the daemon
can decide how to degrade.  ``resolve_identity()`` is the one call the daemon
makes at startup; it is best-effort — missing logind/loginctl does not crash the
daemon, it just yields a coarser ``session_key``.  The standalone ``__main__``
block reproduces the original script's printout.

Three identity layers (02 §1):
  * machine ``boot_id``     — /proc/sys/kernel/random/boot_id (until reboot)
  * ``session_key``         — md5(boot_id ":" session_start) (login session)
  * ``daemon_boot_id``      — uuid4 (this daemon process)
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass

log = logging.getLogger("dovw.identity")


class IdentityError(Exception):
    """Raised by a getter when its source is unavailable."""


def get_boot_id() -> str:
    """Machine boot id; raises if not a Linux /proc system."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as f:
            return f.read().strip()
    except FileNotFoundError as e:
        raise IdentityError("/proc/sys/kernel/random/boot_id not found") from e


def get_boot_time() -> int:
    """System boot time (epoch seconds) from /proc/stat btime."""
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
                    break
    except FileNotFoundError as e:
        raise IdentityError("/proc/stat not found") from e
    except ValueError as e:
        raise IdentityError("failed to parse btime") from e
    raise IdentityError("btime not found in /proc/stat")


def _loginctl(session_id: str, prop: str) -> str:
    try:
        r = subprocess.run(
            ["loginctl", "show-session", session_id, "-p", prop, "--value"],
            capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise IdentityError(f"loginctl failed for session {session_id} ({prop})") from e
    except FileNotFoundError as e:
        raise IdentityError("loginctl not found (systemd-logind missing?)") from e


def get_session_id() -> str:
    sid = os.environ.get("XDG_SESSION_ID")
    if not sid:
        raise IdentityError("XDG_SESSION_ID not set (no logind session?)")
    return sid


def get_session_start_time(session_id: str) -> str:
    return _loginctl(session_id, "Timestamp")


def get_session_user_name(session_id: str) -> str:
    return _loginctl(session_id, "Name")


def get_session_user_uid(session_id: str) -> str:
    return _loginctl(session_id, "User")


@dataclass
class Identity:
    boot_id: str
    boot_epoch: int | None
    session_start: str
    session_key: str        # the stable cross-restart key (liveness scope)
    user_name: str
    uid: str
    daemon_boot_id: str     # uuid4, unique per daemon process


def resolve_identity() -> Identity:
    """Best-effort identity for daemon startup (daemon/02 §1).

    Always returns an ``Identity``: if logind is unavailable we still produce a
    ``session_key`` from whatever we have, so the daemon can run.  The
    ``daemon_boot_id`` is freshly generated each call (one per process).
    """
    boot_id = get_boot_id()

    try:
        boot_epoch: int | None = get_boot_time()
    except IdentityError:
        boot_epoch = None
        log.warning("could not read boot time from /proc/stat")

    session_start = ""
    user_name = os.environ.get("USER", "")
    uid = str(os.getuid()) if hasattr(os, "getuid") else ""
    try:
        sid = get_session_id()
        session_start = get_session_start_time(sid)
        user_name = get_session_user_name(sid) or user_name
        uid = get_session_user_uid(sid) or uid
    except IdentityError as exc:
        log.warning("logind unavailable (%s); falling back to env vars", exc)
        # No logind: fall back so session_key is still stable within this boot.
        session_start = session_start or (str(boot_epoch) if boot_epoch else "unknown")

    session_key = hashlib.md5(f"{boot_id}:{session_start}".encode("utf-8")).hexdigest()
    log.info("identity: session_key=%s user=%s boot_id=%s", session_key, user_name, boot_id)
    return Identity(
        boot_id=boot_id,
        boot_epoch=boot_epoch,
        session_start=session_start,
        session_key=session_key,
        user_name=user_name,
        uid=uid,
        daemon_boot_id=str(uuid.uuid4()),
    )


if __name__ == "__main__":
    ident = resolve_identity()
    human = (time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ident.boot_epoch))
             if ident.boot_epoch else "unknown")
    print(f"Boot ID:            {ident.boot_id}")
    print(f"Boot Time:          {human} (epoch: {ident.boot_epoch})")
    print(f"Session Start Time: {ident.session_start}")
    print(f"Session User Name:  {ident.user_name}")
    print(f"Session User ID:    {ident.uid}")
    print(f"Session Key (md5):  {ident.session_key}")
    print(f"Daemon Boot ID:     {ident.daemon_boot_id}")
