"""Win32 desktop interactions — screenshots, window enumeration, UI elements."""

from __future__ import annotations

import ctypes
import locale
import logging
import time
from dataclasses import dataclass
from typing import Optional

from remoteos.platform.win.dpi import enable_per_monitor_dpi_awareness

log = logging.getLogger("remoteos.platform.win.desktop")

# Per-monitor-v2 DPI awareness MUST be in effect before pyautogui is imported:
# pyautogui's import calls SetProcessDPIAware() (system-aware), after which the
# process can no longer become per-monitor aware (#28). __main__ already did this
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

from remoteos.coords import CaptureGeometry, frame_for  # noqa: E402
from remoteos.platform.win.availability import diagnose, reconnect_to_console  # noqa: E402,F401  (#32)
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


def _get_system_language() -> str:
    """Return current Windows display language."""
    try:
        return locale.getdefaultlocale()[0] or "en_US"
    except Exception:
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
        # all_screens=True: PIL crops the bbox out of the whole virtual screen. Without
        # it the grab is primary-only and a non-primary monitor's bbox comes back black.
        bbox = (geometry.x, geometry.y, geometry.x + geometry.width, geometry.y + geometry.height)
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
    return fit_to_frame(img, frame, geometry), frame


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
    """Launch application via PowerShell Start-Process."""
    import subprocess

    cmd = f'Start-Process "{name}"'
    if args:
        cmd += f' -ArgumentList "{args}"'
    _powershell_ok(subprocess.run(["powershell", "-Command", cmd], timeout=10, capture_output=True, text=True))
    return f"Launched {name}"


def _powershell_ok(result) -> None:
    """Raise when a PowerShell call failed: a non-zero exit is a failure, never a success (#32)."""
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or f"PowerShell exited {result.returncode}")


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize a window by handle."""
    if not HAS_WIN32:
        raise RuntimeError(_NO_PYWIN32)
    rect = win32gui.GetWindowRect(handle)
    win32gui.MoveWindow(handle, rect[0], rect[1], width, height, True)
    return f"Resized {handle} to {width}x{height}"


# ---------------------------------------------------------------------------
# Clipboard — a failure raises (#32); a clipboard without text raises too
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


def show_notification(title: str, message: str) -> str:
    """Show a Windows toast notification via PowerShell."""
    import subprocess

    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast>
  <visual><binding template="ToastGeneric">
    <text>{title}</text>
    <text>{message}</text>
  </binding></visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("remoteos-mcp").Show($toast)
"""
    _powershell_ok(subprocess.run(["powershell", "-Command", ps], timeout=10, capture_output=True, text=True))
    return "Notification shown"
