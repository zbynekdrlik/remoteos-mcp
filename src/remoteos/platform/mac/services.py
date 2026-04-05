"""macOS service and scheduled task management via launchctl."""

from __future__ import annotations

import os
import plistlib
import subprocess


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout+stderr."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"


# ---------------------------------------------------------------------------
# Services (launchctl)
# ---------------------------------------------------------------------------


def service_list(filter_str: str = "") -> str:
    """List macOS services via launchctl."""
    try:
        raw = _run(["launchctl", "list"])
        if filter_str:
            lines = raw.splitlines()
            header = lines[:1]
            filtered = [l for l in lines[1:] if filter_str.lower() in l.lower()]
            return "\n".join(header + filtered) if filtered else f"No services matching '{filter_str}'"
        return raw
    except Exception as e:
        return f"ServiceList error: {e}"


def service_start(name: str) -> str:
    """Start a macOS service."""
    try:
        return _run(["launchctl", "start", name])
    except Exception as e:
        return f"ServiceStart error: {e}"


def service_stop(name: str) -> str:
    """Stop a macOS service."""
    try:
        return _run(["launchctl", "stop", name])
    except Exception as e:
        return f"ServiceStop error: {e}"


# ---------------------------------------------------------------------------
# Scheduled Tasks (LaunchAgents plist)
# ---------------------------------------------------------------------------

_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")


def task_list(filter_str: str = "") -> str:
    """List LaunchAgent plist files."""
    try:
        if not os.path.isdir(_AGENTS_DIR):
            return f"No LaunchAgents directory: {_AGENTS_DIR}"
        files = [f for f in os.listdir(_AGENTS_DIR) if f.endswith(".plist")]
        if filter_str:
            files = [f for f in files if filter_str.lower() in f.lower()]
        if not files:
            return "No matching LaunchAgent plist files found."
        return "\n".join(sorted(files))
    except Exception as e:
        return f"TaskList error: {e}"


def task_create(name: str, command: str, schedule: str) -> str:
    """Create a LaunchAgent plist and load it."""
    try:
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

        load_out = _run(["launchctl", "load", plist_path])
        return f"Created and loaded {plist_path}\n{load_out}"
    except Exception as e:
        return f"TaskCreate error: {e}"


def task_delete(name: str) -> str:
    """Unload and delete a LaunchAgent plist."""
    try:
        label = f"com.remoteos.{name}"
        plist_path = os.path.join(_AGENTS_DIR, f"{label}.plist")

        unload_out = ""
        if os.path.exists(plist_path):
            unload_out = _run(["launchctl", "unload", plist_path])
            os.remove(plist_path)
            return f"Unloaded and deleted {plist_path}\n{unload_out}"
        return f"Plist not found: {plist_path}"
    except Exception as e:
        return f"TaskDelete error: {e}"


# ---------------------------------------------------------------------------
# Event Log (unified log)
# ---------------------------------------------------------------------------


def event_log(log_name: str = "system", count: int = 20, level: str = "") -> str:
    """Read macOS unified log entries."""
    try:
        cmd = ["log", "show", "--last", "1h", "--style", "compact"]
        if level:
            cmd += ["--predicate", f"messageType == {level}"]
        raw = _run(cmd, timeout=60)
        lines = raw.splitlines()
        return "\n".join(lines[:count]) if lines else "(no log entries)"
    except Exception as e:
        return f"EventLog error: {e}"
