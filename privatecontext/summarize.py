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

from .db import connect, init_db

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


def day_range(date_str: str) -> tuple[float, float]:
    """Unix range for a local calendar day (YYYY-MM-DD)."""
    d = date.fromisoformat(date_str)
    start = datetime(d.year, d.month, d.day).astimezone()
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _fmt_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def usage_for_range(con, start: float, end: float,
                    cap_s: float = CAP_S) -> list[tuple[str, float]]:
    """Seconds per app in [start, end). Each window event owns time until the
    next event, capped so a forgotten window doesn't claim the afternoon."""
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

    # Per-window durations, aggregated by (app, title).
    per_window: dict[tuple[str, str], float] = {}
    for i, w in enumerate(windows):
        nxt = windows[i + 1]["ts"] if i + 1 < len(windows) else end
        dur = min(max(0.0, nxt - w["ts"]), CAP_S)
        key = (w["app"] or "unknown", (w["title"] or "")[:80])
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
    lines.append("")
    lines.append("Time per app (hours):")
    for app, secs in usage_for_range(con, start, end)[:12]:
        lines.append(f"  {app[:28]:28s} {secs/3600:5.2f}h")
    lines.append("")
    lines.append("Top windows by time:")
    for (app, title), secs in top_windows:
        h = secs / 3600
        lines.append(f"  {h:4.1f}h  {app[:20]:20s} {title}")
    lines.append("")
    lines.append("Clicks per app:")
    for r in click_apps:
        lines.append(f"  {(r['app'] or 'unknown')[:28]:28s} {r['n']}")
    if tokens:
        lines.append("")
        lines.append("Candidate tokens seen in titles (for tagging): "
                     + ", ".join(tokens))

    digest = "\n".join(lines)
    if len(digest) > MAX_DIGEST_CHARS:
        digest = digest[:MAX_DIGEST_CHARS] + "\n…[truncated]"
    return digest, stats


PROMPT_TEMPLATE = """You are a private, on-device assistant. Summarize one day of computer activity from the CONDENSED digest below. The digest holds aggregates only — no raw events. Do not invent anything not in the digest.

Write exactly two sections:

RECAP:
- 3 to 6 short bullets describing what the person worked on, naming apps, projects, and rough times.

TAGS:
a single comma-separated line of tags: project or repo names, app names, ticket-like tokens (e.g. INC0123456), topics.

Digest:
---
{digest}
---
"""


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


def _parse_summary(text: str) -> tuple[str, str]:
    """Split model output into (recap, tags). Tolerant of formatting wobble."""
    recap_m = re.search(r"RECAP:\s*(.*?)(?:\nTAGS:|\Z)", text, re.S | re.I)
    tags_m = re.search(r"TAGS:\s*(.*)", text, re.S | re.I)
    recap = ""
    if recap_m:
        bullets = []
        for line in recap_m.group(1).strip().splitlines():
            line = line.strip().lstrip("-•* ").strip()
            if line:
                bullets.append("- " + line)
        recap = "\n".join(bullets[:8])
    tags = ""
    if tags_m:
        raw = tags_m.group(1).strip().splitlines()[0]
        tags = ", ".join(t.strip() for t in raw.split(",") if t.strip())[:500]
    if not recap:
        recap = text.strip()[:2000]  # fall back to raw output
    return recap, tags


def summarize_day(db_path: str | None, date_str: str, model: str,
                  dry_run: bool = False) -> int:
    try:
        day_range(date_str)  # validates YYYY-MM-DD
    except ValueError:
        print(f"bad date '{date_str}' — use YYYY-MM-DD", file=sys.stderr)
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
