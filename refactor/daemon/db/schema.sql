-- daemon/db/schema.sql — all DDL for the desktop-overview daemon.
-- Mirrors refactor/plans/daemon/06-data-model.md.  Loaded once at startup by
-- db/store.py, which substitutes {tokenizer} (FTS_TOKENIZER, default 'trigram')
-- and applies the PRAGMAs.  All timestamps are epoch seconds (REAL).
--
-- Safe to re-run: every object uses IF NOT EXISTS.

-- ────────────────────────────── identity & windows (02) ──────────────────────
CREATE TABLE IF NOT EXISTS daemon_run (
  id              INTEGER PRIMARY KEY,
  daemon_boot_id  TEXT NOT NULL UNIQUE,   -- uuid4 per process
  machine_boot_id TEXT,                   -- /proc/sys/kernel/random/boot_id
  session_key     TEXT,                   -- md5(boot_id:session_start)
  user_name       TEXT,
  uid             TEXT,
  started_at      REAL NOT NULL,
  stopped_at      REAL
);

CREATE TABLE IF NOT EXISTS window (
  window_uid          INTEGER PRIMARY KEY,   -- surrogate, referenced everywhere
  session_key         TEXT NOT NULL,         -- X session = liveness/jump scope
  first_daemon_run_id INTEGER REFERENCES daemon_run(id),
  last_daemon_run_id  INTEGER REFERENCES daemon_run(id),
  x_window_id         INTEGER NOT NULL,      -- raw 0x.. id as decimal
  wm_class            TEXT,                  -- app name (xprop WM_CLASS instance, lowercase)
  app_name            TEXT,                  -- alias/wider process name for display/search
  vdesktop_index      INTEGER,               -- current virtual desktop (cached)
  vdesktop_name       TEXT,                  -- current virtual desktop name (cached)
  first_seen          REAL NOT NULL,
  last_seen           REAL NOT NULL,         -- "last access time" (sort key)
  closed_at           REAL,
  alive               INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_window_lastseen ON window(last_seen DESC);
CREATE INDEX IF NOT EXISTS ix_window_session  ON window(session_key, x_window_id);
CREATE INDEX IF NOT EXISTS ix_window_alive    ON window(alive);

-- ────────────────────────────── title / focus / desktop / app_name (06 §2) ───
CREATE TABLE IF NOT EXISTS title_history (
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER NOT NULL REFERENCES window(window_uid),
  title       TEXT,
  changed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_title_window ON title_history(window_uid, changed_at);

CREATE TABLE IF NOT EXISTS app_name_history (    -- process/app name for display + search
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER NOT NULL REFERENCES window(window_uid),
  app_name    TEXT,
  changed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_app_name_window ON app_name_history(window_uid, changed_at);

CREATE TABLE IF NOT EXISTS focus_event (         -- one row per focus gain
  id             INTEGER PRIMARY KEY,
  window_uid     INTEGER REFERENCES window(window_uid),
  vdesktop_index INTEGER,
  vdesktop_name  TEXT,
  focused_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_focus_time ON focus_event(focused_at);
CREATE INDEX IF NOT EXISTS ix_focus_window_time ON focus_event(window_uid, focused_at);

CREATE TABLE IF NOT EXISTS screen_lock_event (   -- lock/unlock boundaries
  id         INTEGER PRIMARY KEY,
  locked     INTEGER NOT NULL,        -- 1 = locked, 0 = unlocked
  method     TEXT,                    -- 'dbus' | 'idle'
  changed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_screen_lock_time ON screen_lock_event(changed_at);

CREATE TABLE IF NOT EXISTS vdesktop_state (      -- desktop rename/count history
  id         INTEGER PRIMARY KEY,
  idx        INTEGER,
  name       TEXT,
  count      INTEGER,
  changed_at REAL NOT NULL
);

-- ────────────────────────────── heartbeat / usage rate ─────────────────────────
-- One row per focused-window snapshot (daemon heartbeat).  Used to compute
-- focused active-minutes per window (usage_5m/10m/30m).
CREATE TABLE IF NOT EXISTS window_heartbeat (
  id             INTEGER PRIMARY KEY,
  daemon_boot_id TEXT NOT NULL,         -- which daemon run produced the beat
  window_uid     INTEGER REFERENCES window(window_uid),
  x_window_id    INTEGER NOT NULL,
  ts             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_heartbeat_window ON window_heartbeat(window_uid, ts);
CREATE INDEX IF NOT EXISTS ix_heartbeat_boot   ON window_heartbeat(daemon_boot_id, ts);
CREATE INDEX IF NOT EXISTS ix_heartbeat_ts     ON window_heartbeat(ts);

-- ────────────────────────────── clipboard / selection / read (06 §3) ─────────
CREATE TABLE IF NOT EXISTS clipboard_event (     -- a COPY (CLIPBOARD owner change)
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  kind        TEXT,                -- TEXT|HTML|IMAGE|FILES|PASSWORD|OTHER
  text        TEXT,                -- NULL for IMAGE/PASSWORD
  image_rel   TEXT,                -- window_captures/.../clip_<ts>.png for IMAGE, else NULL
  n_chars     INTEGER,
  n_bytes     INTEGER,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clip_window ON clipboard_event(window_uid, created_at);

CREATE TABLE IF NOT EXISTS selection_event (     -- a HIGHLIGHT (PRIMARY content)
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  text        TEXT,
  n_chars     INTEGER,
  created_at  REAL NOT NULL        -- = selection start_ts
);
CREATE INDEX IF NOT EXISTS ix_sel_window ON selection_event(window_uid, created_at);

CREATE TABLE IF NOT EXISTS read_event (          -- a PASTE candidate (gesture)
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER REFERENCES window(window_uid),
  selection   TEXT,                -- 'clipboard' | 'primary'
  gesture     TEXT,                -- 'Ctrl+V' | 'Ctrl+Shift+V' | 'middle-click'
  confidence  TEXT,                -- 'strong' | 'weak'
  text        TEXT,                -- optional: content read at paste time
  server_time INTEGER,             -- X event timestamp (ms)
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_read_window ON read_event(window_uid, created_at);

-- ────────────────────────────── keyboard segments (04 / 06 §4) ───────────────
CREATE TABLE IF NOT EXISTS kbd_segment (
  id             INTEGER PRIMARY KEY,
  window_uid     INTEGER REFERENCES window(window_uid),
  text           TEXT NOT NULL,       -- concatenated visible chars
  started_at     REAL NOT NULL,
  ended_at       REAL NOT NULL,
  vdesktop_index INTEGER,
  vdesktop_name  TEXT,
  flush_reason   TEXT                  -- 'idle' | 'focus_change' | 'title_change' | 'shutdown'
);
CREATE INDEX IF NOT EXISTS ix_kbd_window ON kbd_segment(window_uid, started_at);

-- ────────────────────────────── window_captures (05 §5) ───────────────────────────
CREATE TABLE IF NOT EXISTS window_capture (
  id          INTEGER PRIMARY KEY,
  window_uid  INTEGER NOT NULL REFERENCES window(window_uid),
  rel_path    TEXT NOT NULL,        -- window_captures/<boot>/<uid>/<ts>.png
  width       INTEGER,
  height      INTEGER,
  captured_at REAL NOT NULL,
  is_focused  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_window_capture_window ON window_capture(window_uid, captured_at DESC);

CREATE VIEW IF NOT EXISTS window_capture_latest AS
  SELECT t.* FROM window_capture t
  JOIN (SELECT window_uid, MAX(captured_at) AS mx FROM window_capture GROUP BY window_uid) m
    ON t.window_uid = m.window_uid AND t.captured_at = m.mx;

-- ────────────────────────────── full-text search (06 §5) ─────────────────────
-- One external-content FTS5 table per searchable source + sync triggers.
-- NOTE: for external-content FTS5, each FTS column name MUST match the source
-- column name (FTS re-reads `SELECT <colname> FROM <content> WHERE rowid=?` for
-- highlight/snippet). So fts_title's column is `title`, the others are `text`.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_title USING fts5(
  title, content='title_history', content_rowid='id', tokenize='{tokenizer}');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_appname USING fts5(
  app_name, content='app_name_history', content_rowid='id', tokenize='{tokenizer}');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_clip USING fts5(
  text, content='clipboard_event', content_rowid='id', tokenize='{tokenizer}');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_sel USING fts5(
  text, content='selection_event', content_rowid='id', tokenize='{tokenizer}');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_kbd USING fts5(
  text, content='kbd_segment', content_rowid='id', tokenize='{tokenizer}');

-- title_history → fts_title  (column is `title`, matching the source column)
CREATE TRIGGER IF NOT EXISTS title_ai AFTER INSERT ON title_history BEGIN
  INSERT INTO fts_title(rowid, title) VALUES (new.id, new.title);
END;
CREATE TRIGGER IF NOT EXISTS title_ad AFTER DELETE ON title_history BEGIN
  INSERT INTO fts_title(fts_title, rowid, title) VALUES('delete', old.id, old.title);
END;
CREATE TRIGGER IF NOT EXISTS title_au AFTER UPDATE ON title_history BEGIN
  INSERT INTO fts_title(fts_title, rowid, title) VALUES('delete', old.id, old.title);
  INSERT INTO fts_title(rowid, title) VALUES (new.id, new.title);
END;

-- app_name_history → fts_appname (column is `app_name`, matching the source column)
CREATE TRIGGER IF NOT EXISTS app_name_ai AFTER INSERT ON app_name_history BEGIN
  INSERT INTO fts_appname(rowid, app_name) VALUES (new.id, new.app_name);
END;
CREATE TRIGGER IF NOT EXISTS app_name_ad AFTER DELETE ON app_name_history BEGIN
  INSERT INTO fts_appname(fts_appname, rowid, app_name) VALUES('delete', old.id, old.app_name);
END;
CREATE TRIGGER IF NOT EXISTS app_name_au AFTER UPDATE ON app_name_history BEGIN
  INSERT INTO fts_appname(fts_appname, rowid, app_name) VALUES('delete', old.id, old.app_name);
  INSERT INTO fts_appname(rowid, app_name) VALUES (new.id, new.app_name);
END;

-- clipboard_event → fts_clip
CREATE TRIGGER IF NOT EXISTS clip_ai AFTER INSERT ON clipboard_event BEGIN
  INSERT INTO fts_clip(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS clip_ad AFTER DELETE ON clipboard_event BEGIN
  INSERT INTO fts_clip(fts_clip, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS clip_au AFTER UPDATE ON clipboard_event BEGIN
  INSERT INTO fts_clip(fts_clip, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO fts_clip(rowid, text) VALUES (new.id, new.text);
END;

-- selection_event → fts_sel
CREATE TRIGGER IF NOT EXISTS sel_ai AFTER INSERT ON selection_event BEGIN
  INSERT INTO fts_sel(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS sel_ad AFTER DELETE ON selection_event BEGIN
  INSERT INTO fts_sel(fts_sel, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS sel_au AFTER UPDATE ON selection_event BEGIN
  INSERT INTO fts_sel(fts_sel, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO fts_sel(rowid, text) VALUES (new.id, new.text);
END;

-- kbd_segment → fts_kbd
CREATE TRIGGER IF NOT EXISTS kbd_ai AFTER INSERT ON kbd_segment BEGIN
  INSERT INTO fts_kbd(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS kbd_ad AFTER DELETE ON kbd_segment BEGIN
  INSERT INTO fts_kbd(fts_kbd, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS kbd_au AFTER UPDATE ON kbd_segment BEGIN
  INSERT INTO fts_kbd(fts_kbd, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO fts_kbd(rowid, text) VALUES (new.id, new.text);
END;

-- ────────────────────────────── meta / versioning (06 §6) ────────────────────
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
