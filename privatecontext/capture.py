"""Event-driven capture loop for PrivateContext.

Live mode:
  * active-window watcher, once per second, per-OS with stdlib only
    (Linux: xprop/X11, macOS: osascript, Windows: ctypes). Degrades to a
    warning when the platform hooks are unavailable.
  * click watcher via pynput — logs x, y, window, url, timestamp.
    Key *contents* are never recorded; the keyboard listener exists only
    to timestamp activity for AFK detection.
  * AFK watcher: no input for 180 s -> 'afk' event; next input -> 'back'.
  * URL capture: best-effort active-tab URL (macOS browsers via AppleScript
    in v0); query params and fragments are ALWAYS stripped before storage.
  * Screenshots: OFF by default. With --screenshots, the primary monitor is
    captured on app-switch / URL-change only, downscaled to 640 px wide and
    stored as JPEG (quality 70, Pillow) under shots/YYYY-MM-DD/.

Simulate mode (--simulate): writes a realistic synthetic workday straight
into the DB. No OS hooks, no pynput, no mss — runs anywhere, including
headless CI. Used to test the whole pipeline end to end.
"""
from __future__ import annotations

import ctypes
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .db import connect, data_dir, init_db, log_event, strip_url

POLL_S = 1.0
IDLE_AFTER_S = 180
SHOT_MAX_W = 640

_warned: set[str] = set()


def warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[privatecontext] {msg}", file=sys.stderr)


def _run(cmd: list[str], timeout: int = 2) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Active window, per OS (stdlib only)
# ---------------------------------------------------------------------------

def _active_window_linux() -> dict | None:
    if not shutil.which("xprop"):
        warn_once("xprop", "'xprop' not found: window watcher disabled "
                            "(v0 supports X11 only, not Wayland)")
        return None
    root = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    m = re.search(r"0x[0-9a-fA-F]+", root)
    if not m or m.group(0) == "0x0":
        return None
    out = _run(["xprop", "-id", m.group(0), "WM_CLASS", "_NET_WM_NAME"])
    app_m = re.search(r'WM_CLASS\(STRING\) = "(?:[^"]+)", "([^"]+)"', out)
    title_m = re.search(r'_NET_WM_NAME\(UTF8_STRING\) = "(.*)"', out)
    return {"app": app_m.group(1) if app_m else None,
            "title": title_m.group(1) if title_m else None}


def _active_window_macos() -> dict | None:
    app = _run(["osascript", "-e",
                'tell application "System Events" to get name of first '
                'process whose frontmost is true'])
    if not app:
        warn_once("macos", "osascript failed: window watcher disabled")
        return None
    title = _run(["osascript", "-e",
                  'tell application "System Events" to tell '
                  '(first process whose frontmost is true) to get name of front window'])
    return {"app": app or None, "title": title or None}


def _active_window_windows() -> dict | None:
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:
        warn_once("win", "ctypes.windll unavailable: window watcher disabled")
        return None
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    tbuf = ctypes.create_unicode_buffer(512)
    cbuf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, tbuf, 512)
    user32.GetClassNameW(hwnd, cbuf, 256)
    return {"app": cbuf.value or None, "title": tbuf.value or None}


def active_window() -> dict | None:
    """Return {'app': ..., 'title': ...} or None. Never raises."""
    sysname = platform.system()
    try:
        if sysname == "Linux":
            return _active_window_linux()
        if sysname == "Darwin":
            return _active_window_macos()
        if sysname == "Windows":
            return _active_window_windows()
        warn_once("os", f"unsupported OS ({sysname}): window watcher disabled")
        return None
    except Exception as e:  # a watcher must never kill the loop
        warn_once("watcher-err", f"window watcher error: {e}")
        return None


# ---------------------------------------------------------------------------
# Browser URL (best effort; always stripped of query/fragment by log_event)
# ---------------------------------------------------------------------------

_MAC_BROWSERS = {
    "Google Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "Safari": 'tell application "Safari" to get URL of front document',
}


def browser_url(app: str | None, title: str | None) -> str | None:
    if platform.system() != "Darwin" or app not in _MAC_BROWSERS:
        return None  # v0: no browser extension yet; titles still logged
    url = _run(["osascript", "-e", _MAC_BROWSERS[app]])
    return url or None


# ---------------------------------------------------------------------------
# Screenshots (opt-in): downscaled JPEG via Pillow
# ---------------------------------------------------------------------------

JPEG_QUALITY = 70


def _downscale_rgb(rgb: bytes, w: int, h: int, target_w: int = SHOT_MAX_W):
    if w <= target_w:
        return rgb, w, h
    scale = target_w / w
    tw, th = target_w, max(1, int(h * scale))
    out = bytearray(tw * th * 3)
    for y in range(th):
        sy = min(h - 1, int(y / scale))
        for x in range(tw):
            sx = min(w - 1, int(x / scale))
            si, di = (sy * w + sx) * 3, (y * tw + x) * 3
            out[di:di + 3] = rgb[si:si + 3]
    return bytes(out), tw, th


def _write_jpeg(path: Path, rgb: bytes, w: int, h: int,
                quality: int = JPEG_QUALITY) -> None:
    from PIL import Image
    Image.frombytes("RGB", (w, h), rgb).save(path, "JPEG", quality=quality)


def take_screenshot(ts: float) -> str | None:
    """Capture primary monitor, downscaled. Returns path relative to data dir."""
    try:
        import mss
    except Exception as e:
        warn_once("mss", f"mss unavailable ({e}): screenshots disabled")
        return None
    try:
        from PIL import Image  # noqa: F401  (import check; used in _write_jpeg)
    except Exception as e:
        warn_once("pil", f"Pillow unavailable ({e}): screenshots disabled")
        return None
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
        rgb, w, h = _downscale_rgb(shot.rgb, shot.width, shot.height)
        dt = datetime.fromtimestamp(ts).astimezone()
        rel = Path("shots") / dt.strftime("%Y-%m-%d") / (dt.strftime("%H%M%S") + ".jpg")
        p = data_dir() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_jpeg(p, rgb, w, h)
        return str(rel)
    except Exception as e:
        warn_once("shot-err", f"screenshot failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Input watchers (pynput, imported lazily so --simulate never needs it)
# ---------------------------------------------------------------------------

class WatchState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_input = time.time()


def start_input_watchers(con, state: WatchState) -> list:
    """Mouse + keyboard listeners. The keyboard listener records a timestamp
    only — key contents are never stored, anywhere, ever."""
    try:
        from pynput import keyboard, mouse
    except Exception as e:
        warn_once("pynput", f"pynput unavailable ({e}): click + AFK watchers "
                            "disabled — install them with "
                            'pip install "privatecontext[full]"')
        return []

    def touch() -> None:
        with state.lock:
            state.last_input = time.time()

    def on_click(x, y, button, pressed) -> None:
        if not pressed:
            return
        touch()
        w = active_window() or {}
        url = browser_url(w.get("app"), w.get("title"))
        with state.lock:  # sqlite handle is shared with the main loop
            log_event(con, "click", app=w.get("app"), title=w.get("title"),
                      url=url, x=int(x), y=int(y))

    def on_press(key) -> None:
        touch()  # timestamp only — key contents are never recorded

    ml = mouse.Listener(on_click=on_click)
    kl = keyboard.Listener(on_press=on_press)
    ml.daemon = kl.daemon = True
    ml.start()
    kl.start()
    return [ml, kl]


# ---------------------------------------------------------------------------
# Live capture loop
# ---------------------------------------------------------------------------

def run_capture(db_path: str | None = None, screenshots: bool = False) -> None:
    con = connect(db_path)
    init_db(con)
    target = db_path or "default location"
    print(f"[privatecontext] capturing -> {target} "
          f"(screenshots={'ON' if screenshots else 'off'}) — Ctrl-C to stop")
    state = WatchState()
    listeners = start_input_watchers(con, state)

    last_sig: tuple = (None, None)
    last_url: str | None = None
    afk, afk_start = False, 0.0
    try:
        while True:
            now = time.time()
            with state.lock:
                idle = now - state.last_input
            if listeners and not afk and idle > IDLE_AFTER_S:
                afk, afk_start = True, now
                log_event(con, "afk")
                print("[privatecontext] idle: AFK")
            elif afk and idle <= IDLE_AFTER_S:
                afk = False
                log_event(con, "back", duration_ms=int((now - afk_start) * 1000))
                print(f"[privatecontext] back after {int(now - afk_start)}s idle")

            w = active_window() or {}
            sig = (w.get("app"), w.get("title"))
            if any(sig) and sig != last_sig:
                url = browser_url(w.get("app"), w.get("title"))
                log_event(con, "window", app=sig[0], title=sig[1], url=url)
                if screenshots and (sig[0] != last_sig[0] or url != last_url):
                    rel = take_screenshot(now)
                    log_event(con, "shot", app=sig[0], title=sig[1], url=url)
                    # shot path is derivable from ts: shots/YYYY-MM-DD/HHMMSS.jpg
                    _ = rel
                last_sig, last_url = sig, url
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        print("\n[privatecontext] stopped.")
    finally:
        for lst in listeners:
            lst.stop()


# ---------------------------------------------------------------------------
# Simulate mode: a realistic synthetic workday, zero OS hooks
# ---------------------------------------------------------------------------

def simulate(db_path: str | None = None, day: date | None = None) -> None:
    import random

    rng = random.Random(20261013)
    con = connect(db_path)
    init_db(con)
    day = day or date.today()
    base = datetime(day.year, day.month, day.day).astimezone()  # local midnight

    def ts(h: int, m: int, s: int = 0) -> float:
        return (base + timedelta(hours=h, minutes=m, seconds=s)).timestamp()

    # (start, end, app, title, url, clicks_per_min); app=None -> AFK block
    blocks = [
        ((9, 2), (9, 24), "Slack", "OIT Help Desk — #general", None, 3),
        ((9, 24), (11, 38), "Google Chrome",
         "INC0012345 — Printer jam, Hill Library | ServiceNow",
         "https://ncsu.service-now.com/nav_to.do?uri=incident.do&sys_id=9f3a1c", 7),
        ((11, 38), (11, 55), "Google Chrome",
         "eduroam keeps dropping on Dell 5420 — Ask Ubuntu",
         "https://askubuntu.com/questions/12345/eduroam-keeps-dropping?utm_source=x&x=1", 5),
        ((11, 55), (12, 8), "Code", "privatecontext — capture.py", None, 9),
        ((12, 8), (12, 52), None, None, None, 0),  # lunch
        ((12, 52), (14, 5), "Google Chrome",
         "INC0012371 — VPN split tunnel question | ServiceNow",
         "https://ncsu.service-now.com/incident.do?sys_id=77aa01&sysparm_nostack=true", 6),
        ((14, 5), (14, 50), "Zoom", "OIT weekly standup", None, 1),
        ((14, 50), (16, 20), "Code", "privatecontext — dashboard.py", None, 8),
        ((16, 20), (16, 35), "Terminal", "zsh — ~/workspace/privatecontext", None, 4),
        ((16, 35), (17, 10), "Spotify", "Deep Focus", None, 1),
    ]

    n_win = n_click = n_afk = 0
    for (sh, sm), (eh, em), app, title, url, rate in blocks:
        t0, t1 = ts(sh, sm), ts(eh, em)
        if app is None:
            log_event(con, "afk", ts=t0, commit=False)
            log_event(con, "back", ts=t1, duration_ms=int((t1 - t0) * 1000), commit=False)
            n_afk += 1
            continue
        log_event(con, "window", ts=t0, app=app, title=title, url=url, commit=False)
        n_win += 1
        if rng.random() < 0.7:  # mid-block title refinement, like a ticket updating
            tmid = t0 + (t1 - t0) * rng.uniform(0.3, 0.7)
            log_event(con, "window", ts=tmid, app=app, title=title,
                      url=url, commit=False)
            n_win += 1
        if rate:
            t = t0 + rng.uniform(20, 90)
            while t < t1 - 10:
                log_event(con, "click", ts=t, app=app, title=title, url=url,
                          x=rng.randint(100, 1800), y=rng.randint(80, 950),
                          commit=False)
                n_click += 1
                t += (60.0 / rate) * rng.uniform(0.5, 1.5)
    con.commit()
    print(f"[privatecontext] simulated {day.isoformat()}: "
          f"{n_win} window events, {n_click} clicks, {n_afk} AFK periods")
