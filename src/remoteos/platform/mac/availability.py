"""macOS desktop availability (#32): can this process see and drive the user's desktop right now?

``diagnose()`` is re-exported by ``platform.mac.desktop`` as the provider's
diagnosis. Quartz is imported at call time, so the module always uses the
framework currently in ``sys.modules``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Optional

from remoteos import errors

log = logging.getLogger("remoteos.platform.mac.availability")


def _quartz():
    """The Quartz framework (pyobjc), or None off macOS."""
    try:
        import Quartz
    except ImportError:
        return None
    return Quartz


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
    Quartz = _quartz()
    if Quartz is None:
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
