"""daemon/db/search.py — FTS5 query builder + window-result assembly (plan 07 §4, 06 §5).

All the server-side compute the frontend used to do client-side lives here:
per-field FTS5 ``MATCH`` with ``snippet()`` excerpts (match markers already
inserted), rowid→window_uid mapping, grouping, metadata join (current title,
current vdesktop, liveness, last access, latest window_capture), filtering and sort.

Reads only — everything goes through ``Store.fetchall``/``fetchone`` on the
read-only connection, so a heavy search can never stall the writer (01 §3).

The four searchable sources (06 §5).  For each: the FTS table, its external
content table, and the per-source timestamp column used for the ``recency`` sort.
The FTS text column is always index 0 (single-column external-content tables),
so ``snippet(fts_X, 0, …)`` is correct for all of them.
"""
from __future__ import annotations

import logging
import time

from ..config import Settings
from ..heartbeat import USAGE_INTERVALS_S, usage_rates

log = logging.getLogger("dovw.search")
_T = lambda: time.perf_counter() * 1000

FIELDS = {
    # field name : (fts table, content table, ts column on the content table)
    "title":     ("fts_title",    "title_history",    "changed_at"),
    "app_name":  ("fts_appname",  "app_name_history", "changed_at"),
    "clipboard": ("fts_clip",     "clipboard_event",  "created_at"),
    "selection": ("fts_sel",      "selection_event",  "created_at"),
    "keyboard":  ("fts_kbd",      "kbd_segment",       "started_at"),
}
ALL_FIELDS = tuple(FIELDS)

# Assembled window object: current title, current vdesktop, liveness, last access,
# latest-window_capture presence.  Correlated subqueries keep it to one statement so
# the frontend never joins (07 §3).
_WINDOW_COLS = """
  w.window_uid       AS window_uid,
  w.x_window_id      AS x_window_id,
  w.wm_class         AS wm_class,
  w.app_name         AS app_name,
  w.alive            AS alive,
  w.session_key      AS session_key,
  w.vdesktop_index   AS vdesktop_index,
  w.vdesktop_name    AS vdesktop_name,
  w.first_seen       AS first_seen,
  w.closed_at        AS closed_at,
  (SELECT COALESCE(MAX(fe.focused_at), 0) FROM focus_event fe
     WHERE fe.window_uid = w.window_uid)                              AS last_access,
  (SELECT th.title FROM title_history th WHERE th.window_uid = w.window_uid
     ORDER BY th.changed_at DESC, th.id DESC LIMIT 1)            AS current_title,
  (SELECT tl.rel_path FROM window_capture_latest tl WHERE tl.window_uid = w.window_uid) AS window_capture_rel,
  (SELECT tl.captured_at FROM window_capture_latest tl WHERE tl.window_uid = w.window_uid) AS window_capture_ts
"""

_SNIPPET = "snippet({fts}, 0, '<mark>', '</mark>', '…', 12)"


def _alive_sql(alive: str, col: str = "w.alive") -> str:
    """alive filter → SQL fragment (without WHERE/AND)."""
    if alive == "only":
        return f"{col} = 1"
    if alive == "dead":
        return f"{col} = 0"
    return ""   # both


def _boot_filter_sql(current_boot_id: str | None, col: str = "w.last_daemon_run_id") -> str:
    """Current machine-boot filter → SQL fragment referencing the daemon_run table."""
    if not current_boot_id:
        return ""
    return f"{col} IN (SELECT dr.id FROM daemon_run dr WHERE dr.machine_boot_id = ?)"


def _xid_int(xid) -> int | None:
    """Normalize a hex/int x_window_id to int."""
    if xid is None:
        return None
    try:
        if isinstance(xid, str):
            return int(xid, 0)
        return int(xid)
    except (ValueError, TypeError):
        return None


def _filter_self(items: list[dict], self_xid) -> list[dict]:
    """Drop alive windows whose x_window_id matches the caller's own X id."""
    if self_xid is None:
        return items
    target = _xid_int(self_xid)
    if target is None:
        return items
    return [o for o in items if not (o.get("alive") and _xid_int(o.get("x_window_id")) == target)]


def assemble_window(row, current_session_key: str | None) -> dict:
    """Turn a ``_WINDOW_COLS`` row into the ready-to-render window object (07 §3)."""
    uid = row["window_uid"]
    alive = bool(row["alive"])
    same_session = (row["session_key"] == current_session_key)
    vidx = row["vdesktop_index"]
    return {
        "window_uid": uid,
        "x_window_id": f"0x{int(row['x_window_id']):08x}",
        "wm_class": row["wm_class"],
        "app_name": row["app_name"],
        "current_title": row["current_title"],
        "vdesktop": ({"index": vidx, "name": row["vdesktop_name"]}
                     if vidx is not None or row["vdesktop_name"] is not None else None),
        "alive": alive,
        "jumpable": alive and same_session,
        "last_access": row["last_access"],
        "first_seen": row["first_seen"],
        "closed_at": row["closed_at"],
        "window_capture_url": (f"/windows/{uid}/window_capture/latest" if row["window_capture_rel"] else None),
        "window_capture_ts": row["window_capture_ts"],
        "hits": [],
    }


async def _enrich_usage(store, items: list[dict], key: str = "window_uid", now=None) -> None:
    """Attach usage_5m/10m/30m active-minute fields to a list of window/lane dicts."""
    if not items:
        return
    rates = await usage_rates(store, [o[key] for o in items], now=now)
    for o in items:
        o.update(rates.get(o[key], {label: 0.0 for label in USAGE_INTERVALS_S}))


def _focus_score(usage_5m, usage_10m, usage_30m, last_access, now: float, s: Settings) -> float:
    """Blend recent focused usage and recency into a single ranking score."""
    if not last_access:
        return 0.0
    recency_min = (now - last_access) / 60.0
    recency = 1.0 / (1.0 + recency_min)   # avoids division by zero; 1.0 when just focused

    total_usage_w = s.focus_score_w5 + s.focus_score_w10 + s.focus_score_w30
    if total_usage_w <= 0:
        usage = 0.0
    else:
        usage = (
            s.focus_score_w5  * (usage_5m  or 0) / 5.0 +
            s.focus_score_w10 * (usage_10m or 0) / 10.0 +
            s.focus_score_w30 * (usage_30m or 0) / 30.0
        ) / total_usage_w

    total_mix = s.focus_score_usage_weight + s.focus_score_recency_weight
    if total_mix <= 0:
        return 0.0
    score = (s.focus_score_usage_weight * usage + s.focus_score_recency_weight * recency) / total_mix
    return round(score, 4)


async def _enrich_focus_score(store, items: list[dict], now=None) -> None:
    """Attach focus_score to a list of window/lane dicts after usage is present."""
    if not items:
        return
    now = time.time() if now is None else now
    s = store.s
    for o in items:
        o["focus_score"] = _focus_score(
            o.get("usage_5m"), o.get("usage_10m"), o.get("usage_30m"),
            o.get("last_access"), now, s)


def _is_usage_sort(sort: str) -> bool:
    return sort == "focus_score" or sort.startswith("usage_")


JUMP_INTERVALS_S = {
    "jump_5m": 300,
    "jump_10m": 600,
    "jump_30m": 1800,
    "jump_1d": 86400,
}


async def _enrich_jump_counts(store, items: list[dict], key: str = "window_uid", now=None) -> None:
    """Attach jump_5m/10m/30m/1d/total counts to a list of window/lane dicts."""
    if not items:
        return
    now = time.time() if now is None else now
    uids = [o[key] for o in items]
    placeholders = ",".join("?" * len(uids))
    max_age = max(JUMP_INTERVALS_S.values())

    cols = ", ".join(
        f"SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS {label}"
        for label in JUMP_INTERVALS_S
    )
    params = [now - v for v in JUMP_INTERVALS_S.values()]
    params.extend(uids)
    params.append(now - max_age)
    sql = (f"SELECT window_uid, {cols} FROM jump_event "
           f"WHERE window_uid IN ({placeholders}) AND ts >= ? AND success = 1 "
           "GROUP BY window_uid")
    rows = await store.fetchall(sql, tuple(params))
    out: dict[int, dict] = {uid: {label: 0 for label in JUMP_INTERVALS_S} for uid in uids}
    for row in rows:
        uid = row["window_uid"]
        for label in JUMP_INTERVALS_S:
            out[uid][label] = row[label] or 0

    total_sql = (f"SELECT window_uid, COUNT(*) AS n FROM jump_event "
                 f"WHERE window_uid IN ({placeholders}) AND success = 1 "
                 "GROUP BY window_uid")
    total_rows = await store.fetchall(total_sql, tuple(uids))
    for row in total_rows:
        out[row["window_uid"]]["jump_total"] = row["n"] or 0
    for uid in uids:
        out[uid].setdefault("jump_total", 0)

    for o in items:
        o.update(out[o[key]])


async def _focus_span_usage_total(store, uids: list[int]) -> dict[int, float]:
    """Return total focused minutes per window from global focus-event spans.

    A focus span starts at a ``focus_event`` row and ends at the next global
    focus or screen-lock event.  This gives a usable fallback total for dead
    windows that have no recent heartbeat rows.
    """
    if not uids:
        return {}
    placeholders = ",".join("?" * len(uids))
    sql = (
        "WITH events AS ("
        " SELECT 'focus' AS kind, window_uid, focused_at AS ts FROM focus_event"
        " UNION ALL"
        " SELECT 'lock', NULL, changed_at FROM screen_lock_event"
        "),"
        " ordered AS ("
        " SELECT kind, window_uid, ts,"
        "   LEAD(ts) OVER (ORDER BY ts) AS next_ts"
        " FROM events"
        ")"
        " SELECT window_uid, ROUND(SUM(next_ts - ts) / 60.0, 1) AS minutes"
        " FROM ordered"
        f" WHERE kind='focus' AND window_uid IN ({placeholders}) AND next_ts IS NOT NULL"
        " GROUP BY window_uid"
    )
    rows = await store.fetchall(sql, tuple(uids))
    return {row["window_uid"]: row["minutes"] for row in rows}


async def _enrich_usage_total_only(store, items: list[dict], key: str = "window_uid") -> None:
    """Attach usage_total for dead/visible rows.

    Uses heartbeat counts when available; otherwise falls back to the total
    focused duration derived from focus-event spans.
    """
    if not items:
        return
    uids = [o[key] for o in items]
    interval = getattr(store.s, "heartbeat_interval_s", 10.0)
    placeholders = ",".join("?" * len(uids))
    sql = (f"SELECT window_uid, COUNT(*) AS n FROM window_heartbeat "
           f"WHERE window_uid IN ({placeholders}) GROUP BY window_uid")
    rows = await store.fetchall(sql, tuple(uids))
    heartbeat_totals = {row["window_uid"]: round(row["n"] * interval / 60.0, 1) for row in rows}

    # Fall back to focus-span duration for windows with no recorded heartbeats.
    missing = [uid for uid in uids if heartbeat_totals.get(uid, 0.0) == 0.0]
    focus_totals = await _focus_span_usage_total(store, missing) if missing else {}

    for o in items:
        uid = o[key]
        total = heartbeat_totals.get(uid, 0.0)
        if total == 0.0:
            total = focus_totals.get(uid, 0.0)
        o["usage_total"] = total


def _set_dead_scores_zero(items: list[dict]) -> None:
    """Set recent usage fields and focus_score to 0.0 for dead rows (used when sorting by usage/focus).

    ``usage_total`` is preserved/recalculated separately because it can be derived
    from historical heartbeat or focus-span data even for dead windows.
    """
    for o in items:
        for label in USAGE_INTERVALS_S:
            o[label] = 0.0
        o["focus_score"] = 0.0


# ───────────────────────── GET /windows (no FTS) ─────────────────────────
def _window_sort_col(sort: str) -> str:
    """Map public sort key to a SQL column/expression on the window table."""
    if sort == "title":
        return "LOWER(current_title)"
    if sort == "window_id":
        return "w.window_uid"
    return "last_access"  # last_access default, derived from focus_event.focused_at


async def _page_uids_last_access(store, where_clause: str, params: list,
                                 order: str, limit: int, offset: int) -> list[int]:
    """Select just window_uids ordered by last_access via a pre-aggregated join.

    This avoids evaluating the expensive ``_WINDOW_COLS`` correlated subqueries
    for every candidate row; we only fetch full metadata for the small result page.
    """
    direction = "ASC" if order == "asc" else "DESC"
    sql = (
        "WITH last_focus AS ("
        " SELECT window_uid, MAX(focused_at) AS focused_at FROM focus_event GROUP BY window_uid"
        ")"
        " SELECT w.window_uid, COALESCE(last_focus.focused_at, 0) AS last_access"
        " FROM window w"
        " LEFT JOIN last_focus ON last_focus.window_uid = w.window_uid"
        f" {where_clause}"
        f" ORDER BY last_access {direction}, w.window_uid ASC"
        " LIMIT ? OFFSET ?"
    )
    rows = await store.fetchall(sql, tuple(params + [limit, offset]))
    return [r["window_uid"] for r in rows]


async def list_windows(store, *, alive="both", sort="last_access", order="desc",
                       limit=200, offset=0, current_session_key=None,
                       title_denylist=None, self_xid=None,
                       current_boot_id=None, enrich=True) -> list[dict]:
    where = []
    params = []
    a = _alive_sql(alive)
    if a:
        where.append(a)
    b = _boot_filter_sql(current_boot_id)
    if b:
        where.append(b)
        params.append(current_boot_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    now = time.time()

    def _filter_denied(results):
        if not title_denylist:
            return results
        denied = set(title_denylist)
        return [r for r in results if r.get("current_title") not in denied]

    if sort == "focus_score" or sort.startswith("usage_"):
        # focus_score and usage sorts depend on enriched values, so sort globally in Python.
        # We always compute the values needed for ordering; they are stripped before
        # returning when enrich=False so the API contract is preserved.
        t0 = _T()
        sql = f"SELECT {_WINDOW_COLS} FROM window w {where_clause}"
        rows = await store.fetchall(sql, tuple(params))
        t1 = _T()
        log.info("list_windows metadata alive=%s sort=%s enrich=%s rows=%d ms=%.1f",
                 alive, sort, enrich, len(rows), t1 - t0)
        results = [assemble_window(r, current_session_key) for r in rows]
        results = _filter_denied(results)
        results = _filter_self(results, self_xid)
        alive_items = [r for r in results if r["alive"]]
        if alive_items:
            await _enrich_usage(store, alive_items, now=now)
            if sort == "focus_score":
                await _enrich_focus_score(store, alive_items, now=now)
        dead_items = [r for r in results if not r["alive"]]
        if dead_items:
            _set_dead_scores_zero(dead_items)
            await _enrich_usage_total_only(store, dead_items)
        await _enrich_jump_counts(store, results, now=now)
        t2 = _T()
        log.info("list_windows enrich alive=%s sort=%s enrich=%s rows=%d ms=%.1f",
                 alive, sort, enrich, len(results), t2 - t1)
        reverse = order != "asc"
        if sort == "focus_score":
            results.sort(key=lambda o: (o.get("focus_score") or 0, o["window_uid"]), reverse=reverse)
        else:
            results.sort(key=lambda o: (o.get(sort) or 0, o["window_uid"]), reverse=reverse)
        page = results[offset:offset + limit]
        if not enrich:
            for o in page:
                for label in USAGE_INTERVALS_S:
                    o.pop(label, None)
                o.pop("usage_total", None)
                o.pop("focus_score", None)
                for label in JUMP_INTERVALS_S:
                    o.pop(label, None)
                o.pop("jump_total", None)
        return page

    if sort == "last_access":
        # Fast path: pre-aggregate focus times, select uids for the page, then fetch
        # full metadata for just those uids.  This avoids evaluating the expensive
        # _WINDOW_COLS correlated subqueries over the entire candidate set.
        t0 = _T()
        uids = await _page_uids_last_access(store, where_clause, params, order, limit, offset)
        t1 = _T()
        log.info("list_windows uid-select alive=%s sort=%s enrich=%s rows=%d ms=%.1f",
                 alive, sort, enrich, len(uids), t1 - t0)
        if not uids:
            return []
        placeholders = ",".join("?" * len(uids))
        sql = f"SELECT {_WINDOW_COLS} FROM window w WHERE w.window_uid IN ({placeholders})"
        rows = await store.fetchall(sql, tuple(uids))
        row_by_uid = {r["window_uid"]: r for r in rows}
        ordered_rows = [row_by_uid[uid] for uid in uids if uid in row_by_uid]
        results = [assemble_window(r, current_session_key) for r in ordered_rows]
        results = _filter_denied(results)
        results = _filter_self(results, self_xid)
    else:
        sort_col = _window_sort_col(sort)
        direction = "ASC" if order == "asc" else "DESC"
        # secondary key keeps ordering stable when primary key ties
        secondary = "w.window_uid ASC"
        t0 = _T()
        sql = (f"SELECT {_WINDOW_COLS} FROM window w "
               + where_clause
               + f" ORDER BY {sort_col} {direction}, {secondary} LIMIT ? OFFSET ?")
        rows = await store.fetchall(sql, tuple(params + [limit, offset]))
        t1 = _T()
        log.info("list_windows metadata alive=%s sort=%s enrich=%s rows=%d ms=%.1f",
                 alive, sort, enrich, len(rows), t1 - t0)
        results = [assemble_window(r, current_session_key) for r in rows]
        results = _filter_denied(results)
        results = _filter_self(results, self_xid)
    if enrich:
        if alive == "dead":
            await _enrich_usage_total_only(store, results)
        elif alive == "both":
            alive_items = [r for r in results if r["alive"]]
            if alive_items:
                await _enrich_usage(store, alive_items, now=now)
                await _enrich_focus_score(store, alive_items, now=now)
            dead_items = [r for r in results if not r["alive"]]
            if dead_items:
                await _enrich_usage_total_only(store, dead_items)
        else:  # only
            await _enrich_usage(store, results, now=now)
            await _enrich_focus_score(store, results, now=now)
        await _enrich_jump_counts(store, results, now=now)
        t2 = _T()
        log.info("list_windows enrich alive=%s sort=%s enrich=%s rows=%d ms=%.1f",
                 alive, sort, enrich, len(results), t2 - t1)
    return results


async def window_scores(store, uids: list[int], now=None) -> dict[int, dict]:
    """Return {window_uid: scores} for a small set of visible windows.

    Alive rows receive full recent-usage buckets and focus_score.  Dead rows
    receive only usage_total; recent intervals and focus_score are omitted.
    """
    if not uids:
        return {}
    now = time.time() if now is None else now
    placeholders = ",".join("?" * len(uids))
    rows = await store.fetchall(
        f"SELECT window_uid, alive FROM window WHERE window_uid IN ({placeholders})",
        tuple(uids))
    alive_map = {row["window_uid"]: bool(row["alive"]) for row in rows}

    # focus_score needs last_access; fetch it once for all requested windows.
    last_access_rows = await store.fetchall(
        f"SELECT window_uid, COALESCE(MAX(focused_at), 0) AS last_access "
        f"FROM focus_event WHERE window_uid IN ({placeholders}) GROUP BY window_uid",
        tuple(uids))
    last_access_map = {row["window_uid"]: row["last_access"] for row in last_access_rows}

    items = [
        {"window_uid": uid, "alive": alive_map.get(uid, False),
         "last_access": last_access_map.get(uid, 0) or 0}
        for uid in uids
    ]
    await _enrich_usage(store, items, now=now)
    alive_items = [it for it in items if it["alive"]]
    if alive_items:
        await _enrich_focus_score(store, alive_items, now=now)
    await _enrich_jump_counts(store, items, now=now)

    out: dict[int, dict] = {}
    for it in items:
        uid = it["window_uid"]
        if it["alive"]:
            base = {label: it.get(label) for label in USAGE_INTERVALS_S}
            base["usage_total"] = it.get("usage_total")
            base["focus_score"] = it.get("focus_score")
        else:
            base = {"usage_total": it.get("usage_total")}
        base.update({label: it.get(label) for label in JUMP_INTERVALS_S})
        base["jump_total"] = it.get("jump_total")
        out[uid] = base
    return out


# ───────────────────────── GET /search (FTS) ─────────────────────────
async def search(store, *, q=None, window_uid=None, fields=None, alive="both",
                 t_from=None, t_to=None, sort="last_access", order="desc", hits="hit_only",
                 limit=100, offset=0, current_session_key=None,
                 title_denylist=None, self_xid=None, mode="mixed",
                 current_boot_id=None) -> list[dict]:
    """The core multi-field search (07 §4).

    ``q`` empty + ``window_uid`` set → scope-only "show this window's fields"
    (07 §4a), no text match.  ``q`` present → per-field FTS5 MATCH, optionally
    constrained to ``window_uid`` (search-within-window).
    """
    fields = [f for f in (fields or ALL_FIELDS) if f in FIELDS]
    if q:
        per_uid: dict[int, dict] = {}
        if mode in ("fts", "mixed"):
            fts = await _fts_collect(store, q, fields, window_uid, limit_per_field=limit * 4)
            _merge_per_uid(per_uid, fts)
        if mode in ("substring", "mixed"):
            sub = await _substring_collect(store, q, fields, window_uid, limit_per_field=limit * 4)
            _merge_per_uid(per_uid, sub)
    else:
        per_uid = await _scope_collect(store, fields, window_uid, per_field=10)

    if not per_uid:
        return []

    # join window metadata for the matched uids, then filter/sort/paginate.
    uids = list(per_uid)
    placeholders = ",".join("?" * len(uids))
    where = [f"w.window_uid IN ({placeholders})"]
    params: list = list(uids)
    a = _alive_sql(alive)
    if a:
        where.append(a)
    b = _boot_filter_sql(current_boot_id)
    if b:
        where.append(b)
        params.append(current_boot_id)
    sql = f"SELECT {_WINDOW_COLS} FROM window w WHERE " + " AND ".join(where)
    rows = await store.fetchall(sql, tuple(params))

    results = []
    for r in rows:
        agg = per_uid[r["window_uid"]]
        # time-range filter on the matched rows' timestamps (06 §5 step 5)
        if t_from is not None and agg["max_ts"] is not None and agg["max_ts"] < t_from:
            continue
        if t_to is not None and agg["min_ts"] is not None and agg["min_ts"] > t_to:
            continue
        obj = assemble_window(r, current_session_key)
        obj["hits"] = agg["hits"]
        obj["_rank"] = agg["best_rank"]
        obj["_recency"] = agg["max_ts"]
        results.append(obj)

    now = time.time()
    await _enrich_usage(store, results, now=now)
    await _enrich_focus_score(store, results, now=now)
    await _enrich_jump_counts(store, results, now=now)

    reverse = order != "asc"
    if sort == "relevance":
        results.sort(key=lambda o: (o["_rank"] if o["_rank"] is not None else 1e9))
    elif sort == "recency":
        results.sort(key=lambda o: (o["_recency"] or 0), reverse=reverse)
    elif sort == "title":
        results.sort(key=lambda o: (o["current_title"] or "").lower(), reverse=reverse)
    elif sort == "window_id":
        results.sort(key=lambda o: o["window_uid"], reverse=reverse)
    elif sort == "focus_score":
        results.sort(key=lambda o: (o.get("focus_score") or 0, o["window_uid"]), reverse=reverse)
    elif sort.startswith("usage_"):
        results.sort(key=lambda o: (o.get(sort) or 0, o["window_uid"]), reverse=reverse)
    else:  # last_access (default)
        results.sort(key=lambda o: (o["last_access"] or 0, o["window_uid"]),
                     reverse=reverse)

    page = results[offset:offset + limit]
    for o in page:
        o.pop("_rank", None)
        o.pop("_recency", None)
        if hits == "all":
            o["hit_fields"] = sorted({h["field"] for h in o["hits"]})
    if title_denylist:
        denied = set(title_denylist)
        page = [o for o in page if o.get("current_title") not in denied]
    page = _filter_self(page, self_xid)
    return page


def _safe_fts_query(q: str) -> str:
    """Quote every token so FTS5 treats it as a phrase (safe for IPs, punctuation).

    This avoids FTS5 syntax errors on dots/slashes and still does substring-style
    matching because the FTS tables use the trigram tokenizer.
    """
    if not q:
        return q
    tokens = q.strip().split()
    cleaned = [t.replace('"', '') for t in tokens if t.replace('"', '')]
    if not cleaned:
        return q
    return " AND ".join(f'"{t}"' for t in cleaned)


def _mark_substring(text: str, q: str, *, radius: int = 24, max_total: int | None = None) -> str:
    """Build a snippet with the first case-insensitive match wrapped in <mark>.

    ``radius`` is the desired context on each side.  When ``max_total`` is set,
    the radius is reduced so the final snippet (including markup) does not
    exceed that length.
    """
    if not text or not q:
        return text
    lo = text.lower()
    qlo = q.lower()
    idx = lo.find(qlo)
    if idx < 0:
        return text
    if max_total is not None:
        # markup adds <mark>...</mark> (13 chars); reserve a char for each ellipsis.
        max_radius = max(0, (max_total - len(qlo) - 13 - 2) // 2)
        radius = min(radius, max_radius)
    start = max(0, idx - radius)
    end = min(len(text), idx + len(qlo) + radius)
    snippet = text[start:end]
    rel = idx - start
    marked = (
        snippet[:rel] + "<mark>" + snippet[rel:rel + len(qlo)] + "</mark>" + snippet[rel + len(qlo):]
    )
    if start > 0:
        marked = "…" + marked
    if end < len(text):
        marked = marked + "…"
    return marked


async def _substring_token_hits(store, token: str, fields, window_uid, *, limit_per_field) -> dict:
    """Substring hits for a single token across all requested fields."""
    per_uid: dict[int, dict] = {}
    for field in fields:
        _fts, content, ts_col = FIELDS[field]
        text_col = "title" if field == "title" else ("app_name" if field == "app_name" else "text")
        sql = (f"SELECT window_uid AS uid, {text_col} AS excerpt, {ts_col} AS ts "
               f"FROM {content} WHERE INSTR(LOWER({text_col}), LOWER(?)) > 0 ")
        params: list = [token]
        if window_uid is not None:
            sql += "AND window_uid = ? "
            params.append(window_uid)
        sql += f"ORDER BY {ts_col} DESC LIMIT ?"
        params.append(limit_per_field)
        rows = await store.fetchall(sql, tuple(params))
        for r in rows:
            uid = r["uid"]
            if uid is None:
                continue
            excerpt = _mark_substring(r["excerpt"] or "", token)
            _merge_hit(per_uid, uid, field, excerpt, r["ts"], 1000.0)
    return per_uid


async def _substring_collect(store, q, fields, window_uid, *, limit_per_field) -> dict:
    """Whole-text substring fallback using INSTR(LOWER(...), LOWER(?)).

    Multi-word queries are split on whitespace.  A window matches if either:
      * the full query string appears as a substring in any field, OR
      * every individual token appears somewhere across the requested fields
        (the tokens do not need to be in the same field or row).
    """
    q = q.strip()
    if not q:
        return {}
    tokens = q.split()
    if len(tokens) <= 1:
        return await _substring_token_hits(store, q, fields, window_uid,
                                           limit_per_field=limit_per_field)

    # Intersection: each token must be present in at least one field for the window.
    token_results = [
        await _substring_token_hits(store, t, fields, window_uid,
                                    limit_per_field=limit_per_field)
        for t in tokens
    ]
    common_uids = set(token_results[0])
    for res in token_results[1:]:
        common_uids &= set(res)

    combined: dict[int, dict] = {}
    for uid in common_uids:
        for res in token_results:
            agg = res.get(uid)
            if agg is not None:
                _merge_per_uid(combined, {uid: agg})

    # Union with the exact full-phrase matches.
    full_phrase = await _substring_token_hits(store, q, fields, window_uid,
                                              limit_per_field=limit_per_field)
    _merge_per_uid(combined, full_phrase)
    return combined


async def _fts_collect(store, q, fields, window_uid, *, limit_per_field) -> dict:
    """Run each field's FTS5 MATCH; group rowid→window_uid → {hits,rank,ts}."""
    per_uid: dict[int, dict] = {}
    safe_q = _safe_fts_query(q)
    for field in fields:
        fts, content, ts_col = FIELDS[field]
        snippet = _SNIPPET.format(fts=fts)
        sql = (f"SELECT c.window_uid AS uid, {snippet} AS excerpt, "
               f"c.{ts_col} AS ts, {fts}.rank AS rank "
               f"FROM {fts} JOIN {content} c ON c.id = {fts}.rowid "
               f"WHERE {fts} MATCH ? ")
        params: list = [safe_q]
        if window_uid is not None:
            sql += "AND c.window_uid = ? "
            params.append(window_uid)
        sql += f"ORDER BY {fts}.rank LIMIT ?"
        params.append(limit_per_field)
        try:
            rows = await store.fetchall(sql, tuple(params))
        except Exception:
            continue   # malformed MATCH → skip field
        for r in rows:
            uid = r["uid"]
            if uid is None:
                continue
            _merge_hit(per_uid, uid, field, r["excerpt"], r["ts"], r["rank"])
    return per_uid


def _merge_per_uid(target: dict, source: dict) -> None:
    """Merge two {uid: {hits, best_rank, min_ts, max_ts}} maps."""
    for uid, agg in source.items():
        existing = target.get(uid)
        if existing is None:
            target[uid] = {
                "hits": list(agg["hits"]),
                "best_rank": agg["best_rank"],
                "min_ts": agg["min_ts"],
                "max_ts": agg["max_ts"],
            }
            continue
        existing["hits"].extend(agg["hits"])
        if agg["best_rank"] is not None and (existing["best_rank"] is None
                                              or agg["best_rank"] < existing["best_rank"]):
            existing["best_rank"] = agg["best_rank"]
        if agg["min_ts"] is not None:
            existing["min_ts"] = min(existing["min_ts"], agg["min_ts"]) if existing["min_ts"] is not None else agg["min_ts"]
        if agg["max_ts"] is not None:
            existing["max_ts"] = max(existing["max_ts"], agg["max_ts"]) if existing["max_ts"] is not None else agg["max_ts"]


async def _scope_collect(store, fields, window_uid, *, per_field) -> dict:
    """No-q scope mode: pull the most recent rows per field for one window (07 §4a)."""
    if window_uid is None:
        return {}
    per_uid: dict[int, dict] = {}
    for field in fields:
        _fts, content, ts_col = FIELDS[field]
        text_col = "title" if field == "title" else ("app_name" if field == "app_name" else "text")
        sql = (f"SELECT {text_col} AS excerpt, {ts_col} AS ts FROM {content} "
               f"WHERE window_uid = ? AND {text_col} IS NOT NULL "
               f"ORDER BY {ts_col} DESC LIMIT ?")
        rows = await store.fetchall(sql, (window_uid, per_field))
        for r in rows:
            _merge_hit(per_uid, window_uid, field, r["excerpt"], r["ts"], None)
    return per_uid


def _merge_hit(per_uid, uid, field, excerpt, ts, rank) -> None:
    agg = per_uid.get(uid)
    if agg is None:
        agg = {"hits": [], "best_rank": None, "min_ts": None, "max_ts": None}
        per_uid[uid] = agg
    agg["hits"].append({"field": field, "excerpt": excerpt})
    if rank is not None and (agg["best_rank"] is None or rank < agg["best_rank"]):
        agg["best_rank"] = rank
    if ts is not None:
        agg["min_ts"] = ts if agg["min_ts"] is None else min(agg["min_ts"], ts)
        agg["max_ts"] = ts if agg["max_ts"] is None else max(agg["max_ts"], ts)


# ───────────────────────── GET /windows/{uid} detail ─────────────────────────
async def window_detail(store, uid: int, current_session_key=None,
                        recent=20) -> dict | None:
    row = await store.fetchone(f"SELECT {_WINDOW_COLS} FROM window w WHERE w.window_uid = ?",
                               (uid,))
    if row is None:
        return None
    obj = assemble_window(row, current_session_key)
    await _enrich_usage(store, [obj])
    await _enrich_focus_score(store, [obj])
    titles = await store.fetchall(
        "SELECT title, changed_at FROM title_history WHERE window_uid = ? "
        "ORDER BY changed_at DESC, id DESC LIMIT ?", (uid, recent))
    obj["title_history"] = [{"title": t["title"], "changed_at": t["changed_at"]} for t in titles]
    obj["events"] = await _recent_events(store, uid, recent)
    return obj


async def _recent_events(store, uid, recent) -> list[dict]:
    out: list[dict] = []
    clip = await store.fetchall(
        "SELECT kind, text, n_chars, created_at FROM clipboard_event WHERE window_uid=? "
        "ORDER BY created_at DESC LIMIT ?", (uid, recent))
    out += [{"type": "clipboard", "kind": r["kind"], "text": r["text"],
             "ts": r["created_at"]} for r in clip]
    sel = await store.fetchall(
        "SELECT text, created_at FROM selection_event WHERE window_uid=? "
        "ORDER BY created_at DESC LIMIT ?", (uid, recent))
    out += [{"type": "selection", "text": r["text"], "ts": r["created_at"]} for r in sel]
    kbd = await store.fetchall(
        "SELECT text, started_at FROM kbd_segment WHERE window_uid=? "
        "ORDER BY started_at DESC LIMIT ?", (uid, recent))
    out += [{"type": "keyboard", "text": r["text"], "ts": r["started_at"]} for r in kbd]
    out.sort(key=lambda e: e["ts"] or 0, reverse=True)
    return out[:recent]


# ───────────────────────── GET /timeline ─────────────────────────
async def timeline(store, *, t_from=None, t_to=None, window_uid=None, sort="last_access",
                   order="desc", current_session_key=None,
                   title_denylist=None, self_xid=None,
                   current_boot_id=None) -> list[dict]:
    """Windows active in [from,to] with continuous focus spans + instantaneous events.

    Focus spans are derived from the global sequence of focus events: a span ends
    when the next window gains focus.  The last span ends at the query's upper
    bound (or now).  Title, clipboard, selection and keyboard events are attached
    to each lane for hover detail.
    """
    # Fetch focus events globally from the lower bound onward so we can derive
    # true continuous durations (only one window is focused at a time).
    f_where, f_params = [], []
    if t_from is not None:
        f_where.append("focused_at >= ?")
        f_params.append(t_from)
    if current_boot_id:
        f_where.append(
            "window_uid IN (SELECT window_uid FROM window "
            "WHERE last_daemon_run_id IN (SELECT id FROM daemon_run WHERE machine_boot_id = ?))"
        )
        f_params.append(current_boot_id)
    f_clause = ("WHERE " + " AND ".join(f_where)) if f_where else ""
    focus_rows = await store.fetchall(
        f"SELECT window_uid AS uid, focused_at AS ts, "
        f"vdesktop_index AS vidx, vdesktop_name AS vname "
        f"FROM focus_event {f_clause} ORDER BY focused_at ASC", tuple(f_params))

    # Fetch lock/unlock boundaries in the same range; they end focus spans.
    l_where, l_params = [], []
    if t_from is not None:
        l_where.append("changed_at >= ?")
        l_params.append(t_from)
    if t_to is not None:
        l_where.append("changed_at <= ?")
        l_params.append(t_to)
    l_clause = ("WHERE " + " AND ".join(l_where)) if l_where else ""
    lock_rows = await store.fetchall(
        f"SELECT locked, changed_at AS ts FROM screen_lock_event "
        f"{l_clause} ORDER BY changed_at ASC", tuple(l_params))

    end_default = t_to if t_to is not None else time.time()
    lanes: dict[int, dict] = {}

    # Merge focus and lock rows by timestamp.  A lock row acts as a boundary:
    # the currently focused span ends at the lock time, and no window is focused
    # during the locked period.
    merged = []
    for r in focus_rows:
        merged.append(("focus", r["ts"], r))
    for r in lock_rows:
        merged.append(("lock", r["ts"], r))
    merged.sort(key=lambda x: x[1])

    for i, (kind, ts, data) in enumerate(merged):
        if kind != "focus":
            continue
        r = data
        uid = r["uid"]
        start = r["ts"]
        end = merged[i + 1][1] if i + 1 < len(merged) else end_default
        if t_from is not None:
            start = max(start, t_from)
        if t_to is not None:
            end = min(end, t_to)
        if end <= start:
            continue
        lane = lanes.get(uid)
        if lane is None:
            lane = {"window_uid": uid, "focus_spans": [], "titles": [], "events": []}
            lanes[uid] = lane
        lane["focus_spans"].append({
            "focused_at": r["ts"],
            "ended_at": end,
            "vdesktop_index": r["vidx"],
            "vdesktop_name": r["vname"],
        })

    if window_uid is not None:
        lanes = {uid: lane for uid, lane in lanes.items() if uid == window_uid}

    if not lanes:
        return []

    # title history within range, attached to each lane
    for uid, lane in lanes.items():
        tw, tp = ["window_uid = ?"], [uid]
        if t_from is not None:
            tw.append("changed_at >= ?"); tp.append(t_from)
        if t_to is not None:
            tw.append("changed_at <= ?"); tp.append(t_to)
        titles = await store.fetchall(
            "SELECT title, changed_at FROM title_history WHERE " + " AND ".join(tw)
            + " ORDER BY changed_at ASC", tuple(tp))
        lane["titles"] = [{"title": t["title"], "changed_at": t["changed_at"]} for t in titles]
        meta = await store.fetchone(
            f"SELECT {_WINDOW_COLS} FROM window w WHERE w.window_uid = ?", (uid,))
        if meta is not None:
            m = assemble_window(meta, current_session_key)
            lane["x_window_id"] = m["x_window_id"]
            lane["wm_class"] = m["wm_class"]
            lane["app_name"] = m["app_name"]
            lane["current_title"] = m["current_title"]
            lane["vdesktop"] = m["vdesktop"]
            lane["alive"] = m["alive"]
            lane["jumpable"] = m["jumpable"]
            lane["last_access"] = m["last_access"]
            lane["created_since"] = m["first_seen"]
            lane["dead_at"] = m["closed_at"]

    await _enrich_timeline_events(store, lanes, t_from, t_to)

    out = list(lanes.values())
    reverse = order != "asc"
    if sort == "title":
        out.sort(key=lambda l: (l.get("current_title") or "").lower(), reverse=reverse)
    elif sort == "window_id":
        out.sort(key=lambda l: l["window_uid"], reverse=reverse)
    elif sort == "focus_score":
        # focus_score needs usage; compute below and re-sort.
        pass
    elif sort.startswith("usage_"):
        pass
    else:  # last_access: latest focus span per lane, ties by uid for stability
        def _lane_last_access(lane):
            spans = lane.get("focus_spans") or []
            ts = max((s.get("ended_at") or s.get("focused_at") or 0 for s in spans), default=0)
            return ts, lane["window_uid"]
        out.sort(key=_lane_last_access, reverse=reverse)
    if title_denylist:
        denied = set(title_denylist)
        out = [l for l in out if l.get("current_title") not in denied]
    out = _filter_self(out, self_xid)
    now = time.time()
    await _enrich_usage(store, out, key="window_uid", now=now)
    await _enrich_focus_score(store, out, now=now)
    if sort == "focus_score":
        out.sort(key=lambda l: (l.get("focus_score") or 0, l["window_uid"]), reverse=reverse)
    elif sort.startswith("usage_"):
        out.sort(key=lambda l: (l.get(sort) or 0, l["window_uid"]), reverse=reverse)
    return out


async def _enrich_timeline_events(store, lanes: dict[int, dict], t_from, t_to) -> None:
    """Attach instantaneous events (title, clipboard, selection, keyboard) to lanes."""
    uids = list(lanes)
    placeholders = ",".join("?" * len(uids))
    sources = [
        ("title_history", "changed_at", "title", "title", None),
        ("clipboard_event", "created_at", "clipboard", "text", "kind"),
        ("selection_event", "created_at", "selection", "text", None),
        ("kbd_segment", "started_at", "keyboard", "text", None),
    ]
    for table, ts_col, typ, text_col, kind_col in sources:
        where = [f"window_uid IN ({placeholders})"]
        params: list = list(uids)
        if t_from is not None:
            where.append(f"{ts_col} >= ?")
            params.append(t_from)
        if t_to is not None:
            where.append(f"{ts_col} <= ?")
            params.append(t_to)
        cols = f"window_uid AS uid, {text_col} AS text, {ts_col} AS ts"
        if kind_col:
            cols += f", {kind_col} AS kind"
        sql = f"SELECT {cols} FROM {table} WHERE " + " AND ".join(where) + f" ORDER BY {ts_col} DESC"
        rows = await store.fetchall(sql, tuple(params))
        for r in rows:
            event = {"type": typ, "ts": r["ts"], "text": r["text"]}
            if kind_col:
                event["kind"] = r["kind"]
            lanes[r["uid"]]["events"].append(event)
    for lane in lanes.values():
        lane["events"].sort(key=lambda e: e["ts"], reverse=True)
        lane["events"] = lane["events"][:50]
