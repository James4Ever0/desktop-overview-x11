"""daemon/api/routes.py — the HTTP endpoints (plan 07 §3-5).

Handlers stay **thin**: parse params → call ``db/search.py`` / ``db/store.py`` /
read daemon state → serialize.  No business logic here (07 §6).  Everything is
``async def`` and never blocks the loop; the one blocking action (``activate`` →
``xdotool``) goes through the runtime executor (07 §1).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .. import capture
from ..db import search
from .app import DaemonContext, get_ctx
from . import models

log = logging.getLogger("dovw.api")
router = APIRouter()


def _fields(fields: str | None) -> list[str]:
    if not fields:
        return list(search.ALL_FIELDS)
    return [f.strip() for f in fields.split(",") if f.strip() in search.FIELDS]


# ───────────────────────── windows ─────────────────────────
@router.get("/windows", response_model=list[models.WindowOut])
async def get_windows(
    ctx: DaemonContext = Depends(get_ctx),
    sort: str = Query("last_access", pattern="^(last_access|title|window_id)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    alive: str = Query("both", pattern="^(only|dead|both)$"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    log.debug("GET /windows sort=%s order=%s alive=%s limit=%d", sort, order, alive, limit)
    return await search.list_windows(
        ctx.store, alive=alive, sort=sort, order=order, limit=limit, offset=offset,
        current_session_key=ctx.session_key,
        title_denylist=ctx.settings.window_title_denylist)


@router.get("/windows/{uid}", response_model=models.WindowDetail)
async def get_window(uid: int, ctx: DaemonContext = Depends(get_ctx)):
    detail = await search.window_detail(ctx.store, uid, current_session_key=ctx.session_key)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown window")
    return detail


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
    sort: str = Query("last_access", pattern="^(last_access|relevance|recency|title|window_id)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    hits: str = Query("hit_only", pattern="^(hit_only|all)$"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
):
    log.debug("GET /search q=%s window_uid=%s fields=%s sort=%s order=%s",
              q, window_uid, fields, sort, order)
    return await _do_search(ctx, q, window_uid, fields, alive, sort, order, hits, t_from, t_to)


# from/to need explicit aliasing because `from` is a Python keyword
@router.get("/history", response_model=list[models.WindowOut])
async def get_history(
    ctx: DaemonContext = Depends(get_ctx),
    q: str | None = Query(None),
    window_uid: int | None = Query(None),
    fields: str | None = Query(None),
    alive: str = Query("both", pattern="^(only|dead|both)$"),
    sort: str = Query("last_access", pattern="^(last_access|relevance|recency|title|window_id)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    hits: str = Query("hit_only", pattern="^(hit_only|all)$"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
):
    return await _do_search(ctx, q, window_uid, fields, alive, sort, order, hits, t_from, t_to)


async def _do_search(ctx, q, window_uid, fields, alive, sort, order, hits, t_from=None, t_to=None):
    s = ctx.settings
    limit = min(s.search_default_limit, s.search_max_limit)
    return await search.search(
        ctx.store, q=q, window_uid=window_uid, fields=_fields(fields), alive=alive,
        t_from=t_from, t_to=t_to, sort=sort, order=order, hits=hits, limit=limit,
        current_session_key=ctx.session_key,
        title_denylist=s.window_title_denylist)


# ───────────────────────── timeline ─────────────────────────
@router.get("/timeline", response_model=list[models.TimelineLane])
async def get_timeline(
    ctx: DaemonContext = Depends(get_ctx),
    window_uid: int | None = Query(None),
    sort: str = Query("last_access", pattern="^(last_access|title|window_id)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    t_from: float | None = Query(None, alias="from"),
    t_to: float | None = Query(None, alias="to"),
):
    log.debug("GET /timeline window_uid=%s sort=%s order=%s", window_uid, sort, order)
    return await search.timeline(
        ctx.store, t_from=t_from, t_to=t_to, window_uid=window_uid,
        sort=sort, order=order, current_session_key=ctx.session_key,
        title_denylist=ctx.settings.window_title_denylist)


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
    row = await ctx.store.fetchone(
        "SELECT x_window_id, session_key, alive FROM window WHERE window_uid = ?", (uid,))
    if row is None:
        raise HTTPException(status_code=404, detail="unknown window")
    if row["session_key"] != ctx.session_key:
        return models.ActivateOut(ok=False, reason="different-session")
    if not row["alive"]:
        return models.ActivateOut(ok=False, reason="dead")

    x_id = int(row["x_window_id"])
    # re-verify it's still in the live client list before acting (02 §6)
    windows = await _run(ctx, capture.get_window_list)
    live_ids = {capture.normalize_win_id(w) for w, _ in windows}
    if x_id not in live_ids:
        return models.ActivateOut(ok=False, reason="vanished")

    ok = await _run(ctx, capture.activate_window, f"0x{x_id:08x}")
    log.info("activate window_uid=%s x=0x%08x -> %s", uid, x_id, ok)
    return models.ActivateOut(ok=bool(ok), reason="ok" if ok else "vanished")


async def _run(ctx, fn, *args):
    """Run a blocking call via the runtime executor, or inline if no runtime (tests)."""
    if ctx.runtime is not None:
        return await ctx.runtime.run_in_executor(fn, *args)
    return fn(*args)
