"""pctx — command line interface for PrivateContext."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .autostart import install as autostart_install
from .autostart import status as autostart_status
from .autostart import uninstall as autostart_uninstall
from .capture import run_capture, simulate
from .dashboard import serve
from .db import (connect, counts, default_db_path, get_denylist, init_db,
                 parse_purge_before, purge_counts, purge_data, set_denylist)
from .importers import (RefusedError, import_activitywatch, import_browser,
                        import_csv, import_json, import_screenpipe)
from .summarize import DEFAULT_MODEL, summarize_day, summarize_week
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


def _fmt_dur_md(secs: float) -> str:
    secs = round(secs)
    if secs >= 3600:
        return f"{secs // 3600}h {round((secs % 3600) / 60)}m"
    if secs >= 60:
        return f"{round(secs / 60)}m"
    return "<1m"


def cmd_export_markdown(args: argparse.Namespace) -> int:
    """One readable .md per day: header, sticker, summary, notes, tags,
    top apps. Days with no data at all are skipped."""
    from datetime import datetime as _dt
    from .db import app_usage, away_usage, get_note, get_sticker

    def _parse(s: str | None, name: str):
        if s is None:
            return None
        try:
            return _dt.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            print(f"bad --{name} '{s}' — use YYYY-MM-DD", file=sys.stderr)
            return False

    lo = _parse(args.from_date, "from")
    hi = _parse(args.to_date, "to")
    if lo is False or hi is False:
        return 2
    db = _resolve_db(args.db) if args.db else default_db_path()
    if not Path(db).exists():
        print(f"no database at {db} — run `pctx capture` first",
              file=sys.stderr)
        return 1
    con = connect(db)
    init_db(con)
    ev_dates = {r[0] for r in con.execute(
        "SELECT DISTINCT date(datetime(ts, 'unixepoch', 'localtime'))"
        " FROM events")}
    all_dates = (ev_dates
                 | {r["date"] for r in con.execute(
                     "SELECT date FROM days WHERE date NOT LIKE 'week:%'")}
                 | {r["date"] for r in con.execute("SELECT date FROM notes")}
                 | {r["date"] for r in con.execute("SELECT date FROM day_marks")})
    if not all_dates:
        print("[privatecontext] nothing to export — no data yet.",
              file=sys.stderr)
        return 1
    lo = lo or min(all_dates)
    hi = hi or max(all_dates)
    if lo > hi:
        print(f"bad range: --from {lo} is after --to {hi}", file=sys.stderr)
        return 2
    out_dir = Path(os.path.abspath(
        args.out or "./privatecontext-export/"))
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for ds in sorted(all_dates):
        if not (str(lo) <= ds <= str(hi)):
            continue
        d = _dt.strptime(ds, "%Y-%m-%d").date()
        sticker = get_sticker(con, ds)
        note = get_note(con, ds)
        srow = con.execute("SELECT summary, tags FROM days WHERE date = ?",
                           (ds,)).fetchone()
        summary = (srow["summary"] or "").strip() if srow else ""
        tags = (srow["tags"] or "").strip() if srow else ""
        n_events = con.execute(
            "SELECT COUNT(*) FROM events WHERE"
            " date(datetime(ts, 'unixepoch', 'localtime')) = ?",
            (ds,)).fetchone()[0]
        if not (n_events or summary or note or sticker):
            continue  # no data at all — skip the day
        # Cross-platform unpadded day ("October 16", not "October 05").
        parts = [f"# {d.strftime('%A, %B')} {d.day}, {d.year}"]
        if sticker:
            parts.append(f"\n{sticker}\n")
        if summary:
            parts.append(f"## Summary\n\n{summary}\n")
        if note:
            parts.append(f"## Notes\n\n{note}\n")
        if tags:
            parts.append("Tags: " + ", ".join(f"#{t.strip()}"
                                             for t in tags.split(",")
                                             if t.strip()) + "\n")
        apps = [(a, s) for a, s in app_usage(con, date_str=ds,
                                             exclude_away=True)][:8]
        if apps:
            lines = [f"- {a} — {_fmt_dur_md(s)}" for a, s in apps]
            away = away_usage(con, date_str=ds)
            if away >= 60:
                lines.append(f"- Away — {_fmt_dur_md(away)}")
            parts.append("## Where the day went\n\n" + "\n".join(lines) + "\n")
        (out_dir / f"{ds}.md").write_text("\n".join(parts).strip() + "\n",
                                          encoding="utf-8")
        n += 1
    print(f"[privatecontext] wrote {n} day file(s) -> {out_dir}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    if args.markdown:
        return cmd_export_markdown(args)
    if not args.scrubbed:
        print("export needs a mode: `pctx export --scrubbed` (database copy)"
              " or `pctx export --markdown` (readable .md files per day)",
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
    date_str = args.date or _date.today().isoformat()
    if args.week:
        return summarize_week(db, date_str,
                               model=args.model, dry_run=args.dry_run)
    return summarize_day(db, date_str,
                         model=args.model, dry_run=args.dry_run)


def cmd_purge(args: argparse.Namespace) -> int:
    if not args.before and not args.app:
        print("purge needs --before and/or --app:\n"
              "  pctx purge --before 90d [--yes]\n"
              "  pctx purge --app <name> [--yes]",
              file=sys.stderr)
        return 2
    before = None
    if args.before:
        try:
            before = parse_purge_before(args.before)
        except ValueError:
            print(f"bad --before '{args.before}' — use Nd (e.g. 90d)"
                  f" or YYYY-MM-DD", file=sys.stderr)
            return 2
    db = _resolve_db(args.db) if args.db else default_db_path()
    con = connect(db)
    init_db(con)
    counts = purge_counts(con, before, args.app)
    scope = " and ".join(
        ([f"before {before}"] if before else [])
        + ([f"app matching '{args.app}'"] if args.app else []))
    days_bit = (f" and {counts['days']} day summaries"
                if counts["days"] else "")
    if not args.yes:
        print(f"[privatecontext] dry run — would delete "
              f"{counts['events']} events{days_bit} ({scope}). "
              f"Re-run with --yes to delete.")
        return 0
    purge_data(con, before, args.app)
    print(f"[privatecontext] deleted {counts['events']} events{days_bit} "
          f"({scope}).")
    return 0


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
    e.add_argument("--markdown", action="store_true",
                   help="write one readable .md per day instead of a .db copy")
    e.add_argument("--from", dest="from_date", default=None, metavar="DATE",
                   help="first day YYYY-MM-DD (markdown only)")
    e.add_argument("--to", dest="to_date", default=None, metavar="DATE",
                   help="last day YYYY-MM-DD (markdown only)")
    e.add_argument("--out", default=None,
                   help="output .db path (--scrubbed) or directory (--markdown)")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("summarize", help="summarize a day (or week) with a local Ollama model")
    s.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    s.add_argument("--week", action="store_true",
                   help="summarize the trailing 7 days ending at --date")
    s.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Ollama model (default: {DEFAULT_MODEL})")
    s.add_argument("--dry-run", action="store_true",
                   help="print the condensed digest without calling Ollama")
    s.set_defaults(func=cmd_summarize)

    p = sub.add_parser("purge", help="delete old or app-specific captured data")
    p.add_argument("--before", default=None,
                   help="'Nd' (e.g. 90d) or YYYY-MM-DD: delete data older than this")
    p.add_argument("--app", default=None,
                   help="delete events for apps matching this (case-insensitive substring)")
    p.add_argument("--yes", action="store_true",
                   help="actually delete — without it, prints what would be deleted")
    p.set_defaults(func=cmd_purge)

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

    a = sub.add_parser("autostart", help="run capture at login")
    a.add_argument("action", choices=["install", "uninstall", "status"],
                   help="install, remove, or check the login autostart")
    a.set_defaults(func=cmd_autostart)

    dl = sub.add_parser("denylist", help="exclude apps/windows from capture")
    dl.add_argument("action", choices=["add", "remove", "list", "clear"],
                    help="manage denylist patterns (case-insensitive substrings)")
    dl.add_argument("pattern", nargs="?", default=None,
                    help="pattern for add/remove")
    dl.set_defaults(func=cmd_denylist)

    su = sub.add_parser("setup", help="first-run wizard")
    su.add_argument("--yes", action="store_true",
                    help="accept defaults non-interactively")
    su.set_defaults(func=cmd_setup)
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


def cmd_autostart(args: argparse.Namespace) -> int:
    if args.action == "install":
        ok, msg = autostart_install()
    elif args.action == "uninstall":
        ok, msg = autostart_uninstall()
    else:
        ok, msg = autostart_status()
    print(f"[privatecontext] {msg}")
    return 0 if ok else 1


def cmd_denylist(args: argparse.Namespace) -> int:
    db = _resolve_db(args.db) if args.db else default_db_path()
    con = connect(db)
    init_db(con)
    patterns = get_denylist(con)
    if args.action == "list":
        if patterns:
            print("[privatecontext] denylist patterns:")
            for p in patterns:
                print(f"  {p}")
        else:
            print("[privatecontext] denylist is empty — everything is captured.")
        return 0
    if args.action == "clear":
        set_denylist(con, [])
        print(f"[privatecontext] denylist cleared ({len(patterns)} removed).")
        return 0
    if not args.pattern:
        print("denylist add/remove needs a pattern", file=sys.stderr)
        return 2
    if args.action == "add":
        if args.pattern in patterns:
            print(f"[privatecontext] '{args.pattern}' is already denied.")
        else:
            set_denylist(con, patterns + [args.pattern])
            print(f"[privatecontext] denied '{args.pattern}' "
                  f"({len(patterns) + 1} pattern(s)). Restart capture to apply.")
        return 0
    # remove
    if args.pattern in patterns:
        set_denylist(con, [p for p in patterns if p != args.pattern])
        print(f"[privatecontext] '{args.pattern}' is captured again.")
    else:
        print(f"[privatecontext] '{args.pattern}' was not on the denylist.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup import run_setup
    db = _resolve_db(args.db) if args.db else None
    return run_setup(yes=args.yes, db_path=db)


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
