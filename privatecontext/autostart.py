"""Per-OS autostart for `pctx capture`. No admin rights required.

Every public function returns (ok: bool, message: str) and never raises:
missing tools or unsupported platforms produce clear guidance instead.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

TASK_NAME = "PrivateContext"
SERVICE_NAME = "privatecontext-capture.service"
PLIST_LABEL = "com.privatecontext.capture"


def _pctx_path() -> str | None:
    return shutil.which("pctx")


def _pythonw() -> str | None:
    """pythonw.exe next to the current interpreter (Windows, no console)."""
    cand = Path(sys.executable).parent / "pythonw.exe"
    return str(cand) if cand.exists() else None


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "command not found"
    except Exception as e:  # timeouts, permission errors, ...
        return 1, str(e)


# ---------------------------------------------------------------------------
# Pure command builders (unit-tested with mocked platform/executable)
# ---------------------------------------------------------------------------

def windows_task_command(pythonw: str) -> str:
    """The /tr payload: capture with no console window."""
    return (f'"{pythonw}" -c "from privatecontext.cli import main; '
            "main(['capture'])\"")


def build_windows_schtasks(pythonw: str) -> list[str]:
    return ["schtasks", "/create", "/tn", TASK_NAME,
            "/tr", windows_task_command(pythonw),
            "/sc", "onlogon", "/f"]


def systemd_unit(pctx: str) -> str:
    return (
        "[Unit]\n"
        "Description=PrivateContext capture loop\n\n"
        "[Service]\n"
        f"ExecStart={pctx} capture\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def xdg_desktop_entry(pctx: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=PrivateContext\n"
        f"Exec={pctx} capture\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def launchd_plist(pctx: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{PLIST_LABEL}</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"    <string>{pctx}</string>\n"
        "    <string>capture</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "</dict></plist>\n"
    )


def systemd_unit_path(home: Path | None = None) -> Path:
    home = home or Path.home()
    return home / ".config" / "systemd" / "user" / SERVICE_NAME


def xdg_autostart_path(home: Path | None = None) -> Path:
    home = home or Path.home()
    return home / ".config" / "autostart" / "privatecontext.desktop"


def launchd_plist_path(home: Path | None = None) -> Path:
    home = home or Path.home()
    return home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


# ---------------------------------------------------------------------------
# install / uninstall / status
# ---------------------------------------------------------------------------

def install() -> tuple[bool, str]:
    if _pctx_path() is None:
        return False, "pctx not found on PATH — install privatecontext first."
    try:
        sysname = platform.system()
        if sysname == "Windows":
            return _install_windows()
        if sysname == "Darwin":
            return _install_macos()
        if sysname == "Linux":
            return _install_linux()
        return False, f"autostart is not supported on {sysname}."
    except Exception as e:
        return False, f"install failed: {e}"


def _install_windows() -> tuple[bool, str]:
    pythonw = _pythonw() or sys.executable
    code, out = _run(build_windows_schtasks(pythonw))
    if code == 0:
        return True, ("autostart installed: PrivateContext runs at logon "
                      "(Task Scheduler, no console window).")
    return False, f"schtasks failed: {out[:300] or 'unknown error'}"


def _install_linux() -> tuple[bool, str]:
    pctx = _pctx_path() or "pctx"
    if shutil.which("systemctl"):
        unit = systemd_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(pctx))
        _run(["systemctl", "--user", "daemon-reload"])
        code, out = _run(["systemctl", "--user", "enable", "--now",
                          SERVICE_NAME])
        if code == 0:
            return True, (f"autostart installed: {SERVICE_NAME} enabled "
                          "(systemd user service).")
        return False, f"systemctl enable failed: {out[:300] or 'unknown error'}"
    dp = xdg_autostart_path()
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(xdg_desktop_entry(pctx))
    return True, (f"no systemd found — autostart installed via {dp} "
                  "(XDG autostart).")


def _install_macos() -> tuple[bool, str]:
    pctx = _pctx_path() or "pctx"
    plist = launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(launchd_plist(pctx))
    code, out = _run(["launchctl", "load", str(plist)])
    if code == 0:
        return True, f"autostart installed: {PLIST_LABEL} loads at login."
    return False, f"launchctl load failed: {out[:300] or 'unknown error'}"


def uninstall() -> tuple[bool, str]:
    try:
        sysname = platform.system()
        if sysname == "Windows":
            return _uninstall_windows()
        if sysname == "Darwin":
            return _uninstall_macos()
        if sysname == "Linux":
            return _uninstall_linux()
        return False, f"autostart is not supported on {sysname}."
    except Exception as e:
        return False, f"uninstall failed: {e}"


def _uninstall_windows() -> tuple[bool, str]:
    code, out = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    low = out.lower()
    if code == 0:
        return True, "autostart removed (scheduled task deleted)."
    if "cannot find" in low or "does not exist" in low:
        return True, "autostart was not installed."
    return False, f"schtasks delete failed: {out[:300] or 'unknown error'}"


def _uninstall_linux() -> tuple[bool, str]:
    removed: list[str] = []
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
    unit = systemd_unit_path()
    if unit.exists():
        unit.unlink()
        removed.append("systemd unit")
    dp = xdg_autostart_path()
    if dp.exists():
        dp.unlink()
        removed.append("autostart desktop entry")
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"])
    if removed:
        return True, f"autostart removed ({', '.join(removed)})."
    return True, "autostart was not installed."


def _uninstall_macos() -> tuple[bool, str]:
    plist = launchd_plist_path()
    _run(["launchctl", "unload", str(plist)])
    if plist.exists():
        plist.unlink()
        return True, "autostart removed (LaunchAgent unloaded)."
    return True, "autostart was not installed."


def status() -> tuple[bool, str]:
    """(installed, message). Never raises."""
    try:
        sysname = platform.system()
        if sysname == "Windows":
            code, _ = _run(["schtasks", "/query", "/tn", TASK_NAME])
            return (True, "autostart is installed (Task Scheduler).") if code == 0 \
                else (False, "autostart is not installed.")
        if sysname == "Darwin":
            p = launchd_plist_path()
            return (True, f"autostart is installed ({p}).") if p.exists() \
                else (False, "autostart is not installed.")
        if sysname == "Linux":
            bits = []
            if systemd_unit_path().exists():
                bits.append("systemd unit")
            if xdg_autostart_path().exists():
                bits.append("autostart desktop entry")
            if bits:
                return True, f"autostart is installed ({', '.join(bits)})."
            return False, "autostart is not installed."
        return False, f"autostart is not supported on {sysname}."
    except Exception as e:
        return False, f"status check failed: {e}"
