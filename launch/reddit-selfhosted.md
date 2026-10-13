# r/selfhosted draft — DO NOT POST until the readiness checklist passes

Title: `PrivateContext – open-source, local-first activity tracker. Your data stays in a SQLite file you own.`

---

I got tired of the tradeoff between "know where your time goes" and
"install a 24/7 screen recorder that eats your CPU." So I built the
thing I wanted:

- **Event log, not video.** Active window, URL, clicks, idle → local
  SQLite. ~1 MB/day. Nothing to tune, no GPU melting.
- **Truly local.** No accounts, no telemetry, no cloud, no phone-home.
  The dashboard binds to 127.0.0.1. The only network call in the whole
  project is to localhost:11434 when *you* run the Ollama summarizer.
- **Yours to query.** Full-text search built in, plus a first-party MCP
  server (stdio) so your local AI setup can answer "what did I do
  Tuesday?" The schema is documented — `sqlite3` your own life.
- **Data liberation.** `pctx import` pulls in browser history
  (Chrome/Edge/Firefox), ActivityWatch databases, Screenpipe OCR text,
  or any CSV/JSON export. Leaving a tool shouldn't mean losing your
  history.

Privacy design: keystroke contents never recorded, URL query params
stripped before storage, screenshots off by default, big red purge
button. MIT licensed.

Caveats: it's v0, tested mostly on Windows and Linux so far. Browser
URL capture is currently macOS-only; on Windows the browser-history
import covers URLs. Would love feedback from fellow self-hosters —
especially what breaks on your setup.

https://github.com/OpenSourceRulesz/PrivateContext
