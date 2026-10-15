"""Run one OS command for a service / task / defaults helper and return its output as the tool's answer.

The one shared runner of the Windows, Linux and macOS helpers (#32 review round 1).
It replaces their copies of the same ``_run``. ``console_child`` is how every
console program is started and its output decoded (rounds 4-5).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

# Windows: the child gets its own (hidden) console instead of sharing ours. subprocess has this
# constant only on Windows, hence the literal.
CREATE_NO_WINDOW = 0x08000000


def console_child(windows: Optional[bool] = None) -> dict:
    """``subprocess`` arguments for a console program whose output becomes the tool's reply.

    - **Windows:** ``creationflags=CREATE_NO_WINDOW`` gives the child its OWN hidden
      console. Its code page is then the system OEM page that ``encoding="oem"``
      decodes (cp852 on the Slovak fleet).
      - A child writes the CURRENT page of the console it is attached to.
      - When every child shared the server's console, one ``chcp 65001`` in a
        Shell call garbled every later reply for every client (review round 5,
        live on win-pz-server).
      - Python's default for a pipe, the ANSI page (cp1250), garbled "č" (round 4).
    - **Elsewhere:** the locale, and no creationflags (POSIX rejects them).
    - **Always** ``errors="replace"``: an undecodable byte becomes U+FFFD and never
      loses the output. On Windows a decode error in subprocess's reader thread
      leaves stdout ``None``.
    - ``windows=None`` means this machine.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        return {"text": True, "encoding": "oem", "errors": "replace", "creationflags": CREATE_NO_WINDOW}
    return {"text": True, "encoding": None, "errors": "replace"}


def run_command(cmd: list[str], timeout: int = 30, check: bool = False, label: Optional[str] = None) -> str:
    """Run ``cmd`` and return stdout, then ``[STDERR] …`` and ``[Exit code: N]`` when there are any.

    ``check=True`` is for a command that CHANGES state (start, stop, create,
    delete, write): a non-zero exit means it failed, so it raises
    ``RuntimeError("<label> failed (exit N): <stderr>")``, which the tool reports
    as an MCP error. Without it, the exit code is part of the answer, the way a
    listing reports it (e.g. Get-WinEvent's "No events were found").
    """
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, **console_child())
    if check and result.returncode != 0:
        reason = (result.stderr or "").strip() or (result.stdout or "").strip() or "(no output)"
        raise RuntimeError(f"{label or ' '.join(cmd[:2])} failed (exit {result.returncode}): {reason}")
    output = result.stdout or ""
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"
