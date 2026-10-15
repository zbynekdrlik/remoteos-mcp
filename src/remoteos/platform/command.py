"""Run one OS command for a service / task / defaults helper and return its output as the tool's answer.

The one shared runner of the Windows, Linux and macOS helpers (#32 review round 1).
It replaces their copies of the same ``_run``.
"""

from __future__ import annotations

import subprocess
from typing import Optional


def run_command(cmd: list[str], timeout: int = 30, check: bool = False, label: Optional[str] = None) -> str:
    """Run ``cmd`` and return stdout, then ``[STDERR] …`` and ``[Exit code: N]`` when there are any.

    ``check=True`` is for a command that CHANGES state (start, stop, create,
    delete, write): a non-zero exit means it failed, so it raises
    ``RuntimeError("<label> failed (exit N): <stderr>")``, which the tool reports
    as an MCP error. Without it, the exit code is part of the answer, the way a
    listing reports it (e.g. Get-WinEvent's "No events were found").
    """
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        reason = (result.stderr or "").strip() or (result.stdout or "").strip() or "(no output)"
        raise RuntimeError(f"{label or ' '.join(cmd[:2])} failed (exit {result.returncode}): {reason}")
    output = result.stdout or ""
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"
