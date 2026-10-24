"""The server announces Windows and PowerShell on a real Windows runner (#59), no fakes.

The facts come from the runner itself; the text the Linux job pins with faked facts
(``tests/test_identity.py``) is checked here against what Windows really reports.
"""

from __future__ import annotations

import platform
import socket

import remoteos.__main__ as server


def test_the_server_announces_this_windows_machine_and_powershell():
    expected_os = f"Windows {platform.release()} (build {platform.version()})"
    assert server.mcp.instructions.startswith(
        f"Remote OS MCP Server on {socket.gethostname()} — {expected_os}, {platform.machine()}. "
        "The Shell tool runs PowerShell (powershell -NoProfile -Command, through the UTF-8 console trampoline). "
    ), server.mcp.instructions
    assert "for Windows, macOS, and Linux machines" not in server.mcp.instructions


def test_the_announced_shell_really_answers_as_powershell():
    # the PowerShell the instructions name is what Shell runs: a PowerShell-only expression
    assert server.Shell("$PSVersionTable.PSEdition") == "Desktop"
