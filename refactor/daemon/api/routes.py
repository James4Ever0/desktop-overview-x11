"""daemon/api/routes.py — the HTTP endpoints (plan 07 §3-5).

Handlers stay **thin**: parse params → call ``db/search.py`` / ``db/store.py`` /
read daemon state → serialize.  No business logic here (07 §6).  Everything is
``async def`` and never blocks the loop; the one blocking action (``activate`` →
``xdotool``) goes through the runtime executor (07 §1).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .. import capture
from ..db import events, search
from .app import DaemonContext, get_ctx
from . import models

log = logging.getLogger("dovw.api")
router = APIRouter()

SORT_RE = r"^(last_access|focus_score|usage_5m|usage_10m|usage_30m|usage_1d|usage_total|title|window_id|relevance|recency)$"
MODE_RE = r"^(fts|substring|mixed)$"


def _fields(fields: str | None) -> list[str]:
    if not fields:
        return list(search.ALL_FIELDS)
    return [f.strip() for f in fields.split(",") if f.strip() in search.FIELDS]


def _desktop_name(ctx: DaemonContext, idx: int | None, stored: str | None = None) -> str | None:
    """Resolve a virtual-desktop name from live state, falling back to a generic label.

    Stored names are only trusted when present; if missing we re-derive from the
    handler's current desktop list (kept fresh by vdesktop_meta events) or wmctrl -d.
    """
    if idx is None:
        return None
    if stored:
        return stored
    names = ctx.handlers.desktop_names if ctx.handlers else []
    if 0 <= idx < len(names):
        return names[idx]
    # last resort: blocking wmctrl -d (only run if no runtime, i.e. tests)
    if ctx.runtime is None:
        names = capture.get_desktop_names()
        if 0 <= idx < len(names):
            return names[idx]
    return f"Desktop {idx + 1}"


def _enrich_window_vdesktops(ctx: DaemonContext, windows: list[dict]) -> list[dict]:
    for w in windows:
        vd = w.get("vdesktop")
        if vd and vd.get("index") is not None and not vd.get("name"):
            vd["name"] = _desktop_name(ctx, vd["index"], vd.get("name"))
    return windows


def _enrich_timeline_vdesktops(ctx: DaemonContext, lanes: list[dict]) -> list[dict]:
    for lane in lanes:
        vd = lane.get("vdesktop")
        if vd and vd.get("index") is not None and not vd.get("name"):
            vd["name"] = _desktop_name(ctx, vd["index"], vd.get("name"))
        for span in lane.get("focus_spans", []):
            if span.get("vdesktop_index") is not None and not span.get("vdesktop_name"):
                span["vdesktop_name"] = _desktop_name(ctx, span["vdesktop_index"], span.get("vdesktop_name"))
    return lanes


def _current_vdesktop_index(ctx: DaemonContext) -> int | None:
    """Return the handler's current virtual-desktop index, if known."""
    h = ctx.handlers
    return h.vdesktop_index if h else None


def _filter_current_vdesktop(items: list[dict], cur_idx: int | None) -> list[dict]:
    """Keep only windows whose vdesktop index matches the current desktop."""
    if cur_idx is None:
        return items
    return [w for w in items if (w.get("vdesktop") or {}).get("index") == cur_idx]


def _filter_timeline_current_vdesktop(lanes: list[dict], cur_idx: int | None) -> list[dict]:
    """Keep only timeline lanes whose window is on the current virtual desktop."""
    if cur_idx is None:
        return lanes
    return [l for l in lanes if (l.get("vdesktop") or {}).get("index") == cur_idx]


# ───────────────────────── windows ─────────────────────────
@router.get("/windows", response_model=list[models.WindowOut])
async def get_windows(
    ctx: DaemonContext = Depends(get_ctx),
    sort: str = Query("last_access", pattern=SORT_RE),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    alive: str = Query("both", pattern="^(only|dead|both)$"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    self_xid: str | None = Query(None),
    current_boot_only: bool = Query(False),
    current_vdesktop_only: bool = Query(False),
    enrich: bool = Query(True),
):
    log.debug("GET /windows sort=%s order=%s alive=%s limit=%d enrich=%s current_boot_only=%s current_vdesktop_only=%s",
              sort, order, alive, limit, enrich, current_boot_only, current_vdesktop_only)
    current_boot_id = ctx.identity.boot_id if (current_boot_only and ctx.identity) else None
    results = await search.list_windows(
        ctx.store, alive=alive, sort=sort, order=order, limit=limit, offset=offset,
        current_session_key=ctx.session_key,
        title_denylist=ctx.settings.window_title_denylist,
        self_xid=self_xid,
        current_boot_id=current_boot_id,
        enrich=enrich)
    results = _enrich_window_vdesktops(ctx, results)
    if current_vdesktop_only:
        results = _filter_current_vdesktop(results, _current_vdesktop_index(ctx))
    return results


@router.post("/windows/scores")
async def post_window_scores(
    uids: list[int],
    ctx: DaemonContext = Depends(get_ctx),
):
    log.debug("POST /windows/scores uids=%d", len(uids))
    if ctx.score_task is not None and not ctx.score_task.done():
        ctx.score_task.cancel()
        try:
            await ctx.score_task
        except asyncio.CancelledError:
            pass
    task = asyncio.create_task(search.window_scores(ctx.store, uids))
    ctx.score_task = task
    try:
        return await task
    except asyncio.CancelledError:
        log.debug("POST /windows/scores cancelled for stale request")
        return {}


@router.get("/windows/{uid}", response_model=models.WindowDetail)
async def get_window(uid: int, ctx: DaemonContext = Depends(get_ctx)):
    detail = await search.window_detail(ctx.store, uid, current_session_key=ctx.session_key)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown window")
    return _enrich_window_vdesktops(ctx, [detail])[0]


@router.get("/windows/{uid}/window_capture/latest")
async def get_latest_window_capture(uid: int, ctx: DaemonContext = Depends(get_ctx)):
    row = await ctx.store.fetchone(
        "SELECT rel_path FROM window_capture_latest WHERE window_uid = ?", (uid,))
    return _window_capture_response(ctx, row)


@router.get("/windows/{uid}/window_capture/{ts}")
async def get_window_capture_at(uid: int, ts: float, ctx: DaemonContext = Depends(get_ctx)):
    # closest capture at-or-before ts, else the earliest after (timeline scrub).
    row = await ctx.store.fetchone(
        "SELECT rel_path FROM window_capture WHERE window_uid = ? "
        "ORDER BY ABS(captured_at - ?) ASC LIMIT 1", (uid, ts))
    return _window_capture_response(ctx, row)


@router.get("/windows/{uid}/window_captures", response_model=list[models.WindowCaptureRef])
async def list_window_captures(
    uid: int,
    ctx: DaemonContext = Depends(get_ctx),
    before: float | None = Query(None, description="captures strictly before this timestamp"),
    after: float | None = Query(None, description="captures strictly after this timestamp"),
    limit: int = Query(10, ge=1, le=50),
):
    if before is not None:
        rows = await ctx.store.fetchall(
            "SELECT captured_at FROM window_capture WHERE window_uid = ? AND captured_at < ? "
            "ORDER BY captured_at DESC LIMIT ?",
            (uid, before, limit))
    elif after is not None:
        rows = await ctx.store.fetchall(
            "SELECT captured_at FROM window_capture WHERE window_uid = ? AND captured_at > ? "
            "ORDER BY captured_at ASC LIMIT ?",
            (uid, after, limit))
    else:
        rows = await ctx.store.fetchall(
            "SELECT captured_at FROM window_capture WHERE window_uid = ? ORDER BY captured_at DESC LIMIT ?",
            (uid, limit))
    return [
        models.WindowCaptureRef(captured_at=row["captured_at"],
                                url=f"/windows/{uid}/window_capture/{row['captured_at']}")
        for row in rows
    ]


def _window_capture_response(ctx, row):
    if row is None or not row["rel_path"]:
        raise HTTPException(status_code=404, detail="no window_capture")
    abs_path = ctx.settings.data_dir / row["rel_path"]
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="window_capture file missing")
    return FileResponse(str(abs_path), media_type="image/png")


# ───────────────────────── search ─────────────────────────
@router.get("/search", response_model=list[models.WindowOut])
async def get_search(
    ctx: DaemonContext = Depends(get_ctx),
    q: str | None = Query(None),
    window_uid: int | None = Query(None),
    fields: str | None = Query(None),
    alive: str = Query("both", pattern="^(only|dead|both)$"),
    sort: str = Query("last_access", pattern=SORT_RE),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    hits: str = Query("hit_only", pattern="^(hit_only|all)$"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
    self_xid: str | None = Query(None),
    mode: str = Query("mixed", pattern=MODE_RE),
    current_boot_only: bool = Query(False),
    current_vdesktop_only: bool = Query(False),
):
    log.debug("GET /search q=%s window_uid=%s fields=%s sort=%s order=%s mode=%s current_boot_only=%s current_vdesktop_only=%s",
              q, window_uid, fields, sort, order, mode, current_boot_only, current_vdesktop_only)
    return await _do_search(ctx, q, window_uid, fields, alive, sort, order, hits, t_from, t_to,
                            self_xid=self_xid, mode=mode,
                            current_boot_only=current_boot_only,
                            current_vdesktop_only=current_vdesktop_only)


# from/to need explicit aliasing because `from` is a Python keyword
@router.get("/history", response_model=list[models.WindowOut])
async def get_history(
    ctx: DaemonContext = Depends(get_ctx),
    q: str | None = Query(None),
    window_uid: int | None = Query(None),
    fields: str | None = Query(None),
    alive: str = Query("both", pattern="^(only|dead|both)$"),
    sort: str = Query("last_access", pattern=SORT_RE),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    hits: str = Query("hit_only", pattern="^(hit_only|all)$"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
    self_xid: str | None = Query(None),
    mode: str = Query("mixed", pattern=MODE_RE),
    current_boot_only: bool = Query(False),
    current_vdesktop_only: bool = Query(False),
):
    return await _do_search(ctx, q, window_uid, fields, alive, sort, order, hits, t_from, t_to,
                            self_xid=self_xid, mode=mode,
                            current_boot_only=current_boot_only,
                            current_vdesktop_only=current_vdesktop_only)


async def _do_search(ctx, q, window_uid, fields, alive, sort, order, hits, t_from=None, t_to=None,
                     self_xid=None, mode="mixed", current_boot_only=False,
                     current_vdesktop_only=False):
    s = ctx.settings
    limit = min(s.search_default_limit, s.search_max_limit)
    current_boot_id = ctx.identity.boot_id if (current_boot_only and ctx.identity) else None
    results = await search.search(
        ctx.store, q=q, window_uid=window_uid, fields=_fields(fields), alive=alive,
        t_from=t_from, t_to=t_to, sort=sort, order=order, hits=hits, limit=limit,
        current_session_key=ctx.session_key,
        title_denylist=s.window_title_denylist,
        self_xid=self_xid,
        mode=mode,
        current_boot_id=current_boot_id)
    results = _enrich_window_vdesktops(ctx, results)
    if current_vdesktop_only:
        results = _filter_current_vdesktop(results, _current_vdesktop_index(ctx))
    return results


# ───────────────────────── timeline ─────────────────────────
@router.get("/timeline", response_model=list[models.TimelineLane])
async def get_timeline(
    ctx: DaemonContext = Depends(get_ctx),
    window_uid: int | None = Query(None),
    sort: str = Query("last_access", pattern=SORT_RE),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
    self_xid: str | None = Query(None),
    current_boot_only: bool = Query(False),
    current_vdesktop_only: bool = Query(False),
):
    log.debug("GET /timeline window_uid=%s sort=%s order=%s current_boot_only=%s current_vdesktop_only=%s",
              window_uid, sort, order, current_boot_only, current_vdesktop_only)
    current_boot_id = ctx.identity.boot_id if (current_boot_only and ctx.identity) else None
    lanes = await search.timeline(
        ctx.store, t_from=t_from, t_to=t_to, window_uid=window_uid,
        sort=sort, order=order, current_session_key=ctx.session_key,
        title_denylist=ctx.settings.window_title_denylist,
        self_xid=self_xid,
        current_boot_id=current_boot_id)
    lanes = _enrich_timeline_vdesktops(ctx, lanes)
    if current_vdesktop_only:
        lanes = _filter_timeline_current_vdesktop(lanes, _current_vdesktop_index(ctx))
    return lanes


# ───────────────────────── events ─────────────────────────
@router.get("/events", response_model=models.EventListOut)
async def get_events(
    ctx: DaemonContext = Depends(get_ctx),
    q: str | None = Query(None),
    type: str | None = Query(None, description="comma-separated event types"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
    sort: str = Query("ts_desc", pattern="^(ts_desc|ts_asc|rank)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    log.debug("GET /events q=%s type=%s sort=%s limit=%d offset=%d",
              q, type, sort, limit, offset)
    types = [t.strip() for t in type.split(",") if t.strip()] if type else None
    rows, total = await events.search_events(
        ctx.store, q=q, types=types, t_from=t_from, t_to=t_to,
        sort=sort, limit=limit, offset=offset,
        current_session_key=ctx.session_key)
    items = [models.GlobalEvent(**r) for r in rows]
    return models.EventListOut(total=total, items=items)


# ───────────────────────── vdesktops ─────────────────────────
@router.get("/vdesktops", response_model=list[models.VDesktopOut])
async def get_vdesktops(ctx: DaemonContext = Depends(get_ctx)):
    h = ctx.handlers
    names = list(h.desktop_names) if h else []
    cur = h.vdesktop_index if h else None
    out = [models.VDesktopOut(index=i, name=n, current=(i == cur))
           for i, n in enumerate(names)]
    if not out and cur is not None:   # names unknown but we know the current index
        out = [models.VDesktopOut(index=cur, name=(h.vdesktop_name if h else None),
                                  current=True)]
    return out


# ───────────────────────── health ─────────────────────────
@router.get("/health", response_model=models.HealthOut)
async def get_health(ctx: DaemonContext = Depends(get_ctx)):
    s = ctx.settings
    db_size = None
    try:
        db_size = os.path.getsize(s.db_path)
    except OSError:
        pass
    n = await ctx.store.fetchone("SELECT COUNT(*) AS c FROM window")
    rt = ctx.runtime
    return models.HealthOut(
        ok=True,
        session_key=ctx.session_key,
        daemon_boot_id=(ctx.identity.daemon_boot_id if ctx.identity else None),
        kbd_enabled=s.kbd_enabled,
        event_queue_depth=(rt._q.qsize() if rt else 0),
        events_dropped=(rt.dropped if rt else 0),
        writes_dropped=ctx.store.dropped,
        db_size_bytes=db_size,
        last_full_sweep=ctx.stats.get("last_full_sweep"),
        window_count=(n["c"] if n else None),
    )


# ───────────────────────── stats ─────────────────────────
def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                total += os.path.getsize(os.path.join(root, f))
    except OSError:
        pass
    return total


@router.get("/stats", response_model=models.StatsOut)
async def get_stats(ctx: DaemonContext = Depends(get_ctx)):
    s = ctx.settings
    db_size = 0
    try:
        db_size = os.path.getsize(s.db_path)
    except OSError:
        pass
    captures_size = _dir_size(str(s.window_capture_dir))

    alive_row = await ctx.store.fetchone(
        "SELECT COUNT(*) AS c FROM window WHERE session_key=? AND alive=1", (ctx.session_key,))
    dead_row = await ctx.store.fetchone(
        "SELECT COUNT(*) AS c FROM window WHERE session_key=? AND alive=0", (ctx.session_key,))
    alive_count = alive_row["c"] if alive_row else 0
    dead_count = dead_row["c"] if dead_row else 0

    cur_focus = ctx.registry.current_focus_window_uid if ctx.registry else None

    vd_rows = await ctx.store.fetchall(
        "SELECT vdesktop_index, vdesktop_name, COUNT(*) AS c FROM window "
        "WHERE session_key=? AND alive=1 GROUP BY vdesktop_index ORDER BY vdesktop_index",
        (ctx.session_key,))
    vdesktop_counts = [
        models.VDesktopCount(index=r["vdesktop_index"], name=r["vdesktop_name"], alive_count=r["c"])
        for r in vd_rows
    ]
    h = ctx.handlers
    total_vd = len(h.desktop_names) if h and h.desktop_names else len(vdesktop_counts)

    event_stats: list[models.EventTypeStats] = []
    total_events = 0
    for typ, table, ts_col, _text_col, _kind_col, _fts in events._TEXT_SOURCES:
        row = await ctx.store.fetchone(
            f"SELECT COUNT(*) AS c, MIN({ts_col}) AS earliest, MAX({ts_col}) AS latest FROM {table}")
        count = row["c"] if row else 0
        total_events += count
        event_stats.append(models.EventTypeStats(
            type=typ, count=count, earliest=row["earliest"], latest=row["latest"]))
    for typ, table, ts_col, _uid_col in events._NON_TEXT_SOURCES:
        row = await ctx.store.fetchone(
            f"SELECT COUNT(*) AS c, MIN({ts_col}) AS earliest, MAX({ts_col}) AS latest FROM {table}")
        count = row["c"] if row else 0
        total_events += count
        event_stats.append(models.EventTypeStats(
            type=typ, count=count, earliest=row["earliest"], latest=row["latest"]))

    return models.StatsOut(
        db_size_bytes=db_size,
        captures_size_bytes=captures_size,
        alive_window_count=alive_count,
        dead_window_count=dead_count,
        current_focus_window_uid=cur_focus,
        total_vdesktop_count=total_vd,
        vdesktop_counts=vdesktop_counts,
        event_total_count=total_events,
        event_stats=event_stats,
        session_key=ctx.session_key,
        daemon_boot_id=(ctx.identity.daemon_boot_id if ctx.identity else None),
    )


# ───────────────────────── control / write (07 §5) ─────────────────────────
@router.post("/window_captures/refresh", response_model=models.RefreshOut)
async def refresh_window_captures(ctx: DaemonContext = Depends(get_ctx)):
    if ctx.window_captures is None:
        raise HTTPException(status_code=503, detail="capture not running")
    import time
    n = await ctx.window_captures.full_sweep(time.time())
    ctx.stats["last_full_sweep"] = time.time()
    log.info("manual full sweep via API captured %d windows", n)
    return models.RefreshOut(ok=True, captured=n)


@router.post("/control/keyboard", response_model=models.KeyboardToggleOut)
async def control_keyboard(body: models.KeyboardToggleIn, ctx: DaemonContext = Depends(get_ctx)):
    ctx.settings.kbd_enabled = bool(body.enabled)   # runtime-toggle (04 §6)
    log.info("keyboard capture %s via API", "enabled" if body.enabled else "disabled")
    return models.KeyboardToggleOut(enabled=ctx.settings.kbd_enabled)


@router.post("/windows/{uid}/activate", response_model=models.ActivateOut)
async def activate_window(uid: int, ctx: DaemonContext = Depends(get_ctx)):
    """Jump to window — re-verify liveness, then xdotool windowactivate (07 §5, 02 §6)."""
    now = time.time()
    row = await ctx.store.fetchone(
        "SELECT x_window_id, session_key, alive FROM window WHERE window_uid = ?", (uid,))
    if row is None:
        raise HTTPException(status_code=404, detail="unknown window")

    x_id = int(row["x_window_id"])
    success = False
    reason = "ok"

    if row["session_key"] != ctx.session_key:
        reason = "different-session"
    elif not row["alive"]:
        reason = "dead"
    else:
        # re-verify it's still in the live client list before acting (02 §6)
        windows = await _run(ctx, capture.get_window_list)
        live_ids = {capture.normalize_win_id(w) for w, _d, _t in windows}
        if x_id not in live_ids:
            reason = "vanished"
        else:
            ok = await _run(ctx, capture.activate_window, f"0x{x_id:08x}")
            success = bool(ok)
            reason = "ok" if ok else "vanished"

    # Log every jump attempt, successful or not.
    title_row = await ctx.store.fetchone(
        "SELECT title FROM title_history WHERE window_uid=? ORDER BY changed_at DESC, id DESC LIMIT 1",
        (uid,))
    boot_id = ctx.identity.daemon_boot_id if ctx.identity else None
    try:
        await ctx.store.execute(
            "INSERT INTO jump_event(window_uid, daemon_boot_id, ts, success, title) VALUES(?,?,?,?,?)",
            (uid, boot_id, now, 1 if success else 0, title_row["title"] if title_row else None))
    except Exception as exc:                               # noqa: BLE001
        log.warning("failed to log jump_event for uid=%s: %s", uid, exc)

    log.info("activate window_uid=%s x=0x%08x -> reason=%s success=%s", uid, x_id, reason, success)
    return models.ActivateOut(ok=success, reason=reason)


async def _run(ctx, fn, *args):
    """Run a blocking call via the runtime executor, or inline if no runtime (tests)."""
    if ctx.runtime is not None:
        return await ctx.runtime.run_in_executor(fn, *args)
    return fn(*args)
