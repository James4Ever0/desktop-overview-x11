#!/usr/bin/env python3
"""tests/test_db.py — step 1: prove WAL + FTS5 + write paths work.

Run:  python -m tests.test_db        (from refactor/, with gui_agent python)
Uses a throwaway temp data dir; never touches the real ~/.local/share.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# isolate to a temp data dir BEFORE importing config
_TMP = tempfile.mkdtemp(prefix="dovw-test-db-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.config import Settings           # noqa: E402
from daemon.db.store import Store             # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def main():
    s = Settings()
    store = Store(s)
    await store.open()
    print(f"[setup] db at {s.db_path}")

    # 1. WAL actually on
    row = await store.fetchone("PRAGMA journal_mode;")
    check("journal_mode == wal", row[0].lower() == "wal")

    # 2. all expected tables + fts present
    rows = await store.fetchall(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")
    names = {r[0] for r in rows}
    for t in ("window", "title_history", "kbd_segment", "clipboard_event",
              "selection_event", "read_event", "window_capture", "fts_kbd",
              "fts_title", "window_capture_latest", "daemon_run", "meta"):
        check(f"object exists: {t}", t in names)
    check("schema_version recorded",
          (await store.fetchone("SELECT value FROM meta WHERE key='schema_version'"))[0] == "1")

    # 3. immediate write returns rowid (window minting path)
    now = 1751200000.0
    wuid = await store.execute(
        "INSERT INTO window(session_key,x_window_id,wm_class,first_seen,last_seen,alive)"
        " VALUES(?,?,?,?,?,1)", ("sesskey", 0x3a00003, "firefox", now, now))
    check("execute() returns lastrowid", isinstance(wuid, int) and wuid > 0)

    # 4. batched writer path: enqueue title + kbd rows, run writer briefly
    stop = asyncio.Event()
    writer = asyncio.create_task(store.writer_loop(stop))
    store.enqueue("INSERT INTO title_history(window_uid,title,changed_at) VALUES(?,?,?)",
                  (wuid, "Inbox — draft about invoice 2026", now))
    store.enqueue("INSERT INTO kbd_segment(window_uid,text,started_at,ended_at,flush_reason)"
                  " VALUES(?,?,?,?,?)", (wuid, "please send the invoice 2026 today", now, now + 5, "idle"))
    store.enqueue("INSERT INTO clipboard_event(window_uid,kind,text,n_chars,created_at)"
                  " VALUES(?,?,?,?,?)", (wuid, "TEXT", "复制了发票内容 invoice text", 22, now))
    store.enqueue("INSERT INTO selection_event(window_uid,text,n_chars,created_at)"
                  " VALUES(?,?,?,?)", (wuid, "highlighted selection sample", 28, now))
    await asyncio.sleep(0.6)   # let one batch commit
    n_kbd = (await store.fetchone("SELECT COUNT(*) FROM kbd_segment"))[0]
    check("batched writer committed rows", n_kbd == 1)

    # 5. FTS search (trigram substring) with highlight markers, per source
    async def search(fts, term):
        return await store.fetchall(
            f"SELECT highlight({fts}, 0, '<mark>', '</mark>') AS ex, rowid "
            f"FROM {fts} WHERE {fts} MATCH ?", (term,))

    kb = await search("fts_kbd", "invoice 2026")
    check("fts_kbd matches typed text", len(kb) == 1 and "<mark>" in kb[0]["ex"])
    ti = await search("fts_title", "invoice")
    check("fts_title matches title", len(ti) == 1)
    cjk = await search("fts_clip", "发票内")   # trigram needs ≥3 chars to match
    check("fts_clip matches CJK substring (trigram)", len(cjk) == 1)

    # 6. external-content trigger sync on DELETE keeps FTS consistent
    await store.execute("DELETE FROM kbd_segment WHERE window_uid=?", (wuid,))
    kb2 = await search("fts_kbd", "invoice 2026")
    check("fts_kbd cleared after source DELETE (trigger)", len(kb2) == 0)

    stop.set()
    await writer
    await store.close()

    # 7. reopen is idempotent (IF NOT EXISTS schema)
    store2 = Store(s)
    await store2.open()
    check("reopen idempotent; window row persisted",
          (await store2.fetchone("SELECT wm_class FROM window WHERE window_uid=?", (wuid,)))[0] == "firefox")
    await store2.close()

    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
