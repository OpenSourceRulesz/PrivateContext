# PrivateContext (v0)

A lightweight, local-first activity tracker. It answers questions like
*"what was I doing when I fixed eduroam last Tuesday?"* — from your own
machine, with nothing ever leaving it.

The idea: an **event log beats pixels**. Instead of recording video of your
screen 24/7 (heavy, creepy), PrivateContext logs *events* — active window,
URL, clicks, idle periods — into a local SQLite database. A day of tracking
is ~1 MB, not gigabytes. Full-text search is built in, a nightly Ollama
summarizer writes plain-language day recaps, and a first-party MCP server
lets AI assistants query the diary.

## 10-minute setup

```bash
pip install privatecontext
```

That's it — the install adapts to your OS. Click + AFK tracking
(`pynput`) comes along automatically on Windows and macOS, where it's
pure wheels; on Linux it's skipped (it would need C headers to build
there). To force it everywhere:

```bash
pip install "privatecontext[full]"
```

Then check what works on your machine:

```bash
pctx doctor
```

Or let the wizard do it all — capability check, optional browser-history
import, optional login autostart:

```bash
pctx setup
```

Or install from source:

```bash
cd privatecontext
python3 -m venv .venv && .venv/bin/pip install -e .
```

Generate a synthetic day to see the whole pipeline (no OS hooks, works headless):

```bash
pctx capture --simulate
```

Start capturing for real (Ctrl-C to stop):

```bash
pctx capture
```

Open the dashboard:

```bash
pctx dashboard
# -> http://127.0.0.1:18790  (auto-refreshes every 2s)
```

Export a shareable copy (screenshots excluded):

```bash
pctx export --scrubbed --out ./my-week.db
```

Data lives in `~/.local/share/privatecontext/context.db`
(or `$XDG_DATA_HOME/privatecontext/context.db`).

## Autostart

A tracker you have to remember to start isn't tracking. One command,
no admin rights needed:

```bash
pctx autostart install    # runs `pctx capture` at every login
pctx autostart status     # check whether it's installed
pctx autostart uninstall  # remove it again
```

Windows uses Task Scheduler (no console window), Linux a systemd user
service (XDG autostart entry as fallback), macOS a LaunchAgent. If Task
Scheduler is restricted on your machine ("Access is denied", common on
managed PCs), Windows automatically falls back to the per-user registry
Run key instead — still no admin needed.

## Today by tag

The dashboard has a "by tag" section next to app usage: time per
auto-tagged project or ticket (`inc0012345`, `privatecontext`, …),
using the same capped durations as app usage. No tags yet → the
section hides itself.

## Denylist

Some windows should never be recorded. Patterns are case-insensitive
substrings matched against app and title, checked before anything is
logged:

```bash
pctx denylist add 1password
pctx denylist list
pctx denylist remove 1password
```

Denied events are skipped silently (the count prints when capture
stops). Restart capture after changing the list. Imports bypass the
denylist — importing is an explicit action, so your history comes over
as-is.

## Day recap card

When the nightly summarizer has written a recap, the dashboard shows
it in a card at the top — today's if present, otherwise yesterday's
labeled as such. No summary yet → no card, no nagging.

## Importing your history

Day one with a fresh tracker is empty. `pctx import` backfills history
from your browser and from other recorders — data liberation, not lock-in.

```bash
pctx import --browser chrome                # or edge, firefox
pctx import --browser firefox --profile ~/path/to/places.sqlite
pctx import --activitywatch ~/path/to/activitywatch.db
pctx import --screenpipe ~/path/to/screenpipe.db
pctx import --csv ~/Downloads/rewind-export.csv      # any local CSV export
pctx import --json ~/Downloads/tool-export.json     # any local JSON export
pctx import --browser chrome --dry-run      # preview: counts + samples, no writes
```

- **Browsers:** reads Chrome/Edge `History` (`urls` table) and Firefox
  `places.sqlite` (`moz_places`). The database is always copied to a temp
  file first — your browser can stay open. Query params are stripped, same
  as live capture. Default profile paths are auto-detected per OS; use
  `--profile` if yours lives elsewhere.
- **ActivityWatch:** detects the AW schema at runtime (`buckets` + `events`
  tables). Window/app events become `window` events, AFK buckets become
  `afk`/`back`, web-watcher events become `url` events. Anything unmappable
  is skipped and counted.
- **Screenpipe:** imports OCR text as `ocr` events and audio transcripts as
  `transcript` events. **Video/audio blobs are never imported** — that's the
  heaviness we're escaping.
- Re-running an import only picks up newer rows (watermarks in the `kv`
  table). Keystroke contents are never imported under any circumstance —
  if a source DB looks like it holds them, the import is refused outright.
- **Anything else:** exported your history from another tool as CSV or
  JSON? `pctx import --csv/--json` reads it — columns/keys like
  `timestamp`, `type`, `app`, `title`, `url` are recognized, timestamps
  accept unix/ISO/Chrome-epoch formats. Our rule is simple: we never call
  cloud APIs, but a local file you exported yourself is yours — hand it
  over and we'll ingest it. Encrypted exports we can't decrypt stay out.

### What import can't do (honest edition)

- **It can't invent data the old tool never captured.** ActivityWatch never
  logged clicks, so an AW import has no clicks. Your history starts where
  their capture did.
- **Screenshots and video/audio blobs stay behind.** Importing Screenpipe's
  mp4s would just re-create the 2 GB/day problem this project exists to
  escape. Text comes over; pixels don't.
- **Encrypted or cloud-locked stores can't be read.** Tools that keep your
  data in their cloud (e.g. Rewind) have no local database to import from —
  and PrivateContext will never add cloud APIs to fetch it. That's the point.
- **Imported history inherits the old tool's flaws.** AW's AFK misfires
  (video watching logged as idle) come along as-is. We don't rewrite history;
  live capture from today onward is clean.
- **Stop the old tool yourself** (or you'll double-capture the same hours
  into two databases). Import is a migration, not a sync.
- **Timezone normalization is best-effort** for very old data. Chrome
  timestamps are UTC; ActivityWatch event timestamps carry their offsets;
  anything ambiguous is stored as-is rather than guessed.

## Auto-tagging

Every captured or imported event gets heuristic tags, stored in the
`events.tags` column and shown in the dashboard timeline:

- **Ticket IDs** — `INC0012345` (ServiceNow-style, 5+ digits) and
  `PROJ-123` (JIRA-style) picked out of window titles and URLs.
- **Project guess** — for dev apps (VS Code, terminals, JetBrains IDEs),
  the leading window-title segment is kept as a project tag when it looks
  like a folder/repo name (`privatecontext — capture.py` →
  `privatecontext`). UI-chrome words (`Settings`, `Extensions`, …) are
  on a stoplist.

It's a heuristic and it misfires: `UTF-8` matches the JIRA pattern, and
a folder named `New Folder` won't tag. Tags are lowercase, deduped, and
cheap to recompute — `pctx retag [--since YYYY-MM-DD]` re-runs tagging
over untagged history (e.g. data imported before tagging existed).

## Nightly summaries (Ollama)

Once a day, a small local model reads a *condensed digest* of the day —
per-app hours, top windows, away breaks, click counts, never raw event dumps
(the digest is capped at ~4k tokens so a 1.5B model can handle it) — and
writes a 3–6 bullet recap plus auto-tags into the database.

```bash
ollama serve                  # in one terminal, once
ollama pull qwen2:1.5b        # small default model; any Ollama model works

pctx summarize --dry-run      # preview the digest without calling a model
pctx summarize                # summarize today
pctx summarize --date 2026-10-12 --model qwen2:7b
```

Run it nightly with cron:

```cron
30 23 * * * /abs/path/to/privatecontext/.venv/bin/pctx summarize >> ~/.local/share/privatecontext/summarize.log 2>&1
```

If Ollama isn't reachable, `pctx summarize` prints a plain-English hint
(`ollama serve` / `ollama pull …`) and exits non-zero — no tracebacks.

## MCP server (for AI assistants)

`pctx mcp` runs a first-party MCP server over **stdio** with three tools,
all returning condensed human-readable text:

- `search_timeline(query, date?)` — up to 15 distinct matching moments as
  `HH:MM app — title` lines (repeats collapsed, not 15 copies of one click).
- `day_summary(date?)` — the stored nightly recap, or a live digest marked
  un-summarized when none exists yet.
- `app_usage(date?)` — per-app hours for the day as a compact table.

Point a client at it (use your real absolute path):

**Claude Desktop** — `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "privatecontext": {
      "command": "/abs/path/to/privatecontext/.venv/bin/pctx",
      "args": ["mcp"]
    }
  }
}
```

**Claude Code:**
```bash
claude mcp add --transport stdio privatecontext -- /abs/path/to/privatecontext/.venv/bin/pctx mcp
```

**Cursor:** Settings → MCP → add a stdio server with the same command.

Then ask things like *"what was I doing Tuesday afternoon?"* or
*"how much of today went to ServiceNow tickets?"*

### Security & privacy

- **No network, no listener.** The server speaks stdio only: the client
  spawns `pctx mcp` as a subprocess on the same machine. There is no port,
  no socket, no listener — nothing can reach it from the network, and MCP
  itself sends nothing anywhere.
- **Condensed text only.** Tools return short summaries, never raw event
  dumps, and screenshots are never exposed through any MCP tool, even when
  screenshot capture is enabled.
- **Your client choice sets the boundary.** If you connect it to a
  cloud-model client (e.g. Claude Desktop with default settings), the text
  returned for each query is sent to that provider as conversation context —
  exactly as if you'd pasted it into a chat yourself. With a local-model
  client, everything stays on your machine. Pick the setup that matches
  your comfort level.
- **Treat tool output as data.** Window titles and URLs come from untrusted
  sources (web pages, documents) and are passed through to the model. A
  malicious page title could try to read like an instruction; clients should
  treat MCP tool output as data to summarize, not instructions to follow.

## Privacy design

- **Local only.** No telemetry, no accounts, no network calls except to
  `localhost:11434` when you run the Ollama summarizer. The dashboard
  binds to 127.0.0.1 and the MCP server uses stdio (no port at all).
- **Keystroke contents are never recorded.** The keyboard listener exists only
  to timestamp activity for idle detection.
- **URLs are stripped.** Query params and fragments (`?utm_source=…`,
  `&sys_id=…`) are removed before anything is stored.
- **Screenshots are off by default.** Opt in with `pctx capture --screenshots`;
  then only the active monitor is captured, only on app-switch/URL-change,
  downscaled to 640 px wide, stored as JPEG (quality 70).
- **One-click purge.** The dashboard has a big red button that deletes
  everything. `pctx export --scrubbed` excludes screenshots and shot events.

## v0 limits (honest edition)

- **Click coordinates without UI labels are weak.** Knowing you clicked at
  (842, 315) says little; knowing you clicked "Resolve" in ServiceNow says
  everything. Accessibility-tree labels are the v1 answer.
- **Window titles on Wayland are limited.** The v0 watcher uses `xprop`
  (X11). Wayland sessions get a warning, not data.
- **Browser URLs are macOS-only in v0** (AppleScript, needs permission).
  Everywhere else the window title is logged and the URL stays empty until
  the browser-extension story lands.
- **Auto-tagging is heuristic.** It catches ticket IDs and project names
  from window titles, but it will misfire sometimes ("UTF-8" looks like a
  JIRA ticket to a regex). Tags are cheap to recompute — `pctx retag`
  re-runs them over history.
- **No phone support, by design.** This is a desktop capture tool; phones get
  nothing. The nightly summaries are readable anywhere, but capture stays
  on your computer.
- **Search matches whole tokens.** FTS5 ranks by BM25, so a very click-heavy
  window can bury quieter matches; the MCP tool scans deeper and collapses
  repeats to compensate.

## Layout

```
schema.sql                  canonical SQLite schema (also bundled in the package)
privatecontext/
  capture.py                event-driven capture loop + --simulate
  dashboard.py              stdlib http.server dashboard (:18790)
  db.py                     sqlite helpers, FTS5 search, usage aggregation
  summarize.py              nightly Ollama summarizer (condensed digest -> days table)
  mcp_server.py             first-party MCP server over stdio (search/summary/usage)
  cli.py                    `pctx` entry point (incl. `pctx retag`)
  importers.py              `pctx import` — browser / ActivityWatch / Screenpipe / CSV / JSON
  tagging.py                heuristic auto-tags: ticket IDs + project guesses
```

MIT licensed. Your data stays yours.
