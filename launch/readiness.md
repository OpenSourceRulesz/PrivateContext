# Launch readiness checklist

Do not post anything in `launch/` until every box is checked. A launch
spent debugging is a launch wasted.

- [ ] One full week of real daily use on Windows (your machine)
- [ ] Windows watcher path verified: no crashes, CPU negligible,
      events flowing
- [ ] `pctx import --browser chrome` tested against real Chrome history
- [ ] One real Ollama nightly summary generated and read — is it
      actually useful?
- [ ] Dashboard has been opened daily for a week — anything annoying?
- [ ] README has 2–3 real screenshots of the dashboard (not mockups)
- [ ] A friend installs from scratch following only the README and it
      works (the "zero-assumption" test)
- [ ] Known-issues section in README matches reality (no surprises
      for early users)
- [ ] `pctx mcp` tested with at least one real client (Claude Desktop
      or Claude Code)
- [ ] Purge and scrubbed export tested on real data

When all boxes are checked: post Show HN first (Tuesday–Thursday
morning US), then follow `venues.md`.
