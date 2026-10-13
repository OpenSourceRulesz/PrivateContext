"""Data liberation: import history from browsers and other recorders.

All importers share the same contract:
  - Source databases are ALWAYS copied to a temp file first (browsers lock
    the original while running).
  - Dedup watermarks live in the target `kv` table as
    `import:<source>:<id>` = max imported timestamp (or row id). Re-running
    an import only picks up newer rows.
  - `--dry-run` prints counts + sample rows and changes nothing.
  - Keystroke *contents* are never imported. If a source appears to hold
    keystroke text, the import is refused outright.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, init_db, log_event, strip_url

# Seconds between the Chrome epoch (1601-01-01) and the Unix epoch.
CHROME_EPOCH_OFFSET = 11644473600


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------

class RefusedError(Exception):
    """Raised when a source looks like it contains keystroke contents."""


KEYSTROKE_TABLE_RE = re.compile(r"keystroke|key_?log|typed_text|key_press", re.I)
KEYSTROKE_COL_RE = re.compile(r"^(keystroke|keystrokes|key_text|typed_text|key_content)$", re.I)


def refuse_keystrokes(sdb: sqlite3.Connection, label: str) -> None:
    """Refuse to import from a DB that appears to hold keystroke contents."""
    tables = [r[0] for r in sdb.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    for t in tables:
        if KEYSTROKE_TABLE_RE.search(t):
            raise RefusedError(
                f"refusing to import {label}: table '{t}' looks like it holds "
                "keystroke contents. PrivateContext never imports keystrokes.")
        cols = [r[1] for r in sdb.execute(f'PRAGMA table_info("{t}")')]
        for c in cols:
            if KEYSTROKE_COL_RE.match(c or ""):
                raise RefusedError(
                    f"refusing to import {label}: column '{t}.{c}' looks like "
                    "keystroke contents. PrivateContext never imports keystrokes.")


def copy_source(path: str) -> str:
    """Copy a (possibly locked) source DB to a temp file. Caller deletes it."""
    tmp = tempfile.NamedTemporaryFile(prefix="pctx-import-", suffix=".db",
                                      delete=False)
    tmp.close()
    shutil.copy(path, tmp.name)
    return tmp.name


def _wm_key(source: str, sid: str) -> str:
    return f"import:{source}:{sid}"


def get_watermark(con: sqlite3.Connection, source: str, sid: str) -> float | None:
    row = con.execute("SELECT value FROM kv WHERE key = ?",
                      (_wm_key(source, sid),)).fetchone()
    if not row:
        return None
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return None


def set_watermark(con: sqlite3.Connection, source: str, sid: str, ts: float) -> None:
    con.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_wm_key(source, sid), str(ts)))


def _parse_ts(v) -> float | None:
    """Best-effort timestamp -> unix seconds. Handles unix s/ms, ISO strings."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f > 1e14:      # microseconds
            return f / 1e6
        if f > 1e11:      # milliseconds
            return f / 1e3
        return f
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _report(label: str, imported: int, skipped: int,
            lo: float | None, hi: float | None,
            samples: list[dict], dry_run: bool) -> None:
    mode = "DRY RUN — " if dry_run else ""
    print(f"[privatecontext] {mode}{label}: imported {imported}, skipped {skipped}")
    if lo is not None and hi is not None:
        lo_s = datetime.fromtimestamp(lo).strftime("%Y-%m-%d")
        hi_s = datetime.fromtimestamp(hi).strftime("%Y-%m-%d")
        print(f"  date range covered: {lo_s} .. {hi_s}")
    for s in samples[:5]:
        ts = datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d %H:%M")
        extra = f" {strip_url(s['url'])}" if s.get("url") else ""
        print(f"  {ts}  {s['type']:10} {s.get('app') or ''} — "
              f"{(s.get('title') or '')[:70]}{extra}")


# --------------------------------------------------------------------------
# browser history
# --------------------------------------------------------------------------

def _chromium_history_candidates(vendor: str) -> list[Path]:
    """Default History paths for Chrome/Edge. First hit wins."""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        base = local / vendor / "User Data"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support" / vendor
    else:
        slug = {"Google": "google-chrome", "Microsoft Edge": "microsoft-edge"}[vendor]
        base = home / ".config" / slug
    return [base / "Default" / "History",
            base / "Profile 1" / "History"]


def _firefox_places_candidates() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        roots = [Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"]
    elif sys.platform == "darwin":
        roots = [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    else:
        roots = [home / ".mozilla" / "firefox"]
    out: list[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend(sorted(r.glob("*.default*")))
    return out


def _browser_source(browser: str, profile: str | None) -> tuple[Path, list[str]]:
    """Resolve the history DB path. Returns (path, searched_locations)."""
    searched: list[str] = []
    if profile:
        p = Path(profile)
        cands = [p, p / "History", p / "places.sqlite"]
        for c in cands:
            searched.append(str(c))
            if c.is_file():
                return c, searched
        return None, searched
    if browser in ("chrome", "edge"):
        vendor = "Google" if browser == "chrome" else "Microsoft Edge"
        cands = _chromium_history_candidates(vendor)
    else:
        cands = [d / "places.sqlite" for d in _firefox_places_candidates()]
    for c in cands:
        searched.append(str(c))
        if c.is_file():
            return c, searched
    return None, searched


def import_browser(con: sqlite3.Connection, browser: str,
                   profile: str | None = None, dry_run: bool = False) -> int:
    app = {"chrome": "Chrome", "edge": "Edge", "firefox": "Firefox"}[browser]
    src, searched = _browser_source(browser, profile)
    if src is None:
        print(f"could not find {browser} history. I looked in:",
              file=sys.stderr)
        for s in searched:
            print(f"  {s}", file=sys.stderr)
        print("pass --profile PATH to point at the History / places.sqlite file directly.",
              file=sys.stderr)
        return 1
    sid = f"{browser}:{src}"
    wm = get_watermark(con, "browser", sid)

    tmp = copy_source(str(src))
    try:
        sdb = sqlite3.connect(tmp)
        sdb.row_factory = sqlite3.Row
        refuse_keystrokes(sdb, f"{browser} history")
        rows: list[dict] = []
        if browser in ("chrome", "edge"):
            # last_visit_time: microseconds since 1601-01-01 UTC.
            floor = int((wm + CHROME_EPOCH_OFFSET) * 1e6) if wm else 0
            q = ("SELECT url, title, last_visit_time FROM urls"
                 " WHERE last_visit_time > ? ORDER BY last_visit_time")
            for r in sdb.execute(q, (floor,)):
                ts = r["last_visit_time"] / 1e6 - CHROME_EPOCH_OFFSET
                rows.append({"ts": ts, "type": "url", "app": app,
                             "title": r["title"], "url": r["url"]})
        else:
            # last_visit_date: microseconds since the Unix epoch.
            floor = int(wm * 1e6) if wm else 0
            q = ("SELECT url, title, last_visit_date FROM moz_places"
                 " WHERE last_visit_date IS NOT NULL AND last_visit_date > ?"
                 " ORDER BY last_visit_date")
            for r in sdb.execute(q, (floor,)):
                rows.append({"ts": r["last_visit_date"] / 1e6, "type": "url",
                             "app": app, "title": r["title"], "url": r["url"]})
        sdb.close()
    finally:
        os.unlink(tmp)

    return _store(con, "browser", sid, rows, dry_run,
                  label=f"browser history ({app})")


def _store(con: sqlite3.Connection, source: str, sid: str,
           rows: list[dict], dry_run: bool, label: str) -> int:
    lo = min((r["ts"] for r in rows), default=None)
    hi = max((r["ts"] for r in rows), default=None)
    _report(label, len(rows), 0, lo, hi, rows, dry_run)
    if dry_run or not rows:
        return 0
    for r in rows:
        log_event(con, r["type"], ts=r["ts"], app=r.get("app"),
                  title=r.get("title"), url=r.get("url"),
                  duration_ms=r.get("duration_ms"), commit=False)
    con.commit()
    set_watermark(con, source, sid, hi)
    con.commit()
    return 0


# --------------------------------------------------------------------------
# ActivityWatch
# --------------------------------------------------------------------------

def import_activitywatch(con: sqlite3.Connection, path: str,
                         dry_run: bool = False) -> int:
    src = Path(path)
    if not src.is_file():
        print(f"no file at {path}", file=sys.stderr)
        return 1
    tmp = copy_source(str(src))
    imported = 0
    skipped = 0
    samples: list[dict] = []
    lo = hi = None
    try:
        sdb = sqlite3.connect(tmp)
        sdb.row_factory = sqlite3.Row
        refuse_keystrokes(sdb, "ActivityWatch database")
        tables = {r[0] for r in sdb.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"buckets", "events"} <= tables:
            print(f"{path} doesn't look like an ActivityWatch database "
                  f"(tables found: {sorted(tables)})", file=sys.stderr)
            return 1
        buckets = sdb.execute("SELECT id, name, type FROM buckets").fetchall()
        for b in buckets:
            bname = (b["name"] or "").lower()
            btype = (b["type"] or "").lower()
            wm = get_watermark(con, "activitywatch", str(b["id"]))
            q = ("SELECT id, timestamp, duration, datastr FROM events"
                 " WHERE bucketrow = ? AND id > ? ORDER BY id")
            for ev in sdb.execute(q, (b["id"], int(wm) if wm else 0)):
                try:
                    data = json.loads(ev["datastr"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    skipped += 1
                    continue
                ts = _parse_ts(ev["timestamp"])
                if ts is None:
                    skipped += 1
                    continue
                row = _aw_event_to_row(bname, btype, data, ts, ev["duration"])
                if row is None:
                    skipped += 1
                    continue
                imported += 1
                if len(samples) < 5:
                    samples.append(row)
                lo = ts if lo is None else min(lo, ts)
                hi = ts if hi is None else max(hi, ts)
                if not dry_run:
                    log_event(con, row["type"], ts=row["ts"], app=row.get("app"),
                              title=row.get("title"), url=row.get("url"),
                              duration_ms=row.get("duration_ms"), commit=False)
            if not dry_run:
                mx = sdb.execute(
                    "SELECT MAX(id) FROM events WHERE bucketrow = ?",
                    (b["id"],)).fetchone()[0]
                if mx:
                    set_watermark(con, "activitywatch", str(b["id"]), float(mx))
        sdb.close()
    finally:
        os.unlink(tmp)
    if not dry_run:
        con.commit()
    _report("ActivityWatch import", imported, skipped, lo, hi, samples, dry_run)
    return 0


def _aw_event_to_row(bname: str, btype: str, data: dict,
                     ts: float, duration) -> dict | None:
    url = data.get("url")
    if url:
        return {"ts": ts, "type": "url", "app": data.get("app"),
                "title": data.get("title"), "url": url}
    if "afk" in bname or "afk" in btype:
        status = (data.get("status") or "").lower()
        if status == "afk":
            return {"ts": ts, "type": "afk", "app": "system", "title": "away"}
        if status == "not-afk":
            dur_ms = int(float(duration) * 1000) if duration else None
            return {"ts": ts, "type": "back", "app": "system",
                    "title": "back", "duration_ms": dur_ms}
        return None  # unknown AFK payload — skip, don't guess
    app, title = data.get("app"), data.get("title")
    if not app and not title:
        return None
    dur_ms = None
    try:
        dur_ms = int(float(duration) * 1000) if duration else None
    except (TypeError, ValueError):
        pass
    return {"ts": ts, "type": "window", "app": app, "title": title,
            "duration_ms": dur_ms}


# --------------------------------------------------------------------------
# Screenpipe
# --------------------------------------------------------------------------

BLOB_TABLE_RE = re.compile(r"video_chunks|audio_chunks", re.I)


def import_screenpipe(con: sqlite3.Connection, path: str,
                      dry_run: bool = False) -> int:
    src = Path(path)
    if not src.is_file():
        print(f"no file at {path}", file=sys.stderr)
        return 1
    tmp = copy_source(str(src))
    rows: list[dict] = []
    skipped = 0
    notes: list[str] = []
    try:
        sdb = sqlite3.connect(tmp)
        sdb.row_factory = sqlite3.Row
        refuse_keystrokes(sdb, "Screenpipe database")
        tables = [r[0] for r in sdb.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        blob_tables = [t for t in tables if BLOB_TABLE_RE.search(t)]
        if blob_tables:
            notes.append("skipped binary media tables "
                         f"({', '.join(blob_tables)}): video/audio blobs are "
                         "the heaviness we're escaping — text only.")

        # Map frame_id -> app name from any frames-like table.
        frame_apps: dict = {}
        for t in tables:
            if re.search(r"^frames?$", t, re.I):
                cols = {r[1] for r in sdb.execute(f'PRAGMA table_info("{t}")')}
                id_c = "id" if "id" in cols else None
                app_c = next((c for c in ("app_name", "app", "application")
                              if c in cols), None)
                if id_c and app_c:
                    for r in sdb.execute(f'SELECT "{id_c}", "{app_c}" FROM "{t}"'):
                        frame_apps[r[0]] = r[1]

        for t in tables:
            if BLOB_TABLE_RE.search(t):
                continue
            cols = [r[1] for r in sdb.execute(f'PRAGMA table_info("{t}")')]
            low = t.lower()
            if "ocr" in low:
                kind, rtype = "ocr", "ocr"
            elif "transcript" in low:
                kind, rtype = "transcript", "transcript"
            else:
                continue
            text_c = next((c for c in ("text", "content", "ocr_text",
                                       "transcription")
                           if c in cols), None)
            ts_c = next((c for c in ("timestamp", "created_at", "time",
                                     "offset_index")
                         if c in cols), None)
            id_c = next((c for c in ("id", "rowid") if c in cols), None)
            frame_c = next((c for c in ("frame_id", "frameId") if c in cols),
                           None)
            if not text_c or not ts_c:
                notes.append(f"skipped table '{t}': no recognizable "
                             "text/timestamp columns.")
                skipped += 1
                continue
            wm = get_watermark(con, "screenpipe", t)
            order = f'"{id_c}"' if id_c and id_c != "rowid" else "rowid"
            q = f'SELECT rowid, "{text_c}", "{ts_c}"'
            if frame_c:
                q += f', "{frame_c}"'
            q += f' FROM "{t}" ORDER BY {order}'
            for r in sdb.execute(q):
                rid = r[0]
                if wm is not None and rid <= wm:
                    continue
                ts = _parse_ts(r[2])
                text = (r[1] or "").strip()
                if ts is None or not text:
                    skipped += 1
                    continue
                app = frame_apps.get(r[3], "screenpipe") if frame_c else "screenpipe"
                rows.append({"ts": ts, "type": rtype, "app": app,
                             "title": text[:160], "_rid": rid, "_table": t})
        # Per-table watermarks = max rowid imported.
        by_table: dict[str, int] = {}
        for r in rows:
            by_table[r["_table"]] = max(by_table.get(r["_table"], 0), r["_rid"])
        sdb.close()
    finally:
        os.unlink(tmp)

    if not rows and not notes:
        print("no OCR or transcription tables found in "
              f"{path} (tables: {', '.join(tables)}). "
              "Nothing imported.", file=sys.stderr)
        return 1

    lo = min((r["ts"] for r in rows), default=None)
    hi = max((r["ts"] for r in rows), default=None)
    skipped_total = skipped
    _report("Screenpipe import", len(rows), skipped_total, lo, hi, rows, dry_run)
    for n in notes:
        print(f"  note: {n}")
    if dry_run or not rows:
        return 0
    for r in rows:
        log_event(con, r["type"], ts=r["ts"], app=r["app"],
                  title=r["title"], commit=False)
    con.commit()
    for t, mx in by_table.items():
        set_watermark(con, "screenpipe", t, float(mx))
    con.commit()
    return 0


# --------------------------------------------------------------------------
# generic CSV / JSON (for local exports from any other tool)
# --------------------------------------------------------------------------

# Accepted header/key names (case-insensitive) for generic imports.
_FIELD_ALIASES = {
    "ts": {"ts", "timestamp", "time", "datetime", "date", "when"},
    "type": {"type", "event", "event_type", "kind"},
    "app": {"app", "application", "program", "source", "browser"},
    "title": {"title", "name", "subject", "text", "window", "page"},
    "url": {"url", "link", "href"},
}


def _normalize_record(raw: dict) -> dict | None:
    """Map an arbitrary dict to an event row. Returns None if unusable."""
    lowered = {(k or "").strip().lower(): v for k, v in raw.items()}
    for k in lowered:
        if KEYSTROKE_COL_RE.match(k):
            raise RefusedError(
                f"refusing generic import: field '{k}' looks like keystroke "
                "contents. PrivateContext never imports keystrokes.")
    get = {}
    for field, aliases in _FIELD_ALIASES.items():
        for a in aliases:
            if a in lowered and lowered[a] not in (None, ""):
                get[field] = lowered[a]
                break
    ts = _parse_ts(get.get("ts"))
    if ts is None:
        return None
    etype = str(get.get("type") or "").strip().lower() or None
    if etype not in ("window", "url", "click", "afk", "back", "ocr",
                     "transcript"):
        etype = "url" if get.get("url") else "window"
    return {"ts": ts, "type": etype, "app": get.get("app"),
            "title": get.get("title"), "url": get.get("url")}


def _import_records(con: sqlite3.Connection, source: str, sid: str,
                    raws: list[dict], label: str, dry_run: bool) -> int:
    wm = get_watermark(con, source, sid)
    rows: list[dict] = []
    skipped = 0
    for raw in raws:
        try:
            row = _normalize_record(raw)
        except RefusedError:
            raise
        if row is None:
            skipped += 1
            continue
        if wm is not None and row["ts"] <= wm:
            continue
        rows.append(row)
    lo = min((r["ts"] for r in rows), default=None)
    hi = max((r["ts"] for r in rows), default=None)
    _report(label, len(rows), skipped, lo, hi, rows, dry_run)
    if dry_run or not rows:
        return 0
    for r in rows:
        log_event(con, r["type"], ts=r["ts"], app=r.get("app"),
                  title=r.get("title"), url=r.get("url"), commit=False)
    con.commit()
    set_watermark(con, source, sid, hi)
    con.commit()
    return 0


def import_csv(con: sqlite3.Connection, path: str,
               dry_run: bool = False) -> int:
    src = Path(path)
    if not src.is_file():
        print(f"no file at {path}", file=sys.stderr)
        return 1
    with open(src, newline="", encoding="utf-8-sig") as f:
        raws = list(csv.DictReader(f))
    if not raws:
        print(f"{path}: no data rows found.", file=sys.stderr)
        return 1
    return _import_records(con, "csv", str(src), raws,
                           f"CSV import ({src.name})", dry_run)


def import_json(con: sqlite3.Connection, path: str,
                dry_run: bool = False) -> int:
    src = Path(path)
    if not src.is_file():
        print(f"no file at {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"{path}: not valid JSON ({e})", file=sys.stderr)
        return 1
    if isinstance(data, dict):
        # allow {"events": [...]} style wrappers
        for key in ("events", "rows", "data", "history"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        print(f"{path}: expected a JSON array of event objects.",
              file=sys.stderr)
        return 1
    raws = [r for r in data if isinstance(r, dict)]
    return _import_records(con, "json", str(src), raws,
                           f"JSON import ({src.name})", dry_run)
