"""Run one OS command for a service / task / defaults helper and return its output as the tool's answer.

The one shared runner of the Windows, Linux and macOS helpers (#32 review round 1).
It replaces their copies of the same ``_run``. ``console_decoding`` is how every
console program's output is decoded (round 4).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional


def console_decoding(windows: Optional[bool] = None) -> dict:
    """``subprocess`` text-mode arguments for a console program's output.

    On Windows that is the console's OEM code page (``"oem"``): PowerShell and
    native tools write it, e.g. cp852 on the Slovak fleet. Python's default
    for a pipe is the ANSI page (cp1250), which garbled "č" (live on
    win-pz-server). Elsewhere it is the locale. ``errors="replace"`` always: an
    undecodable byte becomes U+FFFD and never loses the output. On Windows a
    decode error in subprocess's reader thread leaves stdout ``None``.
    ``windows=None`` means this machine.
    """
    if windows is None:
        windows = sys.platform == "win32"
    return {"text": True, "encoding": "oem" if windows else None, "errors": "replace"}


def run_command(cmd: list[str], timeout: int = 30, check: bool = False, label: Optional[str] = None) -> str:
    """Run ``cmd`` and return stdout, then ``[STDERR] …`` and ``[Exit code: N]`` when there are any.

    ``check=True`` is for a command that CHANGES state (start, stop, create,
    delete, write): a non-zero exit means it failed, so it raises
    ``RuntimeError("<label> failed (exit N): <stderr>")``, which the tool reports
    as an MCP error. Without it, the exit code is part of the answer, the way a
    listing reports it (e.g. Get-WinEvent's "No events were found").
    """
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, **console_decoding())
    if check and result.returncode != 0:
        reason = (result.stderr or "").strip() or (result.stdout or "").strip() or "(no output)"
        raise RuntimeError(f"{label or ' '.join(cmd[:2])} failed (exit {result.returncode}): {reason}")
    output = result.stdout or ""
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"
