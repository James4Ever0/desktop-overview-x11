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

FIELDS = {
    # field name : (fts table, content table, ts column on the content table)
    "title":     ("fts_title", "title_history",   "changed_at"),
    "clipboard": ("fts_clip",  "clipboard_event", "created_at"),
    "selection": ("fts_sel",   "selection_event", "created_at"),
    "keyboard":  ("fts_kbd",   "kbd_segment",      "started_at"),
}
ALL_FIELDS = tuple(FIELDS)

# Assembled window object: current title, current vdesktop, liveness, last access,
# latest-window_capture presence.  Correlated subqueries keep it to one statement so
# the frontend never joins (07 §3).
_WINDOW_COLS = """
  w.window_uid       AS window_uid,
  w.x_window_id      AS x_window_id,
  w.wm_class         AS wm_class,
  w.alive            AS alive,
  w.session_key      AS session_key,
  w.vdesktop_index   AS vdesktop_index,
  w.vdesktop_name    AS vdesktop_name,
  (SELECT COALESCE(MAX(fe.focused_at), 0) FROM focus_event fe
     WHERE fe.window_uid = w.window_uid)                              AS last_access,
  (SELECT th.title FROM title_history th WHERE th.window_uid = w.window_uid
     ORDER BY th.changed_at DESC, th.id DESC LIMIT 1)            AS current_title,
  (SELECT tl.rel_path FROM window_capture_latest tl WHERE tl.window_uid = w.window_uid) AS window_capture_rel,
  (SELECT CAST(tl.captured_at AS INTEGER) FROM window_capture_latest tl WHERE tl.window_uid = w.window_uid) AS window_capture_ts
"""

_SNIPPET = "snippet({fts}, 0, '<mark>', '</mark>', '…', 12)"


def _alive_sql(alive: str, col: str = "w.alive") -> str:
    """alive filter → SQL fragment (without WHERE/AND)."""
    if alive == "only":
        return f"{col} = 1"
    if alive == "dead":
        return f"{col} = 0"
    return ""   # both


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
        "current_title": row["current_title"],
        "vdesktop": ({"index": vidx, "name": row["vdesktop_name"]}
                     if vidx is not None or row["vdesktop_name"] is not None else None),
        "alive": alive,
        "jumpable": alive and same_session,
        "last_access": row["last_access"],
        "window_capture_url": (f"/windows/{uid}/window_capture/latest" if row["window_capture_rel"] else None),
        "window_capture_ts": row["window_capture_ts"],
        "hits": [],
    }


# ───────────────────────── GET /windows (no FTS) ─────────────────────────
def _window_sort_col(sort: str) -> str:
    """Map public sort key to a SQL column/expression on the window table."""
    if sort == "title":
        return "LOWER(current_title)"
    if sort == "window_id":
        return "w.window_uid"
    return "last_access"  # last_access default, derived from focus_event.focused_at


async def list_windows(store, *, alive="both", sort="last_access", order="desc",
                       limit=200, offset=0, current_session_key=None,
                       title_denylist=None) -> list[dict]:
    where = _alive_sql(alive)
    sort_col = _window_sort_col(sort)
    direction = "ASC" if order == "asc" else "DESC"
    # secondary key keeps ordering stable when primary key ties
    secondary = "w.window_uid ASC"
    sql = (f"SELECT {_WINDOW_COLS} FROM window w "
           + (f"WHERE {where} " if where else "")
           + f"ORDER BY {sort_col} {direction}, {secondary} LIMIT ? OFFSET ?")
    rows = await store.fetchall(sql, (limit, offset))
    results = [assemble_window(r, current_session_key) for r in rows]
    if title_denylist:
        denied = set(title_denylist)
        results = [r for r in results if r.get("current_title") not in denied]
    return results


# ───────────────────────── GET /search (FTS) ─────────────────────────
async def search(store, *, q=None, window_uid=None, fields=None, alive="both",
                 t_from=None, t_to=None, sort="last_access", order="desc", hits="hit_only",
                 limit=100, offset=0, current_session_key=None,
                 title_denylist=None) -> list[dict]:
    """The core multi-field search (07 §4).

    ``q`` empty + ``window_uid`` set → scope-only "show this window's fields"
    (07 §4a), no text match.  ``q`` present → per-field FTS5 MATCH, optionally
    constrained to ``window_uid`` (search-within-window).
    """
    fields = [f for f in (fields or ALL_FIELDS) if f in FIELDS]
    if q:
        per_uid = await _fts_collect(store, q, fields, window_uid, limit_per_field=limit * 4)
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

    reverse = order != "asc"
    if sort == "relevance":
        results.sort(key=lambda o: (o["_rank"] if o["_rank"] is not None else 1e9))
    elif sort == "recency":
        results.sort(key=lambda o: (o["_recency"] or 0), reverse=reverse)
    elif sort == "title":
        results.sort(key=lambda o: (o["current_title"] or "").lower(), reverse=reverse)
    elif sort == "window_id":
        results.sort(key=lambda o: o["window_uid"], reverse=reverse)
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
    return page


async def _fts_collect(store, q, fields, window_uid, *, limit_per_field) -> dict:
    """Run each field's FTS5 MATCH; group rowid→window_uid → {hits,rank,ts}."""
    per_uid: dict[int, dict] = {}
    for field in fields:
        fts, content, ts_col = FIELDS[field]
        snippet = _SNIPPET.format(fts=fts)
        sql = (f"SELECT c.window_uid AS uid, {snippet} AS excerpt, "
               f"c.{ts_col} AS ts, {fts}.rank AS rank "
               f"FROM {fts} JOIN {content} c ON c.id = {fts}.rowid "
               f"WHERE {fts} MATCH ? ")
        params: list = [q]
        if window_uid is not None:
            sql += "AND c.window_uid = ? "
            params.append(window_uid)
        sql += f"ORDER BY {fts}.rank LIMIT ?"
        params.append(limit_per_field)
        try:
            rows = await store.fetchall(sql, tuple(params))
        except Exception:
            continue   # malformed MATCH (user typed FTS operators) → skip field
        for r in rows:
            uid = r["uid"]
            if uid is None:
                continue
            _merge_hit(per_uid, uid, field, r["excerpt"], r["ts"], r["rank"])
    return per_uid


async def _scope_collect(store, fields, window_uid, *, per_field) -> dict:
    """No-q scope mode: pull the most recent rows per field for one window (07 §4a)."""
    if window_uid is None:
        return {}
    per_uid: dict[int, dict] = {}
    for field in fields:
        _fts, content, ts_col = FIELDS[field]
        text_col = "title" if field == "title" else "text"
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
                   title_denylist=None) -> list[dict]:
    """Windows active in [from,to] with their focus spans + title history (07 §3, 08 §3)."""
    where, params = [], []
    if window_uid is not None:
        where.append("fe.window_uid = ?")
        params.append(window_uid)
    if t_from is not None:
        where.append("fe.focused_at >= ?")
        params.append(t_from)
    if t_to is not None:
        where.append("fe.focused_at <= ?")
        params.append(t_to)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    focus = await store.fetchall(
        f"SELECT fe.window_uid AS uid, fe.focused_at AS ts, "
        f"fe.vdesktop_index AS vidx, fe.vdesktop_name AS vname "
        f"FROM focus_event fe {clause} ORDER BY fe.focused_at ASC", tuple(params))

    lanes: dict[int, dict] = {}
    for r in focus:
        uid = r["uid"]
        if uid is None:
            continue
        lane = lanes.get(uid)
        if lane is None:
            lane = {"window_uid": uid, "focus_spans": [], "titles": []}
            lanes[uid] = lane
        lane["focus_spans"].append({"focused_at": r["ts"], "vdesktop_index": r["vidx"],
                                    "vdesktop_name": r["vname"]})

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
            lane["current_title"] = m["current_title"]
            lane["alive"] = m["alive"]
            lane["jumpable"] = m["jumpable"]

    out = list(lanes.values())
    reverse = order != "asc"
    if sort == "title":
        out.sort(key=lambda l: (l.get("current_title") or "").lower(), reverse=reverse)
    elif sort == "window_id":
        out.sort(key=lambda l: l["window_uid"], reverse=reverse)
    else:  # last_access: latest focus span per lane, ties by uid for stability
        def _lane_last_access(lane):
            spans = lane.get("focus_spans") or []
            ts = max((s.get("focused_at") or 0 for s in spans), default=0)
            return ts, lane["window_uid"]
        out.sort(key=_lane_last_access, reverse=reverse)
    if title_denylist:
        denied = set(title_denylist)
        out = [l for l in out if l.get("current_title") not in denied]
    return out
