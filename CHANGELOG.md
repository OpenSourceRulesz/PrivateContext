# Changelog

What changed, in plain language. Newest first.

## 0.6.4

- Fixed `pctx up` failing with "capture failed to start" when run from a
  folder containing a `privatecontext.py` file: the launcher now starts
  its background processes from a neutral folder, so a stray file can no
  longer shadow the installed package.

## 0.6.3

- One command runs everything: `pctx up` starts capture and the
  dashboard in the background (no terminal stays occupied), `pctx down`
  stops both, `pctx status` shows what's running. If the dashboard has
  to move to a fallback port, `pctx up` reports the port it's actually on.
- `pctx summarize --preview` is the new name for `--dry-run` — same
  behavior (print the condensed digest, call no model). `--dry-run`
  keeps working as a silent alias.

## 0.6.2

- Fixed duplicate rows in the summary digest's "Clicks per app" section —
  Teams or Windows Shell could appear several times instead of once, merged.
- The screenshot libraries (mss, Pillow) moved out of the default install
  into a `[screenshots]` extra: the default install carries no screenshot
  libraries at all. (Screenshots were rejected as a feature on security
  grounds; the code path stays dormant without the extra.)
- Summaries are now presented as what they are — optional. The journal
  needs no AI and no Ollama; when Ollama is unreachable, `pctx summarize`
  points at the bring-your-own-model paths instead of just an install link.
- New MCP tool `get_digest`: a connected model can fetch the condensed
  day digest and write a summary itself, no Ollama required.
- A brand-new, empty journal now opens with a welcome panel explaining
  what PrivateContext is and how to start capturing.
- More app-name cleanups: taskbar → Windows Shell, Zoom's hybrid-conf
  window → Zoom, Cisco Secure Client gets its real name.

## 0.6.1

- Summary digests got cleaner app names: Teams variants merge into one,
  Windows shell noise is grouped, and lock-screen time no longer counts
  as an app — it folds into the "Away total" line instead.

## 0.6.0

- Week review page and month view in the dashboard (stickers on the
  calendar, summaries a click away).
- Honest away time: lock screen is shown as "Away", never ranked as an app.
- `pctx export --markdown` writes one readable Markdown file per day.
- MCP server rounded out: notes, tags, week summary, and stickers.

## 0.5.3

- Theme toggle pinned to the window's true top-right corner.
- Tomorrow is a planning page: it opens on Notes so you can jot down
  plans for a day that hasn't happened yet.

## 0.5.2

- Diary mode accent colors now tint the whole page background, not
  just the widgets.
- The hours' period sections (Morning / Afternoon / …) collapse like
  tabs, and labels follow the theme — "Diary | Notes" in diary mode.

## 0.5.1

- "Today's entry" renamed "Today's summary"; theme toggle moved to the
  top-right.
- Diary mode gained a five-color accent picker.
- Summaries use the exact durations from the digest — no more rounded
  or invented times.

## 0.5.0

- A Notes tab for every day (notes feed into the summaries), manual
  stickers to mark days that mattered, and a colorful Diary theme
  alongside the monotone Journal.
- Summaries rewritten in first-person journal voice, with far less
  repetition and no invented details.
- Timeline entries are newest-first within each part of the day, and
  the page opens scrolled to the current period.

## 0.4.0

- The dashboard became a paper journal: masthead date, drop-cap
  summary, "Where the day went", "Marginalia", and "The hours" —
  warm paper, serif type, zero external assets.

## 0.3.0

- Dashboard date navigation: flip through days like pages.
- Weekly summaries (`pctx summarize --week`) and `pctx purge` for
  deleting old data, plus a managed-PC troubleshooting guide.

## 0.2.3

- `OLLAMA_HOST` is honored everywhere — for Ollama served on a
  non-default port.

## 0.2.2

- `pctx summarize` fails fast with a helpful hint when Ollama isn't
  running, instead of hanging.
- Microsoft Teams gets its proper name in summaries.

## 0.2.1

- Windows autostart falls back to the per-user registry Run key when
  Task Scheduler is blocked (common on managed PCs) — still no admin
  needed.

## 0.2.0

- Autostart at login, a denylist for windows you never want recorded,
  a `pctx setup` wizard, the day recap card, and a by-tag view.

## 0.1.6

- Friendly Windows app names instead of raw window-class strings, and
  consecutive clicks collapse into single timeline entries.

## 0.1.5

- Fixed browser-history import paths for Chrome and Edge on Windows.

## 0.1.4

- The dashboard walks to the next free port when the default is
  blocked (the WinError 10013 case) instead of crashing.

## 0.1.3

- The install adapts to your OS, and `pctx doctor` reports what works
  on your machine.

## 0.1.2

- `pynput` became optional (`privatecontext[full]`), so the base
  install is pure wheels on every platform.
