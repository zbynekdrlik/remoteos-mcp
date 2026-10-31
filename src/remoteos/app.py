"""The MCP server instance and what the tool modules share (#49).

``mcp`` is the one FastMCP instance; ``remoteos.tools`` registers every tool on it. This is the first
module of ours the server imports, so the Windows DPI order below holds for all of them.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys as _sys

if _sys.platform == "win32":
    # Per-monitor-v2 DPI awareness MUST be set before pyautogui is imported (#28):
    # pyautogui's import calls SetProcessDPIAware() (system-aware), after which the
    # process can no longer become per-monitor aware and screenshots (physical
    # pixels), window rects and SetCursorPos disagree on mixed-DPI setups.
    from remoteos.platform.win.dpi import enable_per_monitor_dpi_awareness

    _WIN_DPI_MODE = enable_per_monitor_dpi_awareness()
if _sys.platform != "linux":
    import pyautogui

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from remoteos import __version__, desktop
from remoteos.coords import contract_line, frame_for
from remoteos.errors import DesktopUnavailable, failure, redact_fastmcp_argument_logs, reports_errors
from remoteos.identity import has_admin, machine_identity, server_instructions
from remoteos.platform import get_desktop, is_windows

load_dotenv()

log = logging.getLogger("remoteos.mcp")
redact_fastmcp_argument_logs()  # FastMCP's "Invalid arguments" warning would log the argument values (#32)

# The real hostname / OS / shell, read once (#59): a stale win-/linux- MCP name cannot mislead a
# client, which gets these facts at initialize, before its first Shell call, and in /health.
_IDENTITY = machine_identity()

mcp = FastMCP("remoteos-mcp", instructions=server_instructions(**_IDENTITY))


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    # status + version: the installers' self-check reads version; os + hostname are #59's; admin is
    # #57's, read by the gateway's check. No key is needed here (auth._PUBLIC_PATHS), so nothing
    # secret ever goes into this reply.
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "os": _IDENTITY["os_name"],
            "hostname": _IDENTITY["hostname"],
            "admin": await asyncio.to_thread(has_admin),
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tobool(v: bool | str) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def _require_win32(tool_name: str = "This tool") -> None:
    """Raise when the Windows desktop dependency (pywin32) this tool needs is missing."""
    if is_windows() and not desktop.HAS_WIN32:
        raise failure(f"pywin32 not installed — {tool_name} requires it (`pip install pywin32`).")


# Frames and the desktop preflight: shared by the desktop and media tools.


def _current_frame(monitor: int, max_width: int):
    return frame_for(monitor, max_width, get_desktop().capture_geometry(monitor))


def _capture_snapshot_frame(monitor: int, max_width: int):
    """``(image, frame)`` for Snapshot/AnnotatedSnapshot.

    On Windows a failed grab is retried once. If the session went disconnected
    since the preflight it is reattached first; the log and the error say which
    of the two happened (#32 review round 2).
    """
    try:
        img, frame = get_desktop().capture_frame(monitor=monitor, max_width=max_width)
    except Exception as first_error:
        if not is_windows():
            raise
        try:
            status = _ensure_session_connected()
        except RuntimeError as reconnect_error:
            log.warning("capture failed (%s); the session state is unusable: %s", first_error, reconnect_error)
            raise first_error
        how = "after session reconnect" if status == "reattached" else "after a retry"
        log.warning("capture failed (%s) — retrying (%s)", first_error, how)
        try:
            img, frame = get_desktop().capture_frame(monitor=monitor, max_width=max_width)
        except Exception as retry_error:
            raise RuntimeError(f"{how}: {retry_error}") from retry_error
    log.debug("snapshot frame: %s", contract_line(frame))
    return img, frame


def _desktop_preflight(need: str) -> None:
    """Stop a desktop tool before it touches a desktop that cannot be used (#32).

    ``need`` is what the tool needs from the desktop: ``"capture"`` (read the
    screen), ``"input"`` (send mouse/keyboard events) or ``"windows"`` (only the
    window list: the user's desktop, locked or not). The provider's
    ``diagnose(need)`` is cheap and read-only. Only a positive finding raises,
    as :class:`DesktopUnavailable`, its text verbatim to the client. A
    diagnose() that itself fails is logged and the tool goes ahead: a probe
    never blocks a desktop it could not read. Nothing here changes the session.
    A disconnected Windows session is reported, and reattaching it (``tscon``)
    is the explicit ReconnectSession tool's job (#32 review round 1).
    """
    try:
        reason = get_desktop().diagnose(need)
    except Exception as e:
        log.warning("desktop probe failed (%s: %s): assuming the desktop is usable", type(e).__name__, e)
        return
    if reason:
        log.warning("desktop unavailable (%s): %s", need, reason)
        raise DesktopUnavailable(reason)
    log.debug("desktop preflight (%s): usable", need)


# A tool that reads the screen / one that sends input: @reports_errors with the matching preflight.
_capture_tool = reports_errors(preflight=functools.partial(_desktop_preflight, "capture"))
_input_tool = reports_errors(preflight=functools.partial(_desktop_preflight, "input"))


def _ensure_session_connected(force: bool = False) -> str:
    """Attach this server's own Windows session to the console when it is disconnected.

    Returns ``"reattached"`` (tscon ran and the session is attached now) or
    ``"attached"`` (it already was: console or remote desktop; also off Windows).
    Raises RuntimeError when the state cannot be read or tscon did not attach it.
    The provider reads the session through ProcessIdToSessionId + WTSConnectState
    and re-checks it after ``tscon`` (#32 review rounds 1-2). The old ``query
    session`` parse never found a real disconnected session (its SESSIONNAME is blank).
    """
    if not is_windows():
        return "attached"
    return get_desktop().reconnect_to_console(force=force)
