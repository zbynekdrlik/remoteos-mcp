"""Tool failures as real MCP errors (#32): the error type and the tool decorator.

A failing tool RAISES and never returns error text as a successful result.
FastMCP 4.0.3 turns a raised ``ToolError`` into ``CallToolResult(isError=True)``
whose only text block is the error message. ``mask_error_details`` never hides
a ``ToolError``: it masks only other exceptions (``fastmcp/server/server.py``
``call_tool``). So the message is what the model reads, and it has to say what
is wrong and what to do.

- :func:`reports_errors` decorates every tool. It maps any exception to
  ``ToolError("<Tool> error: <reason>")``, the text the tools always used, and
  passes a ``ToolError`` through unchanged.
- :class:`DesktopUnavailable` is a diagnosis: the screen cannot be seen or
  driven. Its message is complete and reaches the client verbatim, without the
  tool prefix. The Linux headless/Wayland stub texts (the cam1..4 contract) and
  the platform providers' ``diagnose()`` messages below travel this way.

Pure: no OS calls. The providers' ``diagnose()`` and the tools' preflight do the I/O.
"""

from __future__ import annotations

import functools
import logging
import traceback
from typing import Callable, Optional

from fastmcp.exceptions import ToolError

__all__ = [
    "DISCONNECTED",
    "LOCKED",
    "MAC_NOT_ON_CONSOLE",
    "MAC_NO_GUI_SESSION",
    "SESSION_ZERO",
    "DesktopUnavailable",
    "ToolError",
    "failure",
    "mac_accessibility",
    "mac_screen_recording",
    "non_interactive_window_station",
    "reports_errors",
    "secure_desktop",
    "stack_frames",
    "tool_error",
]

log = logging.getLogger("remoteos.errors")


class DesktopUnavailable(RuntimeError):
    """The desktop cannot be seen or driven right now (no display, locked, no session, no permission).

    The message is the whole diagnosis, actionable on its own, and reaches the
    client as is. A ``RuntimeError``, so a provider's existing ``except
    RuntimeError`` callers keep working.
    """


# ---------------------------------------------------------------------------
# Diagnoses: what a provider's diagnose(capture) returns (None = the desktop is usable)
# ---------------------------------------------------------------------------

LOCKED = (
    "The screen is locked: screenshots show the lock screen and mouse/keyboard input goes nowhere. "
    "Someone has to unlock it (at the machine or over remote desktop), then retry."
)

SESSION_ZERO = (
    "No interactive desktop: the server runs in session 0 (as a service), which has no screen or input. "
    "Start it in the logged-in user's session with `schtasks /Run /TN RemoteOSMCP`, then retry."
)

DISCONNECTED = (
    "The desktop session is disconnected (no console or remote desktop client attached), so there is no "
    "screen to capture and input goes nowhere. Run ReconnectSession to attach it to the console, then retry."
)

MAC_NO_GUI_SESSION = (
    "No macOS GUI session: the server does not run inside a logged-in user's desktop session (e.g. it was "
    "started over ssh or as a LaunchDaemon), so screenshots and input cannot work. Run it as the logged-in "
    "user's LaunchAgent, then retry."
)

MAC_NOT_ON_CONSOLE = (
    "This macOS user session is not on the screen (another user has it, via fast user switching), so "
    "screenshots and input cannot reach it. Switch back to this user, then retry."
)


def non_interactive_window_station(name: str) -> str:
    """Windows: the process runs in a window station that is not the user's interactive ``WinSta0``."""
    return (
        f"No interactive desktop: the server runs in window station '{name}', not the user's interactive "
        "'WinSta0' (e.g. a scheduled task set to run whether the user is logged on or not), which has no "
        "screen or input. Run it in the logged-in user's session, then retry."
    )


def secure_desktop(name: Optional[str]) -> str:
    """Windows: the input desktop is not the user's: a UAC prompt, Ctrl+Alt+Del or the sign-in screen."""
    which = f"the '{name}' desktop" if name else "a secure desktop"
    return (
        f"The screen shows {which} (a UAC prompt, Ctrl+Alt+Del or the sign-in screen), which this process "
        "cannot capture or send input to. Someone has to answer it at the machine, then retry."
    )


def mac_accessibility(binary: str) -> str:
    """macOS refuses this process the mouse and keyboard (Privacy & Security › Accessibility)."""
    return (
        "macOS does not let this process control the mouse and keyboard (input would be dropped silently). "
        f"Allow {binary} in System Settings › Privacy & Security › Accessibility, then restart remoteos-mcp."
    )


def mac_screen_recording(binary: str) -> str:
    """macOS refuses this process the screen contents (Privacy & Security › Screen Recording)."""
    return (
        "macOS does not let this process record the screen (screenshots would show only the wallpaper). "
        f"Allow {binary} in System Settings › Privacy & Security › Screen Recording, then restart remoteos-mcp."
    )


def stack_frames(exc: BaseException) -> str:
    """``exc``'s traceback for the server log: the frames and the exception TYPE, never its message.

    An error message can echo a tool argument (a path, a window title, a process
    name), and logs never carry tool arguments or replies (#31). The client gets
    the message; the log gets where it broke.
    """
    frames = "".join(traceback.format_tb(exc.__traceback__))
    return f"Traceback (most recent call last):\n{frames}{type(exc).__qualname__} (message not logged)"


def failure(message: str) -> ToolError:
    """A ``ToolError`` for an expected failure whose text needs no tool prefix (``File not found: …``).

    FastMCP logs it at WARNING, like every mapped failure; a bare ``ToolError`` would be ERROR.
    """
    return ToolError(message, log_level=logging.WARNING)


def tool_error(tool: str, exc: BaseException) -> ToolError:
    """The ``ToolError`` a failure of ``tool`` reaches the client as.

    A ``ToolError`` is returned unchanged, and a :class:`DesktopUnavailable`
    keeps its whole message. Anything else becomes ``"<tool> error: <exc>"``.
    All of them are expected failures, logged by FastMCP at WARNING, not ERROR.
    """
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, DesktopUnavailable):
        return failure(str(exc))
    return failure(f"{tool} error: {exc}")


def reports_errors(fn: Optional[Callable] = None, *, preflight: Optional[Callable[[], None]] = None):
    """Decorate a tool so that every failure reaches the client as an MCP error (``isError``).

    ``@reports_errors`` or ``@reports_errors(preflight=check)``. ``check()``
    runs before the tool body and raises (typically :class:`DesktopUnavailable`)
    when the tool cannot work. The error carries the tool's name
    (``fn.__name__``), which is the MCP tool name. ``functools.wraps`` keeps the
    signature and docstring, so FastMCP builds the same schema as for the bare
    function.
    """

    def decorate(func: Callable) -> Callable:
        name = func.__name__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                if preflight is not None:
                    preflight()
                return func(*args, **kwargs)
            except ToolError:
                raise
            except DesktopUnavailable as e:
                log.info("%s: desktop unavailable: %s", name, e)
                raise tool_error(name, e) from e
            except Exception as e:
                # a helper's failure, or a bug: where it broke goes to the log, the message to the client
                log.warning("%s failed: %s\n%s", name, type(e).__name__, stack_frames(e))
                raise tool_error(name, e) from e

        wrapper.__remoteos_reports_errors__ = True  # tests assert every registered tool carries it
        return wrapper

    return decorate(fn) if fn is not None else decorate
