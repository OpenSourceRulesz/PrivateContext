"""pctx — command line interface for PrivateContext."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .capture import run_capture, simulate
from .dashboard import serve
from .db import connect, counts, default_db_path, init_db
from .importers import (RefusedError, import_activitywatch, import_browser,
                        import_csv, import_json, import_screenpipe)
from .summarize import DEFAULT_MODEL, summarize_day
from .tagging import extract_tags


def _resolve_db(path: str) -> str:
    return os.path.abspath(path)


def cmd_capture(args: argparse.Namespace) -> int:
    db = _resolve_db(args.db) if args.db else None
    if args.simulate:
        simulate(db)
    else:
        run_capture(db, screenshots=args.screenshots)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    db = _resolve_db(args.db) if args.db else None
    serve(db, port=args.port)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    if not args.scrubbed:
        print("v0 only supports scrubbed exports: `pctx export --scrubbed`",
              file=sys.stderr)
        return 2
    src_path = _resolve_db(args.db) if args.db else default_db_path()
    if not Path(src_path).exists():
        print(f"no database at {src_path} — run `pctx capture` first",
              file=sys.stderr)
        return 1
    out = args.out or f"./privatecontext-export-{date.today().isoformat()}.db"
    out_path = os.path.abspath(out)
    if Path(out_path).exists():
        os.remove(out_path)

    # Build the export through the real schema so FTS triggers fire on copy.
    dst = sqlite3.connect(out_path)
    schema = Path(__file__).resolve().parent / "schema.sql"
    if not schema.exists():  # running from a source checkout
        schema = Path(__file__).resolve().parent.parent / "schema.sql"
    dst.executescript(schema.read_text())
    # Quote the path for ATTACH: sqlite string literal, ''-escaped.
    lit = "'" + src_path.replace("'", "''") + "'"
    dst.execute(f"ATTACH DATABASE {lit} AS src")
    # Column-explicit copy so pre-tags databases still export cleanly.
    src_cols = [r[1] for r in dst.execute("PRAGMA src.table_info(events)")]
    cols = ["ts", "type", "app", "title", "url", "x", "y", "duration_ms"]
    if "tags" in src_cols:
        cols.append("tags")
    col_list = ",".join(cols)
    dst.execute(f"INSERT INTO events({col_list}) SELECT {col_list} FROM src.events"
                " WHERE type != 'shot'")
    dst.execute("INSERT INTO days SELECT * FROM src.days")
    dst.execute("INSERT INTO kv SELECT * FROM src.kv")
    dst.commit()
    n = dst.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    dst.close()
    print(f"[privatecontext] exported {n} scrubbed events -> {out_path}")
    print("screenshots and 'shot' events were excluded.")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    from datetime import date as _date
    db = _resolve_db(args.db) if args.db else None
    return summarize_day(db, args.date or _date.today().isoformat(),
                         model=args.model, dry_run=args.dry_run)


def cmd_mcp(args: argparse.Namespace) -> int:
    try:
        from .mcp_server import main as mcp_main
    except ImportError:
        print("the 'mcp' package is required for `pctx mcp`:\n"
              "  pip install 'privatecontext[mcp]'  (or: pip install mcp)",
              file=sys.stderr)
        return 1
    db = _resolve_db(args.db) if args.db else None
    return mcp_main(db)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import main as doctor_main
    return doctor_main()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pctx",
        description=f"PrivateContext v{__version__} — lightweight local activity tracker.")
    ap.add_argument("--db", default=None,
                    help=f"database path (default: {default_db_path()})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="run the event capture loop")
    c.add_argument("--simulate", action="store_true",
                   help="write a synthetic workday instead of watching the OS")
    c.add_argument("--screenshots", action="store_true",
                   help="capture downscaled screenshots on app/URL switch (OFF by default)")
    c.set_defaults(func=cmd_capture)

    d = sub.add_parser("dashboard", help="serve the local dashboard")
    d.add_argument("--port", type=int, default=18790)
    d.set_defaults(func=cmd_dashboard)

    e = sub.add_parser("export", help="export a shareable copy of the database")
    e.add_argument("--scrubbed", action="store_true",
                   help="exclude screenshots and shot events (required in v0)")
    e.add_argument("--out", default=None, help="output .db path")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("summarize", help="summarize a day with a local Ollama model")
    s.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    s.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Ollama model (default: {DEFAULT_MODEL})")
    s.add_argument("--dry-run", action="store_true",
                   help="print the condensed digest without calling Ollama")
    s.set_defaults(func=cmd_summarize)

    m = sub.add_parser("mcp", help="run the MCP server over stdio")
    m.set_defaults(func=cmd_mcp)

    d = sub.add_parser("doctor", help="report what works on this machine")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("retag", help="backfill auto-tags on untagged events")
    r.add_argument("--since", default=None,
                   help="only retag events on/after YYYY-MM-DD")
    r.set_defaults(func=cmd_retag)

    i = sub.add_parser("import", help="import history from browsers or other recorders")
    i.add_argument("--browser", choices=["chrome", "edge", "firefox"], default=None,
                   help="import this browser's history")
    i.add_argument("--profile", default=None,
                   help="path to the browser profile History / places.sqlite file")
    i.add_argument("--activitywatch", default=None, metavar="PATH",
                   help="path to an ActivityWatch SQLite database")
    i.add_argument("--screenpipe", default=None, metavar="PATH",
                   help="path to a Screenpipe SQLite database")
    i.add_argument("--csv", default=None, metavar="PATH",
                   help="path to a CSV export (columns: timestamp, type, "
                        "app, title, url)")
    i.add_argument("--json", default=None, metavar="PATH",
                   help="path to a JSON export (array of event objects)")
    i.add_argument("--dry-run", action="store_true",
                   help="print what would be imported without writing anything")
    i.set_defaults(func=cmd_import)
    return ap


def cmd_import(args: argparse.Namespace) -> int:
    sources = [s for s in (args.browser, args.activitywatch, args.screenpipe,
                           args.csv, args.json) if s]
    if len(sources) != 1:
        print("pick exactly one source: --browser chrome|edge|firefox, "
              "--activitywatch PATH, --screenpipe PATH, --csv PATH, or "
              "--json PATH", file=sys.stderr)
        return 2
    db = _resolve_db(args.db) if args.db else default_db_path()
    con = connect(db)
    init_db(con)
    try:
        if args.browser:
            return import_browser(con, args.browser, profile=args.profile,
                                  dry_run=args.dry_run)
        if args.activitywatch:
            return import_activitywatch(con, args.activitywatch,
                                        dry_run=args.dry_run)
        if args.screenpipe:
            return import_screenpipe(con, args.screenpipe, dry_run=args.dry_run)
        if args.csv:
            return import_csv(con, args.csv, dry_run=args.dry_run)
        return import_json(con, args.json, dry_run=args.dry_run)
    except RefusedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def cmd_retag(args: argparse.Namespace) -> int:
    from datetime import datetime as _dt
    db = _resolve_db(args.db) if args.db else default_db_path()
    if not Path(db).exists():
        print(f"no database at {db} — run `pctx capture` first", file=sys.stderr)
        return 1
    con = connect(db)
    init_db(con)
    q = ("SELECT id, app, title, url FROM events"
         " WHERE (tags IS NULL OR tags = '')")
    params: list = []
    if args.since:
        try:
            since_ts = _dt.strptime(args.since, "%Y-%m-%d").astimezone().timestamp()
        except ValueError:
            print("--since must be YYYY-MM-DD", file=sys.stderr)
            return 2
        q += " AND ts >= ?"
        params.append(since_ts)
    rows = con.execute(q, params).fetchall()
    tagged = 0
    for r in rows:
        found = extract_tags(r["app"], r["title"], r["url"])
        tag_str = ",".join(found) if found else None
        con.execute("UPDATE events SET tags = ? WHERE id = ?", (tag_str, r["id"]))
        if tag_str:
            tagged += 1
    con.commit()
    print(f"[privatecontext] retagged {tagged} of {len(rows)} scanned events")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
