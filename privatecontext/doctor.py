"""pctx doctor — platform capability report.

Tells you what works on *this* machine and why not, so "is it working?"
is always one command away. Never fails the install; features degrade
with an explanation instead.
"""
from __future__ import annotations

import ctypes
import platform
import shutil
import sqlite3
import sys
import urllib.request
from importlib.metadata import version as _pkg_version

OK, WARN, FAIL = "ok", "warn", "fail"


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def run_checks() -> list[dict]:
    out: list[dict] = []
    sysname = platform.system()

    try:
        ver = _pkg_version("privatecontext")
    except Exception:
        ver = "unknown (not installed via pip?)"
    out.append(_check(
        f"privatecontext {ver} on {sysname} {platform.machine()} "
        f"(Python {platform.python_version()})", OK, ""))

    # -- window watcher (the core; everything else is enrichment) --
    if sysname == "Windows":
        try:
            ctypes.windll.user32  # noqa: B018
            out.append(_check("window watcher", OK,
                              "active window via Win32 API"))
        except Exception:
            out.append(_check("window watcher", FAIL,
                              "ctypes.windll unavailable (unexpected)"))
    elif sysname == "Darwin":
        if shutil.which("osascript"):
            out.append(_check("window watcher", OK,
                              "active window via osascript"))
        else:
            out.append(_check("window watcher", FAIL,
                              "osascript not found (unexpected on macOS)"))
    else:
        if shutil.which("xprop"):
            out.append(_check("window watcher", OK,
                              "active window via xprop (X11)"))
        else:
            out.append(_check(
                "window watcher", FAIL,
                "xprop not found. On Wayland, window titles are limited; "
                "on headless servers there is nothing to watch — "
                "import/dashboard still work."))

    # -- pynput: clicks + AFK (nice-to-have, auto on Win/macOS) --
    try:
        import pynput  # noqa: F401
        out.append(_check("click + AFK tracking", OK,
                          "pynput installed"))
    except Exception:
        hint = 'pip install "privatecontext[full]"'
        if sysname not in ("Windows", "Darwin"):
            hint += " (needs Python headers on Linux: python3-dev)"
        out.append(_check("click + AFK tracking", WARN,
                          f"pynput not installed — window events still work. "
                          f"For clicks: {hint}"))

    # -- screenshots (opt-in) --
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
        out.append(_check("screenshots", OK,
                          "mss + Pillow installed (off by default; "
                          "pctx capture --screenshots to enable)"))
    except Exception as e:
        out.append(_check("screenshots", WARN,
                          f"unavailable ({e}); reinstall privatecontext"))

    # -- browser URL capture (live) --
    if sysname == "Darwin":
        out.append(_check("live browser URLs", OK,
                          "via AppleScript (macOS will ask for Automation "
                          "permission on first run — allow it)"))
    else:
        out.append(_check(
            "live browser URLs", WARN,
            "not captured live on this OS (window titles still are) — "
            "use: pctx import --browser chrome"))

    # -- Ollama (nightly summaries) --
    try:
        from .summarize import _ollama_base
        obase = _ollama_base()
        req = urllib.request.Request(obase + "/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            models = r.read(2000).decode("utf-8", "replace")
        out.append(_check("Ollama", OK,
                          f"reachable at {obase} ({models[:60]}…)"))
    except Exception:
        out.append(_check("Ollama", WARN,
                          "not reachable — pctx summarize needs "
                          "`ollama serve` + `ollama pull qwen2:1.5b` "
                          "(or set OLLAMA_HOST=host:port)"))

    # -- database --
    try:
        from .db import connect, counts, default_db_path, init_db
        dbp = default_db_path()
        import os
        exists = os.path.exists(dbp)
        con = connect(dbp)
        init_db(con)
        n = sum(counts(con).values())
        con.close()
        out.append(_check("database", OK,
                          f"{dbp} — {n} events" +
                          ("" if exists else " (created just now)")))
    except Exception as e:
        out.append(_check("database", FAIL, f"cannot open/create: {e}"))

    return out


def main() -> int:
    print("[privatecontext] doctor — capability report\n")
    checks = run_checks()
    width = max(len(c["name"]) for c in checks)
    tag = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    for c in checks:
        line = f"  {tag[c['status']]}  {c['name'].ljust(width)}"
        if c["detail"]:
            line += f"  — {c['detail']}"
        print(line)
    n_ok = sum(1 for c in checks if c["status"] == OK)
    n_warn = sum(1 for c in checks if c["status"] == WARN)
    n_fail = sum(1 for c in checks if c["status"] == FAIL)
    print(f"\n{len(checks)} checks: {n_ok} ok, {n_warn} warnings, "
          f"{n_fail} unavailable")
    if n_fail:
        print("Nothing to reinstall — unavailable features are skipped "
              "automatically; see hints above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
