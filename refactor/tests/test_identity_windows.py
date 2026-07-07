#!/usr/bin/env python3
"""tests/test_identity_windows.py — step 2: identity + window registry.

Run:  python -m tests.test_identity_windows   (from refactor/, gui_agent python)
Uses a throwaway temp data dir; never touches the real ~/.local/share.
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-idwin-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings            # noqa: E402
from daemon.db.store import Store              # noqa: E402
from daemon.identity import resolve_identity   # noqa: E402
from daemon.windows import WindowRegistry      # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def main():
    # ── identity ──
    ident = resolve_identity()
    check("boot_id non-empty", bool(ident.boot_id))
    check("session_key is 32-hex md5", bool(re.fullmatch(r"[0-9a-f]{32}", ident.session_key)))
    check("daemon_boot_id is uuid4-shaped",
          bool(re.fullmatch(r"[0-9a-f-]{36}", ident.daemon_boot_id)))
    check("two resolves → distinct daemon_boot_id",
          resolve_identity().daemon_boot_id != ident.daemon_boot_id)

    # ── window registry ──
    s = Settings()
    store = Store(s)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, machine_boot_id, session_key, "
        "user_name, uid, started_at) VALUES(?,?,?,?,?,?)",
        (ident.daemon_boot_id, ident.boot_id, ident.session_key,
         ident.user_name, ident.uid, 1751200000.0))
    reg = WindowRegistry(store, ident.session_key, run_id)
    # tests assume immediate dead-marking; disable the runtime grace period
    reg._alive_grace_s = 0.0

    now = 1751200000.0
    uid1 = await reg.ensure_window(0x3a00003, "firefox", now)
    check("ensure_window mints a uid", isinstance(uid1, int) and uid1 > 0)
    uid1b = await reg.ensure_window(0x3a00003, "firefox", now + 1)
    check("same x_window_id reuses uid (cache)", uid1b == uid1)

    # reuse via DB (drop cache to force the SELECT path)
    reg._alive.clear()
    uid1c = await reg.ensure_window(0x3a00003, "firefox", now + 2)
    check("same x_window_id reuses uid (db lookup)", uid1c == uid1)

    # last_seen bumped by the reuse update
    ls = (await store.fetchone("SELECT last_seen FROM window WHERE window_uid=?", (uid1,)))[0]
    check("last_seen advanced on reuse", ls == now + 2)

    # close then reopen same X id → NEW uid (history never collides)
    await reg.mark_dead(0x3a00003, now + 3)
    dead = await store.fetchone("SELECT alive, closed_at FROM window WHERE window_uid=?", (uid1,))
    check("mark_dead sets alive=0 + closed_at", dead[0] == 0 and dead[1] == now + 3)
    uid2 = await reg.ensure_window(0x3a00003, "firefox", now + 4)
    check("reopened X id mints a fresh uid", uid2 != uid1)

    # second window + attribution pointer
    uid_term = await reg.ensure_window(0x4b00001, "konsole", now + 5)
    reg.set_focus(uid_term)
    check("focus pointer set", reg.current_focus_window_uid == uid_term)
    await reg.bump_last_seen(uid_term, now + 6)
    ls2 = (await store.fetchone("SELECT last_seen FROM window WHERE window_uid=?", (uid_term,)))[0]
    check("bump_last_seen updates column", ls2 == now + 6)

    # ── reconcile: konsole stays, firefox(uid2) gone, a new window appears ──
    # First reconcile acts as the daemon startup sweep and must not write death
    # timestamps for windows that may simply have closed before this run.
    await reg.reconcile(set(), now=now + 6)
    ff_before = await store.fetchone("SELECT alive FROM window WHERE window_uid=?", (uid2,))
    check("first reconcile does not kill pre-existing missing windows", ff_before[0] == 1)

    await reg.reconcile({0x4b00001, 0x5c00009},
                        wm_class_of=lambda x: {0x5c00009: "code"}.get(x), now=now + 7)
    ff = await store.fetchone("SELECT alive FROM window WHERE window_uid=?", (uid2,))
    check("reconcile marks vanished window dead", ff[0] == 0)
    newrow = await store.fetchone(
        "SELECT wm_class, alive FROM window WHERE session_key=? AND x_window_id=?",
        (ident.session_key, 0x5c00009))
    check("reconcile mints newly-appeared window", newrow is not None and newrow[1] == 1)
    check("reconcile kept konsole alive in cache", reg._alive.get(0x4b00001) == uid_term)

    await store.close()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
