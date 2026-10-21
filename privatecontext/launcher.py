"""One-command startup for PrivateContext.

`pctx up` starts capture and the dashboard as detached background
processes (no terminal stays occupied), `pctx down` stops them, and
`pctx status` reports what's running. State (PIDs, port, db path) is
kept in <data_dir>/run/pids.json; each process logs to its own file in
the same directory so a silent background failure is still debuggable.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .db import data_dir

IS_WINDOWS = sys.platform.startswith("win")


def _run_dir() -> Path:
    d = data_dir() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _run_dir() / "pids.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    _state_path().write_text(json.dumps(state, indent=2))


def _clear_state() -> None:
    try:
        _state_path().unlink()
    except OSError:
        pass


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):  # up to ~2s for a clean shutdown
        if not _alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _spawn(name: str, argv: list[str]) -> int:
    log = open(_run_dir() / f"{name}.log", "ab")
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        close_fds=True,
        # Neutral cwd: children are spawned as `python -m ...`, which puts
        # the cwd at sys.path[0] — if the user runs `pctx up` from a folder
        # containing a stray privatecontext.py, that file would shadow the
        # installed package and the child dies with ModuleNotFoundError.
        # The run dir is ours, so nothing can shadow from there.
        cwd=str(_run_dir()),
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    return proc.pid


def _child_cmd(db: str | None, *rest: str) -> list[str]:
    cmd = [sys.executable, "-m", "privatecontext.cli"]
    if db:
        cmd += ["--db", db]
    return cmd + list(rest)


def _answers_as_dashboard(port: int, opener) -> bool:
    # Marker is the dashboard's own markup (the server-rendered
    # data-theme attribute) — a bare name match would false-positive on
    # e.g. a directory listing that happens to contain a privatecontext/
    # folder. Read the whole page; it is small and local.
    try:
        with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as resp:
            return b"data-theme" in resp.read(262144)
    except Exception:
        return False


def _find_dashboard_port(preferred: int, timeout: float = 6.0) -> int | None:
    """Return the port the dashboard actually bound, or None.

    dashboard.serve() walks preferred..preferred+9 and binds the first
    free port (on some Windows machines the default is blocked), so the
    requested port is not necessarily the serving one — probe the same
    range and report the port that answers as PrivateContext.
    ProxyHandler({}) — the user's localhost must never route via a proxy.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while True:
        for candidate in range(preferred, preferred + 10):
            if _answers_as_dashboard(candidate, opener):
                return candidate
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def up(db: str | None = None, port: int = 18790) -> int:
    state = _load_state()
    # Drop stale entries for processes that already exited.
    for name in ("capture", "dashboard"):
        if not _alive(state.get(name)):
            state.pop(name, None)

    if state.get("capture") and state.get("dashboard"):
        print(f"already running — capture (pid {state['capture']}), "
              f"dashboard (pid {state['dashboard']})")
        print(f"dashboard: http://127.0.0.1:{state.get('port', port)}/")
        return 0

    if not state.get("capture"):
        state["capture"] = _spawn("capture", _child_cmd(db, "capture"))
    if not state.get("dashboard"):
        state["dashboard"] = _spawn(
            "dashboard", _child_cmd(db, "dashboard", "--port", str(port)))
        state["port"] = port
    if db:
        state["db"] = db
    _save_state(state)

    time.sleep(0.5)
    for name in ("capture", "dashboard"):
        if not _alive(state.get(name)):
            print(f"{name} failed to start — see {_run_dir() / (name + '.log')}",
                  file=sys.stderr)
            return 1

    actual = _find_dashboard_port(port)
    ready = actual is not None
    if ready and actual != state.get("port"):
        state["port"] = actual
        _save_state(state)
    print("PrivateContext is up:")
    print(f"  capture    pid {state['capture']} (logging to "
          f"{_run_dir() / 'capture.log'})")
    print(f"  dashboard  http://127.0.0.1:{state.get('port', port)}/"
          + ("" if ready else "  (still starting — give it a second)"))
    print("Stop everything with: pctx down")
    return 0


def down() -> int:
    state = _load_state()
    stopped = []
    for name in ("capture", "dashboard"):
        pid = state.get(name)
        if _alive(pid):
            _kill(pid)
            stopped.append(name)
    _clear_state()
    if stopped:
        print("stopped: " + ", ".join(stopped))
    else:
        print("nothing running")
    return 0


def status() -> int:
    state = _load_state()
    cap, dash = state.get("capture"), state.get("dashboard")
    print(f"capture:   {'running (pid ' + str(cap) + ')' if _alive(cap) else 'not running'}")
    if _alive(dash):
        print(f"dashboard: running (pid {dash}) — "
              f"http://127.0.0.1:{state.get('port', 18790)}/")
    else:
        print("dashboard: not running")
    if not _alive(cap) and not _alive(dash):
        print("start everything with: pctx up")
    return 0
