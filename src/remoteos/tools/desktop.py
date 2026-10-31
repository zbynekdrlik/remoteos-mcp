"""Desktop control tools: Snapshot, Click, Type, Scroll, Move, Shortcut, Wait."""

from __future__ import annotations

import sys as _sys
import time

from mcp.types import ImageContent, TextContent

from remoteos.app import (
    ToolAnnotations,
    _capture_snapshot_frame,
    _current_frame,
    _desktop_preflight,
    _input_tool,
    _tobool,
    log,
    mcp,
)

if _sys.platform != "linux":
    import pyautogui  # remoteos.app imported it first: DPI awareness set, FAILSAFE and PAUSE configured

from remoteos.coords import check_space, contract_line, image_to_screen, mapping_note
from remoteos.errors import reports_errors
from remoteos.platform import get_desktop, is_linux
from remoteos.scroll import normalize_request
from remoteos.snapshot import element_lines, encode_jpeg, window_lines
from remoteos.typing_input import check_method

# ============================= DESKTOP CONTROL =============================
#
# Coordinate contract (#28): Snapshot / AnnotatedSnapshot return an image that is
# a FRAME of the screen (``remoteos.coords``) and print it as one contract line.
# Every input tool takes IMAGE pixels of that frame by default and maps them back
# to the screen here, recomputing the frame from (monitor, max_width) + the live
# display geometry — no per-session state. ``coords="screen"`` is the escape hatch.


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
@reports_errors  # its preflight depends on use_vision: "capture" for the screenshot, else only "windows"
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
    parts = []
    if _tobool(use_vision):
        _desktop_preflight("capture")
        img, frame = _capture_snapshot_frame(monitor, max_width)
        parts.append(ImageContent(type="image", data=encode_jpeg(img, quality), mimeType="image/jpeg"))
    else:
        # the window list works on a locked or disconnected desktop, not outside the user's (#32 round 3)
        _desktop_preflight("windows")
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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Click",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Type",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
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
    chars" counts the characters handed to the OS. A desktop that cannot take
    input (a locked Windows/macOS screen, a secure desktop, no display, a missing
    permission) is an error before anything is typed; on Windows, input the OS
    refuses part-way is an error saying how many got through, never a success.
    x/y are IMAGE pixels of the last Snapshot (see Click); 0/0 = type at the
    current focus without clicking.

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
    how = check_method(method)
    points, note = _to_screen(_optional_point(x, y), monitor, max_width, coords)
    sx, sy = points[0] if points else (None, None)
    log.debug("Type %d chars by %s at %s (clear=%s, enter=%s)", len(text), how, points, clear, press_enter)
    reply = get_desktop().type_text(
        text, x=sx, y=sy, clear=_tobool(clear), press_enter=_tobool(press_enter), method=how
    )
    return reply + note


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scroll",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
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
    steps = normalize_request(amount, direction, _tobool(horizontal))
    points, note = _to_screen(_optional_point(x, y), monitor, max_width, coords)
    sx, sy = points[0] if points else (None, None)
    log.debug("Scroll %s x%d (requested %d) at %s", steps.direction, steps.clicks, steps.requested, points)
    return get_desktop().scroll(steps.direction, steps.requested, x=sx, y=sy) + note


@mcp.tool(
    annotations=ToolAnnotations(
        title="Move",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Shortcut",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
def Shortcut(keys: str) -> str:
    """Execute keyboard shortcut.

    Args:
        keys: Shortcut string, e.g. 'ctrl+c', 'alt+tab', 'win+e'.
    """
    if is_linux():
        return get_desktop().shortcut(keys)
    parts = [k.strip() for k in keys.lower().split("+")]
    pyautogui.hotkey(*parts)
    return f"Executed shortcut: {keys}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Wait",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def Wait(seconds: float = 1.0) -> str:
    """Pause execution.

    Args:
        seconds: Seconds to wait.
    """
    time.sleep(seconds)
    return f"Waited {seconds}s"
