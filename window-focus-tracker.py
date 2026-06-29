#!/usr/bin/env python3
"""
Focus Tracker - X11 window usage monitor with screen lock detection.
Timestamps are stored as float (Unix epoch, UTC) and displayed in CST.
"""

import argparse
import sqlite3
import subprocess
import time
import sys
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict

# Use zoneinfo (Python 3.9+) for timezone handling
try:
    import zoneinfo
    CST = zoneinfo.ZoneInfo("Asia/Shanghai")   # China Standard Time (UTC+8)
except ImportError:
    # Fallback for older Python (not needed on Kubuntu 22.04)
    import pytz
    CST = pytz.timezone("Asia/Shanghai")

# ----------------------------- Configuration -----------------------------
DB_FILE = os.path.expanduser("~/.focus_tracker.db")
POLL_INTERVAL_DEFAULT = 2          # seconds

# ----------------------------- Verbose Logging ---------------------------
VERBOSE = False

def vlog(msg: str) -> None:
    """Print a verbose step-by-step log line to stdout when --verbose is set."""
    if VERBOSE:
        print(f"[verbose] {msg}", flush=True)

# ----------------------------- Grouping Helpers --------------------------
# A window_id is stable but its title (window_name) changes over time, so we
# aggregate at the (window_id, title) level: group by window_id, break down by
# title underneath. These keys/placeholders keep that nesting consistent.
NO_TITLE = "(no title)"            # unlocked sample whose window_name was NULL
NO_WINDOW = "No window (desktop?)" # unlocked sample with no focused window id
LOCKED_KEY = "__LOCKED__"          # synthetic window_id bucket for locked time

def _norm_title(title: Optional[str]) -> str:
    """Normalize a possibly-NULL title to a stable display/grouping string."""
    return title if title else NO_TITLE

# ----------------------------- Time Helpers -----------------------------
def format_ts(ts: float) -> str:
    """Convert UTC timestamp to CST formatted string."""
    vlog(f"format_ts: converting UTC epoch {ts} to CST string")
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CST)
    result = dt.strftime("%Y-%m-%d %H:%M:%S")
    vlog(f"format_ts: -> '{result}'")
    return result

def parse_cst_datetime(dt_str: str) -> float:
    """
    Parse a datetime string (YYYY-MM-DD HH:MM) as CST and return UTC timestamp.
    """
    vlog(f"parse_cst_datetime: parsing '{dt_str}' as CST")
    naive = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    # Attach CST timezone
    if hasattr(naive, 'replace'):
        # zoneinfo or pytz
        if isinstance(CST, zoneinfo.ZoneInfo):
            dt_cst = naive.replace(tzinfo=CST)
        else:
            dt_cst = CST.localize(naive)
    else:
        dt_cst = naive  # fallback (should not happen)
    ts = dt_cst.timestamp()
    vlog(f"parse_cst_datetime: '{dt_str}' CST -> UTC epoch {ts}")
    return ts

# ----------------------------- Database Layer ----------------------------
class FocusDB:
    def __init__(self, db_path=DB_FILE):
        vlog(f"FocusDB: opening SQLite database at {db_path}")
        self.conn = sqlite3.connect(db_path)
        self._create_table()
        vlog("FocusDB: database ready")

    def _create_table(self):
        vlog("FocusDB._create_table: ensuring 'focus_log' table exists")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS focus_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,          -- Unix epoch as float (UTC)
                window_id TEXT,                   -- hex ID, NULL if locked
                window_name TEXT,                 -- stored for display
                window_class TEXT,                -- for debugging
                is_locked BOOLEAN DEFAULT 0
            )
        """)
        vlog("FocusDB._create_table: ensuring index 'idx_timestamp' exists")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON focus_log(timestamp)")
        self.conn.commit()
        vlog("FocusDB._create_table: schema committed")

    def insert_sample(self, ts: float, window_id: Optional[str],
                      window_name: Optional[str], window_class: Optional[str],
                      is_locked: bool):
        vlog(f"FocusDB.insert_sample: ts={ts} window_id={window_id} "
             f"name={window_name!r} class={window_class!r} locked={is_locked}")
        self.conn.execute("""
            INSERT INTO focus_log (timestamp, window_id, window_name, window_class, is_locked)
            VALUES (?, ?, ?, ?, ?)
        """, (ts, window_id, window_name, window_class, 1 if is_locked else 0))
        self.conn.commit()
        vlog("FocusDB.insert_sample: row committed")

    def get_samples_in_range(self, start_ts: float, end_ts: float) -> List[Tuple]:
        """Return all records sorted by timestamp in [start_ts, end_ts].

        Each row is (timestamp, window_id, window_name, window_class, is_locked).
        window_class is included so callers can show a stable per-window label
        while breaking the title (window_name) down separately.
        """
        vlog(f"FocusDB.get_samples_in_range: querying rows in [{start_ts}, {end_ts}]")
        cur = self.conn.execute("""
            SELECT timestamp, window_id, window_name, window_class, is_locked
            FROM focus_log
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
        """, (start_ts, end_ts))
        rows = cur.fetchall()
        vlog(f"FocusDB.get_samples_in_range: fetched {len(rows)} row(s)")
        return rows

    def close(self):
        vlog("FocusDB.close: closing database connection")
        self.conn.close()

# ----------------------------- Focus & Lock Detection --------------------
def get_session_id() -> Optional[str]:
    """Return the session ID of the current user (for logind)."""
    vlog("get_session_id: running 'loginctl list-sessions --no-legend'")
    try:
        out = subprocess.check_output(["loginctl", "list-sessions", "--no-legend"],
                                      universal_newlines=True).strip()
        user = os.environ.get("USER")
        vlog(f"get_session_id: looking for session owned by user={user!r}")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == user:
                vlog(f"get_session_id: found session id={parts[0]}")
                return parts[0]
        vlog("get_session_id: no matching session found")
    except Exception as e:
        vlog(f"get_session_id: failed ({e})")
    return None

def is_screen_locked_logind() -> bool:
    """Use systemd-logind to check if the session is locked."""
    vlog("is_screen_locked_logind: checking lock state via logind")
    sid = get_session_id()
    if not sid:
        vlog("is_screen_locked_logind: no session id -> assuming unlocked")
        return False
    try:
        vlog(f"is_screen_locked_logind: running 'loginctl show-session {sid} -p LockedHint'")
        out = subprocess.check_output(["loginctl", "show-session", sid, "-p", "LockedHint"],
                                      universal_newlines=True).strip()
        locked = "LockedHint=yes" in out
        vlog(f"is_screen_locked_logind: LockedHint output={out!r} -> locked={locked}")
        return locked
    except Exception as e:
        vlog(f"is_screen_locked_logind: failed ({e}) -> assuming unlocked")
        return False

def get_focused_window_id() -> Optional[str]:
    """Return the X11 window ID (hex) of the currently focused window, or None."""
    vlog("get_focused_window_id: running 'xdotool getwindowfocus'")
    try:
        out = subprocess.check_output(["xdotool", "getwindowfocus"],
                                      universal_newlines=True).strip()
        vlog(f"get_focused_window_id: focused window id={out or None}")
        return out if out else None
    except subprocess.CalledProcessError as e:
        vlog(f"get_focused_window_id: xdotool failed ({e}) -> None")
        return None

def get_window_properties(window_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (window_name, window_class) using xprop."""
    vlog(f"get_window_properties: inspecting window id={window_id}")
    try:
        vlog(f"get_window_properties: running 'xprop -id {window_id} _NET_WM_NAME WM_NAME'")
        name_out = subprocess.check_output(["xprop", "-id", window_id, "_NET_WM_NAME", "WM_NAME"],
                                           universal_newlines=True, stderr=subprocess.DEVNULL)
        name = None
        for line in name_out.splitlines():
            if "_NET_WM_NAME(STRING)" in line or "WM_NAME(STRING)" in line:
                parts = line.split('=')
                if len(parts) == 2:
                    val = parts[1].strip()
                    if val.startswith('"') and val.endswith('"'):
                        name = val[1:-1]
                    else:
                        name = val
                    break
        vlog(f"get_window_properties: window_name={name!r}")
        vlog(f"get_window_properties: running 'xprop -id {window_id} WM_CLASS'")
        class_out = subprocess.check_output(["xprop", "-id", window_id, "WM_CLASS"],
                                            universal_newlines=True, stderr=subprocess.DEVNULL)
        class_str = None
        for line in class_out.splitlines():
            if "WM_CLASS(STRING)" in line:
                parts = line.split('=')
                if len(parts) == 2:
                    val = parts[1].strip()
                    match = re.search(r'"([^"]+)"', val)
                    if match:
                        class_str = match.group(1)
                    break
        vlog(f"get_window_properties: window_class={class_str!r}")
        return name, class_str
    except Exception as e:
        vlog(f"get_window_properties: failed ({e}) -> (None, None)")
        return None, None

def is_lock_screen_window(window_id: Optional[str]) -> bool:
    vlog(f"is_lock_screen_window: checking if window id={window_id} is a lock screen")
    if not window_id:
        vlog("is_lock_screen_window: no window id -> False")
        return False
    try:
        out = subprocess.check_output(["xprop", "-id", window_id, "WM_CLASS"],
                                      universal_newlines=True, stderr=subprocess.DEVNULL)
        if "kscreenlocker" in out.lower():
            vlog("is_lock_screen_window: WM_CLASS contains 'kscreenlocker' -> True")
            return True
        name, _ = get_window_properties(window_id)
        if name and ("lock" in name.lower() or "screen" in name.lower()):
            vlog(f"is_lock_screen_window: name {name!r} looks like a lock screen -> True")
            return True
    except Exception as e:
        vlog(f"is_lock_screen_window: failed ({e})")
        pass
    vlog("is_lock_screen_window: not a lock screen -> False")
    return False

def is_screen_locked() -> bool:
    vlog("is_screen_locked: starting lock detection")
    if is_screen_locked_logind():
        vlog("is_screen_locked: logind reports locked -> True")
        return True
    wid = get_focused_window_id()
    if wid and is_lock_screen_window(wid):
        vlog("is_screen_locked: focused window is a lock screen -> True")
        return True
    vlog("is_screen_locked: screen is unlocked -> False")
    return False

# ----------------------------- Polling Loop -----------------------------
def poll(db: FocusDB, interval: int):
    vlog(f"poll: entering polling loop with interval={interval}s")
    print(f"Polling every {interval} second(s). Press Ctrl+C to stop.")
    iteration = 0
    while True:
        iteration += 1
        vlog(f"poll: --- iteration {iteration} ---")
        ts = time.time()               # float
        vlog(f"poll: sample timestamp={ts}")
        locked = is_screen_locked()

        window_id = None
        window_name = None
        window_class = None

        if not locked:
            vlog("poll: screen unlocked, resolving focused window")
            wid = get_focused_window_id()
            if wid:
                window_id = wid
                window_name, window_class = get_window_properties(wid)
            else:
                vlog("poll: no focused window detected")
        else:
            vlog("poll: screen locked, skipping window resolution")

        db.insert_sample(ts, window_id, window_name, window_class, locked)
        vlog(f"poll: sleeping {interval}s before next sample")
        time.sleep(interval)

# ----------------------------- Statistics Commands -----------------------
def cmd_top(db: FocusDB, hours: int, limit: int, show_titles: bool = True):
    vlog(f"cmd_top: computing top {limit} windows over last {hours} hour(s) "
         f"(titles={'on' if show_titles else 'off'})")
    now = time.time()
    start_ts = now - hours * 3600
    vlog(f"cmd_top: time range [{start_ts}, {now}]")
    samples = db.get_samples_in_range(start_ts, now)
    if not samples:
        vlog("cmd_top: no samples in range")
        print("No data in the specified time range.")
        return

    # window_id -> {title: count}, plus a representative class per window_id.
    counts: Dict[str, Dict[str, int]] = {}
    wid_class: Dict[str, str] = {}
    for ts, wid, name, cls, locked in samples:
        if locked or wid is None:
            continue
        titles = counts.setdefault(wid, {})
        title = _norm_title(name)
        titles[title] = titles.get(title, 0) + 1
        if cls:
            wid_class[wid] = cls
    vlog(f"cmd_top: aggregated {len(counts)} window id(s), "
         f"{sum(len(t) for t in counts.values())} distinct (id, title) pair(s)")

    if not counts:
        vlog("cmd_top: no unlocked window samples")
        print("No window focus events (screen was locked most of the time).")
        return

    totals = {wid: sum(tc.values()) for wid, tc in counts.items()}
    ordered = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:limit]
    vlog(f"cmd_top: top {len(ordered)} window(s) after sorting by total samples")
    print(f"Top {limit} windows in the last {hours} hour(s):")
    for idx, (wid, total) in enumerate(ordered, 1):
        label = wid_class.get(wid, "unknown")
        print(f"{idx:2d}. {label} (ID: {wid}) - {total} samples")
        if show_titles:
            for title, cnt in sorted(counts[wid].items(), key=lambda x: x[1], reverse=True):
                pct = (cnt / total) * 100
                print(f'      - "{title}"  {cnt} ({pct:.0f}%)')


def cmd_timeline(db: FocusDB, start_str: str, end_str: str, show_titles: bool = True):
    vlog(f"cmd_timeline: building timeline from '{start_str}' to '{end_str}' "
         f"(titles={'on' if show_titles else 'off'})")
    start_ts = parse_cst_datetime(start_str)
    end_ts = parse_cst_datetime(end_str)
    if start_ts >= end_ts:
        vlog("cmd_timeline: start >= end, aborting")
        print("Start time must be before end time.")
        return
    samples = db.get_samples_in_range(start_ts, end_ts)
    if not samples:
        vlog("cmd_timeline: no samples in range")
        print("No data in the given time range.")
        return

    # Each interval is keyed by (window_id, title, locked). When titles are on a
    # title change opens a new interval; when off, only window_id / lock changes
    # do (the flat view). locked time carries no window_id or title.
    def interval_key(wid, name, locked):
        if locked:
            return (None, None, True)
        return (wid, _norm_title(name) if show_titles else None, False)

    wid_class: Dict[str, str] = {}
    intervals = []          # (start_ts, end_ts, (wid, title, locked))
    prev_key = None
    prev_ts = start_ts
    started = False
    for ts, wid, name, cls, locked in samples:
        if cls and wid is not None:
            wid_class[wid] = cls
        key = interval_key(wid, name, locked)
        if not started:
            prev_key, prev_ts, started = key, ts, True
            continue
        if key != prev_key:
            intervals.append((prev_ts, ts, prev_key))
            prev_key, prev_ts = key, ts
    if started:
        intervals.append((prev_ts, end_ts, prev_key))
    vlog(f"cmd_timeline: produced {len(intervals)} interval(s)")

    print(f"Timeline from {start_str} to {end_str} (CST):")
    if show_titles:
        # Nested: a window header per (window_id / lock) run, title spans indented.
        prev_group = object()
        for start, end, (wid, title, locked) in intervals:
            duration = end - start
            if duration <= 0:
                continue
            group = ("LOCKED",) if locked else ("NONE",) if wid is None else ("WID", wid)
            if group != prev_group:
                if locked:
                    print("  SCREEN LOCKED")
                elif wid is None:
                    print(f"  {NO_WINDOW}")
                else:
                    print(f"  {wid_class.get(wid, 'unknown')} (ID: {wid})")
                prev_group = group
            start_dt = format_ts(start)
            end_dt = format_ts(end)
            if locked or wid is None:
                print(f"      {start_dt} – {end_dt}  ({duration:.1f}s)")
            else:
                print(f'      {start_dt} – {end_dt}  ({duration:.1f}s) : "{title}"')
    else:
        # Flat: one line per interval, labeled by window (class + id).
        for start, end, (wid, title, locked) in intervals:
            duration = end - start
            if duration <= 0:
                continue
            start_dt = format_ts(start)
            end_dt = format_ts(end)
            if locked:
                label = "SCREEN LOCKED"
            elif wid is None:
                label = NO_WINDOW
            else:
                label = f"{wid_class.get(wid, 'unknown')} (ID: {wid})"
            print(f"  {start_dt} – {end_dt}  ({duration:.1f}s) : {label}")


def cmd_sections(db: FocusDB, interval_minutes: int, start_str: str, end_str: str,
                 show_titles: bool = True):
    vlog(f"cmd_sections: sectioning '{start_str}'..'{end_str}' into {interval_minutes}-minute bins "
         f"(titles={'on' if show_titles else 'off'})")
    start_ts = parse_cst_datetime(start_str)
    end_ts = parse_cst_datetime(end_str)
    if start_ts >= end_ts:
        vlog("cmd_sections: start >= end, aborting")
        print("Start time must be before end time.")
        return
    interval_sec = interval_minutes * 60
    samples = db.get_samples_in_range(start_ts, end_ts)
    if not samples:
        vlog("cmd_sections: no samples in range")
        print("No data in the given time range.")
        return

    # sec_start -> {window_key: {title: count}}; window_key is a window_id or
    # LOCKED_KEY. wid_class holds a representative app label per window_id.
    section_data: Dict[float, Dict[str, Dict[str, int]]] = {}
    wid_class: Dict[str, str] = {}
    for ts, wid, name, cls, locked in samples:
        offset = ts - start_ts
        sec_start = start_ts + (offset // interval_sec) * interval_sec
        sec = section_data.setdefault(sec_start, {})
        if locked:
            tc = sec.setdefault(LOCKED_KEY, {})
            tc["SCREEN LOCKED"] = tc.get("SCREEN LOCKED", 0) + 1
        elif wid is not None:
            tc = sec.setdefault(wid, {})
            title = _norm_title(name)
            tc[title] = tc.get(title, 0) + 1
            if cls:
                wid_class[wid] = cls
    vlog(f"cmd_sections: bucketed samples into {len(section_data)} section(s)")

    print(f"Sections of {interval_minutes} minute(s) from {start_str} to {end_str} (CST):")
    for sec_start in sorted(section_data.keys()):
        sec = section_data[sec_start]
        if not sec:
            continue
        key_totals = {k: sum(tc.values()) for k, tc in sec.items()}
        total = sum(key_totals.values())
        dom = max(key_totals, key=key_totals.get)   # dominant window in this section
        dom_total = key_totals[dom]
        sec_end = min(sec_start + interval_sec, end_ts)
        start_dt = format_ts(sec_start)
        end_dt = format_ts(sec_end)
        pct = (dom_total / total) * 100
        if dom == LOCKED_KEY:
            label = "SCREEN LOCKED"
        else:
            label = f"{wid_class.get(dom, 'unknown')} (ID: {dom})"
        print(f"  {start_dt} – {end_dt} : {label}  ({pct:.1f}% of section)")
        if show_titles and dom != LOCKED_KEY:
            # Title breakdown for the dominant window, relative to that window.
            for title, cnt in sorted(sec[dom].items(), key=lambda x: x[1], reverse=True):
                tpct = (cnt / dom_total) * 100
                print(f'        - "{title}"  {tpct:.0f}%')

# ----------------------------- Main CLI -----------------------------
def main():
    global VERBOSE

    # Shared parent parser so --verbose works on every subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", "-v", action="store_true", default=argparse.SUPPRESS,
                        help="Print detailed step-by-step logging to stdout")

    # Shared by the reporting commands: toggle the per-title breakdown.
    titles_common = argparse.ArgumentParser(add_help=False)
    titles_common.add_argument("--no-titles", dest="titles", action="store_false", default=True,
                               help="Hide the per-title breakdown (flat, one-window view)")

    parser = argparse.ArgumentParser(description="X11 window focus tracker (CST timezone)",
                                     parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")

    poll_parser = subparsers.add_parser("poll", parents=[common], help="Start polling and storing data")
    poll_parser.add_argument("--interval", "-i", type=int, default=POLL_INTERVAL_DEFAULT,
                             help=f"Polling interval in seconds (default: {POLL_INTERVAL_DEFAULT})")

    top_parser = subparsers.add_parser("top", parents=[common, titles_common], help="Show top used windows")
    top_parser.add_argument("--hours", "-H", type=int, default=24,
                            help="Look back this many hours (default: 24)")
    top_parser.add_argument("--limit", "-L", type=int, default=10,
                            help="Number of top windows to show (default: 10)")

    timeline_parser = subparsers.add_parser("timeline", parents=[common, titles_common], help="Show focus timeline")
    timeline_parser.add_argument("--from", "-f", dest="from_date", required=True,
                                 help="Start datetime (CST), format: 'YYYY-MM-DD HH:MM'")
    timeline_parser.add_argument("--to", "-t", dest="to_date", required=True,
                                 help="End datetime (CST), format: 'YYYY-MM-DD HH:MM'")

    sections_parser = subparsers.add_parser("sections", parents=[common, titles_common], help="Show most used window per time section")
    sections_parser.add_argument("--interval", "-I", type=int, required=True,
                                 help="Section length in minutes")
    sections_parser.add_argument("--from", "-f", dest="from_date", required=True,
                                 help="Start datetime (CST), format: 'YYYY-MM-DD HH:MM'")
    sections_parser.add_argument("--to", "-t", dest="to_date", required=True,
                                 help="End datetime (CST), format: 'YYYY-MM-DD HH:MM'")

    args = parser.parse_args()

    # --verbose may appear before or after the subcommand; SUPPRESS keeps the
    # earlier value from being overwritten when omitted on the subparser.
    VERBOSE = getattr(args, "verbose", False)
    vlog(f"main: verbose logging enabled")
    vlog(f"main: parsed command='{args.command}'")

    vlog("main: initializing database")
    db = FocusDB()
    try:
        if args.command == "poll":
            vlog(f"main: dispatching to poll (interval={args.interval})")
            poll(db, args.interval)
        elif args.command == "top":
            vlog(f"main: dispatching to cmd_top (hours={args.hours}, limit={args.limit}, titles={args.titles})")
            cmd_top(db, args.hours, args.limit, args.titles)
        elif args.command == "timeline":
            vlog(f"main: dispatching to cmd_timeline (from={args.from_date!r}, to={args.to_date!r}, titles={args.titles})")
            cmd_timeline(db, args.from_date, args.to_date, args.titles)
        elif args.command == "sections":
            vlog(f"main: dispatching to cmd_sections (interval={args.interval}, "
                 f"from={args.from_date!r}, to={args.to_date!r}, titles={args.titles})")
            cmd_sections(db, args.interval, args.from_date, args.to_date, args.titles)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        vlog("main: caught KeyboardInterrupt")
        print("\nStopped.")
    finally:
        db.close()
        vlog("main: done")

if __name__ == "__main__":
    main()
