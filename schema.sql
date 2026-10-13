-- PrivateContext v0 schema.
-- Raw event log. `type` is one of: window, click, afk, back, shot,
-- url (browser visits, incl. imported history), ocr (imported OCR text),
-- transcript (imported audio transcripts).
-- Keystroke *contents* are never stored: clicks log coordinates only,
-- keyboard input is used solely as an activity timestamp for AFK detection.
CREATE TABLE IF NOT EXISTS events(
  id          INTEGER PRIMARY KEY,
  ts          REAL    NOT NULL,   -- unix epoch seconds
  type        TEXT    NOT NULL,   -- window | click | afk | back | shot
  app         TEXT,               -- active application name
  title       TEXT,               -- window title
  url         TEXT,               -- browser URL with query params + fragment stripped
  x           INTEGER,            -- click x (click events only)
  y           INTEGER,            -- click y (click events only)
  duration_ms INTEGER,            -- idle duration (back events only)
  tags        TEXT                -- comma-separated auto tags (tagging.py); NULL = untagged
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

-- One row per calendar day, filled in by the nightly summarizer (v0: unused).
CREATE TABLE IF NOT EXISTS days(
  date    TEXT PRIMARY KEY,  -- YYYY-MM-DD, local time
  summary TEXT,              -- natural-language day summary
  tags    TEXT               -- comma-separated auto tags
);

-- Simple settings store (e.g. screenshots on/off, install id).
CREATE TABLE IF NOT EXISTS kv(
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Full-text search over the event log. External-content table kept in sync
-- with `events` by the triggers below.
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
  USING fts5(app, title, url, content='events', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, app, title, url)
    VALUES (new.id, new.app, new.title, new.url);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, app, title, url)
    VALUES ('delete', old.id, old.app, old.title, old.url);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, app, title, url)
    VALUES ('delete', old.id, old.app, old.title, old.url);
  INSERT INTO events_fts(rowid, app, title, url)
    VALUES (new.id, new.app, new.title, new.url);
END;
