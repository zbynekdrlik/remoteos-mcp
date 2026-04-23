"""Linux service and scheduled task management via systemctl/systemd."""

from __future__ import annotations

import os
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


def _shell_quote(s: str) -> str:
    """Return a shell-safe single-quoted string."""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Services (systemctl)
# ---------------------------------------------------------------------------


def service_list(filter_str: str = "") -> str:
    """List systemd services via systemctl."""
    try:
        raw = _run(["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"])
        if filter_str:
            lines = raw.splitlines()
            filtered = [line for line in lines if filter_str.lower() in line.lower()]
            return "\n".join(filtered) if filtered else f"No services matching '{filter_str}'"
        return raw
    except Exception as e:
        return f"ServiceList error: {e}"


def service_start(name: str) -> str:
    """Start a systemd service."""
    try:
        return _run(["systemctl", "start", name])
    except Exception as e:
        return f"ServiceStart error: {e}"


def service_stop(name: str) -> str:
    """Stop a systemd service."""
    try:
        return _run(["systemctl", "stop", name])
    except Exception as e:
        return f"ServiceStop error: {e}"


# ---------------------------------------------------------------------------
# Scheduled Tasks (systemd timers)
# ---------------------------------------------------------------------------

_SYSTEMD_DIR = "/etc/systemd/system"


def task_list(filter_str: str = "") -> str:
    """List systemd timers via systemctl."""
    try:
        raw = _run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
        if filter_str:
            lines = raw.splitlines()
            filtered = [line for line in lines if filter_str.lower() in line.lower()]
            return "\n".join(filtered) if filtered else f"No timers matching '{filter_str}'"
        return raw
    except Exception as e:
        return f"TaskList error: {e}"


def task_create(name: str, command: str, schedule: str) -> str:
    """Create a systemd service + timer pair and enable it."""
    try:
        service_name = f"remoteos-{name}.service"
        timer_name = f"remoteos-{name}.timer"
        service_path = os.path.join(_SYSTEMD_DIR, service_name)
        timer_path = os.path.join(_SYSTEMD_DIR, timer_name)

        # Build the OnCalendar / OnActiveSec / OnBootSec expression
        schedule_upper = schedule.upper()
        if schedule_upper in ("ONSTART", "ONBOOT"):
            timer_section = "OnBootSec=0"
        elif schedule_upper == "DAILY":
            timer_section = "OnCalendar=daily"
        else:
            # Numeric seconds → OnActiveSec; otherwise treat as calendar expression
            try:
                secs = int(schedule)
                timer_section = f"OnActiveSec={secs}s\nOnBootSec=0"
            except ValueError:
                timer_section = f"OnCalendar={schedule}"

        service_content = f"""[Unit]
Description=RemoteOS task: {name}

[Service]
Type=oneshot
ExecStart=/bin/sh -c {_shell_quote(command)}
"""

        timer_content = f"""[Unit]
Description=RemoteOS timer: {name}

[Timer]
{timer_section}
Persistent=true

[Install]
WantedBy=timers.target
"""

        with open(service_path, "w") as f:
            f.write(service_content)

        with open(timer_path, "w") as f:
            f.write(timer_content)

        reload_out = _run(["systemctl", "daemon-reload"])
        enable_out = _run(["systemctl", "enable", "--now", timer_name])
        return f"Created {service_path} and {timer_path}\n{reload_out}\n{enable_out}"
    except Exception as e:
        return f"TaskCreate error: {e}"


def task_delete(name: str) -> str:
    """Disable and remove a remoteos systemd timer and its service."""
    try:
        timer_name = f"remoteos-{name}.timer"
        service_name = f"remoteos-{name}.service"
        service_path = os.path.join(_SYSTEMD_DIR, service_name)
        timer_path = os.path.join(_SYSTEMD_DIR, timer_name)

        disable_out = _run(["systemctl", "disable", "--now", timer_name])
        results = [disable_out]

        for path in (timer_path, service_path):
            if os.path.exists(path):
                os.remove(path)
                results.append(f"Deleted {path}")
            else:
                results.append(f"Not found: {path}")

        results.append(_run(["systemctl", "daemon-reload"]))
        return "\n".join(results)
    except Exception as e:
        return f"TaskDelete error: {e}"


# ---------------------------------------------------------------------------
# Event Log (journalctl)
# ---------------------------------------------------------------------------

_LEVEL_MAP = {
    "critical": "crit",
    "error": "err",
    "warning": "warning",
    "warn": "warning",
    "info": "info",
    "debug": "debug",
}


def event_log(log_name: str = "system", count: int = 20, level: str = "") -> str:
    """Read systemd journal entries via journalctl."""
    try:
        cmd = ["journalctl", "--no-pager", "-n", str(count), "-o", "short-iso"]
        if log_name and log_name.lower() != "system":
            cmd += ["-u", log_name]
        if level:
            priority = _LEVEL_MAP.get(level.lower(), level)
            cmd += ["-p", priority]
        return _run(cmd, timeout=30)
    except Exception as e:
        return f"EventLog error: {e}"
