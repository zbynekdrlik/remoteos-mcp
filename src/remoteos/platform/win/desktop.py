"""Win32 desktop interactions — screenshots, window enumeration, UI elements."""

from __future__ import annotations

import ctypes
import locale
from dataclasses import dataclass
from typing import Optional

from remoteos.platform.win.dpi import enable_per_monitor_dpi_awareness

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
from remoteos.snapshot import encode_jpeg, fit_to_frame  # noqa: E402

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
    return fit_to_frame(img, frame), frame


def take_screenshot(quality: int = 65, max_width: int = 1280, monitor: int = 0) -> str:
    """Capture screen, return base64 JPEG sized to the Snapshot frame.

    Args:
        quality: JPEG quality 1-100.
        max_width: Max width in pixels. 0=no resize (native resolution).
        monitor: 0=all monitors, 1/2/3=specific monitor.
    """
    img, _frame = capture_frame(monitor=monitor, max_width=max_width)
    return encode_jpeg(img, quality)


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Bring a window to the foreground. Fuzzy-match title if provided."""
    if not HAS_WIN32:
        return "Error: pywin32 not installed — run `pip install pywin32`"

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
            return f"No window matching '{title}' (best score {best_score})"

    if not hwnd:
        return "No window found"

    try:
        # Restore if minimized
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return f"Focused window handle={hwnd} title='{win32gui.GetWindowText(hwnd)}'"
    except Exception as e:
        return f"Failed to focus: {e}"


def minimize_all() -> str:
    """Win+D — show desktop."""
    try:
        pyautogui.hotkey("win", "d")
        return "Minimized all windows"
    except Exception as e:
        return f"Failed: {e}"


def launch_app(name: str, args: str = "") -> str:
    """Launch application via PowerShell Start-Process."""
    import subprocess

    try:
        cmd = f'Start-Process "{name}"'
        if args:
            cmd += f' -ArgumentList "{args}"'
        subprocess.run(["powershell", "-Command", cmd], timeout=10, capture_output=True)
        return f"Launched {name}"
    except Exception as e:
        return f"Failed to launch {name}: {e}"


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize a window by handle."""
    if not HAS_WIN32:
        return "Error: pywin32 not installed — run `pip install pywin32`"
    try:
        rect = win32gui.GetWindowRect(handle)
        win32gui.MoveWindow(handle, rect[0], rect[1], width, height, True)
        return f"Resized {handle} to {width}x{height}"
    except Exception as e:
        return f"Failed: {e}"


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def get_clipboard() -> str:
    if not HAS_WIN32:
        return "Error: pywin32 not installed — run `pip install pywin32`"
    try:
        win32clipboard.OpenClipboard()
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return data
    except Exception as e:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return f"Error: {e}"


def set_clipboard(text: str) -> str:
    if not HAS_WIN32:
        return "Error: pywin32 not installed — run `pip install pywin32`"
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return "Clipboard set"
    except Exception as e:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Lock screen
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    try:
        ctypes.windll.user32.LockWorkStation()
        return "Screen locked"
    except Exception as e:
        return f"Failed: {e}"


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
    try:
        subprocess.run(["powershell", "-Command", ps], timeout=10, capture_output=True)
        return "Notification shown"
    except Exception as e:
        return f"Failed: {e}"
