"""remoteos-mcp — CLI entry point and MCP tool definitions."""

from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess
import sys as _sys
import time
from datetime import datetime
from pathlib import Path

import click
from click.core import ParameterSource

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
from mcp.types import ImageContent, TextContent

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from remoteos import __version__, desktop, network, ocr, process_mgr, recording
from remoteos.config import discover_config_path, load_config
from remoteos.coords import check_space, contract_line, frame_for, image_to_screen, mapping_note
from remoteos.platform import get_desktop, get_services, get_system, is_linux, is_macos, is_windows
from remoteos.scroll import normalize_request
from remoteos.security import IPAllowlistMiddleware, parse_ip_allowlist
from remoteos.snapshot import draw_annotations, element_lines, encode_jpeg, window_lines
from remoteos.taskmanager import manager as task_manager
from remoteos.tiers import (
    ALL_TOOLS,
    LINUX_ALWAYS_EXCLUDED_TOOLS,
    LINUX_DISPLAY_TOOLS,
    LINUX_EXCLUDED_TOOLS,
    get_tier_names,
    parse_tool_csv,
    resolve_enabled_tools,
)
from remoteos.typing_input import check_method

load_dotenv()

log = logging.getLogger("remoteos.mcp")

mcp = FastMCP(
    "remoteos-mcp",
    instructions=(
        "Remote OS MCP Server. Provides desktop control, window management, "
        "shell execution, file operations, network tools, registry, services, "
        "and system management tools for Windows, macOS, and Linux machines."
    ),
)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "ok", "version": __version__})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tobool(v: bool | str) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def _check_win32(tool_name: str = "This tool") -> str | None:
    """Return an error string if platform-specific desktop deps are unavailable."""
    if is_windows() and not desktop.HAS_WIN32:
        return f"Error: pywin32 not installed — {tool_name} requires it."
    return None


# ============================= DESKTOP CONTROL =============================
#
# Coordinate contract (#28): Snapshot / AnnotatedSnapshot return an image that is
# a FRAME of the screen (``remoteos.coords``) and print it as one contract line.
# Every input tool takes IMAGE pixels of that frame by default and maps them back
# to the screen here, recomputing the frame from (monitor, max_width) + the live
# display geometry — no per-session state. ``coords="screen"`` is the escape hatch.


def _current_frame(monitor: int, max_width: int):
    return frame_for(monitor, max_width, get_desktop().capture_geometry(monitor))


def _capture_snapshot_frame(monitor: int, max_width: int):
    """``(image, frame)`` for Snapshot/AnnotatedSnapshot.

    On Windows a failed grab is retried once after reconnecting a disconnected RDP
    session to the console (a disconnected session has no desktop to capture).
    """
    try:
        img, frame = get_desktop().capture_frame(monitor=monitor, max_width=max_width)
    except Exception as first_error:
        if not is_windows() or _ensure_session_connected() is not None:
            raise
        log.warning("capture failed (%s) — retrying after session reconnect", first_error)
        try:
            img, frame = get_desktop().capture_frame(monitor=monitor, max_width=max_width)
        except Exception as retry_error:
            raise RuntimeError(f"after session reconnect: {retry_error}") from retry_error
    log.debug("snapshot frame: %s", contract_line(frame))
    return img, frame


def _optional_point(x: int, y: int) -> list[tuple[int, int]]:
    """``[(x, y)]``, or ``[]`` when BOTH are 0 — the input tools' documented "no point" default.

    x=0 alone or y=0 alone is a real column/row of the frame (#31), so the test is
    ``x or y`` (upstream winremote c3a675174c), never ``x and y``.
    """
    return [(x, y)] if (x or y) else []


def _to_screen(points: list[tuple[int, int]], monitor: int, max_width: int, coords: str):
    """Map input-tool points to screen coordinates; returns ``(screen_points, reply_note)``."""
    if check_space(coords) == "screen" or not points:
        return points, ""
    if is_linux() and get_desktop().get_session().mode != "x11":
        return points, ""  # no display: the provider answers with its exact headless/Wayland stub
    frame = _current_frame(monitor, max_width)
    mapped = [image_to_screen(frame, x, y) for x, y in points]
    note = mapping_note(frame, points, mapped)
    log.debug("input mapped%s (monitor=%s, max_width=%s)", note, monitor, max_width)
    return mapped, note


@mcp.tool(
    annotations=ToolAnnotations(
        title="Snapshot",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def Snapshot(
    use_vision: bool | str = True,
    quality: int = 65,
    max_width: int = 1280,
    monitor: int = 0,
) -> list:
    """Capture desktop screenshot, window list, and interactive UI elements.

    The text starts with the coordinate contract, e.g. ``Image 1280x800 = screen
    2560x1600 at origin (0,0), scale 2.000; input tools take IMAGE pixels of this
    frame (monitor=0, max_width=1280)``. Every window rect and element center in
    the text is in IMAGE pixels of the returned image, and Click / Move / Scroll /
    Type take IMAGE pixels by default — read a point off the image (or the list)
    and pass it as-is, with the same monitor/max_width; the server scales it back.

    Args:
        use_vision: Include screenshot image (default True).
        quality: JPEG quality 1-100 (default 65). Lower = smaller.
        max_width: Max image width in pixels. 0=native resolution. Default 1280 to keep images compact.
        monitor: Monitor to capture. 0=all monitors (default; on macOS the main display), 1/2/3=specific monitor.

    Returns a list containing:
    - Screenshot image as JPEG (if use_vision=True)
    - Text: coordinate contract, windows and interactive UI elements (image pixels)
    """
    try:
        parts = []
        if _tobool(use_vision):
            img, frame = _capture_snapshot_frame(monitor, max_width)
            parts.append(ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"))
        else:
            frame = _current_frame(monitor, max_width)

        _desktop = get_desktop()
        windows = _desktop.enumerate_windows()
        sys_lang = _desktop._get_system_language() if hasattr(_desktop, "_get_system_language") else "N/A"
        lines = [f"**Coordinates:** {contract_line(frame)}", "", f"**System Language:** {sys_lang}", ""]
        lines += ["**Windows:**", *window_lines(frame, windows)]

        # Interactive elements from foreground window
        elements = _desktop.get_interactive_elements()
        if elements:
            lines += ["", "**Interactive Elements (foreground window):**", *element_lines(frame, elements[:50])]

        parts.append(TextContent(type="text", text="\n".join(lines)))
        return parts
    except Exception as e:
        return [f"Snapshot error: {e}"]


@mcp.tool(
    annotations=ToolAnnotations(
        title="Click",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def Click(
    x: int,
    y: int,
    button: str = "left",
    action: str = "click",
    monitor: int = 0,
    max_width: int = 1280,
    coords: str = "image",
) -> str:
    """Mouse click at a point of the Snapshot image.

    x/y are IMAGE pixels of the last Snapshot / AnnotatedSnapshot (read them off
    the image or its element list); the server maps them to the screen
    (screen = origin + round(image * scale), clamped to the frame). Pass the same
    monitor/max_width the Snapshot used — the defaults are Snapshot's defaults.

    Args:
        x: X in Snapshot image pixels (screen coordinates if coords="screen").
        y: Y in Snapshot image pixels (screen coordinates if coords="screen").
        button: 'left', 'right', or 'middle'.
        action: 'click', 'double', or 'hover'.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
        coords: 'image' (default) or 'screen' = raw OS coordinates, passed through
            unchanged (physical pixels on Windows/Linux, points on macOS).
    """
    try:
        [(x, y)], note = _to_screen([(x, y)], monitor, max_width, coords)
        if is_linux():
            return get_desktop().click(x, y, button=button, action=action) + note
        if action == "hover":
            pyautogui.moveTo(x, y)
            return f"Hovered at ({x},{y}){note}"
        elif action == "double":
            pyautogui.doubleClick(x, y, button=button)
            return f"Double-clicked {button} at ({x},{y}){note}"
        else:
            pyautogui.click(x, y, button=button)
            return f"Clicked {button} at ({x},{y}){note}"
    except Exception as e:
        return f"Click error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Type",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def Type(
    text: str,
    x: int = 0,
    y: int = 0,
    clear: bool | str = False,
    press_enter: bool | str = False,
    monitor: int = 0,
    max_width: int = 1280,
    coords: str = "image",
    method: str = "keys",
) -> str:
    """Type text in any language, optionally clicking a point of the Snapshot image first.

    Any Unicode text is typed exactly — diacritics (e.g. Slovak "Žltý kôň
    ďakuje"), symbols (€, –) and emoji — whatever the machine's keyboard layout.
    "\\n" (also "\\r\\n") presses Enter and "\\t" presses Tab. The reply "Typed N
    chars" counts the characters handed to the OS. On Windows, input the OS
    refuses part-way (e.g. a locked session) is an error saying how many got
    through, never a success; macOS refuses up front when this process may not
    post keyboard events. Neither macOS nor Linux can confirm delivery to a locked
    console. x/y are IMAGE pixels of the last Snapshot (see Click); 0/0 = type at
    the current focus without clicking.

    Args:
        text: Text to type.
        x: X in Snapshot image pixels. x=0 with y=0 = no click (type at the current
            focus); x=0 alone is the left column.
        y: Y in Snapshot image pixels. y=0 alone is the top row.
        clear: Clear existing content first (select all, Delete).
        press_enter: Press Enter after typing.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
        coords: 'image' (default) or 'screen' = raw OS coordinates.
        method: 'keys' (default) = Unicode keystrokes, the clipboard is not
            touched. 'paste' = put the text on the clipboard, press Ctrl+V
            (Command+V on macOS), then restore the previous clipboard text — only
            for an app that ignores typed keystrokes (some games, nested remote
            desktops). Not for secrets: clipboard history keeps a copy. The reply
            means the paste keys were pressed, not that the app took the text.
    """
    try:
        how = check_method(method)
        points, note = _to_screen(_optional_point(x, y), monitor, max_width, coords)
        sx, sy = points[0] if points else (None, None)
        log.debug("Type %d chars by %s at %s (clear=%s, enter=%s)", len(text), how, points, clear, press_enter)
        reply = get_desktop().type_text(
            text, x=sx, y=sy, clear=_tobool(clear), press_enter=_tobool(press_enter), method=how
        )
        return reply + note
    except Exception as e:
        return f"Type error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scroll",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def Scroll(
    amount: int,
    direction: str = "",
    x: int = 0,
    y: int = 0,
    horizontal: bool | str = False,
    monitor: int = 0,
    max_width: int = 1280,
    coords: str = "image",
) -> str:
    """Turn the mouse wheel at a point of the Snapshot image.

    amount = mouse-wheel CLICKS (notches), the same unit on Windows, macOS and
    Linux; direction = 'up' | 'down' | 'left' | 'right'. Example:
    Scroll(amount=3, direction="down") scrolls down by three wheel notches.
    x/y are IMAGE pixels of the last Snapshot (see Click); 0/0 = scroll where the
    mouse already is.

    Args:
        amount: Wheel clicks, clamped to 1..100. Legacy form (no direction): the
            sign picks the direction — positive = up/right, negative = down/left.
        direction: 'up', 'down', 'left' or 'right'. When given it wins over the
            sign of amount and over horizontal.
        x: X in Snapshot image pixels. x=0 with y=0 = where the mouse already is;
            x=0 alone is the left column.
        y: Y in Snapshot image pixels. y=0 alone is the top row.
        horizontal: Legacy, only without direction: scroll left/right instead of up/down.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
        coords: 'image' (default) or 'screen' = raw OS coordinates.
    """
    try:
        steps = normalize_request(amount, direction, _tobool(horizontal))
        points, note = _to_screen(_optional_point(x, y), monitor, max_width, coords)
        sx, sy = points[0] if points else (None, None)
        log.debug("Scroll %s x%d (requested %d) at %s", steps.direction, steps.clicks, steps.requested, points)
        return get_desktop().scroll(steps.direction, steps.requested, x=sx, y=sy) + note
    except Exception as e:
        return f"Scroll error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Move",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def Move(
    x: int,
    y: int,
    drag: bool | str = False,
    start_x: int = 0,
    start_y: int = 0,
    duration: float = 0.3,
    monitor: int = 0,
    max_width: int = 1280,
    coords: str = "image",
) -> str:
    """Move the mouse, or drag, to a point of the Snapshot image.

    x/y and start_x/start_y are IMAGE pixels of the last Snapshot (see Click).

    Args:
        x: Target X in Snapshot image pixels.
        y: Target Y in Snapshot image pixels.
        drag: If true, drag from start position to target.
        start_x: Drag start X in image pixels. start_x=0 with start_y=0 = drag from
            the current position; start_x=0 alone is the left column.
        start_y: Drag start Y in image pixels. start_y=0 alone is the top row.
        duration: Movement duration in seconds.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
        coords: 'image' (default) or 'screen' = raw OS coordinates.
    """
    try:
        drag = _tobool(drag)
        start = _optional_point(start_x, start_y) if drag else []
        mapped, note = _to_screen([*start, (x, y)], monitor, max_width, coords)
        x, y = mapped[-1]
        start_x, start_y = mapped[0] if start else (None, None)
        if is_linux():
            msg = get_desktop().move(x, y, drag=drag, start_x=start_x, start_y=start_y, duration=duration)
            return msg + note
        if drag:
            if start:
                pyautogui.moveTo(start_x, start_y)
            pyautogui.drag(x - pyautogui.position()[0], y - pyautogui.position()[1], duration=duration)
            return f"Dragged to ({x},{y}){note}"
        else:
            pyautogui.moveTo(x, y, duration=duration)
            return f"Moved to ({x},{y}){note}"
    except Exception as e:
        return f"Move error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Shortcut",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def Shortcut(keys: str) -> str:
    """Execute keyboard shortcut.

    Args:
        keys: Shortcut string, e.g. 'ctrl+c', 'alt+tab', 'win+e'.
    """
    try:
        if is_linux():
            return get_desktop().shortcut(keys)
        parts = [k.strip() for k in keys.lower().split("+")]
        pyautogui.hotkey(*parts)
        return f"Executed shortcut: {keys}"
    except Exception as e:
        return f"Shortcut error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Wait",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def Wait(seconds: float = 1.0) -> str:
    """Pause execution.

    Args:
        seconds: Seconds to wait.
    """
    time.sleep(seconds)
    return f"Waited {seconds}s"


# =========================== WINDOW MANAGEMENT ============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="FocusWindow",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def FocusWindow(title: str = "", handle: int = 0) -> str:
    """Bring a window to the foreground.

    Args:
        title: Window title (fuzzy matched).
        handle: Window handle (exact).
    """
    err = _check_win32("FocusWindow")
    if err:
        return err
    try:
        return get_desktop().focus_window(title=title or None, handle=handle or None)
    except Exception as e:
        return f"FocusWindow error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="MinimizeAll",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def MinimizeAll() -> str:
    """Minimize all windows (Win+D — show desktop)."""
    try:
        return get_desktop().minimize_all()
    except Exception as e:
        return f"MinimizeAll error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="App",
        destructiveHint=False,
        openWorldHint=True,
    )
)
def App(
    action: str = "launch",
    name: str = "",
    args: str = "",
    handle: int = 0,
    width: int = 0,
    height: int = 0,
) -> str:
    """Launch, switch to, or resize an application.

    Args:
        action: 'launch', 'switch', or 'resize'.
        name: Application name or path (for launch/switch).
        args: Arguments (for launch).
        handle: Window handle (for resize/switch).
        width: New width (for resize), in raw screen units (the coords="screen" space:
            physical pixels on Windows/Linux, points on macOS) — not Snapshot image pixels.
        height: New height (for resize), in the same raw screen units as width.
    """
    try:
        if action == "launch":
            return get_desktop().launch_app(name, args)
        elif action == "switch":
            err = _check_win32("App(switch)")
            if err:
                return err
            return get_desktop().focus_window(title=name or None, handle=handle or None)
        elif action == "resize":
            err = _check_win32("App(resize)")
            if err:
                return err
            if not handle:
                return "resize requires a window handle"
            return get_desktop().resize_window(handle, width, height)
        return f"Unknown action: {action}"
    except Exception as e:
        return f"App error: {e}"


# =========================== REMOTE MANAGEMENT ============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Shell",
        destructiveHint=True,
        openWorldHint=True,
    )
)
def Shell(command: str, timeout: int = 30, cwd: str = "") -> str:
    """Execute a shell command (PowerShell on Windows, zsh on macOS, bash on Linux).

    Args:
        command: Command to execute.
        timeout: Timeout in seconds (default 30).
        cwd: Working directory (spaces and quotes are fine; ~ and $VAR, or %VAR%
            on Windows, are expanded). If provided, the command runs inside that
            directory; a path that is not an existing directory is an error and
            the command does not run.
    """
    try:
        # The directory goes to the process itself (#31): pasting it into the
        # command as ``cd {cwd}`` broke on spaces/quotes and ran ``; <cmd>`` in it.
        workdir = None
        if cwd:
            workdir = os.path.expandvars(os.path.expanduser(cwd))
            if not os.path.isdir(workdir):
                problem = "is not a directory" if os.path.exists(workdir) else "not found"
                log.warning("Shell refused: working directory %s: %r", problem, cwd)
                return f"Shell error: working directory {problem}: {cwd}"
        if is_windows():
            shell_cmd = ["powershell", "-NoProfile", "-Command", command]
        elif is_macos():
            shell_cmd = ["/bin/zsh", "-c", command]
        else:
            shell_cmd = ["/bin/bash", "-c", command]

        proc = subprocess.Popen(
            shell_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workdir,
            start_new_session=not is_windows(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if is_windows():
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
            else:
                import signal

                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return f"Command timed out after {timeout}s"
        output = stdout or ""
        if stderr:
            output += f"\n[STDERR] {stderr}"
        if proc.returncode != 0:
            output += f"\n[Exit code: {proc.returncode}]"
        return output.strip() or "(no output)"
    except Exception as e:
        return f"Shell error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="GetClipboard",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def GetClipboard() -> str:
    """Read the clipboard text content."""
    err = _check_win32("GetClipboard")
    if err:
        return err
    try:
        return get_desktop().get_clipboard()
    except Exception as e:
        return f"GetClipboard error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="SetClipboard",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def SetClipboard(text: str) -> str:
    """Set the clipboard text content.

    Args:
        text: Text to place on clipboard.
    """
    err = _check_win32("SetClipboard")
    if err:
        return err
    try:
        return get_desktop().set_clipboard(text)
    except Exception as e:
        return f"SetClipboard error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="ListProcesses",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
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
    try:
        return process_mgr.list_processes(filter_name=filter, sort_by=sort_by, limit=limit)
    except Exception as e:
        return f"ListProcesses error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="KillProcess",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def KillProcess(pid: int = 0, name: str = "") -> str:
    """Kill a process by PID or name.

    Args:
        pid: Process ID.
        name: Process name (fuzzy matched).
    """
    try:
        return process_mgr.kill_process(pid=pid, name=name)
    except Exception as e:
        return f"KillProcess error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="GetSystemInfo",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def GetSystemInfo() -> str:
    """Get system information: CPU, memory, disk, network, uptime."""
    try:
        return process_mgr.get_system_info()
    except Exception as e:
        return f"GetSystemInfo error: {e}"


def _ensure_session_connected(force: bool = False) -> str | None:
    """Reconnect a disconnected desktop session to console.

    Returns None on success or if already connected, error string on failure.
    """
    if not is_windows():
        return None
    try:
        result = subprocess.run(["query", "session"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return f"Failed to query sessions: {result.stderr}"

        lines = result.stdout.strip().split("\n")
        user_session_id = None
        is_disconnected = False

        for line in lines[1:]:
            line = line.lstrip(">").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            # Format: sessionname username ID state ...
            # or:     sessionname          ID state ... (no user)
            name = parts[0].lower()
            if name in ("services", "rdp-tcp"):
                continue
            # Find the numeric session ID and state
            for i, p in enumerate(parts[1:], 1):
                if p.isdigit():
                    sid = int(p)
                    if i + 1 < len(parts):
                        state = parts[i + 1].lower()
                        # Check if there's a username before the ID
                        has_user = i > 1 and not parts[i - 1].isdigit()
                        if has_user or name == "console":
                            user_session_id = sid
                            # Chinese Windows: 已断开=Disc, 运行中=Active
                            is_disconnected = state in (
                                "disc",
                                "断开",
                                "已断开",
                                "disconnected",
                            )
                    break

        if user_session_id is None:
            return "No user session found"

        if not is_disconnected and not force:
            return None  # Already connected

        result = subprocess.run(
            ["tscon", str(user_session_id), "/dest:console"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            return f"tscon failed: {err}"
        time.sleep(1)  # Wait for session to stabilize
        return None
    except subprocess.TimeoutExpired:
        return "Session reconnect timed out"
    except Exception as e:
        return f"Session reconnect error: {e}"


@mcp.tool(annotations=ToolAnnotations(title="ReconnectSession", readOnlyHint=False))
def ReconnectSession(force: bool = False) -> list:
    """Reconnect a disconnected Windows desktop session to the console.

    This enables screenshot and UI automation tools to work when no RDP
    client is actively connected. Runs 'tscon' to attach the user's
    session to the console.

    Args:
        force: Reconnect even if session appears active (default False).
    """
    err = _ensure_session_connected(force=force)
    if err:
        return [TextContent(type="text", text=f"ReconnectSession failed: {err}")]
    return [TextContent(type="text", text="Session connected to console")]


@mcp.tool(
    annotations=ToolAnnotations(
        title="Notification",
        destructiveHint=False,
        openWorldHint=False,
    )
)
def Notification(title: str = "remoteos-mcp", message: str = "") -> str:
    """Show a desktop notification.

    Args:
        title: Notification title.
        message: Notification body text.
    """
    try:
        return get_desktop().show_notification(title, message)
    except Exception as e:
        return f"Notification error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="LockScreen",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def LockScreen() -> str:
    """Lock the workstation."""
    try:
        return get_desktop().lock_screen()
    except Exception as e:
        return f"LockScreen error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scrape",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def Scrape(url: str) -> str:
    """Fetch URL content and return as markdown.

    Args:
        url: URL to fetch.
    """
    try:
        import urllib.request

        from markdownify import markdownify

        req = urllib.request.Request(url, headers={"User-Agent": "remoteos-mcp/0.3"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        md = markdownify(html, heading_style="ATX", strip=["script", "style"])
        # Truncate
        if len(md) > 50000:
            md = md[:50000] + "\n\n[... truncated]"
        return md
    except Exception as e:
        return f"Scrape error: {e}"


# ============================== FILE OPERATIONS ============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileRead",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def FileRead(path: str, encoding: str = "utf-8") -> str:
    """Read file content. Returns base64 for binary files.

    Args:
        path: File path.
        encoding: Text encoding (default utf-8). Use 'binary' for base64 output.
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        if encoding == "binary":
            data = p.read_bytes()
            return base64.b64encode(data).decode()
        else:
            text = p.read_text(encoding=encoding, errors="replace")
            if len(text) > 100000:
                text = text[:100000] + "\n\n[... truncated at 100KB]"
            return text
    except Exception as e:
        return f"FileRead error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileWrite",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def FileWrite(path: str, content: str, encoding: str = "utf-8", append: bool | str = False) -> str:
    """Write content to a file.

    Args:
        path: File path.
        content: Content to write.
        encoding: Text encoding (default utf-8).
        append: Append instead of overwrite.
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if _tobool(append) else "w"
        with open(p, mode, encoding=encoding) as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}"
    except Exception as e:
        return f"FileWrite error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileList",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def FileList(path: str = ".", show_hidden: bool | str = False) -> str:
    """List directory contents with size and modification date.

    Args:
        path: Directory path.
        show_hidden: Include hidden files/folders.
    """
    try:
        from tabulate import tabulate

        p = Path(path)
        if not p.is_dir():
            return f"Not a directory: {path}"

        rows = []
        for item in sorted(p.iterdir()):
            name = item.name
            if not _tobool(show_hidden) and name.startswith("."):
                continue
            try:
                stat = item.stat()
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                kind = "DIR" if item.is_dir() else "FILE"
                if item.is_dir():
                    size_str = "<DIR>"
                elif size < 1024:
                    size_str = f"{size}B"
                elif size < 1048576:
                    size_str = f"{size // 1024}KB"
                else:
                    size_str = f"{size // 1048576}MB"
                rows.append([kind, name, size_str, mtime])
            except Exception:
                rows.append(["?", name, "?", "?"])

        if not rows:
            return "Directory is empty."
        return tabulate(rows, headers=["Type", "Name", "Size", "Modified"], tablefmt="simple")
    except Exception as e:
        return f"FileList error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileSearch",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def FileSearch(pattern: str, path: str = ".", recursive: bool | str = True, limit: int = 50) -> str:
    """Search files by name pattern.

    Args:
        pattern: Glob pattern (e.g. '*.py', 'report*').
        path: Root directory to search.
        recursive: Search subdirectories.
        limit: Max results.
    """
    try:
        p = Path(path)
        glob_iter = p.rglob(pattern) if _tobool(recursive) else p.glob(pattern)

        # Stream results — stop as soon as we have enough (don't scan entire disk)
        lines = []
        total = 0
        for m in glob_iter:
            total += 1
            if len(lines) < limit:
                try:
                    size = m.stat().st_size
                    lines.append(f"  {m} ({size} bytes)")
                except Exception:
                    lines.append(f"  {m}")
            if len(lines) >= limit:
                break  # Don't keep scanning after hitting limit

        if not lines:
            return f"No files matching '{pattern}' in {path}"

        result = f"Found {total}+ files (showing first {limit})"
        result += ":\n" + "\n".join(lines)
        return result
    except Exception as e:
        return f"FileSearch error: {e}"


# ========================== FILE TRANSFER (BINARY) =========================


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileDownload",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def FileDownload(path: str) -> str:
    """Download a file as base64-encoded content. Use for binary files.

    Args:
        path: File path to download.
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"base64:{len(data)}bytes:{b64}"
    except Exception as e:
        return f"FileDownload error: {e}"


# FileUpload cap on the base64 TEXT (~75 MB of file), checked before decoding (#31).
FILE_UPLOAD_MAX_BASE64 = 100 * 1024 * 1024
_B64_WHITESPACE = str.maketrans("", "", " \t\r\n")  # line-wrapped base64 (e.g. `base64` output) is fine


@mcp.tool(
    annotations=ToolAnnotations(
        title="FileUpload",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def FileUpload(path: str, data_base64: str) -> str:
    """Upload a file from base64-encoded content. Use for binary files.

    Args:
        path: Destination file path.
        data_base64: Base64-encoded file content, at most 100 MB of base64 text
            (about 75 MB of file). Line breaks are allowed; any other character
            outside the base64 alphabet is an error and nothing is written.
    """
    try:
        # Both checks run before the directory is created or the file opened (#31):
        # lax decoding dropped junk characters, so "!!!!" truncated a file to 0 bytes.
        if len(data_base64) > FILE_UPLOAD_MAX_BASE64:
            log.warning("FileUpload refused %s: %d base64 characters over the cap", path, len(data_base64))
            return (
                f"FileUpload error: data_base64 is {len(data_base64)} characters; "
                f"the limit is {FILE_UPLOAD_MAX_BASE64} (100 MB of base64)"
            )
        try:
            data = base64.b64decode(data_base64.translate(_B64_WHITESPACE), validate=True)
        except ValueError as e:  # binascii.Error is one; non-ASCII text raises a plain ValueError
            log.warning("FileUpload refused %s: invalid base64 (%s)", path, e)
            return f"FileUpload error: data_base64 is not valid base64 ({e})"
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        log.info("FileUpload wrote %d bytes to %s", len(data), path)
        return f"Written {len(data)} bytes to {path}"
    except Exception as e:
        return f"FileUpload error: {e}"


# ============================== REGISTRY ===================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="RegRead",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def RegRead(key: str, value_name: str) -> str:
    """Read system preferences (Windows registry / macOS defaults).

    Args:
        key: Registry key path, e.g. "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion".
        value_name: Name of the value to read.
    """
    try:
        return get_system().reg_read(key, value_name)
    except Exception as e:
        return f"RegRead error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="RegWrite",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def RegWrite(key: str, value_name: str, data: str, reg_type: str = "REG_SZ") -> str:
    """Write system preferences (Windows registry / macOS defaults).

    Args:
        key: Registry key path, e.g. "HKCU\\SOFTWARE\\MyApp".
        value_name: Name of the value to write.
        data: Value data. For REG_DWORD/REG_QWORD pass as string number. For REG_MULTI_SZ use | separator.
        reg_type: Registry type: REG_SZ, REG_EXPAND_SZ, REG_DWORD, REG_QWORD, REG_BINARY, REG_MULTI_SZ.
    """
    try:
        return get_system().reg_write(key, value_name, data, reg_type)
    except Exception as e:
        return f"RegWrite error: {e}"


# ============================= SERVICES ====================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="ServiceList",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def ServiceList(filter: str = "") -> str:
    """List system services.

    Args:
        filter: Filter by service name or display name (substring match).
    """
    try:
        return get_services().service_list(filter)
    except Exception as e:
        return f"ServiceList error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="ServiceStart",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def ServiceStart(name: str) -> str:
    """Start a system service.

    Args:
        name: Service name.
    """
    try:
        return get_services().service_start(name)
    except Exception as e:
        return f"ServiceStart error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="ServiceStop",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def ServiceStop(name: str) -> str:
    """Stop a system service.

    Args:
        name: Service name.
    """
    try:
        return get_services().service_stop(name)
    except Exception as e:
        return f"ServiceStop error: {e}"


# ========================= SCHEDULED TASKS =================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="TaskList",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def TaskList(filter: str = "") -> str:
    """List scheduled tasks.

    Args:
        filter: Filter by task name (substring match).
    """
    try:
        return get_services().task_list(filter)
    except Exception as e:
        return f"TaskList error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="TaskCreate",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def TaskCreate(name: str, command: str, schedule: str) -> str:
    """Create a scheduled task.

    Args:
        name: Task name.
        command: Command to execute.
        schedule: Schedule type (ONCE, DAILY, WEEKLY, MONTHLY, ONSTART, ONLOGON, ONIDLE).
    """
    try:
        return get_services().task_create(name, command, schedule)
    except Exception as e:
        return f"TaskCreate error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="TaskDelete",
        destructiveHint=True,
        openWorldHint=False,
    )
)
def TaskDelete(name: str) -> str:
    """Delete a scheduled task.

    Args:
        name: Task name.
    """
    try:
        return get_services().task_delete(name)
    except Exception as e:
        return f"TaskDelete error: {e}"


# ============================= NETWORK =====================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="Ping",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def Ping(host: str, count: int = 4) -> str:
    """Ping a host.

    Args:
        host: Hostname or IP address.
        count: Number of ping requests (default 4).
    """
    try:
        return network.ping(host, count)
    except Exception as e:
        return f"Ping error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="PortCheck",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def PortCheck(host: str, port: int, timeout: float = 5.0) -> str:
    """Check if a TCP port is open.

    Args:
        host: Hostname or IP address.
        port: Port number.
        timeout: Connection timeout in seconds (default 5).
    """
    try:
        return network.port_check(host, port, timeout)
    except Exception as e:
        return f"PortCheck error: {e}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="NetConnections",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def NetConnections(filter: str = "", limit: int = 50) -> str:
    """List network connections.

    Args:
        filter: Filter connections by local/remote address, status, or PID.
        limit: Maximum number of connections to return (default 50).
    """
    try:
        return network.net_connections(filter, limit=limit)
    except Exception as e:
        return f"NetConnections error: {e}"


# ============================ EVENT LOG ====================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="EventLog",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def EventLog(log_name: str = "System", count: int = 20, level: str = "") -> str:
    """Read system event log entries.

    Args:
        log_name: Log name (System, Application, Security, etc.).
        count: Number of entries to retrieve (default 20).
        level: Filter by level: critical, error, warning, information, verbose.
    """
    try:
        return get_services().event_log(log_name, count, level)
    except Exception as e:
        return f"EventLog error: {e}"


# ============================== OCR ========================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="OCR",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def OCR(
    left: int = 0,
    top: int = 0,
    right: int = 0,
    bottom: int = 0,
    lang: str = "eng",
) -> str:
    """Extract text from screen using OCR. Captures a region or the full screen.

    Uses pytesseract if available, falls back to platform-specific OCR engine.

    The region is in SCREEN coordinates (physical pixels on Windows/Linux, points
    on macOS) — NOT Snapshot image pixels.

    Args:
        left: Left edge of region (0 = full screen).
        top: Top edge of region.
        right: Right edge of region.
        bottom: Bottom edge of region.
        lang: OCR language for pytesseract (default 'eng').
    """
    try:
        region = {}
        if left or top or right or bottom:
            region = {"left": left, "top": top, "right": right, "bottom": bottom}
        text = ocr.run_ocr(**region, lang=lang) if region else ocr.run_ocr(lang=lang)
        if not text:
            return "(no text detected)"
        return text
    except ImportError as e:
        return f"OCR error: {e}"
    except Exception as e:
        return f"OCR error: {e}"


# ========================== SCREEN RECORDING ===============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="ScreenRecord",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def ScreenRecord(
    duration: float = 3.0,
    fps: int = 5,
    left: int = 0,
    top: int = 0,
    right: int = 0,
    bottom: int = 0,
    max_width: int = 800,
) -> list:
    """Record the screen and return an animated GIF.

    The region is in SCREEN coordinates (physical pixels on Windows/Linux, points
    on macOS) — NOT Snapshot image pixels.

    Args:
        duration: Recording length in seconds (default 3, max 10).
        fps: Frames per second (default 5, max 10).
        left: Left edge of capture region (0 = full screen).
        top: Top edge of capture region.
        right: Right edge of capture region.
        bottom: Bottom edge of capture region.
        max_width: Max width of output GIF (default 800).
    """
    try:
        region = {}
        if left or top or right or bottom:
            region = {"left": left, "top": top, "right": right, "bottom": bottom}
        if is_linux():
            # Linux path: ffmpeg x11grab in the resolved session (issue #8). Same
            # base64-GIF return shape as recording.record_screen, so the content
            # below is identical to the Windows/macOS path.
            b64 = get_desktop().record_screen(duration=duration, fps=fps, max_width=max_width, **region)
        else:
            b64 = recording.record_screen(duration=duration, fps=fps, max_width=max_width, **region)
        return [
            ImageContent(type="image", data=b64, mimeType="image/gif"),
            TextContent(
                type="text",
                text=f"Recorded {duration}s at {fps}fps ({len(b64) * 3 // 4 // 1024}KB GIF)",
            ),
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"ScreenRecord error: {e}")]


# ======================== ANNOTATED SNAPSHOT ===============================


@mcp.tool(
    annotations=ToolAnnotations(
        title="AnnotatedSnapshot",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def AnnotatedSnapshot(
    max_elements: int = 30,
    quality: int = 65,
    max_width: int = 1280,
    monitor: int = 0,
) -> list:
    """Take a screenshot with numbered labels on interactive UI elements.

    Draws red rectangles and white numbered labels on each interactive element,
    making it easy for AI agents to identify click targets visually. Same frame and
    coordinate contract as Snapshot: the text starts with the contract line, element
    centers are IMAGE pixels, and Click / Move / Scroll / Type take them as-is.

    Uses the platform accessibility tree for the foreground window (Win32 child
    windows on Windows, AT-SPI on Linux X11); returns the plain screenshot with a
    note when the focused app exposes no elements.

    Args:
        max_elements: Maximum number of elements to annotate (default 30).
        quality: JPEG quality 1-100 (default 65).
        max_width: Max image width in pixels. 0=native resolution. Default 1280 (same as Snapshot).
        monitor: Monitor to capture. 0=all monitors (default; on macOS the main display), 1/2/3=specific monitor.
    """
    try:
        if is_linux():
            d = get_desktop()
            if d.get_session().mode != "x11":
                return [TextContent(type="text", text=d._unavailable())]
        try:
            img, frame = _capture_snapshot_frame(monitor, max_width)
        except Exception as e:
            return [TextContent(type="text", text=f"AnnotatedSnapshot error: {e}")]
        coordinates = f"**Coordinates:** {contract_line(frame)}"

        elements = get_desktop().get_interactive_elements()
        if not elements:
            note = "No interactive elements found"
            if is_linux():
                note += " (AT-SPI exposed none for the focused app)"
            return [
                ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"),
                TextContent(type="text", text=f"{coordinates}\n\n{note}."),
            ]

        annotated = draw_annotations(img, elements, frame, max_elements)
        text_summary = f"{coordinates}\n\n**Annotated {len(annotated)} elements:**\n" + "\n".join(annotated)
        return [
            ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"),
            TextContent(type="text", text=text_summary),
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"AnnotatedSnapshot error: {e}")]


# ================================ Task Management ================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def CancelTask(task_id: str) -> str:
    """Cancel a running or pending task by its task ID.

    Args:
        task_id: The task ID returned when the tool was invoked (e.g. from [task:abc123]).
    """
    result = task_manager.cancel_task(task_id)
    if "error" in result:
        return f"Cancel failed: {result['error']}"
    return f"Cancelled task {task_id} ({result['tool_name']})"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def GetTaskStatus(task_id: str = "") -> str:
    """Get status of a specific task or list recent tasks.

    Args:
        task_id: If provided, get status of this task. If empty, list recent tasks.
    """
    import json

    if task_id:
        info = task_manager.get_task(task_id)
        if info is None:
            return f"Task {task_id} not found"
        return json.dumps(info, indent=2)
    tasks = task_manager.list_tasks()
    if not tasks:
        return "No tasks in history."
    lines = ["Recent tasks:"]
    for t in tasks[:20]:
        dur = f" ({t['duration']}s)" if t["duration"] is not None else ""
        err = f" — {t['error']}" if t.get("error") else ""
        lines.append(f"  [{t['task_id']}] {t['tool_name']} → {t['status']}{dur}{err}")
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def GetRunningTasks() -> str:
    """List all currently running and pending tasks."""

    running = task_manager.list_tasks("running")
    pending = task_manager.list_tasks("pending")
    all_active = running + pending
    if not all_active:
        return "No active tasks."
    lines = [f"Active tasks ({len(all_active)}):"]
    for t in all_active:
        dur = f" ({t['duration']}s)" if t["duration"] is not None else ""
        lines.append(f"  [{t['task_id']}] {t['tool_name']} [{t['category']}] {t['status']}{dur}")
    return "\n".join(lines)


# ====================== Apply task manager wrapping ========================


def _get_registered_tools() -> dict[str, object]:
    """Return tool-name -> tool object map across fastmcp 2.x/3.x internals."""
    # fastmcp 2.x
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_mgr, "_tools", None)
    if isinstance(tools, dict):
        return tools

    # fastmcp 3.x
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", None)
    if isinstance(components, dict):
        out: dict[str, object] = {}
        for comp_key, comp in components.items():
            if not isinstance(comp_key, str) or not comp_key.startswith("tool:"):
                continue
            name = getattr(comp, "name", None)
            if not isinstance(name, str) or not name:
                name = comp_key.split(":", 1)[1].split("@", 1)[0]
            out[name] = comp
        return out

    raise RuntimeError("Unsupported fastmcp internals: cannot locate registered tools")


def _remove_tool(name: str) -> None:
    """Remove a tool by name across fastmcp 2.x/3.x internals."""
    # fastmcp 2.x
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_mgr, "_tools", None)
    if isinstance(tools, dict):
        tools.pop(name, None)
        return

    # fastmcp 3.x
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", None)
    if isinstance(components, dict):
        keys_to_remove = [
            k
            for k, v in components.items()
            if isinstance(k, str)
            and k.startswith("tool:")
            and ((getattr(v, "name", None) == name) or k.split(":", 1)[1].split("@", 1)[0] == name)
        ]
        for k in keys_to_remove:
            components.pop(k, None)
        return


def _wrap_all_tools():
    """Wrap all registered MCP tools with task manager for error resilience + concurrency."""
    # Skip wrapping the task management tools themselves
    skip = {"CancelTask", "GetTaskStatus", "GetRunningTasks"}
    for name, tool in _get_registered_tools().items():
        if name in skip:
            continue
        original_fn = getattr(tool, "fn", None)
        if callable(original_fn):
            tool.fn = task_manager.wrap_sync_tool(name, original_fn)


_wrap_all_tools()

if is_macos():
    _remove_tool("ReconnectSession")


def _param_explicit(ctx: click.Context, name: str) -> bool:
    src = ctx.get_parameter_source(name)
    return src in {ParameterSource.COMMANDLINE, ParameterSource.ENVIRONMENT}


def _choose_value(ctx: click.Context, name: str, cli_value, config_value, default_value):
    if _param_explicit(ctx, name):
        return cli_value
    if config_value is not None:
        return config_value
    return default_value


def _apply_tool_filter(enabled_tools: set[str]) -> None:
    for tool_name in list(_get_registered_tools().keys()):
        if tool_name not in enabled_tools:
            _remove_tool(tool_name)


def _enable_debug_logging() -> None:
    """``--debug`` (upstream winremote pull 65): the ``remoteos`` loggers at DEBUG on stderr.

    Only our own logger tree. The root logger and FastMCP stay at their levels:
    at DEBUG, FastMCP and sse_starlette log every tool call's arguments and reply
    (typed text, clipboard, Shell commands, FileUpload data) — #31 review. stderr,
    because in stdio mode stdout is the JSON-RPC stream.
    """
    handler = logging.StreamHandler(_sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    ours = logging.getLogger("remoteos")
    ours.addHandler(handler)
    ours.setLevel(logging.DEBUG)
    log.debug("debug logging on")


# ================================== CLI ====================================


@click.group(invoke_without_command=True)
@click.option("--transport", default="streamable-http", type=click.Choice(["stdio", "streamable-http"]))
@click.option("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1; use 0.0.0.0 for remote access)")
@click.option("--port", default=8092, type=int)
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="No effect, kept so existing command lines still start: uvicorn cannot hot-reload the in-process server",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help=(
        "Debug logging on stderr for remoteos and uvicorn "
        "(never typed text, clipboard, Shell commands, file data or tool replies)"
    ),
)
@click.option("--auth-key", default=None, envvar="REMOTEOS_AUTH_KEY", help="API key for authentication")
@click.option("--config", default=None, help="Path to remoteos.toml config file")
@click.option(
    "--enable-all",
    is_flag=True,
    default=False,
    help="Enable all tools including high-risk Tier 3 tools (backward-compatible)",
)
@click.option("--enable-tier3", is_flag=True, default=False, help="Enable tier3 destructive tools")
@click.option("--disable-tier2", is_flag=True, default=False, help="Disable tier2 interactive tools")
@click.option("--tools", default="", help="Comma-separated tools to enable (highest precedence)")
@click.option("--exclude-tools", default="", help="Comma-separated tools to disable")
@click.option("--ip-allowlist", default="", help="Comma-separated IPs/CIDRs allowed to access HTTP transport")
@click.option("--ssl-certfile", default=None, help="Path to SSL certificate file for HTTPS")
@click.option("--ssl-keyfile", default=None, help="Path to SSL private key file for HTTPS")
@click.option("--oauth-client-id", default=None, envvar="REMOTEOS_OAUTH_CLIENT_ID", help="OAuth client ID whitelist")
@click.option("--oauth-client-secret", default=None, envvar="REMOTEOS_OAUTH_CLIENT_SECRET", help="OAuth client secret")
@click.pass_context
def cli(
    ctx,
    transport: str,
    host: str,
    port: int,
    reload: bool,
    debug: bool,
    auth_key: str | None,
    config: str | None,
    enable_all: bool,
    enable_tier3: bool,
    disable_tier2: bool,
    tools: str,
    exclude_tools: str,
    ip_allowlist: str,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
):
    """Start the remoteos MCP server."""
    if ctx.invoked_subcommand is not None:
        return  # subcommand will handle it

    if debug:
        _enable_debug_logging()
    if reload:
        # FastMCP serves an app OBJECT through uvicorn.Server; uvicorn reloads only an
        # import-string app via uvicorn.run (#31), so passing reload would do nothing.
        click.echo(
            "  WARNING: --reload has no effect: uvicorn cannot hot-reload the in-process server. "
            "Restart the service to load new code.",
            err=True,
        )

    config_path = discover_config_path(config)
    cfg = load_config(config_path)

    host = _choose_value(ctx, "host", host, cfg.server.host, "127.0.0.1")
    port = int(_choose_value(ctx, "port", port, cfg.server.port, 8092))
    auth_key = _choose_value(ctx, "auth_key", auth_key, cfg.server.auth_key, None)
    ssl_certfile = _choose_value(ctx, "ssl_certfile", ssl_certfile, cfg.server.ssl_certfile, None)
    ssl_keyfile = _choose_value(ctx, "ssl_keyfile", ssl_keyfile, cfg.server.ssl_keyfile, None)
    if bool(ssl_certfile) != bool(ssl_keyfile):
        # one of the pair used to drop TLS silently and serve plain HTTP (#31)
        raise click.UsageError("--ssl-certfile and --ssl-keyfile must be given together (CLI or remoteos.toml)")
    oauth_client_id = _choose_value(ctx, "oauth_client_id", oauth_client_id, cfg.security.oauth_client_id, None)
    oauth_client_secret = _choose_value(
        ctx,
        "oauth_client_secret",
        oauth_client_secret,
        cfg.security.oauth_client_secret,
        None,
    )

    enable_tier3 = bool(_choose_value(ctx, "enable_tier3", enable_tier3, cfg.security.enable_tier3, False))
    disable_tier2 = bool(_choose_value(ctx, "disable_tier2", disable_tier2, cfg.security.disable_tier2, False))

    cli_tools = parse_tool_csv(tools)
    cli_excluded = parse_tool_csv(exclude_tools)
    cli_allowlist = parse_tool_csv(ip_allowlist)

    selected_tools = cli_tools if _param_explicit(ctx, "tools") else cfg.tools.enable
    excluded_tools = cli_excluded if _param_explicit(ctx, "exclude_tools") else cfg.tools.exclude
    allowlist_entries = cli_allowlist if _param_explicit(ctx, "ip_allowlist") else cfg.security.ip_allowlist

    # On Linux, gate the display-dependent tools on a runtime session check:
    # a machine with an active X11 session (e.g. imag-nb) gets the full desktop
    # tool set; a headless box (cam1..4) keeps them excluded — zero regression.
    # Status lines go to stderr: in stdio mode stdout is the JSON-RPC stream (#31 review).
    platform_excludes = None
    if is_windows():
        click.echo(f"  Windows DPI awareness: {_WIN_DPI_MODE}", err=True)
    if is_linux():
        platform_excludes = set(LINUX_ALWAYS_EXCLUDED_TOOLS)
        try:
            from remoteos.platform.linux import desktop as _linux_desktop

            session = _linux_desktop.get_session()
            if session.mode != "x11":
                platform_excludes |= LINUX_DISPLAY_TOOLS
            click.echo(
                f"  Linux session: {session.mode}" + (f" (display={session.display})" if session.mode == "x11" else ""),
                err=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            click.echo(f"  Linux session detection failed ({exc}); excluding display tools", err=True)
            platform_excludes = set(LINUX_EXCLUDED_TOOLS)
    enabled_tools = resolve_enabled_tools(
        enable_tier3=enable_tier3,
        disable_tier2=disable_tier2,
        enable_all=enable_all,
        explicit_tools=selected_tools,
        exclude_tools=excluded_tools,
        platform_excludes=platform_excludes,
    )
    _apply_tool_filter(enabled_tools)
    enabled_tiers = get_tier_names(enabled_tools)

    # ---- OAuth setup ----
    oauth_store = None
    oauth_validator = None
    use_oauth = bool(oauth_client_id or oauth_client_secret)

    if use_oauth and transport != "stdio":
        from remoteos.oauth import OAuthStore, build_oauth_routes, validate_oauth_token

        oauth_store = OAuthStore()
        scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
        issuer = f"{scheme}://{host}:{port}"

        routes = build_oauth_routes(
            store=oauth_store,
            issuer=issuer,
            configured_client_id=oauth_client_id,
            configured_client_secret=oauth_client_secret,
        )
        for path, (handler, methods) in routes.items():
            mcp.custom_route(path, methods=methods)(handler)

        oauth_validator = lambda tok: validate_oauth_token(oauth_store, tok)  # noqa: E731

    # ---- Middleware ----
    middleware: list[Middleware] = []

    if allowlist_entries:
        allowlist_networks = parse_ip_allowlist(allowlist_entries)
        middleware.append(Middleware(IPAllowlistMiddleware, allowlist=allowlist_networks))

    if auth_key:
        from remoteos.auth import AuthKeyMiddleware

        middleware.append(Middleware(AuthKeyMiddleware, auth_key=auth_key, oauth_validator=oauth_validator))
    elif oauth_validator:
        from remoteos.auth import OAuthOnlyMiddleware

        middleware.append(Middleware(OAuthOnlyMiddleware, oauth_validator=oauth_validator))

    class BannerFilter(logging.Filter):
        """Inject our banner after uvicorn's 'Application startup complete' log."""

        _shown = False

        def filter(self, record):
            if not self._shown and "Application startup complete" in record.getMessage():
                self._shown = True
                auth_line = "[auth ON]" if auth_key else "[no auth]"
                ssl_line = "[https ON]" if (ssl_certfile and ssl_keyfile) else ""
                oauth_line = "[oauth ON]" if use_oauth else ""
                bind_line = f"[{host}:{port}]"
                tiers_line = f"[tiers: {','.join(enabled_tiers)}]"
                tools_line = f"[tools: {len(enabled_tools)}/{len(ALL_TOOLS)}]"
                pad = " " * 10  # align with uvicorn log text
                ver_line = f"remoteos-mcp v{__version__}"
                lines = [
                    f"{pad}+----------------------------------+",
                    f"{pad}|  {ver_line:<32s}|",
                    f"{pad}|  by dddabtc                      |",
                    f"{pad}|  github.com/dddabtc              |",
                    f"{pad}|  {auth_line:<32s}|",
                    *([f"{pad}|  {ssl_line:<32s}|"] if ssl_line else []),
                    *([f"{pad}|  {oauth_line:<32s}|"] if oauth_line else []),
                    f"{pad}|  {bind_line:<32s}|",
                    f"{pad}|  {tiers_line:<16s}{tools_line:<16s}|",
                    f"{pad}+----------------------------------+",
                ]
                if host == "0.0.0.0" and not auth_key:
                    lines.append(f"{pad}  WARNING: open to network without auth!")
                    lines.append(f"{pad}  Use --auth-key for security.")
                if enable_all:
                    lines.append(f"{pad}  INFO: High-risk Tier 3 tools enabled!")
                print("\n" + "\n".join(lines) + "\n", flush=True)
            return True

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        logging.getLogger("uvicorn.error").addFilter(BannerFilter())
        run_kwargs = dict(transport="streamable-http", host=host, port=port, stateless_http=True)
        if middleware:
            run_kwargs["middleware"] = middleware
        if platform.system() == "Windows":
            os.environ.setdefault("NO_COLOR", "1")
        # FastMCP 4.0.3 run_http_async takes uvicorn_config, never uvicorn_args (#31).
        # Debug goes to uvicorn only, not run_http_async(log_level=...): that would
        # also turn FastMCP to DEBUG, which logs every tool call's arguments.
        uvicorn_config: dict[str, object] = {}
        if debug:
            uvicorn_config["log_level"] = "debug"
        if ssl_certfile and ssl_keyfile:
            uvicorn_config.update(ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
        if uvicorn_config:
            run_kwargs["uvicorn_config"] = uvicorn_config
        mcp.run(**run_kwargs)


@cli.command()
def install():
    """Create a Windows scheduled task for auto-start."""
    import getpass
    import os

    username = getpass.getuser()

    # Create start_mcp.bat for Chinese Windows compatibility
    python_exe = subprocess.run(["where", "python"], capture_output=True, text=True).stdout.strip().split("\n")[0]
    bat_content = f"""@echo off
rem remoteos-mcp startup script with UTF-8 encoding for Chinese Windows
set PYTHONIOENCODING=utf-8
"{python_exe}" -m remoteos %*
"""

    # Write batch file to user's profile directory
    user_profile = os.environ.get("USERPROFILE", ".")
    bat_path = os.path.join(user_profile, "start_mcp.bat")

    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)
        click.echo(f"[OK] Created startup script: {bat_path}")
    except Exception as e:
        click.echo(f"[ERROR] Failed to create startup script: {e}")
        return

    # Create scheduled task using the batch file
    task_cmd = f'schtasks /Create /SC ONSTART /TN "RemoteOSMCP" /TR "{bat_path}" /RU {username} /F'
    try:
        result = subprocess.run(task_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            click.echo("[OK] Scheduled task 'RemoteOSMCP' created for auto-start.")
            click.echo("The server will start automatically on system boot.")
            click.echo("Note: Uses start_mcp.bat for Chinese Windows compatibility.")
        else:
            click.echo(f"[ERROR] Failed to create task:\n{result.stderr or result.stdout}")
    except Exception as e:
        click.echo(f"[ERROR] Error: {e}")


@cli.command()
def uninstall():
    """Remove the RemoteOSMCP scheduled task."""
    import os

    task_cmd = 'schtasks /Delete /TN "RemoteOSMCP" /F'
    try:
        result = subprocess.run(task_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            click.echo("[OK] Scheduled task 'RemoteOSMCP' removed.")
        else:
            click.echo(f"[ERROR] Failed to remove task:\n{result.stderr or result.stdout}")
    except Exception as e:
        click.echo(f"[ERROR] Error: {e}")

    # Also remove the batch file
    user_profile = os.environ.get("USERPROFILE", ".")
    bat_path = os.path.join(user_profile, "start_mcp.bat")
    try:
        if os.path.exists(bat_path):
            os.remove(bat_path)
            click.echo(f"[OK] Removed startup script: {bat_path}")
    except Exception as e:
        click.echo(f"[ERROR] Failed to remove startup script: {e}")


@cli.command()
def health():
    """Print health status JSON."""
    import json

    click.echo(json.dumps({"status": "ok", "version": __version__}))


if __name__ == "__main__":
    cli()
