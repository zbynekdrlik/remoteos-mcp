"""macOS desktop interactions — screenshots, window enumeration, UI elements."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import pyautogui

from remoteos import errors
from remoteos.coords import CaptureGeometry, frame_for
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

log = logging.getLogger("remoteos.platform.mac.desktop")

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


def capture_frame(monitor: int = 0, max_width: int = 1280):
    """Capture ``monitor`` and fit it to its frame → ``(PIL image, Frame)``."""
    if Image is None:
        raise RuntimeError("Pillow not installed — run `pip install Pillow`")
    geometry = capture_geometry(monitor)
    frame = frame_for(monitor, max_width, geometry)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    # screencapture -x = no sound; -D N selects the display (1 = main)
    cmd = ["screencapture", "-x"]
    if monitor > 0:
        cmd.extend(["-D", str(monitor)])
    cmd.append(tmp_path)

    try:
        try:
            subprocess.run(cmd, timeout=10, capture_output=True, check=True)
        except FileNotFoundError:
            raise RuntimeError("screencapture not found — requires macOS")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"screencapture failed: {e.stderr.decode().strip()}")
        img = Image.open(tmp_path)
        img.load()  # read the pixels before the temp file is unlinked
        return fit_to_frame(img, frame, geometry), frame
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
    for event in events:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return steps.summary()


def _created(event, factory: str):
    """``event``, or a RuntimeError when the Quartz ``factory`` returned no event."""
    if event is None:
        raise RuntimeError(f"{factory} returned no event")
    return event


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


def _require_post_event_access() -> None:
    """Raise unless this process may post events: without it macOS drops them SILENTLY."""
    preflight = getattr(Quartz, "CGPreflightPostEventAccess", None)  # macOS 10.15+
    if preflight is not None and not preflight():
        raise RuntimeError(
            "macOS does not let this process post keyboard events — allow it in System Settings › "
            "Privacy & Security › Accessibility (nothing was clicked, typed or pasted)"
        )


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

    # Escape double quotes in title for AppleScript. "error" makes osascript exit
    # non-zero, so a missing process raises instead of being returned as text.
    safe_title = title.replace('"', '\\"')
    script = f'''
tell application "System Events"
    set allProcs to every process whose visible is true
    repeat with proc in allProcs
        if name of proc contains "{safe_title}" then
            set frontmost of proc to true
            return "Focused " & name of proc
        end if
    end repeat
    error "No process matching \\"{safe_title}\\""
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
    safe_name = _app_of_window(handle).replace('"', '\\"')
    # an AppleScript error makes osascript exit non-zero: _run_osascript raises it
    script = f'''
tell application "System Events"
    tell process "{safe_name}"
        set size of front window to {{{width}, {height}}}
        return "Resized {safe_name} to {width}x{height}"
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
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    _run_osascript(script)
    return "Notification shown"


# ---------------------------------------------------------------------------
# Desktop availability (#32): GUI session, lock screen, Screen Recording, Accessibility
# ---------------------------------------------------------------------------

# AXIsProcessTrusted lives in HIServices (ApplicationServices), which pyobjc-framework-Quartz
# does not wrap; ctypes reaches it without a new dependency.
_APPLICATION_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_SCREEN_IS_LOCKED = "CGSSessionScreenIsLocked"  # set in the session dictionary while the lock screen shows


def _load_application_services():
    return ctypes.CDLL(_APPLICATION_SERVICES)


def _accessibility_trusted() -> Optional[bool]:
    """``AXIsProcessTrusted()``: may this process control the mouse and keyboard? None = unknown."""
    try:
        lib = _load_application_services()
    except OSError as e:
        log.debug("ApplicationServices not loadable (%s): Accessibility state unknown", e)
        return None
    trusted = lib.AXIsProcessTrusted
    trusted.restype = ctypes.c_bool
    trusted.argtypes = []
    return bool(trusted())


def _binary() -> str:
    """The Python binary macOS grants the permissions to (the venv's symlink resolved)."""
    return os.path.realpath(sys.executable)


def diagnose(capture: bool = False) -> Optional[str]:
    """Why the desktop cannot be seen (``capture``) or driven (input) now, or None when it can (#32).

    The window-server session first (none, not on the console, locked), then the
    permission this use needs: Screen Recording to capture, Accessibility to
    send input. Without them macOS fails SILENTLY (a wallpaper-only screenshot,
    dropped events). Only a positive finding is a diagnosis. An API that is
    missing (an older macOS) or a probe that fails means "usable" (fail open).
    """
    try:
        return _diagnose(capture)
    except Exception as e:
        log.warning("desktop probe failed (%s: %s): assuming the desktop is usable", type(e).__name__, e)
        return None


def _diagnose(capture: bool) -> Optional[str]:
    if not HAS_QUARTZ:
        return None  # the capture / input functions raise their own "Quartz not available"
    session_info = getattr(Quartz, "CGSessionCopyCurrentDictionary", None)
    if session_info is not None:
        info = session_info()
        if info is None:  # NULL outside a window-server (GUI) session
            return errors.MAC_NO_GUI_SESSION
        on_console = getattr(Quartz, "kCGSessionOnConsoleKey", "kCGSSessionOnConsoleKey")
        if not info.get(on_console, True):
            return errors.MAC_NOT_ON_CONSOLE
        if info.get(_SCREEN_IS_LOCKED, False):
            return errors.LOCKED
    if capture:
        screen_access = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)  # macOS 10.15+
        if screen_access is not None and not screen_access():
            return errors.mac_screen_recording(_binary())
    elif _accessibility_trusted() is False:
        return errors.mac_accessibility(_binary())
    log.debug("desktop probe: macOS session usable (capture=%s)", capture)
    return None
