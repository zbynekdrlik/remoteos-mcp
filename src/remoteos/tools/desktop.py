"""Desktop control tools: Snapshot, Zoom, DisplayInventory, CursorPosition, Click, Type, Scroll, Move,
Shortcut, HoldKey, Wait, WaitFor, ElementTree, ClickElement, TypeElement.
"""

from __future__ import annotations

import sys as _sys
import time

from mcp.types import ImageContent, TextContent

from remoteos import ocr
from remoteos.app import (
    ToolAnnotations,
    _capture_snapshot_frame,
    _capture_tool,
    _capture_with_retry,
    _current_frame,
    _desktop_preflight,
    _display_tool,
    _input_tool,
    _tobool,
    log,
    mcp,
)

if _sys.platform != "linux":
    import pyautogui  # remoteos.app imported it first: DPI awareness set, FAILSAFE and PAUSE configured

from remoteos.coords import (
    check_space,
    contract_line,
    frame_for,
    image_to_screen,
    in_image,
    mapping_note,
    region_geometry,
    region_to_screen,
    screen_to_image,
    zoom_line,
)
from remoteos.displays import inventory_text
from remoteos.errors import failure, reports_errors
from remoteos.input_keys import (
    MAX_HOLD_S,
    check_click,
    check_modifiers,
    check_repeat,
    held_keys,
    hold_seconds,
    split_keys,
)
from remoteos.platform import get_desktop, is_linux
from remoteos.scroll import normalize_request
from remoteos.snapshot import element_lines, encode_jpeg, fit_to_frame, tree_lines, window_lines
from remoteos.typing_input import check_method

# ============================= DESKTOP CONTROL =============================
#
# Coordinate contract (#28): Snapshot / AnnotatedSnapshot return an image that is
# a FRAME of the screen (``remoteos.coords``) and print it as one contract line.
# Every input tool takes IMAGE pixels of that frame by default and maps them back
# to the screen here, recomputing the frame from (monitor, max_width) + the live
# display geometry — no per-session state. ``coords="screen"`` is the escape hatch.
# Zoom (#33) is view-only: its rectangle maps through that same frame, and its reply
# states the region in screen coordinates, so no input tool needs a second mode.

_ZOOM_QUALITY = 85  # small text is why Zoom exists: Snapshot's default 65 smears it
_MAX_WAIT_S = 300.0
_SNAPSHOT_ELEMENTS = 50
_POLL_S = 0.5


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
    elements = _desktop.get_interactive_elements(limit=_SNAPSHOT_ELEMENTS)
    if elements:
        lines += [
            "",
            "**Interactive Elements (foreground window):**",
            *element_lines(frame, elements[:_SNAPSHOT_ELEMENTS]),
        ]

    parts.append(TextContent(type="text", text="\n".join(lines)))
    return parts


@mcp.tool(
    annotations=ToolAnnotations(
        title="Zoom",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_capture_tool
def Zoom(
    x: int,
    y: int,
    width: int,
    height: int,
    monitor: int = 0,
    max_width: int = 1280,
    coords: str = "image",
) -> list:
    """Capture a part of the Snapshot image at native resolution, to read small text.

    View only. The rectangle is in IMAGE pixels of the last Snapshot (pass the same
    monitor/max_width), cut to that frame. The result is captured at native
    resolution and downscaled only when wider than max_width. Input tools keep
    taking Snapshot image pixels: to click something seen in the zoom, convert it
    with the reply's origin and scale (screen = origin + round(image * scale)) and
    pass coords="screen". For a capture wider than 1280 px use max_width=0 with
    coords="screen".

    Args:
        x: Left edge in Snapshot image pixels (screen coordinates if coords="screen").
        y: Top edge in Snapshot image pixels (screen coordinates if coords="screen").
        width: Rectangle width, in the same units as x.
        height: Rectangle height, in the same units as y.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame, also the widest image returned (0=native).
        coords: 'image' (default) or 'screen' = raw OS coordinates (physical pixels
            on Windows/Linux, points on macOS).
    """
    space = check_space(coords)
    desktop = get_desktop()
    geometry = desktop.capture_geometry(monitor)
    bbox = region_to_screen(frame_for(monitor, max_width, geometry), x, y, width, height, space)
    region = region_geometry(geometry, bbox)
    frame = frame_for(monitor, max_width, region)
    img = fit_to_frame(_capture_with_retry(lambda: desktop.capture_region(bbox)), frame, region)
    log.debug("zoom frame: %s", zoom_line(frame))
    return [
        ImageContent(type="image", data=encode_jpeg(img, _ZOOM_QUALITY), mimeType="image/jpeg"),
        TextContent(type="text", text=f"**Coordinates:** {zoom_line(frame)}"),
    ]


@mcp.tool(
    annotations=ToolAnnotations(
        title="DisplayInventory",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_display_tool
def DisplayInventory() -> str:
    """List the displays: what each `monitor` value of the screen and input tools means.

    One line per display: its index (the tools' monitor=N), name, bounds and origin
    in screen coordinates, scale, and whether it is the primary. The first line says
    what monitor=0 is on this machine: all monitors on Windows and Linux, the main
    display on macOS.
    """
    desktop = get_desktop()
    return inventory_text(desktop.MONITOR_ZERO, desktop.capture_geometry(0), desktop.list_displays())


@mcp.tool(
    annotations=ToolAnnotations(
        title="CursorPosition",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_display_tool
def CursorPosition(monitor: int = 0, max_width: int = 1280) -> str:
    """Where the mouse cursor is, in IMAGE pixels of a Snapshot frame and in screen coordinates.

    Args:
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
    """
    x, y = get_desktop().cursor_position()
    frame = _current_frame(monitor, max_width)
    ix, iy = screen_to_image(frame, x, y)
    marker = "" if in_image(frame, ix, iy) else " (off-frame)"
    return f"Cursor at image ({ix},{iy}){marker}, screen ({x},{y}) (monitor={monitor}, max_width={max_width})"


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
    modifiers: str = "",
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
        action: 'click', 'double', 'triple', 'hover', or 'down' / 'up' = press the
            button and leave it down / release it, for a drag in steps: Click 'down',
            Move, Click 'up'. A pressed button stays down until its 'up'.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
        coords: 'image' (default) or 'screen' = raw OS coordinates, passed through
            unchanged (physical pixels on Windows/Linux, points on macOS).
        modifiers: Keys held during the action, e.g. 'ctrl', 'ctrl+shift', 'alt', 'win'
            ('cmd' on macOS). Always released afterwards, also when the click fails.
    """
    check_click(button, action)
    mods = check_modifiers(modifiers)
    [(x, y)], note = _to_screen([(x, y)], monitor, max_width, coords)
    desktop = get_desktop()
    with held_keys(mods, desktop):
        return desktop.click(x, y, button=button, action=action) + note


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
    modifiers: str = "",
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
        modifiers: Keys held while the wheel turns, e.g. 'ctrl' to zoom. Always released afterwards.
    """
    steps = normalize_request(amount, direction, _tobool(horizontal))
    mods = check_modifiers(modifiers)
    points, note = _to_screen(_optional_point(x, y), monitor, max_width, coords)
    sx, sy = points[0] if points else (None, None)
    log.debug("Scroll %s x%d (requested %d) at %s", steps.direction, steps.clicks, steps.requested, points)
    desktop = get_desktop()
    with held_keys(mods, desktop):
        return desktop.scroll(steps.direction, steps.requested, x=sx, y=sy) + note


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
    move_held = getattr(get_desktop(), "move_held", None)  # macOS: a held button needs drag events (#34)
    if move_held is None or not move_held(x, y):
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
def Shortcut(keys: str, repeat: int = 1) -> str:
    """Execute keyboard shortcut, or press one key several times.

    Args:
        keys: Shortcut string, e.g. 'ctrl+c', 'alt+tab', 'win+e', 'down'.
        repeat: How many times to press it, 1..100.
    """
    repeat = check_repeat(repeat)
    if is_linux():
        return get_desktop().shortcut(keys, repeat)
    parts = [k.strip() for k in keys.lower().split("+")]
    for _ in range(repeat):
        pyautogui.hotkey(*parts)
    return f"Executed shortcut: {keys}" + (f" x{repeat}" if repeat > 1 else "")


@mcp.tool(
    annotations=ToolAnnotations(
        title="HoldKey",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
def HoldKey(keys: str, seconds: float = 1.0) -> str:
    """Hold keys down for a time, then release them. They are released also on a failure.

    Args:
        keys: One key or several joined by '+', e.g. 'w', 'shift+w', 'ctrl+alt': letters,
            digits, F-keys and named keys (space, enter, tab, esc, up, pageup, ...).
        seconds: How long to hold, capped at 30.
    """
    names = split_keys(keys)
    if not names:
        raise ValueError("No keys given")
    hold = hold_seconds(seconds)
    log.debug("HoldKey %d keys for %ss", len(names), hold)
    with held_keys(names, get_desktop()):
        time.sleep(hold)
    return f"Held {keys} for {hold}s" + (f" (capped at {MAX_HOLD_S:.0f}s)" if hold < seconds else "")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Wait",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def Wait(seconds: float = 1.0) -> str:
    """Pause execution, without blocking other tools. To wait for something on screen use WaitFor.

    Args:
        seconds: Seconds to wait, capped at 300.
    """
    wait = min(seconds, _MAX_WAIT_S)
    time.sleep(wait)
    return f"Waited {wait}s" + (f" (capped at {_MAX_WAIT_S:.0f}s)" if wait < seconds else "")


def _squeeze(text: str) -> str:
    """``text`` for matching: case-folded, runs of whitespace as one space (OCR breaks lines freely)."""
    return " ".join(text.split()).casefold()


def _find_text(needle: str, monitor: int) -> str | None:
    """A reply when OCR of ``monitor`` shows ``needle``, else None."""
    if monitor > 0:
        g = get_desktop().capture_geometry(monitor)
        seen = ocr.run_ocr(left=g.x, top=g.y, right=g.x + g.width, bottom=g.y + g.height)
    else:
        seen = ocr.run_ocr()
    wanted = _squeeze(needle)
    if wanted not in _squeeze(seen):
        return None
    lines = [line.strip() for line in seen.splitlines() if wanted in _squeeze(line)]
    return f"text on monitor {monitor}" + (f", line: {lines[0]}" if lines else "")


def _find_window(needle: str, monitor: int, max_width: int) -> str | None:
    """A reply when a window title contains ``needle``, else None. A failing window list raises."""
    wanted = needle.strip().casefold()
    hits = [w for w in get_desktop().list_windows() if wanted in w.title.casefold()]
    if not hits:
        return None
    return "\n".join(["window (image pixels):", *window_lines(_current_frame(monitor, max_width), hits)])


@mcp.tool(
    annotations=ToolAnnotations(
        title="WaitFor",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors  # its preflight depends on what it waits for, as Snapshot's on use_vision
def WaitFor(
    text: str = "",
    window: str = "",
    timeout: float = 30,
    monitor: int = 0,
    max_width: int = 1280,
) -> str:
    """Wait until text shows on screen or a window appears; other tools keep working meanwhile.

    Checks every 0.5 s and replies with what matched and where. Not found in time
    is an error. Matching is case-insensitive and on part of the text or title. Text
    is read by OCR: give a short distinctive string, OCR is not exact.

    Args:
        text: Text to find by OCR. Give exactly one of text and window.
        window: Part of a window title to find.
        timeout: Seconds to keep trying, 0..300 (0 = check once).
        monitor: Monitor to read (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame for a window's rect (default 1280, as Snapshot).
    """
    by_text = bool(text.strip())
    if by_text == bool(window.strip()):
        raise ValueError("Give exactly one of text and window")
    if not 0 <= timeout <= _MAX_WAIT_S:
        raise ValueError(f"timeout must be 0..{_MAX_WAIT_S:.0f}, got {timeout}")
    if monitor < 0:
        raise ValueError(f"No monitor {monitor}")
    _desktop_preflight("capture" if by_text else "windows")
    _current_frame(monitor, max_width)  # a monitor that does not exist fails now, not after the wait
    started = time.monotonic()
    while True:
        found = _find_text(text, monitor) if by_text else _find_window(window, monitor, max_width)
        elapsed = time.monotonic() - started
        if found:
            log.debug("WaitFor matched after %.1fs", elapsed)
            return f"Found after {elapsed:.1f}s: {found}"
        if elapsed >= timeout:
            what = f"text {text!r}" if by_text else f"window titled {window!r}"
            raise failure(f"WaitFor timed out after {timeout}s: no {what}")
        time.sleep(min(_POLL_S, timeout - elapsed))


def _elements_provider():
    """The provider, once the OS lets this process read other apps' controls (macOS: #37)."""
    _desktop_preflight("accessibility")
    return get_desktop()


@mcp.tool(
    annotations=ToolAnnotations(
        title="ElementTree",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@_display_tool
def ElementTree(window: str = "", monitor: int = 0, max_width: int = 1280) -> str:
    """List a window's interactive UI elements from the OS accessibility tree, each with a ref.

    Unlike a screenshot this sees real controls: buttons, fields and links in browsers
    and in Electron, WPF and UWP apps too, each with its role, name, state and bounds.
    One line per element: [ref] role "name" enabled|disabled (WxH at X,Y). Bounds are
    IMAGE pixels of the Snapshot frame (monitor, max_width). Pass a ref to ClickElement
    or TypeElement instead of guessing pixels. Refs belong to the latest tree only: a
    new ElementTree or AnnotatedSnapshot ends them. At most 300 elements are listed, and the reply says when
    the list is incomplete. UI Automation on Windows, the Accessibility API on macOS (it
    needs that permission), AT-SPI on Linux X11, where an app that shows nothing gets a hint
    how to start it so that it does.

    Args:
        window: Part of a window title, matched as FocusWindow does. Empty = the foreground window.
        monitor: monitor of the Snapshot frame (default 0, as Snapshot).
        max_width: max_width of the Snapshot frame (default 1280, as Snapshot).
    """
    desktop = _elements_provider()
    frame = _current_frame(monitor, max_width)
    tree = desktop.element_tree(window)
    log.debug("ElementTree: %d elements, truncated=%s", len(tree.items), bool(tree.truncated))
    lines = [f"**Coordinates:** {contract_line(frame)}", "", f"**Elements of {tree.title!r}:** {len(tree.items)}"]
    lines += tree_lines(frame, tree.items)
    if tree.truncated:
        lines += ["", f"**Incomplete:** {tree.truncated}."]
    if tree.hint:
        lines += ["", f"**Hint:** {tree.hint}."]
    return "\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(
        title="ClickElement",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
def ClickElement(ref: str) -> str:
    """Activate an element from the latest ElementTree or AnnotatedSnapshot, by its ref.

    Uses the element's own action (Invoke, AXPress, AT-SPI's press), which needs no mouse.
    An element without one gets a left click at its centre, unless something else is on top
    of it there. A ref of
    an older tree, or an element that is gone or disabled, is an error, never a click elsewhere.

    Args:
        ref: The element's ref from ElementTree, e.g. "4821.12".
    """
    return _elements_provider().click_element(ref)


@mcp.tool(
    annotations=ToolAnnotations(
        title="TypeElement",
        destructiveHint=False,
        openWorldHint=False,
    )
)
@_input_tool
def TypeElement(ref: str, text: str) -> str:
    """Enter text in an element from the latest ElementTree or AnnotatedSnapshot, by its ref.

    An element with a settable value (most text fields) gets ``text`` as its whole new
    value, replacing what was there, with no keystrokes. Any other element gets the
    keyboard focus and ``text`` is typed at its caret, as Type does; the reply says which
    one happened. A read-only element, or one that will not take the focus, is an error
    and nothing is typed. A ref of an older tree, or an element that is gone or disabled,
    is an error too.

    Args:
        ref: The element's ref from ElementTree, e.g. "4821.12".
        text: Text to enter, in any language.
    """
    log.debug("TypeElement %d chars", len(text))
    return _elements_provider().type_element(ref, text)
