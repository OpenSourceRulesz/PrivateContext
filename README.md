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
  downscaled to 640 px wide.
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
- **Screenshots are PNG, not JPEG** (stdlib encoder, no Pillow dependency yet).
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
  cli.py                    `pctx` entry point
```

MIT licensed. Your data stays yours.
