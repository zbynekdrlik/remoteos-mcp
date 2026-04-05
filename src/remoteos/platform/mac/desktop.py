"""macOS desktop interactions — screenshots, window enumeration, UI elements."""

from __future__ import annotations

import base64
import io
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

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


def _get_monitor_bbox(monitor: int) -> tuple[int, int, int, int] | None:
    """Get bounding box for a specific monitor (1-indexed). Returns None for all monitors."""
    if monitor <= 0:
        return None
    if not HAS_QUARTZ:
        return None
    try:
        max_displays = 16
        (err, display_ids, count) = Quartz.CGGetActiveDisplayList(max_displays, None, None)
        if err != 0 or monitor > count:
            raise IndexError(f"Monitor {monitor} not found (have {count})")
        display_id = display_ids[monitor - 1]
        bounds = Quartz.CGDisplayBounds(display_id)
        x = int(bounds.origin.x)
        y = int(bounds.origin.y)
        w = int(bounds.size.width)
        h = int(bounds.size.height)
        return (x, y, x + w, y + h)
    except Exception:
        raise


def take_screenshot(quality: int = 65, max_width: int = 1280, monitor: int = 0) -> str:
    """Capture screen, return base64 JPEG. Resizes if wider than max_width.

    Args:
        quality: JPEG quality 1-100.
        max_width: Max width in pixels. 0=no resize (native resolution).
        monitor: 0=all monitors, 1/2/3=specific monitor.
    """
    if Image is None:
        raise RuntimeError("Pillow not installed — run `pip install Pillow`")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    # screencapture -x = no sound, captures full screen
    cmd = ["screencapture", "-x"]
    if monitor > 0:
        # -D flag selects display (1-indexed)
        cmd.extend(["-D", str(monitor)])
    cmd.append(tmp_path)

    try:
        subprocess.run(cmd, timeout=10, capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("screencapture not found — requires macOS")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"screencapture failed: {e.stderr.decode().strip()}")

    try:
        img = Image.open(tmp_path)
        # Resize if needed
        if max_width > 0 and img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), resample=Image.LANCZOS)
        # Convert to JPEG
        if img.mode in ("RGBA", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    finally:
        import os

        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Bring a window to the foreground via AppleScript."""
    if not title and not handle:
        return "No window specified"

    if handle:
        # Try to find the window owner by handle from enumeration
        try:
            for w in enumerate_windows():
                if w.handle == handle:
                    # Extract the app name (before " — ")
                    app_name = w.title.split(" — ")[0] if " — " in w.title else w.title
                    title = app_name
                    break
            else:
                return f"No window with handle {handle}"
        except Exception as e:
            return f"Failed to look up handle {handle}: {e}"

    if not title:
        return "No window found"

    # Escape double quotes in title for AppleScript
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
    return "No process matching \\"{safe_title}\\""
end tell
'''
    try:
        return _run_osascript(script)
    except Exception as e:
        return f"Failed to focus: {e}"


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
    try:
        _run_osascript(script)
        return "Minimized all windows"
    except Exception as e:
        return f"Failed: {e}"


def launch_app(name: str, args: str = "") -> str:
    """Launch application via 'open -a'."""
    try:
        cmd = ["open", "-a", name]
        if args:
            cmd.append("--args")
            cmd.extend(args.split())
        subprocess.run(cmd, timeout=10, capture_output=True, check=True)
        return f"Launched {name}"
    except subprocess.CalledProcessError as e:
        return f"Failed to launch {name}: {e.stderr.decode().strip()}"
    except Exception as e:
        return f"Failed to launch {name}: {e}"


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize the front window of the app owning the given window handle."""
    # Find the app name from the handle
    app_name = None
    try:
        for w in enumerate_windows():
            if w.handle == handle:
                app_name = w.title.split(" — ")[0] if " — " in w.title else w.title
                break
    except Exception:
        pass

    if not app_name:
        return f"No window with handle {handle}"

    safe_name = app_name.replace('"', '\\"')
    script = f'''
tell application "System Events"
    tell process "{safe_name}"
        try
            set size of front window to {{{width}, {height}}}
            return "Resized {safe_name} to {width}x{height}"
        on error errMsg
            return "Failed: " & errMsg
        end try
    end tell
end tell
'''
    try:
        return _run_osascript(script)
    except Exception as e:
        return f"Failed: {e}"


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def get_clipboard() -> str:
    """Get clipboard contents via pbpaste."""
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        return result.stdout
    except FileNotFoundError:
        return "Error: pbpaste not found — requires macOS"
    except Exception as e:
        return f"Error: {e}"


def set_clipboard(text: str) -> str:
    """Set clipboard contents via pbcopy."""
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            timeout=5,
            check=True,
        )
        return "Clipboard set"
    except FileNotFoundError:
        return "Error: pbcopy not found — requires macOS"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Lock screen
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    """Put display to sleep (locks screen if password required on wake)."""
    try:
        subprocess.run(["pmset", "displaysleepnow"], timeout=5, check=True)
        return "Screen locked"
    except Exception as e:
        return f"Failed: {e}"


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def show_notification(title: str, message: str) -> str:
    """Show a macOS notification via osascript."""
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        _run_osascript(script)
        return "Notification shown"
    except Exception as e:
        return f"Failed: {e}"
