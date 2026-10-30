"""What the server tells a client about the machine it runs on (#59).

A box's MCP name (``win-stream-snv``) is only a label, and it went stale when stream-snv was
reinstalled from Windows to Ubuntu: clients kept sending PowerShell to a bash box. So the server
announces its real hostname, OS, architecture and shell in the MCP ``instructions`` (sent in the
``initialize`` result, before any tool call) and in ``/health``.

``server_instructions`` is pure: facts in, text out. ``machine_identity`` reads the live facts.
The POSIX shells live here and ``Shell`` runs exactly these (one place); the Windows text
describes ``powershell_argv``. ``tests/test_identity.py`` pins that ``Shell`` runs what is announced.

``has_admin`` is the ``admin`` of ``/health`` (#57): can this server administer its box? It is read
live and cached, never once at import: the installers write the sudoers drop-in AFTER their proof
and restart nothing, so a first install would report false until its next restart.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import socket
import threading
import time

from remoteos.platform import is_macos, is_windows
from remoteos.platform.command import run_child

log = logging.getLogger("remoteos.identity")

# The shell binary Shell runs as ``[<shell>, "-c", command]`` on macOS / Linux.
MACOS_SHELL = "/bin/zsh"
LINUX_SHELL = "/bin/bash"
# What Shell runs on Windows: ``powershell_argv`` (remoteos/platform/command.py).
WINDOWS_SHELL_TEXT = "PowerShell (powershell -NoProfile -Command, through the UTF-8 console trampoline)"

# /health is public: at most one `sudo` per minute, however often it is asked.
ADMIN_CACHE_S = 60
SUDO_TIMEOUT_S = 5

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
        # A Server's release() is a guess: CPython 3.12 names every build from 20348 up
        # "2022Server" (a Server 2025 too), so a Server is named by its build alone.
        if "Server" in (platform.win32_edition() or ""):
            return f"Windows Server (build {platform.version()})"
        return f"Windows {platform.release()} (build {platform.version()})"
    if is_macos():
        version = platform.mac_ver()[0]
        return f"macOS {version}" if version else f"macOS (Darwin {platform.release()})"
    try:
        pretty = platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except (OSError, ValueError):  # no os-release file, or one that is not UTF-8: never stop the server
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


_admin_lock = threading.Lock()
_admin_cache: tuple[float, bool] | None = None  # (read at, answer)


def _read_admin() -> bool:
    """Windows: an elevated token. Linux, macOS: root, or sudo that asks no password."""
    if is_windows():
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    if os.geteuid() == 0:
        return True
    return run_child(["sudo", "-n", "true"], timeout=SUDO_TIMEOUT_S).returncode == 0


def has_admin() -> bool:
    """The cached answer. Whatever fails reads as False and never reaches ``/health``'s caller."""
    global _admin_cache
    with _admin_lock:
        now = time.monotonic()
        if _admin_cache is not None and now - _admin_cache[0] < ADMIN_CACHE_S:
            return _admin_cache[1]
        try:
            answer = _read_admin()
        except Exception as exc:  # no sudo on PATH, a timeout: no proof of admin
            log.info("admin check failed: %s", type(exc).__name__)
            answer = False
        _admin_cache = (now, answer)
        return answer
