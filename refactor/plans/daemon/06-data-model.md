# 06 — Data Model (SQLite + FTS5 + image files)

`schematic.txt`: *"background daemon use sqlite for text storage, and file for
storing images. reference those images with relative file path in the
database."*

All timestamps are epoch seconds (REAL). WAL mode (`daemon/01 §3`). Images live
on disk (`daemon/05 §3`); only paths are stored here.

---

## 1. Identity & windows (from `daemon/02`)

```sql
CREATE TABLE daemon_run (
  id              INTEGER PRIMARY KEY,
  daemon_boot_id  TEXT NOT NULL UNIQUE,   -- uuid4 per process
  machine_boot_id TEXT,                   -- /proc/.../boot_id
  session_key     TEXT,                   -- md5(boot_id:session_start)
  user_name       TEXT,
  uid             TEXT,
  started_at      REAL NOT NULL,
  stopped_at      REAL
);

CREATE TABLE window (
  window_uid    INTEGER PRIMARY KEY,      -- surrogate, referenced everywhere
  session_key   TEXT NOT NULL,            -- X session = liveness/jump scope (02 §2)
  first_daemon_run_id INTEGER REFERENCES daemon_run(id),
  last_daemon_run_id  INTEGER REFERENCES daemon_run(id),
  x_window_id   INTEGER NOT NULL,         -- raw 0x.. id as decimal
  wm_class      TEXT,                     -- app name (xprop WM_CLASS)
  first_seen    REAL NOT NULL,
  last_seen     REAL NOT NULL,            -- "last access time" (sort key)
  closed_at     REAL,
  alive         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_window_lastseen ON window(last_seen DESC);
CREATE INDEX ix_window_session  ON window(session_key, x_window_id);
CREATE INDEX ix_window_alive    ON window(alive);
```

> Liveness/jump key on `session_key`, **not** the daemon process, so windows
> survive a daemon restart within the same login session (`02 §2-3,6`). On
> startup the daemon reuses `window_uid` for x_window_ids still in
> `_NET_CLIENT_LIST` under the current `session_key`.

## 2. Title & focus & desktop history

Titles are **append-only history** so the timeline can show every title a
window displayed.

```sql
CREATE TABLE title_history (
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER NOT NULL REFERENCES window(window_uid),
  title       TEXT,
  changed_at  REAL NOT NULL
);
CREATE INDEX ix_title_window ON title_history(window_uid, changed_at);

CREATE TABLE focus_event (         -- one row per focus gain
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  vdesktop_index INTEGER,
  vdesktop_name  TEXT,
  focused_at  REAL NOT NULL
);
CREATE INDEX ix_focus_time ON focus_event(focused_at);

CREATE TABLE vdesktop_state (      -- desktop rename/count history (root-level)
  id         INTEGER PRIMARY KEY,
  idx        INTEGER,
  name       TEXT,
  count      INTEGER,
  changed_at REAL NOT NULL
);
```

The "current title" of a window = latest `title_history` row; "current desktop
of a window" = the `vdesktop_*` recorded on its latest `focus_event`. Both are
exposed pre-computed by the API so the **frontend does no joins** (`api/07 §3`).

## 3. Clipboard / selection / read events

```sql
CREATE TABLE clipboard_event (     -- a COPY (CLIPBOARD owner change)
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  kind        TEXT,                -- TEXT|HTML|IMAGE|FILES|PASSWORD|OTHER
  text        TEXT,                -- NULL for IMAGE/PASSWORD
  image_rel   TEXT,               -- window_captures/.../clip_<ts>.png for IMAGE, else NULL
  n_chars     INTEGER, n_bytes INTEGER,
  created_at  REAL NOT NULL
);

CREATE TABLE selection_event (     -- a HIGHLIGHT (PRIMARY content, Strategy C)
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  text        TEXT,
  n_chars     INTEGER,
  created_at  REAL NOT NULL        -- = selection start_ts
);

CREATE TABLE read_event (          -- a PASTE candidate (gesture-detected)
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  selection   TEXT,                -- 'clipboard' | 'primary'
  gesture     TEXT,                -- 'Ctrl+V' | 'Ctrl+Shift+V' | 'middle-click'
  confidence  TEXT,                -- 'strong' | 'weak'
  text        TEXT,                -- optional: content read at paste time
  server_time INTEGER,             -- X event timestamp (ms)
  created_at  REAL NOT NULL
);
```

Notes:
- `PASSWORD` clipboard events store `text=NULL` + `kind='PASSWORD'` (redacted at
  the collector, `daemon/03 §3`).
- IMAGE clipboard content is saved as a file like window_captures and referenced by
  `image_rel`.

## 4. Keyboard segments (from `daemon/04`)

```sql
CREATE TABLE kbd_segment (
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  text        TEXT NOT NULL,       -- concatenated visible chars
  started_at  REAL NOT NULL,
  ended_at    REAL NOT NULL,
  vdesktop_index INTEGER, vdesktop_name TEXT,
  flush_reason TEXT                -- 'idle' | 'focus_change' | 'enter' | 'tab'
);
CREATE INDEX ix_kbd_window ON kbd_segment(window_uid, started_at);
```

## 5. Full-text search (FTS5)

The frontend searches four fields: **title, clipboard content, selection
content, keyboard input text** (`schematic.txt`). We build **one external-content
FTS5 table per searchable source**, so each can be queried independently *or*
combined, and so hits can be attributed to the right field (for "show hit
fields / highlight excerpt", `frontend/08 §4`).

```sql
-- one representative; repeat the pattern for each source
CREATE VIRTUAL TABLE fts_kbd USING fts5(
  text,
  content='kbd_segment', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);
-- triggers keep fts in sync on INSERT/DELETE of kbd_segment
```

Tables to index:
- `fts_title`   ← `title_history.title`
- `fts_clip`    ← `clipboard_event.text`
- `fts_sel`     ← `selection_event.text`
- `fts_kbd`     ← `kbd_segment.text`

Why per-source instead of one combined FTS table:
- The frontend can let the user **select which fields to search** (`schematic`)
  → just query the chosen FTS tables.
- "Combine all fields" → query all four and **union by `window_uid`**.
- Each hit naturally knows its **source field** → drives the "hit fields" /
  excerpt highlighting (`fts5` `snippet()` / `highlight()` give the excerpt with
  match markers — server-side, so the frontend does no excerpting).
- CJK note: `unicode61` doesn't segment Chinese; for the project's CJK content
  prefer the **`trigram`** tokenizer (`tokenize='trigram'`, FTS5 ≥ 3.34) on the
  text tables, which gives substring matching across CJK + latin. Decide per
  field; trigram is the safe default for mixed CN/EN. Document the chosen
  tokenizer.

### Search query shape (server-side, `api/07 §4`)
For each selected source: `SELECT rowid, snippet(...) FROM fts_X WHERE fts_X
MATCH ?`. Join rowid→source row→`window_uid`. Aggregate per `window_uid`:
collect which fields hit + their snippets. Apply liveness filter + time range +
sort. Return assembled result objects.

## 6. Pragmas & migration

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=3000;
```

Schema versioning: a `meta(key,value)` table with `schema_version`; apply
ordered migration steps on startup (`CREATE TABLE IF NOT EXISTS` for v1, real
migrations later). Keep DDL in `daemon/db/schema.sql` loaded at init.

Config knobs (this section): `FTS_TOKENIZER` (`trigram`), `SQLITE_JOURNAL_MODE`
(`WAL`), `SQLITE_SYNCHRONOUS` (`NORMAL`), `SQLITE_BUSY_TIMEOUT_MS` (3000),
`SEARCH_DEFAULT_LIMIT`, `SEARCH_MAX_LIMIT`. Defaults, types, and set-via →
**`10-configuration.md §4`**.

## 7. Module shape

```
daemon/db/schema.sql     # all DDL above
daemon/db/store.py       # aiosqlite open, pragmas, write-queue API, read helpers
daemon/db/search.py      # FTS query builder + result assembly (07 §4 lives here)
```
