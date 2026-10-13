# Show HN draft — DO NOT POST until the readiness checklist passes

Title: `Show HN: PrivateContext – a local-first activity diary (open source, no screen recording)`

---

I built this because I wanted to know where my days go without
installing a 24/7 screen recorder or paying $400 for the privilege.

PrivateContext logs *events*, not pixels: active window, URL, clicks,
idle periods — into a local SQLite database. A day of tracking is ~1 MB,
not gigabytes. Full-text search answers "what was I doing when I fixed
eduroam last Tuesday?" There's a small dashboard, a nightly summarizer
that runs a local model via Ollama, and a first-party MCP server so AI
assistants can query the diary over stdio.

Why not the existing options:

- ActivityWatch is great and free, but setup is fiddly (a separate
  extension per browser), the dashboard is static, and the AFK detector
  thinks watching a video is being idle.
- Screenpipe is impressive tech, but users report 700% CPU spikes on an
  M3 Max, and — honestly — people recoil at "24/7 screen recording" even
  when it's local. An event log is psychologically ten times easier to
  accept than a camera pointed at your monitor.

What I'd like from HN: try it, tell me what breaks. Especially the
Windows capture path — that's the least-tested part.

Honest limits (v0): click coordinates without UI labels are weak signal;
browser URL capture is macOS-only for now (Windows gets window titles +
browser-history import); Wayland window titles are limited; screenshots
are off by default. Keystroke contents are never recorded, URLs are
stripped of query params, one-click purge, no telemetry, MIT licensed.

https://github.com/OpenSourceRulesz/PrivateContext
