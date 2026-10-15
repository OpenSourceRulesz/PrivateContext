"""pctx setup — first-run wizard.

Welcome -> doctor checks -> optional browser-history import ->
optional autostart -> the three commands that matter. Every prompt
accepts empty input (the default); --yes runs fully non-interactively.
"""
from __future__ import annotations


def _ask(input_fn, prompt: str, default: str = "", yes: bool = False) -> str:
    if yes:
        return default
    try:
        ans = input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return ans or default


def run_setup(input_fn=input, yes: bool = False,
              db_path: str | None = None) -> int:
    from .doctor import FAIL, OK, WARN, run_checks

    print("PrivateContext setup — local-first activity tracking.")
    print("Everything stays on this machine. Nothing is uploaded, ever.\n")

    print("Checking this machine:")
    tag = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    for c in run_checks():
        line = f"  {tag[c['status']]}  {c['name']}"
        if c["detail"]:
            line += f"  — {c['detail']}"
        print(line)
    print()

    # Browser history import (day-one value).
    choice = _ask(
        input_fn,
        "Import browser history? [chrome/edge/firefox/skip] (default: skip): ",
        default="", yes=yes,
    ).lower()
    if choice in ("chrome", "edge", "firefox"):
        from .db import connect, init_db
        from .importers import import_browser
        con = connect(db_path)
        init_db(con)
        rc = import_browser(con, choice)
        print("import finished.\n" if rc == 0
              else "import had issues (see above).\n")
    else:
        print("skipped browser import.\n")

    # Autostart.
    ans = _ask(
        input_fn,
        "Start capture automatically at login? [y/N] (default: no): ",
        default="", yes=yes,
    ).lower()
    if ans in ("y", "yes"):
        from .autostart import install
        ok, msg = install()
        print(("installed — " if ok else "not installed — ") + msg + "\n")
    else:
        print("skipped autostart — run `pctx capture` manually.\n")

    print("You're set. The three commands that matter:")
    print("  pctx capture     start the capture loop")
    print("  pctx dashboard   open the local dashboard")
    print("  pctx summarize   nightly recap with a local model (needs Ollama)")
    return 0


def main(argv_yes: bool = False, db_path: str | None = None) -> int:
    return run_setup(yes=argv_yes, db_path=db_path)


if __name__ == "__main__":
    raise SystemExit(run_setup())
