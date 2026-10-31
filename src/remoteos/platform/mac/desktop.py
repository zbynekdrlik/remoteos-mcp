"""macOS desktop interactions — screenshots, window enumeration, UI elements."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import pyautogui

from remoteos.coords import Bbox, CaptureGeometry, frame_for
from remoteos.displays import DisplayInfo
from remoteos.input_keys import canonical
from remoteos.platform.mac.availability import diagnose  # noqa: F401  (the provider's diagnose(), #32)
from remoteos.scroll import chunks, wheel_steps
from remoteos.snapshot import fit_to_frame
from remoteos.text_entry import enter_text
from remoteos.typing_input import ENTER, TAB, Stroke, plan

# The modifier of the edit shortcuts (select all, paste) that type_text() sends: Command.
# Ctrl+A in a Cocoa text field moves the caret to the start of the paragraph instead.
_SHORTCUT_MODIFIER = "command"

# Quartz imports (will fail on non-macOS — caught at tool level)
try:
    import Quartz  # noqa: F401

    HAS_QUARTZ = True
except ImportError:
    HAS_QUARTZ = False

HAS_WIN32 = False  # compatibility flag — always False on macOS

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tobool(v: bool | str) -> bool:
    """Handle MCP's bool-as-string quirk."""
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def applescript_literal(value: str) -> str:
    """``value`` as an AppleScript string literal: ``\\`` escaped FIRST, then ``"`` (#39).

    Escaping only the quote turned ``\\"`` into ``\\\\"``: an escaped backslash, then a quote that
    ended the string, and the rest of the value ran as AppleScript (``do shell script`` included).
    """
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_osascript(script: str) -> str:
    """Run an AppleScript via osascript and return stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"osascript exited {result.returncode}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Window info
# ---------------------------------------------------------------------------


@dataclass
class WindowInfo:
    handle: int
    title: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom
    visible: bool
    pid: int = 0

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def list_windows() -> list[WindowInfo]:
    """The window list for a caller that polls: :func:`enumerate_windows` already raises on a failure."""
    return enumerate_windows()


def enumerate_windows() -> list[WindowInfo]:
    """List all visible on-screen windows."""
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    results: list[WindowInfo] = []
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
    if window_list is None:
        return results
    for win in window_list:
        owner = win.get(Quartz.kCGWindowOwnerName, "")
        name = win.get(Quartz.kCGWindowName, "")
        title = f"{owner} — {name}" if name else owner
        if not title:
            continue
        bounds = win.get(Quartz.kCGWindowBounds, {})
        x = int(bounds.get("X", 0))
        y = int(bounds.get("Y", 0))
        w = int(bounds.get("Width", 0))
        h = int(bounds.get("Height", 0))
        rect = (x, y, x + w, y + h)
        wid = int(win.get(Quartz.kCGWindowNumber, 0))
        pid = int(win.get(Quartz.kCGWindowOwnerPID, 0))
        results.append(WindowInfo(handle=wid, title=title, rect=rect, visible=True, pid=pid))
    return results


def get_interactive_elements() -> list[dict]:
    """Stub — macOS Accessibility API integration deferred."""
    return []


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def _display_id(monitor: int):
    """Quartz display id for ``monitor``: 0 = main display, N = N-th active display.

    ``CGGetActiveDisplayList`` always lists the main display first, matching
    ``screencapture -D 1`` (= main), so ``-D N`` and this id are the same display.
    """
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    if monitor <= 0:
        return Quartz.CGMainDisplayID()
    (err, display_ids, count) = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0 or monitor > count:
        raise IndexError(f"Monitor {monitor} not found (have {count})")
    return display_ids[monitor - 1]


def capture_geometry(monitor: int = 0) -> CaptureGeometry:
    """Screen bounds of a capture target for the coordinate contract (#28).

    Bounds are in POINTS (``CGDisplayBounds``, the global space pyautogui moves
    in); the capture size is the display mode's PIXEL size (what ``screencapture``
    writes). On Retina the two differ by the backing scale, so the 2x lands in the
    frame's ``scale`` instead of in every click. ``monitor=0`` is the main display
    (``screencapture`` with one output file captures one display), origin (0,0).
    """
    display_id = _display_id(monitor)
    bounds = Quartz.CGDisplayBounds(display_id)
    x, y = int(bounds.origin.x), int(bounds.origin.y)
    width, height = int(bounds.size.width), int(bounds.size.height)
    mode = Quartz.CGDisplayCopyDisplayMode(display_id)
    pixel_width = int(Quartz.CGDisplayModeGetPixelWidth(mode)) if mode is not None else 0
    pixel_height = int(Quartz.CGDisplayModeGetPixelHeight(mode)) if mode is not None else 0
    if pixel_width <= 0 or pixel_height <= 0:  # no mode info → assume 1:1
        pixel_width, pixel_height = width, height
    return CaptureGeometry(x=x, y=y, width=width, height=height, pixel_width=pixel_width, pixel_height=pixel_height)


MONITOR_ZERO = "the main display"


def list_displays() -> list[DisplayInfo]:
    """The displays behind ``monitor=N`` (#33). A Retina display reports scale 2.0.

    macOS has no short display name below AppKit, so ``name`` is the Quartz display id.
    """
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    (err, display_ids, count) = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0:
        raise RuntimeError(f"CGGetActiveDisplayList failed ({err})")
    main = Quartz.CGMainDisplayID()
    displays = []
    for index, display_id in enumerate(display_ids[:count], start=1):
        g = capture_geometry(index)
        scale = g.pixel_width / g.width
        displays.append(
            DisplayInfo(index, g.x, g.y, g.width, g.height, scale, display_id == main, f"display {display_id}")
        )
    return displays


def capture_frame(monitor: int = 0, max_width: int = 1280):
    """Capture ``monitor`` and fit it to its frame → ``(PIL image, Frame)``."""
    geometry = capture_geometry(monitor)
    frame = frame_for(monitor, max_width, geometry)
    # -D N selects the display (1 = main)
    img = _screencapture(["-D", str(monitor)] if monitor > 0 else [])
    return fit_to_frame(img, frame, geometry), frame


def capture_region(bbox: Bbox):
    """PIL image of ``bbox`` (points, the global display space) at native resolution.

    ``screencapture -R x,y,w,h`` takes points and writes the display's pixels.
    """
    left, top, right, bottom = bbox
    return _screencapture(["-R", f"{left},{top},{right - left},{bottom - top}"])


def _screencapture(args: list[str]):
    """Run ``screencapture -x`` (no sound) with ``args`` and return the capture as a PIL image."""
    if Image is None:
        raise RuntimeError("Pillow not installed — run `pip install Pillow`")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    cmd = ["screencapture", "-x", *args, tmp_path]

    try:
        try:
            subprocess.run(cmd, timeout=10, capture_output=True, check=True)
        except FileNotFoundError:
            raise RuntimeError("screencapture not found — requires macOS")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"screencapture failed: {e.stderr.decode().strip()}")
        img = Image.open(tmp_path)
        img.load()  # read the pixels before the temp file is unlinked
        return img
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Mouse wheel (#29)
# ---------------------------------------------------------------------------

# Apple (CGEventCreateScrollWheelEvent): deltas are "typically in a range from -10 to +10.
# Large values may have unexpected results" — so a scroll is posted in events of <= 10 lines.
_MAX_LINES_PER_EVENT = 10


def scroll(direction: str, clicks: int, x: int | None = None, y: int | None = None) -> str:
    """Turn the wheel ``clicks`` lines toward ``direction``, at screen point (x, y) if given.

    Quartz line events, one line per click: wheel1 = vertical (positive = up),
    wheel2 = horizontal with POSITIVE = LEFT (Quartz's sign — Hammerspoon's
    scrollWheel passes its offsets straight through and documents "positive values
    scroll up or left"), so ``right`` posts a negative wheel2. With a point (points,
    the ``CGDisplayBounds`` space), a mouse-moved event puts the cursor there first
    and every scroll event is located at it.
    """
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    steps = wheel_steps(direction, clicks)
    point = (float(x), float(y)) if x is not None and y is not None else None
    events = []  # built first, posted only when every event exists: all or nothing
    if point is not None:
        move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft)
        events.append(_created(move, "CGEventCreateMouseEvent"))
    for lines in chunks(steps.signed_clicks, _MAX_LINES_PER_EVENT):
        wheel1, wheel2 = (0, -lines) if steps.horizontal else (lines, 0)
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, wheel1, wheel2)
        events.append(_created(event, "CGEventCreateScrollWheelEvent"))
        if point is not None:
            Quartz.CGEventSetLocation(event, point)
        _apply_held_flags(event)
    for event in events:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return steps.summary()


def _created(event, factory: str):
    """``event``, or a RuntimeError when the Quartz ``factory`` returned no event."""
    if event is None:
        raise RuntimeError(f"{factory} returned no event")
    return event


def _require_post_event_access() -> None:
    """Raise unless this process may post events: without it macOS drops them SILENTLY."""
    preflight = getattr(Quartz, "CGPreflightPostEventAccess", None)  # macOS 10.15+
    if preflight is not None and not preflight():
        raise RuntimeError(
            "macOS does not let this process post keyboard events — allow it in System Settings › "
            "Privacy & Security › Accessibility (nothing was clicked, typed or pasted)"
        )


# ---------------------------------------------------------------------------
# Mouse buttons, held keys, the cursor (#34)
# ---------------------------------------------------------------------------

_MOUSE_MOVED = 5  # kCGEventMouseMoved
_BUTTONS = {"left": (1, 2, 6, 0), "right": (3, 4, 7, 1), "middle": (25, 26, 27, 2)}  # down, up, dragged, CGMouseButton
_CLICK_STATE_FIELD = 1  # kCGMouseEventClickState: 2 makes the app see a double click
_CLICKS = {"click": (1, "Clicked"), "double": (2, "Double-clicked"), "triple": (3, "Triple-clicked")}

# kVK_* virtual key codes by `input_keys.canonical` name, on the ANSI layout.
_KEY_CODES = {
    **dict(zip("asdfhgzxcv", range(10))),
    **dict(zip("bqweryt123465", (11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23))),
    **dict(zip("978", (25, 26, 28))),
    **dict(zip("0ouipljknm", (29, 31, 32, 34, 35, 37, 38, 40, 45, 46))),
    "enter": 0x24, "tab": 0x30, "space": 0x31, "backspace": 0x33, "esc": 0x35,
    "super": 0x37, "shift": 0x38, "alt": 0x3A, "ctrl": 0x3B,
    "home": 0x73, "pageup": 0x74, "delete": 0x75, "end": 0x77, "pagedown": 0x79,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
}  # fmt: skip
# kCGEventFlagMask* per modifier: a posted mouse or wheel event does not pick up a posted
# modifier key by itself, so the flags go on every event while a modifier is held.
_MODIFIER_FLAGS = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "super": 0x100000}

_held: list[str] = []  # the keys down now; the desktop lock runs one input tool at a time
_held_button: str | None = None  # the button Click(action="down") left pressed


def _held_flags() -> int:
    flags = 0
    for key in _held:
        flags |= _MODIFIER_FLAGS.get(key, 0)
    return flags


def _apply_held_flags(event) -> None:
    flags = _held_flags()
    if flags:
        Quartz.CGEventSetFlags(event, flags)


def _key_name(key: str) -> str:
    name = canonical(key).lower()
    if name not in _KEY_CODES:
        raise ValueError(f"Unknown key: {key}")
    return name


def check_key(key: str) -> None:
    """Raise when ``key`` is not one :func:`key_down` can press."""
    _key_name(key)


def _post_key(key: str, down: bool) -> None:
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    _require_post_event_access()
    name = _key_name(key)
    event = _created(Quartz.CGEventCreateKeyboardEvent(None, _KEY_CODES[name], down), "CGEventCreateKeyboardEvent")
    if down:
        _held.append(name)
    elif name in _held:
        _held.remove(name)
    _apply_held_flags(event)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def key_down(key: str) -> None:
    """Press ``key`` and leave it down; a modifier also marks every mouse and wheel event until released."""
    _post_key(key, True)


def key_up(key: str) -> None:
    """Release a key pressed by :func:`key_down`."""
    _post_key(key, False)


def _mouse_event(kind: int, point: tuple[float, float], button: int, clicks: int = 0):
    event = _created(Quartz.CGEventCreateMouseEvent(None, kind, point, button), "CGEventCreateMouseEvent")
    if clicks:
        Quartz.CGEventSetIntegerValueField(event, _CLICK_STATE_FIELD, clicks)
    _apply_held_flags(event)
    return event


def _post_mouse(x: int, y: int, button: str, presses: list[bool]) -> None:
    """Move to (x, y), then one press or release per ``presses`` entry: all built, then all posted.

    Quartz, not pyautogui: pyautogui's events carry neither modifier flags nor a click
    state. The click state counts the presses, so ``[True, False] * 3`` is a triple click.
    The move to (x, y) is a drag while a button is held: a plain move is none.
    """
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    _require_post_event_access()
    point = (float(x), float(y))
    down, up, _dragged, code = _BUTTONS[button]
    if _held_button is None:
        events = [_mouse_event(_MOUSE_MOVED, point, code)]
    else:
        events = [_mouse_event(_BUTTONS[_held_button][2], point, _BUTTONS[_held_button][3])]
    count = 0
    for pressed in presses:
        count += pressed
        events.append(_mouse_event(down if pressed else up, point, code, max(count, 1)))
    for event in events:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def click(x: int, y: int, button: str = "left", action: str = "click") -> str:
    """Mouse click / double / triple / hover / button down / button up at screen coordinates (points).

    A plain single click stays pyautogui's. The rest is Quartz: a double or triple click needs the
    click state, or apps see single clicks, and a modifier needs the flags.
    """
    global _held_button
    if action == "down":
        _post_mouse(x, y, button, [True])
        _held_button = button
        return f"Pressed {button} at ({x},{y})"
    if action == "up":
        _post_mouse(x, y, button, [False])
        _held_button = None
        return f"Released {button} at ({x},{y})"
    if action == "hover":
        if not move_held(x, y):
            pyautogui.moveTo(x, y)
        return f"Hovered at ({x},{y})"
    count, word = _CLICKS[action]
    if count > 1 or _held_flags():
        _post_mouse(x, y, button, [True, False] * count)
    else:
        pyautogui.click(x, y, button=button)
    return f"{word} {button} at ({x},{y})"


def move_held(x: int, y: int) -> bool:
    """With a button held, post the move as a drag and return True: apps ignore a plain move then."""
    if _held_button is None:
        return False
    _require_post_event_access()
    dragged, code = _BUTTONS[_held_button][2:]
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, _mouse_event(dragged, (float(x), float(y)), code))
    return True


def cursor_position() -> tuple[int, int]:
    """The cursor in points, the ``CGDisplayBounds`` space the input tools use (top-left origin)."""
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    location = Quartz.CGEventGetLocation(_created(Quartz.CGEventCreate(None), "CGEventCreate"))
    return int(location.x), int(location.y)


# ---------------------------------------------------------------------------
# Unicode typing (#30)
# ---------------------------------------------------------------------------

# A keyboard event's Unicode string holds at most 20 UTF-16 units — Quartz truncates a
# longer one — so text is posted in runs of <= 20 units (a character is never split).
_MAX_UNITS_PER_EVENT = 20
_KEYCODES = {ENTER: 0x24, TAB: 0x30}  # kVK_Return, kVK_Tab
_RUN_INTERVAL_S = 0.01  # between runs: apps that drain input slowly keep up


def _key_pair(stroke: Stroke) -> list:
    """The key-down + key-up events of one stroke: the Return/Tab keycode, or a Unicode string."""
    keycode = _KEYCODES[stroke.key] if stroke.key is not None else 0
    events = []
    for down in (True, False):
        event = _created(Quartz.CGEventCreateKeyboardEvent(None, keycode, down), "CGEventCreateKeyboardEvent")
        if stroke.key is None:
            # The string replaces what the keycode would type, whatever the keyboard layout.
            Quartz.CGEventKeyboardSetUnicodeString(event, len(stroke.units), stroke.source)
        events.append(event)
    return events


def type_unicode(text: str) -> int:
    """Type ``text`` at the keyboard focus; returns the number of characters handed to macOS.

    Quartz keyboard events carrying the text as a Unicode string, in runs of at
    most 20 UTF-16 units (``remoteos.typing_input.plan``); line breaks and tabs are
    the real Return / Tab keys. Not pyautogui: 0.9.54 maps only ASCII and silently
    skips everything else. ``CGEventPost`` reports nothing, so the posting
    permission is checked first, and every event is built before any is posted:
    a refusal types nothing. A locked console still drops posted events unseen.
    """
    if not HAS_QUARTZ:
        raise RuntimeError("Quartz not available — requires macOS with pyobjc-framework-Quartz")
    _require_post_event_access()
    strokes = plan(text, max_units=_MAX_UNITS_PER_EVENT)
    pairs = [_key_pair(stroke) for stroke in strokes]  # all or nothing
    for index, pair in enumerate(pairs):
        if index:
            time.sleep(_RUN_INTERVAL_S)
        for event in pair:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return sum(stroke.chars for stroke in strokes)


def type_text(
    text: str,
    x: int | None = None,
    y: int | None = None,
    clear: bool = False,
    press_enter: bool = False,
    method: str = "keys",
) -> str:
    """The Type tool: click screen point (x, y) if given, clear, type or paste ``text``, press Enter.

    Points are in the ``CGDisplayBounds`` space. Text goes through
    ``remoteos.text_entry`` (``type_unicode`` keystrokes, or a clipboard paste).
    pyautogui only clicks and presses named keys here (Command+A, Delete,
    Command+V, Enter); it never types text. Its events are ``CGEventPost`` too,
    so the posting permission is checked before anything is clicked or pressed.
    """
    if HAS_QUARTZ:
        _require_post_event_access()
    if x is not None and y is not None:
        pyautogui.click(x, y)
        time.sleep(0.1)
    if clear:
        pyautogui.hotkey(_SHORTCUT_MODIFIER, "a")
        pyautogui.press("delete")
        time.sleep(0.05)
    reply = enter_text(
        text,
        method,
        type_unicode=type_unicode,
        get_clipboard=get_clipboard,
        set_clipboard=set_clipboard,
        press_paste=lambda: pyautogui.hotkey(_SHORTCUT_MODIFIER, "v"),
    )
    if press_enter:
        pyautogui.press("enter")
    return reply


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------


def _app_of_window(handle: int) -> str:
    """The app name owning window ``handle`` (the title before " — "); raises when there is none."""
    for w in enumerate_windows():
        if w.handle == handle:
            return w.title.split(" — ")[0] if " — " in w.title else w.title
    raise LookupError(f"No window with handle {handle}")


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Bring a window to the foreground via AppleScript; raises when it cannot (#32)."""
    if not title and not handle:
        raise ValueError("No window specified")
    if handle:
        title = _app_of_window(handle)
    if not title:
        raise LookupError("No window found")

    # The title is an AppleScript literal (#39). "error" makes osascript exit
    # non-zero, so a missing process raises instead of being returned as text.
    title_literal = applescript_literal(title)
    script = f'''
tell application "System Events"
    set allProcs to every process whose visible is true
    repeat with proc in allProcs
        if name of proc contains {title_literal} then
            set frontmost of proc to true
            return "Focused " & name of proc
        end if
    end repeat
    error "No process matching " & {title_literal}
end tell
'''
    return _run_osascript(script)


def minimize_all() -> str:
    """Hide all visible applications via AppleScript."""
    script = '''
tell application "System Events"
    set allProcs to every process whose visible is true
    repeat with proc in allProcs
        set visible of proc to false
    end repeat
end tell
'''
    _run_osascript(script)
    return "Minimized all windows"


def launch_app(name: str, args: str = "") -> str:
    """Launch application via 'open -a'."""
    cmd = ["open", "-a", name]
    if args:
        cmd.append("--args")
        cmd.extend(args.split())
    try:
        subprocess.run(cmd, timeout=10, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to launch {name}: {e.stderr.decode().strip()}") from e
    return f"Launched {name}"


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize the front window of the app owning the given window handle."""
    name_literal = applescript_literal(_app_of_window(handle))
    # an AppleScript error makes osascript exit non-zero: _run_osascript raises it
    script = f'''
tell application "System Events"
    tell process {name_literal}
        set size of front window to {{{int(width)}, {int(height)}}}
        return "Resized " & {name_literal} & " to {int(width)}x{int(height)}"
    end tell
end tell
'''
    return _run_osascript(script)


# ---------------------------------------------------------------------------
# Clipboard — a failure raises (#32)
# ---------------------------------------------------------------------------


def get_clipboard() -> str:
    """Get clipboard contents via pbpaste ("" for an empty or non-text clipboard)."""
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        raise RuntimeError("pbpaste not found — requires macOS") from None
    return result.stdout


def set_clipboard(text: str) -> str:
    """Set clipboard contents via pbcopy."""
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            timeout=5,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("pbcopy not found — requires macOS") from None
    return "Clipboard set"


# ---------------------------------------------------------------------------
# Lock screen
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    """Put display to sleep (locks screen if password required on wake)."""
    subprocess.run(["pmset", "displaysleepnow"], timeout=5, check=True)
    return "Screen locked"


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def show_notification(title: str, message: str) -> str:
    """Show a macOS notification via osascript."""
    script = f"display notification {applescript_literal(message)} with title {applescript_literal(title)}"
    _run_osascript(script)
    return "Notification shown"
