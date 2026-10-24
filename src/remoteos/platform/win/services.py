"""Windows service and scheduled task management.

Every caller value goes into the PowerShell script as ``ps_literal(value)``, a single-quoted literal
that PowerShell never expands (#39): a name like ``$(Set-Content x pwned)`` is a name, never code.
"""

from __future__ import annotations

from remoteos.platform.command import powershell_argv, run_command
from remoteos.platform.win.psquote import ps_literal


def _ps(command: str, timeout: int = 30, check: bool = False) -> str:
    """Run a PowerShell command, its output as exact UTF-8 (#48); ``check=True`` for one that changes state."""
    return run_command(powershell_argv(command), timeout, check=check, label="PowerShell")


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def service_list(filter_str: str = "") -> str:
    """List Windows services."""
    cmd = "Get-Service"
    if filter_str:
        pattern = ps_literal(f"*{filter_str}*")
        cmd += f" | Where-Object {{ $_.DisplayName -like {pattern} -or $_.Name -like {pattern} }}"
    cmd += " | Format-Table Name, DisplayName, Status -AutoSize"
    return _ps(cmd)


def service_start(name: str) -> str:
    """Start a Windows service."""
    return _ps(f"Start-Service -Name {ps_literal(name)} -PassThru | Format-Table Name, Status -AutoSize", check=True)


def service_stop(name: str) -> str:
    """Stop a Windows service."""
    return _ps(
        f"Stop-Service -Name {ps_literal(name)} -Force -PassThru | Format-Table Name, Status -AutoSize", check=True
    )


# ---------------------------------------------------------------------------
# Scheduled Tasks
# ---------------------------------------------------------------------------


def task_list(filter_str: str = "") -> str:
    """List scheduled tasks."""
    cmd = "Get-ScheduledTask"
    if filter_str:
        cmd += f" | Where-Object {{ $_.TaskName -like {ps_literal(f'*{filter_str}*')} }}"
    cmd += " | Format-Table TaskName, State, TaskPath -AutoSize"
    return _ps(cmd)


def task_create(name: str, command: str, schedule: str) -> str:
    """Create a scheduled task using schtasks."""
    cmd = f"schtasks /Create /TN {ps_literal(name)} /TR {ps_literal(command)} /SC {ps_literal(schedule)} /F"
    return _ps(cmd, check=True)


def task_delete(name: str) -> str:
    """Delete a scheduled task."""
    return _ps(f"schtasks /Delete /TN {ps_literal(name)} /F", check=True)


# ---------------------------------------------------------------------------
# Event Log
# ---------------------------------------------------------------------------


def event_log(log_name: str = "System", count: int = 20, level: str = "") -> str:
    """Read Windows Event Log entries."""
    count = int(count)  # a number in the script, never text (#39)
    log = ps_literal(log_name)
    cmd = f"Get-WinEvent -LogName {log} -MaxEvents {count}"
    if level:
        level_map = {
            "critical": 1,
            "error": 2,
            "warning": 3,
            "information": 4,
            "verbose": 5,
        }
        lvl_num = level_map.get(level.lower())
        if lvl_num:
            cmd = f"Get-WinEvent -FilterHashtable @{{LogName={log};Level={lvl_num}}} -MaxEvents {count}"
    cmd += " | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap"
    return _ps(cmd, timeout=30)
