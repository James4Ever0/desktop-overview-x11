#!/usr/bin/env python3
"""tests/test_keyboard_aggregator.py — step 5: clipboard/selection/paste + keyboard.

Fully headless & deterministic — no live X.  The blocking xclip reads are stubbed
and the XRecord/XFIXES decode is exercised with synthetic event objects.  Covers:
  - keyboard.py    visible-char keysym decode + chord/whitespace/backspace rules
  - aggregator.py  segment chunking via focus / title / idle flush triggers
  - handlers.py    clipboard copy (TEXT/PASSWORD/dedup), PRIMARY Strategy C, paste

Like the other step tests we set ``rt.loop`` manually (no writer_loop) and commit
deterministically via ``store.flush()``.

Run:  python -m tests.test_keyboard_aggregator   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="dovw-test-kbd-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from Xlib import X, XK                              # noqa: E402
from daemon.config import Settings                  # noqa: E402
from daemon.db.store import Store                    # noqa: E402
from daemon.windows import WindowRegistry            # noqa: E402
from daemon.runtime import Runtime                   # noqa: E402
from daemon import handlers as handlers_mod          # noqa: E402
from daemon.handlers import EventHandlers            # noqa: E402
from daemon.aggregator import KeyboardAggregator     # noqa: E402
from daemon.collectors import clipboard, selection, keyboard  # noqa: E402
from daemon.collectors.keyboard import KeyboardCollector, keysym_to_unicode  # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


# ───────────────────────── keyboard decode (no async, no X) ─────────────────────────
class _FakeDpy:
    """Minimal local_dpy: keycode_to_keysym(detail, index) -> keysym."""
    def __init__(self, table):
        self._t = table   # {detail: {index: keysym}}

    def keycode_to_keysym(self, detail, index):
        return self._t.get(detail, {}).get(index, self._t.get(detail, {}).get(0, 0))


def _kev(detail, state=0):
    return SimpleNamespace(type=X.KeyPress, detail=detail, state=state)


def test_keyboard_decode():
    print("[keyboard] visible-char decode")
    check("ascii keysym -> char", keysym_to_unicode(XK.XK_a) == "a")
    check("space keysym -> ' '", keysym_to_unicode(XK.XK_space) == " ")
    check("enter keysym -> newline", keysym_to_unicode(XK.XK_Return) == "\n")
    check("function key -> None", keysym_to_unicode(XK.XK_F1) is None)
    check("unicode keysym (中)", keysym_to_unicode(0x01000000 + 0x4e2d) == "中")

    table = {
        38: {0: XK.XK_a, 1: XK.XK_A},     # 'a'/'A'
        65: {0: XK.XK_space},             # space
        22: {0: XK.XK_BackSpace},         # backspace
        54: {0: XK.XK_c, 1: XK.XK_C},     # 'c'
    }
    events = []
    kc = KeyboardCollector(_FakeDpy(table), events.append, Settings())

    kc.on_event(_kev(38))                              # 'a'
    kc.on_event(_kev(38, state=X.ShiftMask))           # 'A'
    kc.on_event(_kev(65))                              # ' '
    kc.on_event(_kev(54, state=X.ControlMask))         # Ctrl+C -> dropped (chord)
    kc.on_event(_kev(22))                              # backspace

    chars = [e.get("char") for e in events if "char" in e]
    check("decoded a/A/space in order", chars == ["a", "A", " "])
    check("Ctrl chord dropped", "c" not in chars and "C" not in chars)
    check("backspace emitted as edit", any(e.get("backspace") for e in events))


# ───────────────────────── shared async harness ─────────────────────────
_run_seq = 0


async def _new_runtime(s):
    global _run_seq
    _run_seq += 1
    store = Store(s)
    await store.open()
    run_id = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at)"
        " VALUES(?,'sess',1.0)", (f"boot{_run_seq}",))
    reg = WindowRegistry(store, "sess", run_id)
    rt = Runtime(store, s)
    rt.loop = asyncio.get_running_loop()    # executor works; no writer_loop (deterministic flush)
    return store, reg, rt


# ───────────────────────── aggregator chunking ─────────────────────────
async def test_aggregator():
    print("[aggregator] segment chunking")
    s = Settings().with_overrides(kbd_idle_flush_s=3.0)
    store, reg, rt = await _new_runtime(s)
    uidA = await reg.ensure_window(0xAAA, "term", 10.0)
    uidB = await reg.ensure_window(0xBBB, "editor", 10.0)

    agg = KeyboardAggregator(store, reg, s, vdesktop_provider=lambda: (1, "Code"))

    # type "hi there" into window A, then a focus change flushes it
    reg.set_focus(uidA)
    for i, ch in enumerate("hi there"):
        await agg.on_char({"kind": "kbd_char", "char": ch, "ts": 100.0 + i})
    await agg.on_focus_change(uidB)
    await store.flush()
    rows = await store.fetchall(
        "SELECT window_uid, text, started_at, ended_at, vdesktop_name, flush_reason "
        "FROM kbd_segment ORDER BY id")
    check("focus-change flushed one segment", len(rows) == 1)
    check("segment text concatenated (whitespace is content)", rows[0][1] == "hi there")
    check("segment attributed to focused window A", rows[0][0] == uidA)
    check("started_at = first key, ended_at = last key", rows[0][2] == 100.0 and rows[0][3] == 107.0)
    check("segment stamped with vdesktop", rows[0][4] == "Code")
    check("flush_reason recorded", rows[0][5] == "focus_change")

    # backspace pops last char, idle flush finalizes (kept above the min-char threshold)
    reg.set_focus(uidB)
    for i, ch in enumerate("abcde"):
        await agg.on_char({"kind": "kbd_char", "char": ch, "ts": 200.0 + i})
    await agg.on_char({"kind": "kbd_char", "backspace": True, "ts": 205.0})
    flushed = await agg.maybe_idle_flush(now=208.5)   # 208.5 - 205.0 >= 3.0
    await store.flush()
    check("idle flush fired", flushed is True)
    seg2 = await store.fetchone(
        "SELECT text, flush_reason FROM kbd_segment WHERE window_uid=?", (uidB,))
    check("backspace popped last char", seg2[0] == "abcd")
    check("idle flush_reason", seg2[1] == "idle")

    # title change is also a cut
    reg.set_focus(uidA)
    for i, ch in enumerate("note"):
        await agg.on_char({"kind": "kbd_char", "char": ch, "ts": 300.0 + i})
    await agg.on_title_change(uidA)
    await store.flush()
    titleseg = await store.fetchone(
        "SELECT text FROM kbd_segment WHERE flush_reason='title_change'")
    check("title-change flushed segment", titleseg is not None and titleseg[0] == "note")

    # FTS over the aggregated segment
    hit = await store.fetchall("SELECT rowid FROM fts_kbd WHERE fts_kbd MATCH 'there'")
    check("kbd segment is FTS-searchable", len(hit) == 1)

    await store.close()


# ───────────────────────── min-segment-char threshold (04 §3) ─────────────────────────
async def test_threshold():
    print("[aggregator] min-segment-char threshold (strip, then > 3)")
    s = Settings()                      # kbd_min_segment_chars = 3 (default)
    store, reg, rt = await _new_runtime(s)
    uid = await reg.ensure_window(0xD00D, "app", 10.0)
    agg = KeyboardAggregator(store, reg, s)
    reg.set_focus(uid)

    async def type_and_flush(text, t0):
        for i, ch in enumerate(text):
            await agg.on_char({"kind": "kbd_char", "char": ch, "ts": t0 + i})
        await agg.flush("idle")
        await store.flush()

    # exactly 3 chars -> NOT greater than threshold -> dropped before buffer/db
    await type_and_flush("abc", 700.0)
    n = await store.fetchone("SELECT COUNT(*) FROM kbd_segment WHERE window_uid=?", (uid,))
    check("3-char segment dropped (not > 3)", n[0] == 0)

    # stripped before the check: "  hi  " -> "hi" (2) -> dropped
    await type_and_flush("  hi  ", 710.0)
    n = await store.fetchone("SELECT COUNT(*) FROM kbd_segment WHERE window_uid=?", (uid,))
    check("padding stripped before threshold; sub-threshold dropped", n[0] == 0)

    # >3 chars survive, and the stored text is the stripped chunk
    await type_and_flush("  hello  ", 720.0)
    row = await store.fetchone("SELECT text FROM kbd_segment WHERE window_uid=?", (uid,))
    check("4+ char segment stored", row is not None and row[0] == "hello")
    check("stored text is stripped", row[0] == "hello")

    await store.close()


# ───────────────────────── clipboard handler ─────────────────────────
async def test_clipboard():
    print("[clipboard] copy / password / dedup")
    s = Settings()
    store, reg, rt = await _new_runtime(s)
    uid = await reg.ensure_window(0xC0FFEE, "app", 10.0)
    reg.set_focus(uid)
    h = EventHandlers(store, reg, s, runtime=rt)

    # --- TEXT copy ---
    clipboard.get_targets = lambda sel: ["UTF8_STRING", "STRING"]
    clipboard.read_content_bytes = lambda sel, kind, targets: "复制了发票内容 invoice".encode()
    await h.handle_clipboard_write({"selection": "CLIPBOARD", "ts": 400.0})
    await store.flush()
    row = await store.fetchone(
        "SELECT kind, text, n_chars FROM clipboard_event WHERE created_at=400.0")
    check("TEXT clipboard row written", row is not None and row[0] == "TEXT")
    check("clipboard text stored", row[1] == "复制了发票内容 invoice")
    clip_hit = await store.fetchall("SELECT rowid FROM fts_clip WHERE fts_clip MATCH 'invoice'")
    check("clipboard text FTS-searchable (ascii)", len(clip_hit) == 1)
    cjk = await store.fetchall("SELECT rowid FROM fts_clip WHERE fts_clip MATCH '发票内'")
    check("clipboard text FTS-searchable (CJK trigram)", len(cjk) == 1)

    # --- dedup: same content again, no new row ---
    await h.handle_clipboard_write({"selection": "CLIPBOARD", "ts": 401.0})
    await store.flush()
    n = await store.fetchone("SELECT COUNT(*) FROM clipboard_event")
    check("identical copy deduped", n[0] == 1)

    # --- PASSWORD: stored as fact only, content never read ---
    read_called = {"v": False}

    def _boom(*a):
        read_called["v"] = True
        return b"secret"
    clipboard.get_targets = lambda sel: ["x-kde-passwordManagerHint", "UTF8_STRING"]
    clipboard.read_content_bytes = _boom
    await h.handle_clipboard_write({"selection": "CLIPBOARD", "ts": 402.0})
    await store.flush()
    pw = await store.fetchone(
        "SELECT kind, text, n_bytes FROM clipboard_event WHERE created_at=402.0")
    check("PASSWORD row written as fact", pw is not None and pw[0] == "PASSWORD")
    check("PASSWORD content never read", read_called["v"] is False)
    check("PASSWORD text is redaction marker", "REDACTED" in pw[1] and pw[2] == 0)

    await store.close()


# ───────────────────────── selection (Strategy C) handler ─────────────────────────
async def test_selection():
    print("[selection] PRIMARY Strategy C")
    s = Settings()
    store, reg, rt = await _new_runtime(s)
    uid = await reg.ensure_window(0x5E1EC7, "app", 10.0)
    reg.set_focus(uid)
    h = EventHandlers(store, reg, s, runtime=rt)

    texts = ["alpha selection one", "zzz different content two"]
    selection.read_primary_text = lambda: (texts.pop(0), 0) if texts else (None, 0)

    # ev1: first selection ever -> recorded as candidate, NOT enqueued
    await h.handle_selection_owner({"selection": "PRIMARY", "ts": 500.0})
    await store.flush()
    none_yet = await store.fetchone("SELECT COUNT(*) FROM selection_event")
    check("first selection not enqueued (lockout)", none_yet[0] == 0)

    # ev2: gap 0.5s >= 0.25 threshold, non-overlapping -> enqueues candidate #1
    await h.handle_selection_owner({"selection": "PRIMARY", "ts": 500.5})
    await store.flush()
    sel = await store.fetchone(
        "SELECT text, n_chars, created_at FROM selection_event")
    check("stable selection enqueued", sel is not None and sel[0] == "alpha selection one")
    check("selection created_at = selection-start ts", sel[2] == 500.0)
    sel_hit = await store.fetchall("SELECT rowid FROM fts_sel WHERE fts_sel MATCH 'selection'")
    check("selection text FTS-searchable", len(sel_hit) == 1)

    await store.close()


# ───────────────────────── paste / read-event handler ─────────────────────────
async def test_paste():
    print("[paste] read-event candidate")
    s = Settings()
    store, reg, rt = await _new_runtime(s)
    uid = await reg.ensure_window(0x9A57E, "app", 10.0)
    reg.set_focus(uid)
    h = EventHandlers(store, reg, s, runtime=rt)

    # CLIPBOARD paste (Ctrl+V) — content read via the clipboard text reader
    handlers_mod._read_clipboard_text = lambda: "pasted clipboard text"
    await h.handle_read_event({"selection": "clipboard", "gesture": "Ctrl+V",
                               "confidence": "strong", "server_time": 123, "ts": 600.0})
    # PRIMARY paste (middle-click) — content read via PRIMARY reader
    selection.read_primary_text = lambda: ("middle pasted primary", 0)
    await h.handle_read_event({"selection": "primary", "gesture": "middle-click",
                               "confidence": "weak", "server_time": 456, "ts": 601.0})
    await store.flush()

    r1 = await store.fetchone(
        "SELECT selection, gesture, confidence, text, server_time, window_uid "
        "FROM read_event WHERE created_at=600.0")
    check("clipboard paste candidate stored", r1 is not None and r1[1] == "Ctrl+V")
    check("paste labeled by confidence (candidate)", r1[2] == "strong")
    check("paste enriched with clipboard content", r1[3] == "pasted clipboard text")
    check("paste associated with focused window", r1[5] == uid)

    r2 = await store.fetchone(
        "SELECT gesture, selection, text FROM read_event WHERE created_at=601.0")
    check("primary paste candidate stored", r2 is not None and r2[0] == "middle-click")
    check("primary paste enriched with primary content", r2[2] == "middle pasted primary")

    await store.close()


async def main():
    test_keyboard_decode()
    await test_aggregator()
    await test_threshold()
    await test_clipboard()
    await test_selection()
    await test_paste()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
