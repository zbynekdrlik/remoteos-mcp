"""Win32 desktop interactions — screenshots, window enumeration, UI elements."""

from __future__ import annotations

import ctypes
import locale
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from remoteos.platform.win.dpi import enable_per_monitor_dpi_awareness

log = logging.getLogger("remoteos.platform.win.desktop")

# Per-monitor-v2 DPI awareness MUST be in effect before pyautogui is imported:
# pyautogui's import calls SetProcessDPIAware() (system-aware), after which the
# process can no longer become per-monitor aware (#28). remoteos.app already did this
# before its own pyautogui import; this idempotent call covers any other importer.
enable_per_monitor_dpi_awareness()

import pyautogui  # noqa: E402

# Win32 imports (will fail on non-Windows — caught at tool level)
try:
    import win32api  # noqa: F401
    import win32clipboard
    import win32con
    import win32gui
    import win32process

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

from PIL import ImageGrab  # noqa: E402

from remoteos.coords import Bbox, CaptureGeometry, frame_for  # noqa: E402
from remoteos.displays import DisplayInfo  # noqa: E402
from remoteos.platform.command import powershell_argv, run_command  # noqa: E402
from remoteos.platform.win.availability import diagnose, reconnect_to_console  # noqa: E402,F401  (#32)
from remoteos.platform.win.psquote import ps_literal  # noqa: E402
from remoteos.scroll import wheel_steps  # noqa: E402
from remoteos.snapshot import fit_to_frame  # noqa: E402
from remoteos.text_entry import enter_text  # noqa: E402
from remoteos.typing_input import ENTER, TAB, Stroke, plan  # noqa: E402

# The modifier of the edit shortcuts (select all, paste) that type_text() sends.
_SHORTCUT_MODIFIER = "ctrl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tobool(v: bool | str) -> bool:
    """Handle MCP's bool-as-string quirk."""
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


_LOCALE_NAME_MAX_LENGTH = 85  # winnls.h, including the terminating null


def _get_system_language() -> str:
    """The user's Windows locale name (e.g. ``sk-SK``) for Snapshot's "System Language" line.

    kernel32 ``GetUserDefaultLocaleName`` (Vista+), then ``locale.getlocale()``, then ``en_US``.
    The ``locale`` module's default-locale getter, used before, is deprecated since Python 3.11
    and removed in 3.15 (#48).
    """
    try:
        buf = ctypes.create_unicode_buffer(_LOCALE_NAME_MAX_LENGTH)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, _LOCALE_NAME_MAX_LENGTH) and buf.value:
            return buf.value
        log.debug("GetUserDefaultLocaleName returned no name")
    except Exception as e:
        log.debug("GetUserDefaultLocaleName failed (%s)", type(e).__name__)
    try:
        return locale.getlocale()[0] or "en_US"
    except Exception as e:
        log.debug("locale.getlocale failed (%s)", type(e).__name__)
        return "en_US"


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
    """List all visible top-level windows."""
    if not HAS_WIN32:
        raise RuntimeError("pywin32 not installed — run `pip install pywin32`")
    results: list[WindowInfo] = []

    def _cb(hwnd: int, _extra: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        rect = win32gui.GetWindowRect(hwnd)
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        results.append(WindowInfo(handle=hwnd, title=title, rect=rect, visible=True, pid=pid))
        return True

    win32gui.EnumWindows(_cb, None)
    return results


def get_interactive_elements() -> list[dict]:
    """Simplified accessibility tree — enumerate child windows with class/text."""
    if not HAS_WIN32:
        raise RuntimeError("pywin32 not installed — run `pip install pywin32`")
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return []
    elements: list[dict] = []
    idx = [0]

    def _cb(hwnd: int, _extra: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        text = win32gui.GetWindowText(hwnd)
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        idx[0] += 1
        elements.append(
            {
                "index": idx[0],
                "class": cls,
                "text": text,
                "rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
            }
        )
        return True

    try:
        win32gui.EnumChildWindows(fg, _cb, None)
    except Exception:
        pass
    return elements


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def _get_monitor_bbox(monitor: int) -> tuple[int, int, int, int] | None:
    """Get bounding box for a specific monitor (1-indexed). Returns None for all monitors."""
    if monitor <= 0:
        return None  # all monitors
    try:
        if HAS_WIN32:
            monitors = win32api.EnumDisplayMonitors()
            if monitor <= len(monitors):
                _hmon, _hdc, rect = monitors[monitor - 1]
                return (rect[0], rect[1], rect[2], rect[3])
            raise IndexError(f"Monitor {monitor} not found (have {len(monitors)})")
        else:
            raise RuntimeError("pywin32 needed for specific monitor selection")
    except Exception:
        raise


MONITOR_ZERO = "the whole virtual screen, all monitors"

_MONITORINFOF_PRIMARY = 1
_MDT_EFFECTIVE_DPI = 0
_DEFAULT_DPI = 96


def _monitor_scale(hmonitor) -> float | None:
    """The monitor's scale (effective DPI / 96), or ``None`` when Windows cannot say.

    ``GetDpiForMonitor`` is Windows 8.1+ (shcore).
    """
    dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
    try:
        result = ctypes.windll.shcore.GetDpiForMonitor(
            ctypes.c_void_p(int(hmonitor)), _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
        )
    except (AttributeError, OSError) as e:
        log.warning("GetDpiForMonitor unavailable (%s)", type(e).__name__)
        return None
    if result != 0 or dpi_x.value == 0:
        log.warning("GetDpiForMonitor failed (HRESULT %s)", result)
        return None
    return dpi_x.value / _DEFAULT_DPI


def list_displays() -> list[DisplayInfo]:
    """The displays behind ``monitor=N`` (#33): the ``EnumDisplayMonitors`` order of ``_get_monitor_bbox``."""
    if not HAS_WIN32:
        raise RuntimeError("pywin32 not installed — run `pip install pywin32`")
    displays = []
    for index, (hmonitor, _hdc, rect) in enumerate(win32api.EnumDisplayMonitors(), start=1):
        info = win32api.GetMonitorInfo(hmonitor)
        left, top, right, bottom = rect
        displays.append(
            DisplayInfo(
                index=index,
                x=left,
                y=top,
                width=right - left,
                height=bottom - top,
                scale=_monitor_scale(hmonitor),
                primary=bool(info["Flags"] & _MONITORINFOF_PRIMARY),
                name=info["Device"],
            )
        )
    return displays


# GetSystemMetrics indices of the virtual screen (the union of all monitors).
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79


def capture_geometry(monitor: int = 0) -> CaptureGeometry:
    """Screen bounds of a capture target for the coordinate contract (#28).

    ``monitor=0`` is the virtual screen (all monitors — its origin is negative
    when a monitor sits left of / above the primary); ``monitor=N`` is the N-th
    ``EnumDisplayMonitors`` rect. With per-monitor-v2 DPI awareness these are
    physical pixels — the same space ``ImageGrab``, window rects and
    ``SetCursorPos`` (pyautogui) use, so the capture size equals the bounds.
    """
    if monitor <= 0:
        metric = ctypes.windll.user32.GetSystemMetrics
        x, y = metric(_SM_XVIRTUALSCREEN), metric(_SM_YVIRTUALSCREEN)
        width, height = metric(_SM_CXVIRTUALSCREEN), metric(_SM_CYVIRTUALSCREEN)
    else:
        left, top, right, bottom = _get_monitor_bbox(monitor)
        x, y, width, height = left, top, right - left, bottom - top
    return CaptureGeometry(x=x, y=y, width=width, height=height, pixel_width=width, pixel_height=height)


def capture_frame(monitor: int = 0, max_width: int = 1280):
    """Capture ``monitor`` and fit it to its frame → ``(PIL image, Frame)``."""
    geometry = capture_geometry(monitor)
    frame = frame_for(monitor, max_width, geometry)
    if monitor <= 0:
        img = ImageGrab.grab(all_screens=True)
    else:
        img = capture_region((geometry.x, geometry.y, geometry.x + geometry.width, geometry.y + geometry.height))
    return fit_to_frame(img, frame, geometry), frame


def capture_region(bbox: Bbox):
    """PIL image of ``bbox`` (screen coordinates) at native resolution.

    all_screens=True: PIL crops the bbox out of the whole virtual screen. Without it
    the grab is primary-only and a bbox on a non-primary monitor comes back black.
    """
    return ImageGrab.grab(bbox=bbox, all_screens=True)


# ---------------------------------------------------------------------------
# Mouse wheel (#29)
# ---------------------------------------------------------------------------

_INPUT_MOUSE = 0
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000


class _MOUSEINPUT(ctypes.Structure):
    # Fixed-width fields = the Win32 layout on x86 and x64 (and importable off Windows for
    # the tests). mouseData is a DWORD in the header; c_int32 is the same 32 bits, and a
    # wheel delta is signed.
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class _KEYBDINPUT(ctypes.Structure):
    # The Win32 layout with fixed-width fields: wVk / wScan are WORDs, dwFlags / time DWORDs.
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member of INPUT's union (KEYBDINPUT is smaller), so
    # sizeof(_INPUT) is the real sizeof(INPUT) (40 on x64, 28 on x86) — SendInput
    # rejects any other cbSize.
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


def scroll(direction: str, clicks: int, x: int | None = None, y: int | None = None) -> str:
    """Turn the wheel ``clicks`` notches toward ``direction``, at screen point (x, y) if given.

    One ``SendInput`` event of ``±clicks × WHEEL_DELTA`` (120): ``MOUSEEVENTF_WHEEL``
    for up/down (positive = up), ``MOUSEEVENTF_HWHEEL`` for left/right (positive =
    right). Not pyautogui: 0.9.54 sends the raw count as the delta (1/120 of a notch)
    and routes hscroll into the vertical wheel. With a point, the cursor is put there
    first (``SetCursorPos``, physical pixels under per-monitor-v2) — the wheel goes to
    the window under it.
    """
    steps = wheel_steps(direction, clicks)
    user32 = ctypes.windll.user32
    if x is not None and y is not None and not user32.SetCursorPos(int(x), int(y)):
        raise RuntimeError(f"SetCursorPos({x},{y}) failed — no interactive desktop (locked or disconnected session?)")
    events = (_INPUT * 1)()
    events[0].type = _INPUT_MOUSE
    events[0].u.mi.dwFlags = _MOUSEEVENTF_HWHEEL if steps.horizontal else _MOUSEEVENTF_WHEEL
    events[0].u.mi.mouseData = steps.delta
    # A 0 return means the input desktop refused the event. A UIPI block (a higher-integrity
    # window under the cursor) is NOT reported: SendInput still returns 1 (Microsoft docs).
    sent = user32.SendInput(1, events, ctypes.sizeof(_INPUT))
    if sent != 1:
        raise RuntimeError(
            f"SendInput inserted {sent} of 1 wheel events — input is blocked (locked or disconnected session?)"
        )
    return steps.summary()


# ---------------------------------------------------------------------------
# Unicode typing (#30)
# ---------------------------------------------------------------------------

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VIRTUAL_KEYS = {ENTER: 0x0D, TAB: 0x09}  # VK_RETURN, VK_TAB
_KEY_INTERVAL_S = 0.01  # between characters: apps that drain input slowly keep up


def _key_events(stroke: Stroke):
    """The INPUT array of one stroke: a down + up per UTF-16 code unit, or of its virtual key."""
    if stroke.key is not None:
        presses = [(_VIRTUAL_KEYS[stroke.key], 0, 0)]
    else:
        # KEYEVENTF_UNICODE: wVk = 0 and wScan = the code unit; Windows delivers it as a
        # VK_PACKET → WM_CHAR, independent of the keyboard layout. A surrogate pair is two
        # code units in the same call, so the pair always arrives whole.
        presses = [(0, unit, _KEYEVENTF_UNICODE) for unit in stroke.units]
    events = (_INPUT * (2 * len(presses)))()
    for i, (vk, scan, flags) in enumerate(presses):
        for j, up in enumerate((0, _KEYEVENTF_KEYUP)):
            event = events[2 * i + j]
            event.type = _INPUT_KEYBOARD
            event.u.ki.wVk = vk
            event.u.ki.wScan = scan
            event.u.ki.dwFlags = flags | up
    return events


def type_unicode(text: str) -> int:
    """Type ``text`` at the keyboard focus; returns the number of characters delivered.

    One ``SendInput`` call per character (``remoteos.typing_input.plan``): Unicode
    packets for text, ``VK_RETURN`` / ``VK_TAB`` for line breaks and tabs. Not
    pyautogui: 0.9.54 maps only chr(32..127) and silently skips everything else.
    A call that inserts fewer events than it was given (a locked or disconnected
    session) raises, naming how many characters got through. A UIPI block (a
    higher-integrity window has the focus) is silent: SendInput still succeeds.
    """
    user32 = ctypes.windll.user32
    size = ctypes.sizeof(_INPUT)
    delivered = 0
    for index, stroke in enumerate(plan(text)):
        if index:
            time.sleep(_KEY_INTERVAL_S)
        events = _key_events(stroke)
        sent = user32.SendInput(len(events), events, size)
        if sent != len(events):
            raise RuntimeError(
                f"SendInput inserted {sent} of {len(events)} key events after {delivered} of {len(text)} chars"
                " — input is blocked (locked or disconnected session?)"
            )
        delivered += stroke.chars
    return delivered


def type_text(
    text: str,
    x: int | None = None,
    y: int | None = None,
    clear: bool = False,
    press_enter: bool = False,
    method: str = "keys",
) -> str:
    """The Type tool: click screen point (x, y) if given, clear, type or paste ``text``, press Enter.

    Text goes through ``remoteos.text_entry`` (``type_unicode`` keystrokes, or a
    clipboard paste). pyautogui only clicks and presses named keys here (Ctrl+A,
    Delete, Ctrl+V, Enter); it never types text.
    """
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


_NO_PYWIN32 = "pywin32 not installed — run `pip install pywin32`"


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Bring a window to the foreground. Fuzzy-match title if provided; raises when it cannot (#32)."""
    if not HAS_WIN32:
        raise RuntimeError(_NO_PYWIN32)

    hwnd = None
    if handle:
        hwnd = handle
    elif title:
        from thefuzz import fuzz

        best_score = 0
        for w in enumerate_windows():
            score = fuzz.partial_ratio(title.lower(), w.title.lower())
            if score > best_score:
                best_score = score
                hwnd = w.handle
        if best_score < 50:
            raise LookupError(f"No window matching '{title}' (best score {best_score})")

    if not hwnd:
        raise LookupError("No window found")

    try:
        # Restore if minimized
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        raise RuntimeError(f"Failed to focus: {e}") from e
    return f"Focused window handle={hwnd} title='{win32gui.GetWindowText(hwnd)}'"


def minimize_all() -> str:
    """Win+D — show desktop."""
    pyautogui.hotkey("win", "d")
    return "Minimized all windows"


def launch_app(name: str, args: str = "") -> str:
    """Launch application via PowerShell Start-Process; a non-zero exit (e.g. no such app) raises (#32).

    The name and the arguments are single-quoted literals (#39): ``$(...)`` in either is text.
    """
    cmd = f"Start-Process {ps_literal(name)}"
    if args:
        cmd += f" -ArgumentList {ps_literal(args)}"
    run_command(powershell_argv(cmd), timeout=10, check=True, label="PowerShell")
    return f"Launched {name}"


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize a window by handle."""
    if not HAS_WIN32:
        raise RuntimeError(_NO_PYWIN32)
    rect = win32gui.GetWindowRect(handle)
    win32gui.MoveWindow(handle, rect[0], rect[1], width, height, True)
    return f"Resized {handle} to {width}x{height}"


# ---------------------------------------------------------------------------
# Clipboard — a failure raises (#32); a clipboard without text reads as ""
# ---------------------------------------------------------------------------


def _close_clipboard_after(error: Exception) -> None:
    """Close the clipboard after ``error``; a failed close is logged, ``error`` is what gets raised."""
    try:
        win32clipboard.CloseClipboard()
    except Exception as e:
        log.debug("CloseClipboard after %r failed: %s", error, e)


def get_clipboard() -> str:
    if not HAS_WIN32:
        raise RuntimeError(_NO_PYWIN32)
    try:
        win32clipboard.OpenClipboard()
        # an empty clipboard, or one holding only an image, is "no text", not a failure
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            data = ""
        else:
            data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return data
    except Exception as e:
        _close_clipboard_after(e)
        raise


def set_clipboard(text: str) -> str:
    if not HAS_WIN32:
        raise RuntimeError(_NO_PYWIN32)
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return "Clipboard set"
    except Exception as e:
        _close_clipboard_after(e)
        raise


# ---------------------------------------------------------------------------
# Lock screen
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    if not ctypes.windll.user32.LockWorkStation():
        raise RuntimeError("LockWorkStation failed (no interactive desktop?)")
    return "Screen locked"


# ---------------------------------------------------------------------------
# Toast notification
# ---------------------------------------------------------------------------


# The toast's title and message never enter the script (#39): an expandable here-string ran any
# ``$(...)`` in them, and Notification is a tier-1 tool while Shell is tier 3. They travel in these
# environment variables, and the script sets them as XML text nodes, so any character (``<``, ``&``,
# a quote, a newline) is just text. The variables are set only in the child's environment.
TOAST_TITLE_VAR = "REMOTEOS_TOAST_TITLE"
TOAST_MESSAGE_VAR = "REMOTEOS_TOAST_MESSAGE"

# Builds ``$xml``, the toast document, from the two variables. Kept apart from the line that shows
# it, so tests_windows can run exactly this part in real PowerShell and read the XML back.
TOAST_XML_SCRIPT = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:{TOAST_TITLE_VAR})) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:{TOAST_MESSAGE_VAR})) | Out-Null
"""

_TOAST_SHOW_SCRIPT = """$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("remoteos-mcp").Show($toast)
"""


def toast_environment(title: str, message: str) -> dict[str, str]:
    """The toast child's whole environment: the server's own plus the title and the message."""
    env = os.environ.copy()
    env[TOAST_TITLE_VAR] = title
    env[TOAST_MESSAGE_VAR] = message
    return env


def show_notification(title: str, message: str) -> str:
    """Show a Windows toast notification via PowerShell; a non-zero exit raises (#32).

    The text reaches PowerShell in the environment, never in the script (#39).
    """
    run_command(
        powershell_argv(TOAST_XML_SCRIPT + _TOAST_SHOW_SCRIPT),
        timeout=10,
        check=True,
        label="PowerShell",
        env=toast_environment(title, message),
    )
    return "Notification shown"
