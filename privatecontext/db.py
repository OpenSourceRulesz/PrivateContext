"""SQLite helpers for PrivateContext: connect, init, insert, search, usage."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .tagging import extract_tags

DENYLIST_KEY = "denylist"

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
    # Migration for databases created before the tags column existed.
    cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    if "tags" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN tags TEXT")
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
              tags: str | list | None = None,
              commit: bool = True) -> int:
    if tags is None:
        found = extract_tags(app, title, url)
        tag_str = ",".join(found) if found else None
    elif isinstance(tags, (list, tuple)):
        tag_str = ",".join(tags) if tags else None
    else:
        tag_str = tags or None
    cur = con.execute(
        "INSERT INTO events(ts, type, app, title, url, x, y, duration_ms, tags)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else time.time(), type, app, title,
         strip_url(url), x, y, duration_ms, tag_str),
    )
    if commit:
        con.commit()
    return cur.lastrowid


def today_range() -> tuple[float, float]:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now.timestamp()


def day_range(date_str: str) -> tuple[float, float]:
    """Unix range for a local calendar day (YYYY-MM-DD). Raises ValueError
    on bad format."""
    d = date.fromisoformat(date_str)
    start = datetime(d.year, d.month, d.day).astimezone()
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def collapse_clicks(rows: list[dict]) -> list[dict]:
    """Collapse consecutive click rows sharing app+title into one row.

    Presentation only — storage is untouched. Rows are newest-first; the
    merged row keeps the newest ts, gains ts_end (oldest ts of the run),
    clicks (run length), x/y cleared, and the union of tags. Any non-click
    row (window/afk/url/ocr/...) breaks a run. Single clicks pass through
    unchanged.
    """
    out: list[dict] = []
    run: list[dict] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            newest, oldest = run[0], run[-1]
            tags: list[str] = []
            for r in run:
                for t in (r.get("tags") or "").split(","):
                    t = t.strip()
                    if t and t not in tags:
                        tags.append(t)
            merged = dict(newest)
            merged["ts_end"] = oldest["ts"]
            merged["clicks"] = len(run)
            merged["x"] = None
            merged["y"] = None
            merged["tags"] = ",".join(sorted(tags)) if tags else None
            out.append(merged)
        run.clear()

    for r in rows:
        if (r.get("type") == "click" and run
                and run[0].get("app") == r.get("app")
                and run[0].get("title") == r.get("title")):
            run.append(r)
        else:
            flush()
            if r.get("type") == "click":
                run = [r]
            else:
                out.append(r)
    flush()
    return out


def timeline(con: sqlite3.Connection, limit: int = 200,
             date_str: str | None = None) -> list[dict]:
    """Newest-first events for one day (default: today)."""
    start, end = day_range(date_str) if date_str else today_range()
    rows = con.execute(
        "SELECT id, ts, type, app, title, url, x, y, duration_ms, tags FROM events"
        " WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT ?",
        (start, end, limit),
    ).fetchall()
    return collapse_clicks([dict(r) for r in rows])


def app_usage(con: sqlite3.Connection, cap_s: float = 600,
              date_str: str | None = None) -> list[tuple[str, float]]:
    """Seconds per app for one day (default: today). Each window event owns
    time until the next event, capped so a forgotten window doesn't claim
    the whole afternoon. For past days the last event owns time until
    midnight, not until now."""
    if date_str:
        start, end = day_range(date_str)
    else:
        start, _ = today_range()
        end = time.time()
    rows = con.execute(
        "SELECT ts, app FROM events WHERE type = 'window'"
        " AND ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
    usage: dict[str, float] = {}
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else end
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


# ---------------------------------------------------------------------------
# kv store + denylist
# ---------------------------------------------------------------------------

def kv_get(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    con.commit()


def get_denylist(con: sqlite3.Connection) -> list[str]:
    """Denylist patterns (JSON array in kv). Empty list = allow everything."""
    raw = kv_get(con, DENYLIST_KEY)
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(p) for p in val] if isinstance(val, list) else []


def set_denylist(con: sqlite3.Connection, patterns: list[str]) -> None:
    kv_set(con, DENYLIST_KEY, json.dumps(sorted(set(patterns))))


def denylist_match(patterns: list[str], app: str | None,
                   title: str | None) -> bool:
    """True if any pattern is a case-insensitive substring of app or title.
    Empty pattern list never matches (allow-all default)."""
    if not patterns:
        return False
    hay = f"{app or ''}\n{title or ''}".lower()
    return any(p and p.lower() in hay for p in patterns)


# ---------------------------------------------------------------------------
# usage aggregation
# ---------------------------------------------------------------------------

def tag_usage(con: sqlite3.Connection, cap_s: float = 600,
              date_str: str | None = None) -> list[tuple[str, float]]:
    """Seconds per tag for one day (default: today). Each window event owns
    the time until the next window event (capped, exactly like app_usage);
    that time is credited to every tag on the event, so multi-tagged events
    count toward each tag."""
    if date_str:
        start, end = day_range(date_str)
    else:
        start, _ = today_range()
        end = time.time()
    rows = con.execute(
        "SELECT ts, tags FROM events WHERE type = 'window'"
        " AND ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
    usage: dict[str, float] = {}
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else end
        dur = min(max(0.0, nxt - r["ts"]), cap_s)
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                usage[t] = usage.get(t, 0.0) + dur
    return sorted(usage.items(), key=lambda kv: -kv[1])


def day_recap(con: sqlite3.Connection,
              date_str: str | None = None) -> dict:
    """Summary for one day. With date_str: exactly that date, no fallback.
    Without (dashboard home view): today's summary, else yesterday's
    (labeled). Empty dict when no summary exists — callers must not nag."""
    if date_str:
        row = con.execute("SELECT summary FROM days WHERE date = ?",
                          (date_str,)).fetchone()
        if row and row["summary"]:
            return {"label": "Recap", "date": date_str,
                    "summary": row["summary"]}
        return {}
    today = datetime.now().astimezone().date().isoformat()
    row = con.execute("SELECT summary FROM days WHERE date = ?",
                      (today,)).fetchone()
    if row and row["summary"]:
        return {"label": "Today's recap", "date": today,
                "summary": row["summary"]}
    yest = (datetime.now().astimezone().date()
            - timedelta(days=1)).isoformat()
    row = con.execute("SELECT summary FROM days WHERE date = ?",
                      (yest,)).fetchone()
    if row and row["summary"]:
        return {"label": "Yesterday's recap", "date": yest,
                "summary": row["summary"]}
    return {}


# ---------------------------------------------------------------------------
# notes (per-day handwritten notes, dashboard Notes tab)
# ---------------------------------------------------------------------------

def get_note(con: sqlite3.Connection, date_str: str) -> str | None:
    """The user's handwritten note for a day, or None when unwritten."""
    row = con.execute("SELECT body FROM notes WHERE date = ?",
                      (date_str,)).fetchone()
    if row and (row["body"] or "").strip():
        return row["body"]
    return None


def set_note(con: sqlite3.Connection, date_str: str, body: str | None) -> str:
    """Save a day's note. Empty/whitespace-only body deletes the row.
    Returns the stored body ("" when deleted)."""
    body = (body or "").strip()
    if not body:
        con.execute("DELETE FROM notes WHERE date = ?", (date_str,))
        con.commit()
        return ""
    con.execute(
        "INSERT INTO notes(date, body, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(date) DO UPDATE SET body = excluded.body,"
        " updated_at = excluded.updated_at",
        (date_str, body, int(time.time())),
    )
    con.commit()
    return body


# ---------------------------------------------------------------------------
# stickers (per-day marks, awarded manually — never auto-assigned)
# ---------------------------------------------------------------------------

# The full curated set. Gold star = the user's own "most productive day"
# ritual; the rest are for fun. Emoji render from system fonts — no assets.
STICKER_SET = ["⭐", "🦄", "🧜", "🚀", "🎨", "🔥", "💡", "🌙"]


def get_sticker(con: sqlite3.Connection, date_str: str) -> str | None:
    """The sticker marking a day, or None when unmarked."""
    row = con.execute("SELECT sticker FROM day_marks WHERE date = ?",
                      (date_str,)).fetchone()
    s = row["sticker"] if row else None
    return s if s in STICKER_SET else None


def set_sticker(con: sqlite3.Connection, date_str: str,
                sticker: str | None) -> str | None:
    """Award (or with None, clear) a day's sticker. Anything outside the
    curated set raises ValueError — the set is the product, not a text field."""
    if sticker is None:
        con.execute("DELETE FROM day_marks WHERE date = ?", (date_str,))
        con.commit()
        return None
    if sticker not in STICKER_SET:
        raise ValueError(f"not a curated sticker: {sticker!r}")
    con.execute(
        "INSERT INTO day_marks(date, sticker) VALUES (?, ?)"
        " ON CONFLICT(date) DO UPDATE SET sticker = excluded.sticker",
        (date_str, sticker),
    )
    con.commit()
    return sticker


# ---------------------------------------------------------------------------
# theme (Journal = default monotone paper; Diary = colorful playful skin)
# ---------------------------------------------------------------------------

THEME_KEY = "theme"
THEMES = ("journal", "diary")


def get_theme(con: sqlite3.Connection) -> str:
    """Dashboard theme. Defaults to 'journal'; unknown stored values fall
    back to it rather than breaking the page."""
    v = kv_get(con, THEME_KEY, "journal")
    return v if v in THEMES else "journal"


def set_theme(con: sqlite3.Connection, theme: str) -> str:
    """Persist the dashboard theme. Raises ValueError on unknown themes."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r} (choose: journal, diary)")
    kv_set(con, THEME_KEY, theme)
    return theme


# ---------------------------------------------------------------------------
# diary accent (color choice, diary mode only — journal stays monotone)
# ---------------------------------------------------------------------------

ACCENT_KEY = "accent"
# name -> hex. Journal never reads these; monotone is its identity.
ACCENTS = {
    "raspberry": "#d6457b",  # default
    "ocean": "#2563eb",
    "forest": "#16a34a",
    "plum": "#9333ea",
    "tangerine": "#ea580c",
}


def get_accent(con: sqlite3.Connection) -> str:
    """Diary accent color name. Defaults to 'raspberry'; unknown stored
    values fall back rather than breaking the page."""
    v = kv_get(con, ACCENT_KEY, "raspberry")
    return v if v in ACCENTS else "raspberry"


def set_accent(con: sqlite3.Connection, accent: str) -> str:
    """Persist the diary accent. Raises ValueError on unknown names."""
    if accent not in ACCENTS:
        raise ValueError(f"unknown accent: {accent!r} (choose: "
                         + ", ".join(ACCENTS))
    kv_set(con, ACCENT_KEY, accent)
    return accent


def remove_day_entry(con: sqlite3.Connection, date_str: str) -> bool:
    """Delete one day's summary row (exact date match — `week:` keys are
    never touched). Returns True when a row was actually removed."""
    cur = con.execute("DELETE FROM days WHERE date = ?", (date_str,))
    con.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------

def parse_purge_before(s: str) -> str:
    """'90d' -> YYYY-MM-DD 90 days ago; 'YYYY-MM-DD' validated as-is.
    Strict: rejects '90x', 'd', and malformed dates. Raises ValueError."""
    s = (s or "").strip()
    m = re.fullmatch(r"(\d+)d", s)
    if m:
        d = datetime.now().astimezone().date() - timedelta(days=int(m.group(1)))
        return d.isoformat()
    return date.fromisoformat(s).isoformat()  # raises ValueError


def _purge_cutoff_ts(date_str: str) -> float:
    """Start of the cutoff day, local time. Events at/after this are kept."""
    d = date.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day).astimezone().timestamp()


def purge_counts(con: sqlite3.Connection, before: str | None = None,
                 app: str | None = None) -> dict:
    """Dry-run counts for a purge: {'events': n, 'days': m}."""
    conds: list[str] = []
    params: list = []
    if before:
        conds.append("ts < ?")
        params.append(_purge_cutoff_ts(before))
    if app:
        conds.append("LOWER(app) LIKE '%' || LOWER(?) || '%'")
        params.append(app)
    where = " AND ".join(conds)
    n_events = con.execute(
        f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()[0]
    n_days = 0
    if before:
        # Daily keys compare directly; week: keys compare on the Monday date.
        # ISO dates sort lexicographically, so string comparison is exact.
        n_days = con.execute(
            "SELECT COUNT(*) FROM days WHERE"
            " (date NOT LIKE 'week:%' AND date < ?)"
            " OR (date LIKE 'week:%' AND substr(date, 6) < ?)",
            (before, before)).fetchone()[0]
    return {"events": n_events, "days": n_days}


def purge_data(con: sqlite3.Connection, before: str | None = None,
               app: str | None = None) -> dict:
    """Delete matching events (and old day summaries when --before is set).
    Returns the same counts purge_counts reported. Commits + VACUUMs."""
    result = purge_counts(con, before, app)
    conds: list[str] = []
    params: list = []
    if before:
        conds.append("ts < ?")
        params.append(_purge_cutoff_ts(before))
    if app:
        conds.append("LOWER(app) LIKE '%' || LOWER(?) || '%'")
        params.append(app)
    con.execute(f"DELETE FROM events WHERE {' AND '.join(conds)}", params)
    if before:
        con.execute(
            "DELETE FROM days WHERE"
            " (date NOT LIKE 'week:%' AND date < ?)"
            " OR (date LIKE 'week:%' AND substr(date, 6) < ?)",
            (before, before))
    con.commit()
    con.execute("VACUUM")
    return result
