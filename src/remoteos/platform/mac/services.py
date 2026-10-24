"""macOS service and scheduled task management via launchctl."""

from __future__ import annotations

import os
import plistlib

from remoteos.platform.command import run_command as _run

# ---------------------------------------------------------------------------
# Services (launchctl)
# ---------------------------------------------------------------------------


def service_list(filter_str: str = "") -> str:
    """List macOS services via launchctl."""
    raw = _run(["launchctl", "list"])
    if filter_str:
        lines = raw.splitlines()
        header = lines[:1]
        filtered = [line for line in lines[1:] if filter_str.lower() in line.lower()]
        return "\n".join(header + filtered) if filtered else f"No services matching '{filter_str}'"
    return raw


def service_start(name: str) -> str:
    """Start a macOS service."""
    return _run(["launchctl", "start", name], check=True)


def service_stop(name: str) -> str:
    """Stop a macOS service."""
    return _run(["launchctl", "stop", name], check=True)


# ---------------------------------------------------------------------------
# Scheduled Tasks (LaunchAgents plist)
# ---------------------------------------------------------------------------

_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")


def task_list(filter_str: str = "") -> str:
    """List LaunchAgent plist files."""
    if not os.path.isdir(_AGENTS_DIR):
        return f"No LaunchAgents directory: {_AGENTS_DIR}"
    files = [f for f in os.listdir(_AGENTS_DIR) if f.endswith(".plist")]
    if filter_str:
        files = [f for f in files if filter_str.lower() in f.lower()]
    if not files:
        return "No matching LaunchAgent plist files found."
    return "\n".join(sorted(files))


def task_create(name: str, command: str, schedule: str) -> str:
    """Create a LaunchAgent plist and load it."""
    os.makedirs(_AGENTS_DIR, exist_ok=True)
    label = f"com.remoteos.{name}"
    plist_path = os.path.join(_AGENTS_DIR, f"{label}.plist")

    plist_data: dict = {
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-c", command],
    }

    schedule_upper = schedule.upper()
    if schedule_upper in ("ONLOGON", "ONSTART"):
        plist_data["RunAtLoad"] = True
    elif schedule_upper == "DAILY":
        plist_data["StartInterval"] = 86400
    else:
        # Treat as raw interval in seconds if numeric, otherwise RunAtLoad
        try:
            plist_data["StartInterval"] = int(schedule)
        except ValueError:
            plist_data["RunAtLoad"] = True

    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    load_out = _run(["launchctl", "load", plist_path], check=True)
    return f"Created and loaded {plist_path}\n{load_out}"


def task_delete(name: str) -> str:
    """Unload and delete a LaunchAgent plist."""
    label = f"com.remoteos.{name}"
    plist_path = os.path.join(_AGENTS_DIR, f"{label}.plist")

    if not os.path.exists(plist_path):
        raise FileNotFoundError(f"Plist not found: {plist_path}")
    # an agent that is not loaded fails to unload; the plist is deleted anyway, so this stays an answer
    unload_out = _run(["launchctl", "unload", plist_path])
    os.remove(plist_path)
    return f"Unloaded and deleted {plist_path}\n{unload_out}"


# ---------------------------------------------------------------------------
# Event Log (unified log)
# ---------------------------------------------------------------------------


def event_log(log_name: str = "system", count: int = 20, level: str = "") -> str:
    """Read macOS unified log entries."""
    cmd = ["log", "show", "--last", "1h", "--style", "compact"]
    if level:
        if not (level.isascii() and level.isalpha()):  # a bare word, never a predicate (#39 review)
            raise ValueError("level must be a single word (e.g. error, fault, info, debug)")
        cmd += ["--predicate", f"messageType == {level}"]
    raw = _run(cmd, timeout=60)
    lines = raw.splitlines()
    return "\n".join(lines[:count]) if lines else "(no log entries)"
