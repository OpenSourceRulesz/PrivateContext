"""SQLite helpers for PrivateContext: connect, init, insert, search, usage."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

APP_NAME = "privatecontext"
DB_NAME = "context.db"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_db_path() -> str:
    return str(data_dir() / DB_NAME)


def _schema_text() -> str:
    here = Path(__file__).resolve()
    for p in (here.parent / "schema.sql", here.parent.parent / "schema.sql"):
        if p.exists():
            return p.read_text()
    raise FileNotFoundError("schema.sql not found next to package or repo root")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the DB, creating parent dirs. Safe to share across threads."""
    p = Path(path) if path else Path(default_db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(_schema_text())
    con.commit()


def strip_url(url: str | None) -> str | None:
    """Strip query params and fragments. Never store tracking junk."""
    if not url:
        return url
    return url.split("#", 1)[0].split("?", 1)[0]


def log_event(con: sqlite3.Connection, type: str, ts: float | None = None,
              app: str | None = None, title: str | None = None,
              url: str | None = None, x: int | None = None,
              y: int | None = None, duration_ms: int | None = None,
              commit: bool = True) -> int:
    cur = con.execute(
        "INSERT INTO events(ts, type, app, title, url, x, y, duration_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else time.time(), type, app, title,
         strip_url(url), x, y, duration_ms),
    )
    if commit:
        con.commit()
    return cur.lastrowid


def today_range() -> tuple[float, float]:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now.timestamp()


def timeline(con: sqlite3.Connection, limit: int = 200) -> list[dict]:
    start, _ = today_range()
    rows = con.execute(
        "SELECT id, ts, type, app, title, url, x, y, duration_ms FROM events"
        " WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
        (start, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def app_usage(con: sqlite3.Connection, cap_s: float = 600) -> list[tuple[str, float]]:
    """Seconds per app today. Each window event owns time until the next
    event, capped so a forgotten window doesn't claim the whole afternoon."""
    start, _ = today_range()
    rows = con.execute(
        "SELECT ts, app FROM events WHERE type = 'window' AND ts >= ? ORDER BY ts",
        (start,),
    ).fetchall()
    usage: dict[str, float] = {}
    now = time.time()
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else now
        dur = min(max(0.0, nxt - r["ts"]), cap_s)
        app = r["app"] or "unknown"
        usage[app] = usage.get(app, 0.0) + dur
    return sorted(usage.items(), key=lambda kv: -kv[1])


def _fts_query(q: str) -> str:
    # Quote each token so user punctuation can't break FTS5 syntax.
    toks = [t for t in q.split() if t]
    if not toks:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def search(con: sqlite3.Connection, q: str, limit: int = 50) -> list[dict]:
    if not q.strip():
        return []
    rows = con.execute(
        "SELECT e.id, e.ts, e.type, e.app, e.title, e.url"
        " FROM events_fts f JOIN events e ON e.id = f.rowid"
        " WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
        (_fts_query(q), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def counts(con: sqlite3.Connection) -> dict:
    rows = con.execute("SELECT type, COUNT(*) AS n FROM events GROUP BY type").fetchall()
    return {r["type"]: r["n"] for r in rows}
