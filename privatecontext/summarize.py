"""Nightly Ollama batch summarizer for PrivateContext.

Reads one day's events, condenses them into a small digest (never raw event
dumps — small local models can't hold 50k events), asks a local Ollama model
for a recap + auto-tags, and stores the result in the `days` table.

Only stdlib is used for the Ollama HTTP call.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from .db import (away_usage, connect, day_range, get_note, init_db,
                 is_away_app)

def _ollama_base() -> str:
    """Ollama server base URL. Honors OLLAMA_HOST (Ollama's own convention,
    e.g. 127.0.0.1:11435), defaulting to 127.0.0.1:11434. 127.0.0.1, not
    localhost: on some machines (proxies, IPv6) `localhost` hangs instead
    of failing fast. The numeric loopback always fails fast when nothing
    listens."""
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip()
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")


OLLAMA_HOST = _ollama_base()
OLLAMA_URL = OLLAMA_HOST + "/api/generate"


def ollama_reachable(timeout: float = 3) -> bool:
    """Fast pre-flight: is anything listening? Never hangs the CLI."""
    try:
        req = urllib.request.Request(OLLAMA_HOST + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def ollama_hint() -> str:
    return (f"can't reach ollama at {OLLAMA_HOST} — is it installed?\n"
            "  Install it:  https://ollama.com/download  (then restart this terminal)\n"
            "  Then run:    ollama pull qwen2:1.5b\n"
            "  And start:   ollama serve  (usually starts automatically)\n"
            "  (set OLLAMA_HOST=host:port if your server runs elsewhere)")
DEFAULT_MODEL = "qwen2:1.5b"
# ~4000 tokens. The digest is built to stay far under this.
MAX_DIGEST_CHARS = 16000
CAP_S = 600  # max seconds one window event can claim (same rule as dashboard)

TICKET_RE = re.compile(r"\b(?:[A-Z]{2,}\d{3,}|[A-Z]+-\d+|#\d+)\b")


def _fmt_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


# Stored app names that predate (or slip past) capture-time friendly
# naming. Digest/MCP-only display normalization — history is never
# rewritten; old ugly rows and new clean rows merge under one label.
_DIGEST_APP_NAMES = {
    "chrome_widgetwin_1": "Chromium app",
    "ms-teams": "Teams",
    "teamswebview": "Teams",
    "xamlexplorerhostislandwindow": "Windows Shell",
    "xamlwindow": "Windows Shell",
    "windows.ui.core.corewindow": "Windows Shell",
    "shellexperiencehost": "Windows Shell",
    "searchhost": "Windows Shell",
    "slidetoshutdown": "Windows Shell",
}


def display_app(app: str | None) -> str:
    """Clean display name for a stored app name. Pure function."""
    return _DIGEST_APP_NAMES.get((app or "").strip().lower(), app or "unknown")


def usage_for_range(con, start: float, end: float,
                    cap_s: float = CAP_S,
                    exclude_away: bool = False) -> list[tuple[str, float]]:
    """Seconds per app in [start, end). Each window event owns time until the
    next event, capped so a forgotten window doesn't claim the afternoon.
    With exclude_away, lock-screen pseudo-apps still bound their neighbors'
    ownership but are skipped in the tally. Names are display-normalized."""
    rows = con.execute(
        "SELECT ts, app FROM events WHERE type = 'window'"
        " AND ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
    usage: dict[str, float] = {}
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else end
        dur = min(max(0.0, nxt - r["ts"]), cap_s)
        raw = r["app"] or "unknown"
        if exclude_away and is_away_app(raw):
            continue
        app = display_app(raw)
        usage[app] = usage.get(app, 0.0) + dur
    return sorted(usage.items(), key=lambda kv: -kv[1])


def build_day_digest(con, date_str: str) -> tuple[str, dict]:
    """Condense one day into small-model-friendly text.

    Returns (digest_text, stats). Never includes raw per-click rows —
    clicks appear only as per-app counts.
    """
    start, end = day_range(date_str)
    stats: dict = {"date": date_str}

    windows = con.execute(
        "SELECT ts, app, title FROM events WHERE type = 'window'"
        " AND ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
    stats["n_windows"] = len(windows)

    n_clicks = con.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'click'"
        " AND ts >= ? AND ts < ?",
        (start, end),
    ).fetchone()[0]
    stats["n_clicks"] = n_clicks

    if not windows:
        return f"Day digest for {date_str}: no activity recorded.", stats

    # Per-window durations, aggregated by (app, title). Away apps
    # (lock screen) bound their neighbors' ownership but are not activity.
    per_window: dict[tuple[str, str], float] = {}
    for i, w in enumerate(windows):
        nxt = windows[i + 1]["ts"] if i + 1 < len(windows) else end
        dur = min(max(0.0, nxt - w["ts"]), CAP_S)
        if is_away_app(w["app"]):
            continue
        key = (display_app(w["app"]), (w["title"] or "")[:80])
        per_window[key] = per_window.get(key, 0.0) + dur
    top_windows = sorted(per_window.items(), key=lambda kv: -kv[1])[:20]

    # AFK periods come from 'back' events (duration_ms = idle length).
    afk_rows = con.execute(
        "SELECT ts, duration_ms FROM events WHERE type = 'back'"
        " AND ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
    afk_periods = []
    for r in afk_rows:
        idle_s = (r["duration_ms"] or 0) / 1000
        if idle_s >= 60:
            afk_periods.append((_fmt_hm(r["ts"] - idle_s), _fmt_hm(r["ts"]), idle_s))
    stats["afk_periods"] = len(afk_periods)

    click_apps = con.execute(
        "SELECT app, COUNT(*) AS n FROM events WHERE type = 'click'"
        " AND ts >= ? AND ts < ? GROUP BY app ORDER BY n DESC LIMIT 10",
        (start, end),
    ).fetchall()

    # Candidate tokens for tagging (ticket IDs, PR refs), from titles only.
    tokens: list[str] = []
    seen: set[str] = set()
    for w in windows:
        for tok in TICKET_RE.findall(w["title"] or ""):
            if tok not in seen and len(tokens) < 20:
                seen.add(tok)
                tokens.append(tok)

    first_ts, last_ts = windows[0]["ts"], windows[-1]["ts"]
    stats.update({"first": _fmt_hm(first_ts), "last": _fmt_hm(last_ts)})

    lines = [
        f"Day digest for {date_str} (local time).",
        f"{len(windows)} window switches, {n_clicks} clicks captured.",
        f"Active roughly {_fmt_hm(first_ts)} -> {_fmt_hm(last_ts)}.",
    ]
    if afk_periods:
        breaks = ", ".join(f"{a}->{b} ({s/60:.0f}m)" for a, b, s in afk_periods[:8])
        lines.append(f"Away breaks: {breaks}.")
    away_s = away_usage(con, CAP_S, date_str)
    if away_s >= 60:
        lines.append(f"Away total (screen locked + idle): {away_s/3600:.2f}h.")
    lines.append("")
    lines.append("Time per app (hours):")
    for app, secs in usage_for_range(con, start, end,
                                     exclude_away=True)[:12]:
        lines.append(f"  {app[:28]:28s} {secs/3600:5.2f}h")
    lines.append("")
    lines.append("Top windows by time:")
    for (app, title), secs in top_windows:
        h = secs / 3600
        lines.append(f"  {h:4.1f}h  {app[:20]:20s} {title}")
    lines.append("")
    lines.append("Clicks per app:")
    for r in click_apps:
        lines.append(f"  {display_app(r['app'])[:28]:28s} {r['n']}")
    if tokens:
        lines.append("")
        lines.append("Candidate tokens seen in titles (for tagging): "
                     + ", ".join(tokens))

    # The user's own handwritten notes carry the most signal of all —
    # they say what the day *meant*, not just what happened.
    note = get_note(con, date_str)
    if note:
        lines.append("")
        lines.append("My notes today:")
        lines.append(note.strip()[:2000])

    digest = "\n".join(lines)
    if len(digest) > MAX_DIGEST_CHARS:
        digest = digest[:MAX_DIGEST_CHARS] + "\n…[truncated]"
    return digest, stats


PROMPT_TEMPLATE = """You are the user's private journal. Write today's journal entry from the activity digest below. The digest holds aggregates only — no raw events.

Rules:
- Write in FIRST PERSON, past tense — these are the user's own words ("I spent the morning…", never "the user spent…").
- 3 to 6 bullets. Each bullet covers something DIFFERENT — never mention the same app or project twice.
- Name apps, files, ticket IDs, and the EXACT durations printed in the digest (e.g. 0.85h, 27m) — never round them to whole hours, never invent times.
- Report ONLY what the digest shows. Never write "likely", "probably", or "some time", and never invent a purpose for an activity.
- If the digest has a "My notes today:" section, weave what the user wrote into the entry — their words outrank the aggregates.
- End with exactly one line starting with TAGS: followed by comma-separated tags (project/repo names, app names, ticket-like tokens such as INC0123456).

Example — digest:
Day digest for 2026-10-15 (local time).
Time per app (hours):
  Edge                           3.20h
  VS Code                        1.50h
Top windows by time:
   2.0h  Edge                 Kuali - ServiceNow
   1.5h  VS Code              privatecontext - dashboard.py
My notes today:
Testing the Windows build; the Ollama install fought the proxy all afternoon.

Entry:
- I spent the morning in Edge on the Kuali ServiceNow portal, 2.00h on tickets.
- I put 1.50h into the PrivateContext Windows build in VS Code, working on the new dashboard.
- My notes: testing the Windows build; the Ollama install fought the proxy all afternoon.
TAGS: kuali, servicenow, privatecontext, edge, vs code

Digest:
---
{digest}
---
"""


WEEK_PROMPT_TEMPLATE = """You are the user's private journal. Write the week's journal entry from the CONDENSED per-day digests below (each headed === YYYY-MM-DD ===). The digests hold aggregates only — no raw events.

Rules:
- Write in FIRST PERSON, past tense — these are the user's own words ("I spent the week…", never "the user spent…").
- 5 to 10 bullets. Each bullet covers something DIFFERENT — never mention the same app or project twice.
- Name apps, files, ticket IDs, and the EXACT durations printed in the digests (e.g. 4.10h, 27m) — never round them to whole hours, never invent times. Call out trends, unusually heavy or light days, and recurring themes.
- Report ONLY what the digests show. Never write "likely", "probably", or "some time", and never invent a purpose for an activity.
- If a day has a "My notes" section, weave what the user wrote into the entry — their words outrank the aggregates.
- End with exactly one line starting with TAGS: followed by comma-separated tags (project/repo names, app names, ticket-like tokens such as INC0123456).

Example — digests:
=== 2026-10-13 ===
Day digest for 2026-10-13 (local time).
Time per app (hours):
  Edge                           4.10h
Top windows by time:
   3.0h  Edge                 Kuali - ServiceNow
=== 2026-10-14 ===
Day digest for 2026-10-14 (local time).
Time per app (hours):
  VS Code                        5.00h
Top windows by time:
   4.2h  VS Code              tiny-jepa - train.py

Entry:
- I spent Monday buried in Edge on the Kuali ServiceNow portal, 4.10h of ticket work.
- Tuesday belonged to VS Code and tiny-jepa training runs, a solid 5.00h block.
- The week tilted from support work toward deep project time as it went on.
TAGS: kuali, servicenow, tiny-jepa, edge, vs code

Digests:
---
{digest}
---
"""

# 3x the daily budget: 7 day-digests must fit, each already condensed.
WEEK_DIGEST_CHARS = 3 * MAX_DIGEST_CHARS


def week_monday(date_str: str) -> str:
    """Monday (YYYY-MM-DD) of the week containing date_str. Used as the
    storage label for weekly summaries."""
    d = date.fromisoformat(date_str)
    return (d - timedelta(days=d.weekday())).isoformat()


def week_key(date_str: str) -> str:
    """Storage key in the days table: 'week:YYYY-MM-DD' (Monday's date).
    TEXT column — never collides with daily YYYY-MM-DD keys."""
    return "week:" + week_monday(date_str)


def build_week_digest(con, end_date_str: str) -> tuple[str, dict]:
    """Condense the trailing 7 days ending at end_date_str (inclusive).

    Returns (digest_text, stats). Each day gets its existing day digest
    under an === YYYY-MM-DD === header; quiet days get a one-liner.
    """
    end = date.fromisoformat(end_date_str)
    days = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    parts: list[str] = []
    stats: dict = {"days": days, "n_days_active": 0, "n_windows": 0}
    for day in days:
        digest, dstats = build_day_digest(con, day)
        stats["n_windows"] += dstats.get("n_windows", 0)
        if dstats.get("n_windows", 0) == 0:
            parts.append(f"=== {day} ===\nno activity recorded.")
        else:
            stats["n_days_active"] += 1
            parts.append(f"=== {day} ===\n{digest}")
        # The day digest already embeds "My notes today:"; keep an explicit
        # per-day marker too so the weekly prompt can cite notes by day.
        note = get_note(con, day)
        if note and "My notes today:" not in digest:
            parts[-1] += f"\n\nMy notes that day:\n{note.strip()[:2000]}"
    digest = "\n\n".join(parts)
    if len(digest) > WEEK_DIGEST_CHARS:
        digest = digest[:WEEK_DIGEST_CHARS] + "\n…[truncated]"
    return digest, stats


def _call_ollama(model: str, prompt: str) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 404 and "not found" in detail.lower():
            print(f"ollama doesn't have the model '{model}'.\n"
                  f"  Run:  ollama pull {model}\n"
                  f"  Then: pctx summarize --model {model}", file=sys.stderr)
        else:
            print(f"ollama returned HTTP {e.code}: {detail}", file=sys.stderr)
        return ""
    except urllib.error.URLError:
        print(ollama_hint(), file=sys.stderr)
        return ""
    except (OSError, ValueError) as e:  # timeouts, bad JSON
        print(f"ollama request failed: {e}", file=sys.stderr)
        return ""
    return body.get("response", "")


def _dedupe_sentences(lines: list[str]) -> list[str]:
    """Drop near-duplicate lines (the "Section 2 restates Section 1" failure
    mode). A line is dropped when its normalized form equals, contains, or
    is contained by an earlier line, or when word overlap >= 80%.
    Deterministic, no dependencies."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    seen: list[str] = []
    out: list[str] = []
    for line in lines:
        n, words = norm(line), set(norm(line).split())
        dup = False
        for s in seen:
            sn, swords = norm(s), set(norm(s).split())
            if not words or not swords:
                continue
            if n == sn or n in sn or sn in n:
                dup = True
                break
            if len(words & swords) / max(len(words), len(swords)) >= 0.8:
                dup = True
                break
        if not dup:
            seen.append(line)
            out.append(line)
    return out


def _parse_summary(text: str, max_bullets: int = 8) -> tuple[str, str]:
    """Split model output into (recap, tags).

    Expected shape (per the prompt): bullet lines, then exactly one
    `TAGS:` line. Robust fallback for misbehaving models: strips
    "Section N:" headers, dedupes near-duplicate sentences, and never
    returns an empty recap when text exists.
    """
    tags = ""
    body_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"(?i)^tags\s*:\s*(.*)$", line.strip())
        if m:
            tags = m.group(1).strip()
        else:
            body_lines.append(line)
    bullets: list[str] = []
    for line in body_lines:
        line = re.sub(r"(?i)^section\s+\d+\s*:\s*", "", line.strip()).strip()
        line = re.sub(r"^[-•*]\s*", "", line).strip()
        # Skip stray headers like "RECAP:" left by older prompts.
        if re.fullmatch(r"(?i)recap\s*:?", line):
            continue
        if line:
            bullets.append(line)
    bullets = _dedupe_sentences(bullets)
    recap = "\n".join("- " + b for b in bullets[:max_bullets])
    if tags:
        tags = ", ".join(t.strip() for t in tags.split(",") if t.strip())[:500]
    if not recap and text.strip():
        cleaned = re.sub(r"(?i)^section\s+\d+\s*:\s*", "",
                         text.strip(), flags=re.M)
        cleaned_lines = [l.strip() for l in cleaned.splitlines()
                         if l.strip()]
        deduped = _dedupe_sentences(cleaned_lines)
        recap = "\n".join("- " + b for b in deduped[:max_bullets])
        if not recap:
            recap = text.strip()[:2000]
    return recap, tags


def summarize_day(db_path: str | None, date_str: str, model: str,
                  dry_run: bool = False) -> int:
    try:
        day_range(date_str)  # validates YYYY-MM-DD
    except ValueError:
        print(f"bad date '{date_str}' — use YYYY-MM-DD", file=sys.stderr)
        return 2
    # Future dates are planning pages — there is nothing to summarize yet.
    if date_str > date.today().isoformat():
        print("can't summarize a day that hasn't happened yet",
              file=sys.stderr)
        return 2
    con = connect(db_path)
    init_db(con)  # no-op if capture already created the schema
    digest, stats = build_day_digest(con, date_str)
    if stats.get("n_windows", 0) == 0:
        print(f"no activity recorded for {date_str} — nothing to summarize.")
        return 0
    if dry_run:
        print(digest)
        print(f"\n[digest: {len(digest)} chars "
              f"(budget {MAX_DIGEST_CHARS}); {stats['n_windows']} windows, "
              f"{stats['n_clicks']} clicks — no raw click rows included]")
        return 0
    prompt = PROMPT_TEMPLATE.format(digest=digest)
    if not ollama_reachable():
        print(ollama_hint(), file=sys.stderr)
        return 1
    print(f"[privatecontext] asking {model} to summarize {date_str}…",
          file=sys.stderr)
    text = _call_ollama(model, prompt)
    if not text:
        return 1
    recap, tags = _parse_summary(text)
    con.execute("INSERT OR REPLACE INTO days(date, summary, tags) VALUES (?,?,?)",
                (date_str, recap, tags))
    con.commit()
    print(f"[privatecontext] summary stored for {date_str}")
    print(recap)
    if tags:
        print(f"\ntags: {tags}")
    return 0


def summarize_week(db_path: str | None, end_date_str: str, model: str,
                   dry_run: bool = False) -> int:
    """Summarize the trailing 7 days ending at end_date_str (inclusive).

    One Ollama call over the joined per-day digests; stored in the days
    table under 'week:YYYY-MM-DD' (Monday's date)."""
    try:
        day_range(end_date_str)  # validates YYYY-MM-DD
    except ValueError:
        print(f"bad date '{end_date_str}' — use YYYY-MM-DD", file=sys.stderr)
        return 2
    con = connect(db_path)
    init_db(con)  # no-op if capture already created the schema
    digest, stats = build_week_digest(con, end_date_str)
    if stats["n_days_active"] == 0:
        print(f"no activity recorded in the 7 days ending {end_date_str}"
              f" — nothing to summarize.")
        return 0
    key = week_key(end_date_str)
    if dry_run:
        print(digest)
        print(f"\n[week digest: {len(digest)} chars "
              f"(budget {WEEK_DIGEST_CHARS}); {stats['n_days_active']}/7 days "
              f"active, {stats['n_windows']} windows — no raw click rows included]")
        return 0
    prompt = WEEK_PROMPT_TEMPLATE.format(digest=digest)
    if not ollama_reachable():
        print(ollama_hint(), file=sys.stderr)
        return 1
    print(f"[privatecontext] asking {model} to summarize week of {key}…",
          file=sys.stderr)
    text = _call_ollama(model, prompt)
    if not text:
        return 1
    recap, tags = _parse_summary(text, max_bullets=10)
    con.execute("INSERT OR REPLACE INTO days(date, summary, tags) VALUES (?,?,?)",
                (key, recap, tags))
    con.commit()
    print(f"[privatecontext] weekly summary stored under {key}")
    print(recap)
    if tags:
        print(f"\ntags: {tags}")
    return 0
