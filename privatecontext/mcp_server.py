"""First-party MCP server for PrivateContext (stdio transport).

Exposes the local activity diary to AI assistants (Claude Desktop, Cursor,
Claude Code, …). Every tool returns condensed, human-readable text — small
models get summaries, never raw event dumps. Condensed text ONLY: top-N
aggregates, never event firehoses, never anything screenshot-like.

Run with:  pctx mcp   (needs the [mcp] extra: pip install "privatecontext[mcp]")
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from mcp.server.mcpserver import MCPServer

from .db import (connect, get_note, get_sticker, is_away_app,
                 search as db_search, tag_usage)
from .summarize import (build_day_digest, day_range, usage_for_range,
                        week_key, week_monday)

server = MCPServer(
    name="privatecontext",
    description=(
        "Local-only personal activity diary. Ask what you did, when, and in "
        "which apps — e.g. 'what was I doing Tuesday afternoon?' All data "
        "stays on this machine; nothing is uploaded."
    ),
)


_DB_PATH: str | None = None  # set by main() from `pctx mcp --db`


def _conn():
    return connect(_DB_PATH)


def _today() -> str:
    return date.today().isoformat()


def _fmt_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


@server.tool()
def search_timeline(query: str, date: Optional[str] = None) -> str:
    """Search the activity log for a keyword (app name, window title, URL).

    Returns up to 15 matching moments as 'HH:MM app — title' lines.
    """
    day = date or _today()
    start, end = day_range(day)
    con = _conn()
    # Collapse repeats: clicks inherit the active window's app/title, so
    # consecutive identical hits are one moment, not many.
    seen: set[tuple[str, str]] = set()
    moments = []
    for h in db_search(con, query, limit=500):
        if not (start <= h["ts"] < end):
            continue
        key = (h["app"] or "unknown", h["title"] or "")
        if key in seen:
            continue
        seen.add(key)
        moments.append(h)
        if len(moments) == 15:
            break
    if not moments:
        return f"No matches for '{query}' on {day}."
    lines = [f"Matches for '{query}' on {day}:"]
    for h in moments:
        title = (h["title"] or "")[:90]
        lines.append(f"  {_fmt_hm(h['ts'])}  {(h['app'] or 'unknown')[:24]:24s} {title}")
    return "\n".join(lines)


@server.tool()
def day_summary(date: Optional[str] = None) -> str:
    """Recap of a day: what was worked on, in which apps, for how long.

    Returns the stored nightly summary when one exists; otherwise a live
    digest built from the event log (marked as un-summarized).
    """
    day = date or _today()
    con = _conn()
    sticker = get_sticker(con, day)
    row = con.execute("SELECT summary, tags FROM days WHERE date = ?",
                      (day,)).fetchone()
    if row and row["summary"]:
        out = f"Summary for {day} (nightly):\n{row['summary']}"
        if row["tags"]:
            out += f"\n\nTags: {row['tags']}"
        if sticker:
            out += f"\n\nSticker: {sticker} (awarded by the user)"
        return out
    digest, stats = build_day_digest(con, day)
    if stats.get("n_windows", 0) == 0:
        return f"No activity recorded for {day}."
    out = (f"No nightly summary stored for {day} yet — live digest below.\n\n"
           f"{digest}")
    if sticker:
        out += f"\n\nSticker: {sticker} (awarded by the user)"
    return out


@server.tool()
def get_digest(date: Optional[str] = None) -> str:
    """The condensed day digest itself — per-app hours, top windows,
    clicks per app, away breaks, and the day's note.

    This is the exact text `pctx summarize --preview` prints and the
    Ollama summarizer reads. Use it to write a summary yourself: no
    Ollama and no stored summary need to exist.
    """
    day = date or _today()
    con = _conn()
    digest, stats = build_day_digest(con, day)
    if stats.get("n_windows", 0) == 0:
        return f"No activity recorded for {day}."
    return digest


@server.tool()
def app_usage(date: Optional[str] = None) -> str:
    """Time spent per application on a day, as a compact table (hours).

    Lock-screen pseudo-apps are excluded — absence is not activity.
    """
    day = date or _today()
    start, end = day_range(day)
    con = _conn()
    usage = [(a, s) for a, s in usage_for_range(con, start, end)
             if not is_away_app(a)][:15]
    if not usage:
        return f"No activity recorded for {day}."
    total_h = sum(s for _, s in usage) / 3600
    lines = [f"App usage for {day} (total {total_h:.1f}h tracked):"]
    for app, secs in usage:
        lines.append(f"  {(app or 'unknown')[:30]:30s} {secs/3600:6.2f}h")
    return "\n".join(lines)


@server.tool()
def get_notes(date: Optional[str] = None) -> str:
    """The user's handwritten note for a day (meeting notes, plans, ideas).

    Notes are the highest-signal content in the diary — the user's own
    words about what the day meant.
    """
    day = date or _today()
    note = get_note(_conn(), day)
    if not note:
        return f"No note written for {day}."
    return f"Note for {day}:\n{note}"


@server.tool()
def list_tags(date: Optional[str] = None) -> str:
    """Tags used on a day, with time credited per tag (compact list)."""
    day = date or _today()
    tags = tag_usage(_conn(), date_str=day)[:15]
    if not tags:
        return f"No tags recorded for {day}."
    lines = [f"Tags for {day}:"]
    for tag, secs in tags:
        lines.append(f"  #{tag}  {secs/3600:.2f}h")
    return "\n".join(lines)


@server.tool()
def week_summary() -> str:
    """This week's recap: the stored weekly summary when one exists,
    otherwise a condensed per-day rollup (top apps only — never raw
    events). Weeks run Monday to Sunday."""
    today = _today()
    con = _conn()
    monday = week_monday(today)
    row = con.execute("SELECT summary FROM days WHERE date = ?",
                      (week_key(today),)).fetchone()
    if row and row["summary"]:
        return f"Weekly summary (week of {monday}):\n{row['summary']}"
    # No stored summary — condensed rollup, one line per day.
    lines = [f"No stored weekly summary — rollup for the week of {monday}:"]
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        ds = d.isoformat()
        start, end = day_range(ds)
        apps = [(a, s) for a, s in usage_for_range(con, start, end)
                if not is_away_app(a)][:3]
        sticker = get_sticker(con, ds)
        if not apps:
            lines.append(f"  {d.strftime('%a %m/%d')}: no activity"
                         + (f" {sticker}" if sticker else ""))
            continue
        detail = ", ".join(f"{a} {s/3600:.1f}h" for a, s in apps)
        lines.append(f"  {d.strftime('%a %m/%d')}: {detail}"
                     + (f" {sticker}" if sticker else ""))
    return "\n".join(lines)


def main(db_path: str | None = None) -> int:
    global _DB_PATH
    _DB_PATH = db_path  # None -> db.connect() default
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
