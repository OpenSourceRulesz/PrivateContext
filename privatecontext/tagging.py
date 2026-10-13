"""Heuristic auto-tagging for PrivateContext.

extract_tags(app, title, url) -> list[str] finds:

  * ticket IDs — ServiceNow-style ``INC12345`` (5+ digits) and
    JIRA-style ``PROJ-123``;
  * a project guess for dev apps (editors, terminals, JetBrains IDEs):
    the leading window-title segment before " — " / " - ", kept only
    when it looks like a folder/repo name.

This is deliberately heuristic: it will misfire sometimes (e.g. the
JIRA pattern matches "UTF-8"). Tags are lowercase, deduped, and cheap
to recompute — `pctx retag` re-runs them over history.
"""
from __future__ import annotations

import re

# Ticket patterns. Kept deliberately to these two — no overreach.
INC_RE = re.compile(r"\bINC\d{5,}\b")
JIRA_RE = re.compile(r"\b[A-Z]{2,10}-\d+\b")

# App names (lowercased) whose window titles usually lead with a project.
DEV_APPS = {
    "code", "vscode", "visual studio code", "vscodium",
    "terminal", "iterm", "iterm2", "windows terminal", "gnome-terminal",
    "zsh", "bash", "fish", "powershell", "cmd", "command prompt",
    "intellij idea", "pycharm", "webstorm", "goland", "rider", "clion",
    "phpstorm", "rubymine", "datagrip", "android studio", "neovim", "vim",
}

# Leading title segments that are UI chrome, not projects.
STOPLIST = {
    "settings", "extensions", "terminal", "output", "problems", "debug",
    "console", "search", "explorer", "source control", "git", "welcome",
    "getting started", "new", "untitled", "untitled-1", "home",
    "documents", "desktop", "downloads",
    "zsh", "bash", "sh", "fish", "powershell", "cmd", "python", "node",
}

# Looks like a folder/repo name: 2-40 chars, lowercase alnum + -/_.
REPO_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")

_SPLIT_RE = re.compile(r"\s+[—–-]\s+")


def _project_guess(app: str | None, title: str | None) -> str | None:
    if not app or not title:
        return None
    if app.strip().lower() not in DEV_APPS:
        return None
    head = _SPLIT_RE.split(title, maxsplit=1)[0].strip()
    if not REPO_RE.match(head):
        return None
    if head in STOPLIST:
        return None
    return head


def extract_tags(app: str | None, title: str | None,
                 url: str | None = None) -> list[str]:
    """Return deduped lowercase tags for an event. Never raises."""
    tags: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = t.lower()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)

    try:
        hay = " ".join(x for x in (title, url) if x)
        for m in INC_RE.finditer(hay):
            add(m.group(0))
        for m in JIRA_RE.finditer(hay):
            add(m.group(0))
        proj = _project_guess(app, title)
        if proj:
            add(proj)
    except Exception:
        pass  # tagging must never break capture
    return tags
