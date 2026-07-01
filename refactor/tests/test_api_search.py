#!/usr/bin/env python3
"""tests/test_api_search.py — step 6: db/search.py pipeline + API endpoints.

Fully headless & deterministic — no live X, no real socket.  We seed the DB
directly, exercise the search assembly in ``daemon/db/search.py``, then drive the
FastAPI app in-process via ``httpx.ASGITransport`` (same JSON the UDS would serve,
07 §2).  ``capture`` is monkeypatched for the one endpoint with a desktop side
effect (``activate``).

Run:  python -m tests.test_api_search   (from refactor/, gui_agent python)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="dovw-test-api-")
os.environ["DESKTOP_OVERVIEW_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx                                            # noqa: E402
from PIL import Image                                    # noqa: E402

from daemon.config import Settings                       # noqa: E402
from daemon.db.store import Store                         # noqa: E402
from daemon.windows import WindowRegistry                 # noqa: E402
from daemon.db import search                              # noqa: E402
from daemon import capture as capture_mod                 # noqa: E402
from daemon.api.app import DaemonContext, create_app      # noqa: E402

_fails = 0


def check(name, cond):
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


async def _seed(store):
    """Seed windows + title/focus/clip/sel/kbd rows + one window_capture file."""
    rid = await store.execute(
        "INSERT INTO daemon_run(daemon_boot_id, session_key, started_at) VALUES('b','sess',1.0)")

    async def win(xid, sess, alive, last_seen, wm, vidx=None, vname=None, app_name=None):
        uid = await store.execute(
            "INSERT INTO window(session_key, first_daemon_run_id, last_daemon_run_id,"
            " x_window_id, wm_class, app_name, vdesktop_index, vdesktop_name, first_seen, last_seen, alive) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sess, rid, rid, xid, wm, app_name, vidx, vname, 1.0, last_seen, alive))
        if app_name:
            await store.execute(
                "INSERT INTO app_name_history(window_uid, app_name, changed_at) VALUES(?,?,?)",
                (uid, app_name, last_seen))
        return uid

    a = await win(0xABCDE, "sess", 1, 300.0, "firefox", 1, "Web", "firefox")    # alive, this session → jumpable
    b = await win(0xBBBBB, "sess", 0, 200.0, "code", 1, "Web", "code")        # dead
    c = await win(0xCCCCC, "other", 1, 250.0, "mail", 1, "Web", "mail")       # different session
    for uid, title, t in [(a, "Inbox — invoice 2026", 110.0),
                          (b, "draft document", 120.0),
                          (c, "mail client", 130.0)]:
        await store.execute(
            "INSERT INTO title_history(window_uid, title, changed_at) VALUES(?,?,?)",
            (uid, title, t))
        await store.execute(
            "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
            " VALUES(?,?,?,?)", (uid, 1, "Web", t))
    # searchable content across the four fields
    await store.execute(
        "INSERT INTO clipboard_event(window_uid, kind, text, n_chars, n_bytes, created_at)"
        " VALUES(?,?,?,?,?,?)", (a, "TEXT", "copied an invoice number", 24, 24, 140.0))
    await store.execute(
        "INSERT INTO clipboard_event(window_uid, kind, text, n_chars, n_bytes, created_at)"
        " VALUES(?,?,?,?,?,?)", (a, "TEXT", "server 10.10.11.149 is up", 25, 25, 142.0))
    await store.execute(
        "INSERT INTO selection_event(window_uid, text, n_chars, created_at) VALUES(?,?,?,?)",
        (a, "highlighted invoice total", 26, 141.0))
    await store.execute(
        "INSERT INTO kbd_segment(window_uid, text, started_at, ended_at, flush_reason)"
        " VALUES(?,?,?,?,?)", (b, "typed the word invoice here", 150.0, 151.0, "idle"))
    # a real window_capture file + row for window a
    rel = os.path.join("window_captures", "b", str(a), "300000.png")
    abs_path = Path(_TMP) / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(abs_path, "PNG")
    await store.execute(
        "INSERT INTO window_capture(window_uid, rel_path, width, height, captured_at, is_focused)"
        " VALUES(?,?,?,?,?,?)", (a, rel, 8, 8, 300.0, 1))
    return a, b, c


# ───────────────────────── search.py unit-level ─────────────────────────
async def test_search_pipeline(store, a, b, c):
    print("[search] db/search.py pipeline")
    res = await search.search(store, q="invoice", current_session_key="sess")
    by_uid = {r["window_uid"]: r for r in res}
    check("matched windows a and b (clip/sel/kbd/title)", a in by_uid and b in by_uid)
    check("different-session window not text-matched", c not in by_uid)

    ra = by_uid[a]
    fields_hit = {h["field"] for h in ra["hits"]}
    check("window a hit in title+clipboard+selection",
          {"title", "clipboard", "selection"}.issubset(fields_hit))
    check("excerpt carries <mark> highlight",
          any("<mark>" in (h["excerpt"] or "") for h in ra["hits"]))
    check("alive+same-session window is jumpable", ra["jumpable"] is True)
    check("assembled current title", ra["current_title"] == "Inbox — invoice 2026")
    check("assembled vdesktop", ra["vdesktop"]["name"] == "Web")
    check("window_capture_url present", ra["window_capture_url"] == f"/windows/{a}/window_capture/latest")
    check("assembled app_name", ra["app_name"] == "firefox")

    # app_name field search
    app_only = await search.search(store, q="firefox", fields=["app_name"],
                                   current_session_key="sess")
    check("fields=app_name finds window a", {r["window_uid"] for r in app_only} == {a})

    # IP / punctuation: mixed mode uses quoted FTS + substring fallback
    ip = await search.search(store, q="10.10.11.149", current_session_key="sess")
    check("mixed search finds IP address", {r["window_uid"] for r in ip} == {a})
    check("IP hit excerpt is marked",
          any(h["field"] == "clipboard" and "<mark>10.10.11.149</mark>" in (h["excerpt"] or "")
              for r in ip for h in r["hits"]))

    # substring-only still works for short tokens the trigram tokenizer cannot index
    short = await search.search(store, q="an", mode="substring", current_session_key="sess")
    check("substring mode finds short token", a in {r["window_uid"] for r in short})

    # multi-word substring: full phrase OR (token_a AND token_b AND ...) across fields
    multi = await search.search(store, q="invoice number", mode="substring",
                                current_session_key="sess")
    check("multi-word substring matches window with both tokens", a in {r["window_uid"] for r in multi})
    check("multi-word substring excludes window missing a token", b not in {r["window_uid"] for r in multi})
    multi2 = await search.search(store, q="invoice up", mode="substring",
                                 current_session_key="sess")
    check("multi-word substring AND across fields", a in {r["window_uid"] for r in multi2})
    check("multi-word substring excludes window missing a token (cross-field)", b not in {r["window_uid"] for r in multi2})
    phrase = await search.search(store, q="copied an invoice", mode="substring",
                                 current_session_key="sess")
    check("substring full-phrase match still works", a in {r["window_uid"] for r in phrase})

    # field subset
    only_kbd = await search.search(store, q="invoice", fields=["keyboard"],
                                   current_session_key="sess")
    kuids = {r["window_uid"] for r in only_kbd}
    check("fields=keyboard restricts to kbd hits", kuids == {b})

    # alive filter
    alive_only = await search.search(store, q="invoice", alive="only",
                                     current_session_key="sess")
    check("alive=only filters out dead window b", b not in {r["window_uid"] for r in alive_only})

    # scope-only (no q): window a's own fields
    scoped = await search.search(store, window_uid=a, current_session_key="sess")
    check("scope-only returns the single window", len(scoped) == 1 and scoped[0]["window_uid"] == a)
    check("scope-only gathers window fields without q",
          {h["field"] for h in scoped[0]["hits"]} >= {"title", "clipboard", "selection"})

    # search-within-window: q + window_uid
    within = await search.search(store, q="invoice", window_uid=b,
                                 current_session_key="sess")
    check("q + window_uid scopes match to that window",
          {r["window_uid"] for r in within} == {b})


async def test_list_and_timeline(store, a, b, c):
    print("[search] list_windows + timeline")
    wins = await search.list_windows(store, current_session_key="sess")
    order = [w["window_uid"] for w in wins]
    check("list_windows sorted by last_access desc", order[:3] == [c, b, a])
    check("dead window present in both filter", b in order)
    alive_list = await search.list_windows(store, alive="only", current_session_key="sess")
    check("list alive=only excludes dead", b not in {w["window_uid"] for w in alive_list})

    # new sort options
    by_title = await search.list_windows(store, sort="title", order="asc",
                                         current_session_key="sess")
    check("list_windows sort=title asc",
          [w["window_uid"] for w in by_title] == [b, a, c])
    by_title_desc = await search.list_windows(store, sort="title", order="desc",
                                              current_session_key="sess")
    check("list_windows sort=title desc",
          [w["window_uid"] for w in by_title_desc] == [c, a, b])
    by_id = await search.list_windows(store, sort="window_id", order="asc",
                                      current_session_key="sess")
    check("list_windows sort=window_id asc",
          [w["window_uid"] for w in by_id] == sorted([a, b, c]))

    # search sort/order
    srch = await search.search(store, q="invoice", sort="last_access", order="desc",
                               current_session_key="sess")
    check("search sort=last_access desc", [w["window_uid"] for w in srch] == [b, a])
    srch_title = await search.search(store, q="invoice", sort="title", order="asc",
                                     current_session_key="sess")
    check("search sort=title asc",
          [w["window_uid"] for w in srch_title] == [b, a])

    tl = await search.timeline(store, current_session_key="sess")
    lanes = {l["window_uid"]: l for l in tl}
    check("timeline has a lane per focused window", {a, b, c} <= set(lanes))
    check("lane carries focus spans", len(lanes[a]["focus_spans"]) == 1)
    check("lane carries title history", lanes[a]["titles"][0]["title"] == "Inbox — invoice 2026")
    check("lane carries app_name", lanes[a].get("app_name") == "firefox")
    # focus spans now have derived ended_at from the next focus event
    a_span = lanes[a]["focus_spans"][0]
    b_span = lanes[b]["focus_spans"][0]
    check("focus span has ended_at", a_span.get("ended_at") is not None)
    check("focus span ended_at is next global focus", a_span["ended_at"] == 120.0)
    check("focus span b ended_at is next global focus", b_span["ended_at"] == 130.0)
    # instantaneous events are attached to lanes
    a_event_types = {e["type"] for e in lanes[a].get("events", [])}
    check("lane a events include title+clipboard+selection",
          {"title", "clipboard", "selection"}.issubset(a_event_types))
    b_event_types = {e["type"] for e in lanes[b].get("events", [])}
    check("lane b events include keyboard", "keyboard" in b_event_types)
    tl_one = await search.timeline(store, window_uid=a, current_session_key="sess")
    check("timeline window_uid scopes to one lane", [l["window_uid"] for l in tl_one] == [a])

    # timeline sort/order
    tl_la = await search.timeline(store, sort="last_access", order="desc",
                                  current_session_key="sess")
    check("timeline sort=last_access desc",
          [l["window_uid"] for l in tl_la[:3]] == [c, b, a])
    tl_title = await search.timeline(store, sort="title", order="asc",
                                     current_session_key="sess")
    check("timeline sort=title asc",
          [l["window_uid"] for l in tl_title[:3]] == [b, a, c])


# ───────────────────────── API endpoints (ASGI in-process) ─────────────────────────
async def test_api(store, a, b, c):
    print("[api] endpoints over ASGI transport")
    reg = WindowRegistry(store, "sess", 1)
    ctx = DaemonContext(store=store, registry=reg, settings=Settings(),
                        runtime=None, identity=None, handlers=None)
    # give /vdesktops something to report
    class _H:  # minimal stand-in for EventHandlers vdesktop state
        desktop_names = ["Web", "Code"]
        vdesktop_index = 0
        vdesktop_name = "Web"
    ctx.handlers = _H()
    app = create_app(ctx)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cli:
        r = await cli.get("/windows")
        check("GET /windows 200", r.status_code == 200)
        check("GET /windows returns all three", len(r.json()) == 3)
        check("GET /windows x_window_id hex-formatted",
              r.json()[0]["x_window_id"].startswith("0x"))

        r = await cli.get("/search", params={"q": "invoice"})
        check("GET /search 200", r.status_code == 200)
        suids = {w["window_uid"] for w in r.json()}
        check("GET /search finds a and b", {a, b} <= suids)

        r = await cli.get(f"/windows/{a}")
        check("GET /windows/{uid} 200", r.status_code == 200)
        check("detail has title_history", len(r.json()["title_history"]) >= 1)
        check("detail has recent events", len(r.json()["events"]) >= 1)
        r = await cli.get("/windows/999999")
        check("GET unknown window 404", r.status_code == 404)

        r = await cli.get(f"/windows/{a}/window_capture/latest")
        check("GET window_capture/latest 200 image/png",
              r.status_code == 200 and r.headers["content-type"] == "image/png")
        r = await cli.get(f"/windows/{b}/window_capture/latest")
        check("GET window_capture for window w/o window_capture 404", r.status_code == 404)

        r = await cli.get("/timeline")
        check("GET /timeline 200", r.status_code == 200 and len(r.json()) == 3)

        r = await cli.get("/vdesktops")
        check("GET /vdesktops lists desktops with current flag",
              r.status_code == 200 and any(d["current"] for d in r.json()))

        r = await cli.get("/health")
        h = r.json()
        check("GET /health ok + session_key + window_count",
              r.status_code == 200 and h["ok"] and h["session_key"] == "sess"
              and h["window_count"] == 3)

        # control: keyboard toggle mutates settings
        r = await cli.post("/control/keyboard", json={"enabled": False})
        check("POST /control/keyboard disables", r.json()["enabled"] is False
              and ctx.settings.kbd_enabled is False)
        await cli.post("/control/keyboard", json={"enabled": True})

        # activate: reason codes
        r = await cli.post(f"/windows/{b}/activate")
        check("activate dead window -> reason dead", r.json()["reason"] == "dead")
        r = await cli.post(f"/windows/{c}/activate")
        check("activate other-session -> reason different-session",
              r.json()["reason"] == "different-session")

        # activate alive window: monkeypatch capture (no live X in CI)
        capture_mod.get_window_list = lambda: [("0x000abcde", 0, "Inbox")]
        capture_mod.activate_window = lambda wid: True
        r = await cli.post(f"/windows/{a}/activate")
        check("activate live window -> ok", r.json()["ok"] is True and r.json()["reason"] == "ok")
        capture_mod.get_window_list = lambda: []   # now vanished from client list
        r = await cli.post(f"/windows/{a}/activate")
        check("activate vanished window -> reason vanished", r.json()["reason"] == "vanished")


async def test_focus_score_and_usage(store, a, b, c):
    print("[search] focus_score + usage sorts")
    # Use a large heartbeat interval so a handful of rows produce whole minutes.
    store.s.heartbeat_interval_s = 60.0
    now = 2000.0
    original_time = search.time.time
    search.time.time = lambda: now
    try:
        # five focused minutes for window a within the last 5 min
        for offset in (30, 90, 150, 210, 270):
            await store.execute(
                "INSERT INTO window_heartbeat(daemon_boot_id, window_uid, x_window_id, ts)"
                " VALUES(?,?,?,?)", ("b", a, 0xABCDE, now - offset))
        # one focused minute for window b (counts in 10m/30m, not 5m)
        await store.execute(
            "INSERT INTO window_heartbeat(daemon_boot_id, window_uid, x_window_id, ts)"
            " VALUES(?,?,?,?)", ("b", b, 0xBBBBB, now - 400))

        # recency: a is most recent, then b, then c
        for uid, t in [(a, now - 10), (b, now - 60), (c, now - 120)]:
            await store.execute(
                "INSERT INTO focus_event(window_uid, vdesktop_index, vdesktop_name, focused_at)"
                " VALUES(?,?,?,?)", (uid, 1, "Web", t))

        wins = await search.list_windows(store, current_session_key="sess")
        by_uid = {w["window_uid"]: w for w in wins}
        check("window a has 5m usage 5.0", by_uid[a].get("usage_5m") == 5.0)
        check("window a has total usage 5.0", by_uid[a].get("usage_total") == 5.0)
        check("window b has 5m usage 0.0", by_uid[b].get("usage_5m") == 0.0)
        check("window b has total usage 1.0", by_uid[b].get("usage_total") == 1.0)

        by_fs = await search.list_windows(store, sort="focus_score", order="desc",
                                          current_session_key="sess")
        check("focus_score desc order", [w["window_uid"] for w in by_fs[:3]] == [a, b, c])
        by_fs_asc = await search.list_windows(store, sort="focus_score", order="asc",
                                              current_session_key="sess")
        check("focus_score asc order", [w["window_uid"] for w in by_fs_asc[:3]] == [c, b, a])

        by_tot = await search.list_windows(store, sort="usage_total", order="desc",
                                           current_session_key="sess")
        check("usage_total desc order", [w["window_uid"] for w in by_tot[:3]] == [a, b, c])

        srch = await search.search(store, q="invoice", sort="focus_score", order="desc",
                                   current_session_key="sess")
        check("search sort=focus_score desc", [w["window_uid"] for w in srch] == [a, b])

        tl = await search.timeline(store, sort="focus_score", order="desc",
                                   current_session_key="sess")
        check("timeline sort=focus_score desc",
              [l["window_uid"] for l in tl[:3]] == [a, b, c])
    finally:
        search.time.time = original_time


async def main():
    s = Settings()
    store = Store(s)
    await store.open()
    a, b, c = await _seed(store)
    await test_search_pipeline(store, a, b, c)
    await test_list_and_timeline(store, a, b, c)
    await test_focus_score_and_usage(store, a, b, c)
    await test_api(store, a, b, c)
    await store.close()
    print(f"\n{'ALL PASS' if _fails == 0 else str(_fails) + ' FAILED'}  (temp dir {_TMP})")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
