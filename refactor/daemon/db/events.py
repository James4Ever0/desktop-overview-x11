"""daemon/db/events.py — global event stream for the Events tab (plan 13).

Provides paginated, time-ordered event browsing and hybrid search across all
indexed event sources: title/app-name history, clipboard, selection, keyboard
segments, read events, focus events and screen-lock events.
"""
from __future__ import annotations

from .search import _mark_substring, _WINDOW_COLS, assemble_window


ALL_EVENT_TYPES = ("title", "app_name", "clipboard", "selection", "keyboard", "read", "focus", "lock", "jump")

# (type, table, ts_col, text_col, kind_col, fts_table)
_TEXT_SOURCES = (
    ("title",     "title_history",    "changed_at", "title",     None,       "fts_title"),
    ("app_name",  "app_name_history", "changed_at", "app_name",  None,       "fts_appname"),
    ("clipboard", "clipboard_event",  "created_at", "text",      "kind",     "fts_clip"),
    ("selection", "selection_event",  "created_at", "text",      None,       "fts_sel"),
    ("keyboard",  "kbd_segment",      "started_at", "text",      None,       "fts_kbd"),
    ("read",      "read_event",       "created_at", "text",      None,       None),
    ("jump",      "jump_event",       "ts",         "title",     None,       None),
)

_NON_TEXT_SOURCES = (
    ("focus", "focus_event", "focused_at", "window_uid"),
    ("lock",  "screen_lock_event", "changed_at", None),
)

_SNIPPET = "snippet({fts}, 0, '<mark>', '</mark>', '…', 180)"


def _query_tokens(q: str) -> list[str]:
    """Lower-case whitespace-separated tokens."""
    return [t for t in q.lower().split() if t.strip()]


def _match_intervals(text: str, tokens: list[str]) -> list[tuple[int, int]]:
    """Return sorted, non-overlapping intervals of token matches in text."""
    if not text or not tokens:
        return []
    lo = text.lower()
    intervals: list[tuple[int, int]] = []
    for tok in tokens:
        start = 0
        while True:
            idx = lo.find(tok, start)
            if idx < 0:
                break
            intervals.append((idx, idx + len(tok)))
            start = idx + len(tok)
    if not intervals:
        return []
    intervals.sort()
    merged = []
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return merged


def _mark_region(text: str, intervals: list[tuple[int, int]],
                 region_start: int, region_end: int) -> str:
    """Wrap every interval inside [region_start, region_end) with <mark>."""
    parts = []
    pos = region_start
    for s, e in intervals:
        s = max(s, region_start)
        e = min(e, region_end)
        if s < pos:
            s = pos
        parts.append(text[pos:s])
        parts.append("<mark>")
        parts.append(text[s:e])
        parts.append("</mark>")
        pos = e
    parts.append(text[pos:region_end])
    marked = "".join(parts)
    if region_start > 0:
        marked = "…" + marked
    if region_end < len(text):
        marked = marked + "…"
    return marked


def _highlight_hits(text: str, q: str, *, radius: int = 100,
                    max_total: int = 200, max_excerpts: int = 3) -> tuple[list[str], int]:
    """Return (up to max_excerpts marked snippets, total hit count)."""
    tokens = _query_tokens(q)
    if not text or not tokens:
        return [], 0
    intervals = _match_intervals(text, tokens)
    if not intervals:
        return [], 0
    total = len(intervals)
    excerpts = []
    last_end = -1
    for s, e in intervals:
        # Avoid overlapping excerpts.
        if s < last_end:
            continue
        # Per-snippet context, respecting the overall max_total cap.
        max_radius = max(0, (max_total - (e - s) - 13 - 2) // 2)
        eff = min(radius, max_radius)
        rs = max(0, s - eff)
        re = min(len(text), e + eff)
        # Recompute intervals that fall inside this region after clipping.
        inside = [(a, b) for a, b in intervals if a >= rs and b <= re]
        excerpts.append(_mark_region(text, inside, rs, re))
        last_end = re
        if len(excerpts) >= max_excerpts:
            break
    return excerpts, total


def _types_set(types) -> set[str]:
    if not types:
        return set(ALL_EVENT_TYPES)
    wanted = set(types)
    return {t for t in ALL_EVENT_TYPES if t in wanted}


def _time_where(ts_col: str, t_from, t_to) -> tuple[str, list]:
    clauses = []
    params = []
    if t_from is not None:
        clauses.append(f"{ts_col} >= ?")
        params.append(t_from)
    if t_to is not None:
        clauses.append(f"{ts_col} <= ?")
        params.append(t_to)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _source_select(typ: str, table: str, ts_col: str, text_col: str | None,
                   kind_col: str | None, uid_col: str | None = "window_uid") -> str:
    if typ == "jump":
        kind_sel = "CASE WHEN t.success THEN 'success' ELSE 'failure' END AS kind"
    elif kind_col:
        kind_sel = f"{kind_col} AS kind"
    else:
        kind_sel = "NULL AS kind"
    if typ == "focus":
        text_sel = "'(focus)' AS text"
    elif typ == "lock":
        text_sel = "CASE WHEN locked THEN 'locked' ELSE 'unlocked' END AS text"
    elif text_col:
        text_sel = f"{text_col} AS text"
    else:
        text_sel = "NULL AS text"
    uid_sel = f"t.{uid_col} AS window_uid" if uid_col else "NULL AS window_uid"
    return (
        f"SELECT '{typ}' AS type, t.id, {uid_sel}, {kind_sel}, "
        f"t.{ts_col} AS ts, {text_sel} FROM {table} t"
    )


async def _enrich_events(store, rows: list[dict], current_session_key: str | None) -> list[dict]:
    """Add window metadata (app name, title, vdesktop, alive) to event rows."""
    uids = list({r["window_uid"] for r in rows if r.get("window_uid")})
    if not uids:
        return rows
    placeholders = ",".join("?" * len(uids))
    win_rows = await store.fetchall(
        f"SELECT {_WINDOW_COLS} FROM window w WHERE w.window_uid IN ({placeholders})",
        tuple(uids))
    meta = {}
    for row in win_rows:
        w = assemble_window(row, current_session_key)
        meta[w["window_uid"]] = w
    for r in rows:
        w = meta.get(r.get("window_uid"))
        if w is not None:
            r["wm_class"] = w.get("wm_class")
            r["app_name"] = w.get("app_name")
            r["current_title"] = w.get("current_title")
            r["vdesktop"] = w.get("vdesktop")
            r["alive"] = w.get("alive")
    return rows


async def _latest_events(store, types: set[str], t_from, t_to, sort: str, limit: int, offset: int):
    """Return the latest events across all requested sources, plus total count."""
    parts = []
    params = []
    order = "DESC" if sort == "ts_desc" else "ASC"

    for typ, table, ts_col, text_col, kind_col, _fts in _TEXT_SOURCES:
        if typ not in types:
            continue
        where, wp = _time_where(ts_col, t_from, t_to)
        parts.append(_source_select(typ, table, ts_col, text_col, kind_col) + f" WHERE {where}")
        params.extend(wp)

    for typ, table, ts_col, uid_col in _NON_TEXT_SOURCES:
        if typ not in types:
            continue
        where, wp = _time_where(ts_col, t_from, t_to)
        parts.append(_source_select(typ, table, ts_col, None, None, uid_col) + f" WHERE {where}")
        params.extend(wp)

    if not parts:
        return [], 0

    union = " UNION ALL ".join(parts)
    count_sql = f"SELECT COUNT(*) FROM ({union})"
    total_row = await store.fetchone(count_sql, tuple(params))
    total = total_row[0] if total_row else 0

    sql = f"{union} ORDER BY ts {order} LIMIT ? OFFSET ?"
    qp = list(params) + [limit, offset]
    rows = await store.fetchall(sql, tuple(qp))
    return [dict(r) for r in rows], total


async def _search_events(store, q: str, types: set[str], t_from, t_to, sort: str, limit: int, offset: int):
    """Hybrid FTS + substring search across text event sources."""
    q_lower = q.lower().strip()
    parts = []
    params = []

    for typ, table, ts_col, text_col, kind_col, fts in _TEXT_SOURCES:
        if typ not in types:
            continue
        where, wp = _time_where(f"t.{ts_col}", t_from, t_to)
        kind_sel = f"t.{kind_col} AS kind" if kind_col else "NULL AS kind"

        if fts is not None:
            # FTS branch: use FTS to find matching rows, then highlight all hits ourselves.
            fts_sql = (
                f"SELECT '{typ}' AS type, t.id, t.window_uid, {kind_sel}, "
                f"t.{ts_col} AS ts, t.{text_col} AS text, NULL AS excerpt, rank "
                f"FROM {fts} "
                f"JOIN {table} t ON t.id = {fts}.rowid "
                f"WHERE {fts} MATCH ? AND {where}"
            )
            try:
                rows = await store.fetchall(fts_sql, (q,) + tuple(wp))
                for r in rows:
                    parts.append(dict(r))
                continue
            except Exception:  # noqa: BLE001
                # Malformed MATCH (e.g. bare quotes); fall through to substring.
                pass

        # Substring branch.
        sub_sql = (
            f"SELECT '{typ}' AS type, t.id, t.window_uid, {kind_sel}, "
            f"t.{ts_col} AS ts, t.{text_col} AS text, NULL AS excerpt, 1000.0 AS rank "
            f"FROM {table} t "
            f"WHERE LOWER(t.{text_col}) LIKE '%' || ? || '%' AND {where}"
        )
        sub_rows = await store.fetchall(sub_sql, (q_lower,) + tuple(wp))
        for r in sub_rows:
            parts.append(dict(r))

    if not parts:
        return [], 0

    # De-duplicate: an event can appear from both FTS and substring.
    seen = set()
    deduped = []
    for d in parts:
        key = (d.get("type"), d.get("id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)

    # Highlight all hits per event and attach hit metadata.
    for d in deduped:
        excerpts, count = _highlight_hits(d.get("text") or "", q_lower,
                                          radius=100, max_total=200, max_excerpts=3)
        d["hit_excerpts"] = excerpts
        d["hit_count"] = count
        if not d.get("excerpt") and excerpts:
            d["excerpt"] = excerpts[0]

    # Sort.
    if sort == "ts_asc":
        deduped.sort(key=lambda r: (r.get("ts") or 0, r.get("type"), r.get("id")))
    elif sort == "rank":
        deduped.sort(key=lambda r: (r.get("rank") or 1e9, -(r.get("ts") or 0)))
    else:  # ts_desc
        deduped.sort(key=lambda r: (r.get("ts") or 0, r.get("type"), r.get("id")), reverse=True)

    total = len(deduped)
    page = deduped[offset:offset + limit]
    return page, total


async def search_events(
    store,
    *,
    q: str | None = None,
    types=None,
    t_from=None,
    t_to=None,
    sort: str = "ts_desc",
    limit: int = 100,
    offset: int = 0,
    current_session_key: str | None = None,
):
    """Public entry point for the Events tab.

    Returns (events, total) where events are plain dicts ready to be turned into
    ``GlobalEvent`` response models.
    """
    wanted = _types_set(types)
    if q:
        rows, total = await _search_events(store, q, wanted, t_from, t_to, sort, limit, offset)
    else:
        rows, total = await _latest_events(store, wanted, t_from, t_to, sort, limit, offset)

    rows = await _enrich_events(store, rows, current_session_key)
    return rows, total
