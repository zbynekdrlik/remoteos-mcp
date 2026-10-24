"""The server announces Windows and PowerShell on a real Windows runner (#59), no fakes.

The facts come from the runner itself (windows-latest is a Windows Server); the text the Linux job
pins with faked facts (``tests/test_identity.py``) is checked here against what Windows reports.
"""

from __future__ import annotations

import platform
import socket

import remoteos.__main__ as server
from remoteos.platform.command import powershell_argv


def test_the_server_announces_this_windows_machine():
    instructions = server.mcp.instructions
    assert instructions.startswith(f"Remote OS MCP Server on {socket.gethostname()} — Windows "), instructions
    assert f"(build {platform.version()}), {platform.machine()}. " in instructions, instructions
    if "Server" in (platform.win32_edition() or ""):  # the CI runner: never CPython's "2022Server" guess
        assert " — Windows Server (build " in instructions, instructions
    assert "for Windows, macOS, and Linux machines" not in instructions


def test_the_announced_shell_is_the_powershell_argv_shell_really_runs():
    # what the instructions name is the PowerShell command line of the real builder on this box
    announced = " ".join(powershell_argv("x")[8:11])  # powershell -NoProfile -Command
    assert f"The Shell tool runs PowerShell ({announced}, through the UTF-8 console trampoline). " in (
        server.mcp.instructions
    )
