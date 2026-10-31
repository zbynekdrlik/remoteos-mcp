"""Remote management tools: Shell, clipboard, processes, session, notification, Scrape."""

from __future__ import annotations

import os
import subprocess

from mcp.types import TextContent

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from remoteos import process_mgr
from remoteos.app import _ensure_session_connected, _require_win32, log, mcp
from remoteos.errors import failure, reports_errors
from remoteos.identity import LINUX_SHELL, MACOS_SHELL
from remoteos.platform import get_desktop, is_macos, is_windows
from remoteos.platform.command import powershell_argv, run_child
from remoteos.platform.linux.shell_env import shell_env
from remoteos.taskmanager import StructuredText


@mcp.tool(
    annotations=ToolAnnotations(
        title="Shell",
        destructiveHint=True,
        openWorldHint=True,
    ),
    output_schema={
        "type": "object",
        "properties": {
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "exit_code": {"type": "integer"},
            "task_id": {"type": "string"},
        },
        "required": ["stdout", "stderr", "exit_code"],
    },
)
@reports_errors
def Shell(command: str, timeout: int = 30, cwd: str = "") -> str:
    """Execute a shell command (PowerShell on Windows, zsh on macOS, bash on Linux).

    On Windows the command runs unchanged in a console switched to UTF-8 before PowerShell
    starts, so PowerShell's output comes back as exact Unicode; error records, exit codes and
    ``param(...)`` are plain PowerShell's. On macOS/Linux the output is read in the locale's
    encoding. On Linux the command also gets the server user's own session variables the
    service lacks (XDG_RUNTIME_DIR, the D-Bus session bus, DISPLAY), so ``systemctl --user``
    works. Start a GUI program detached (``setsid -f prog >/dev/null 2>&1``): a plain
    ``prog &`` keeps the output open, so the call runs to its timeout and the program is
    stopped with it. A prompt (ssh, sudo -A, a locked keyring) may open a dialog on the screen
    instead of failing: run it non-interactively (``ssh -o BatchMode=yes``,
    ``env -u DBUS_SESSION_BUS_ADDRESS``).

    The text reply is stdout, ``[STDERR] …`` and ``[Exit code: N]``; the structured content has
    the exact ``stdout``, ``stderr`` and ``exit_code``, for a client that must not parse text.

    Args:
        command: Command to execute.
        timeout: Timeout in seconds (default 30).
        cwd: Working directory (spaces and quotes are fine; ~ and $VAR, or %VAR%
            on Windows, are expanded). If provided, the command runs inside that
            directory; a path that is not an existing directory is an error and
            the command does not run.
    """
    # The directory goes to the process itself (#31): pasting it into the
    # command as ``cd {cwd}`` broke on spaces/quotes and ran ``; <cmd>`` in it.
    workdir = None
    if cwd:
        workdir = os.path.expandvars(os.path.expanduser(cwd))
        if not os.path.isdir(workdir):
            problem = "is not a directory" if os.path.exists(workdir) else "not found"
            log.warning("Shell refused: working directory %s", problem)  # never the path (#31)
            raise ValueError(f"working directory {problem}: {cwd}")
    windows = is_windows()
    env = None  # Windows and macOS: the server's own environment
    if windows:
        # unchanged, in its own console switched to UTF-8 before PowerShell starts (#48 round 2).
        # The cwd goes to PowerShell through the trampoline, which itself runs in the server's
        # directory, where "powershell" is looked up (review round 1)
        shell_cmd, workdir = powershell_argv(command, cwd=workdir), None
    elif is_macos():  # the shells identity announces at initialize (#59)
        shell_cmd = [MACOS_SHELL, "-c", command]
    else:
        shell_cmd = [LINUX_SHELL, "-c", command]
        # a system unit has no login-session environment: add the server user's own (#52)
        env = shell_env()

    # the shared runner (#48 round 2): binary, in its own console (Windows) or session (POSIX);
    # decode_output reads the bytes, and a timeout kills everything the command started
    try:
        result = run_child(shell_cmd, timeout, windows, cwd=workdir, env=env)
    except subprocess.TimeoutExpired:
        raise failure(f"Command timed out after {timeout}s") from None  # run_child logged it
    # A non-zero exit is the command's own answer (it ran), not a tool failure.
    return StructuredText(
        result.reply(), {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="GetClipboard",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def GetClipboard() -> str:
    """Read the clipboard text content."""
    _require_win32("GetClipboard")
    return get_desktop().get_clipboard()


@mcp.tool(
    annotations=ToolAnnotations(
        title="SetClipboard",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@reports_errors
def SetClipboard(text: str) -> str:
    """Set the clipboard text content.

    Args:
        text: Text to place on clipboard.
    """
    _require_win32("SetClipboard")
    return get_desktop().set_clipboard(text)


@mcp.tool(
    annotations=ToolAnnotations(
        title="ListProcesses",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def ListProcesses(
    filter: str = "",
    sort_by: str = "memory",
    limit: int = 30,
) -> str:
    """List running processes with CPU and memory usage.

    Args:
        filter: Fuzzy filter by process name.
        sort_by: Sort by 'cpu', 'memory', or 'name'.
        limit: Max number of processes to return.
    """
    return process_mgr.list_processes(filter_name=filter, sort_by=sort_by, limit=limit)


@mcp.tool(
    annotations=ToolAnnotations(
        title="KillProcess",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def KillProcess(pid: int = 0, name: str = "") -> str:
    """Kill a process by PID or name.

    Args:
        pid: Process ID.
        name: Process name (fuzzy matched).
    """
    return process_mgr.kill_process(pid=pid, name=name)


@mcp.tool(
    annotations=ToolAnnotations(
        title="GetSystemInfo",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def GetSystemInfo() -> str:
    """Get system information: CPU, memory, disk, network, uptime."""
    return process_mgr.get_system_info()


@mcp.tool(annotations=ToolAnnotations(title="ReconnectSession", readOnlyHint=False))
@reports_errors
def ReconnectSession(force: bool = False) -> list:
    """Reconnect a disconnected Windows desktop session to the console.

    This enables screenshot and UI automation tools to work when no RDP
    client is actively connected. Runs 'tscon' to attach this server's own
    session to the console, then checks it worked. This puts the desktop on the
    machine's physical screen. A session that is already attached (console or
    remote desktop) is left alone.

    Args:
        force: Reconnect even if session appears active (default False).
    """
    try:
        status = _ensure_session_connected(force=force)
    except RuntimeError as e:
        raise failure(f"ReconnectSession failed: {e}") from e
    if status == "reattached":
        return [TextContent(type="text", text="Session connected to console")]
    return [TextContent(type="text", text="Session already attached (console or remote desktop): nothing to do")]


@mcp.tool(
    annotations=ToolAnnotations(
        title="Notification",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@reports_errors
def Notification(title: str = "remoteos-mcp", message: str = "") -> str:
    """Show a desktop notification.

    Args:
        title: Notification title.
        message: Notification body text.
    """
    return get_desktop().show_notification(title, message)


@mcp.tool(
    annotations=ToolAnnotations(
        title="LockScreen",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def LockScreen() -> str:
    """Lock the workstation."""
    return get_desktop().lock_screen()


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scrape",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
@reports_errors
def Scrape(url: str) -> str:
    """Fetch an http(s) URL and return its content as markdown (at most 5 MiB read).

    Args:
        url: URL to fetch.
    """
    from remoteos.scrape import fetch_markdown

    return fetch_markdown(url)
