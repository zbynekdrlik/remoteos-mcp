"""What the server tells a client about the machine it runs on (#59).

A box's MCP name (``win-stream-snv``) is only a label, and it went stale when stream-snv was
reinstalled from Windows to Ubuntu: clients kept sending PowerShell to a bash box. So the server
announces its real hostname, OS, architecture and shell in the MCP ``instructions`` (sent in the
``initialize`` result, before any tool call) and in ``/health``.

``server_instructions`` is pure: facts in, text out. ``machine_identity`` reads the live facts.
The POSIX shells live here and ``Shell`` runs exactly these (one place); the Windows text
describes ``powershell_argv``. ``tests/test_identity.py`` pins that ``Shell`` runs what is announced.
"""

from __future__ import annotations

import platform
import socket

from remoteos.platform import is_macos, is_windows

# The shell binary Shell runs as ``[<shell>, "-c", command]`` on macOS / Linux.
MACOS_SHELL = "/bin/zsh"
LINUX_SHELL = "/bin/bash"
# What Shell runs on Windows: ``powershell_argv`` (remoteos/platform/command.py).
WINDOWS_SHELL_TEXT = "PowerShell (powershell -NoProfile -Command, through the UTF-8 console trampoline)"

CAPABILITIES = (
    "Provides desktop control, window management, shell execution, file operations, network tools, "
    "registry, services, and system management tools."
)


def server_instructions(*, hostname: str, os_name: str, arch: str, shell: str) -> str:
    """The MCP ``instructions`` text for a machine with these facts."""
    return f"Remote OS MCP Server on {hostname} — {os_name}, {arch}. The Shell tool runs {shell}. {CAPABILITIES}"


def shell_text() -> str:
    """What the Shell tool runs on this OS, the same branch ``Shell`` takes."""
    if is_windows():
        return WINDOWS_SHELL_TEXT
    return f"{MACOS_SHELL if is_macos() else LINUX_SHELL} -c"


def _os_name() -> str:
    if is_windows():
        return f"Windows {platform.release()} (build {platform.version()})"
    if is_macos():
        version = platform.mac_ver()[0]
        return f"macOS {version}" if version else f"macOS (Darwin {platform.release()})"
    try:
        pretty = platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except OSError:  # no /etc/os-release nor /usr/lib/os-release
        pretty = ""
    return pretty or f"{platform.system()} {platform.release()}"


def machine_identity() -> dict[str, str]:
    """The live facts ``server_instructions`` takes: hostname, OS name, architecture, shell."""
    return {
        "hostname": socket.gethostname() or "unknown host",
        "os_name": _os_name(),
        "arch": platform.machine() or "unknown architecture",
        "shell": shell_text(),
    }
