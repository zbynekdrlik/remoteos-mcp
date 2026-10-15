"""macOS desktop availability (#32): can this process see and drive the user's desktop right now?

``diagnose()`` is re-exported by ``platform.mac.desktop`` as the provider's
diagnosis. Quartz is imported at call time, so the module always uses the
framework currently in ``sys.modules``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from remoteos import errors

log = logging.getLogger("remoteos.platform.mac.availability")

_SCREEN_IS_LOCKED = "CGSSessionScreenIsLocked"  # set in the session dictionary while the lock screen shows


def _quartz():
    """The Quartz framework (pyobjc), or None off macOS."""
    try:
        import Quartz
    except ImportError:
        return None
    return Quartz


def _binary() -> str:
    """The Python binary macOS grants the permissions to (the venv's symlink resolved)."""
    return os.path.realpath(sys.executable)


def diagnose(need: str = "input") -> Optional[str]:
    """Why the desktop cannot be seen (``"capture"``) or driven (``"input"``) now, or None when it can (#32).

    The window-server session first (none, not on the console, locked), then the
    permission this use needs. Without it macOS fails SILENTLY (a wallpaper-only
    screenshot, dropped events).

    - Screen Recording, to capture: ``CGPreflightScreenCaptureAccess``.
    - Posting events, for input: ``CGPreflightPostEventAccess``, the same check the
      event-posting code (``desktop._require_post_event_access``) gates on. It
      is granted under Privacy & Security › Accessibility.

    Only a positive finding is a diagnosis. An API that is missing (an older
    macOS) or a probe that fails means "usable" (fail open).
    """
    try:
        return _diagnose(need)
    except Exception as e:
        log.warning("desktop probe failed (%s: %s): assuming the desktop is usable", type(e).__name__, e)
        return None


def _diagnose(need: str) -> Optional[str]:
    quartz = _quartz()
    if quartz is None:
        return None  # the capture / input functions raise their own "Quartz not available"
    session_info = getattr(quartz, "CGSessionCopyCurrentDictionary", None)
    if session_info is not None:
        info = session_info()
        if info is None:  # NULL outside a window-server (GUI) session
            return errors.MAC_NO_GUI_SESSION
        on_console = getattr(quartz, "kCGSessionOnConsoleKey", "kCGSSessionOnConsoleKey")
        if not info.get(on_console, True):
            return errors.MAC_NOT_ON_CONSOLE
        if info.get(_SCREEN_IS_LOCKED, False):
            return errors.LOCKED
    if need == "capture":
        screen_access = getattr(quartz, "CGPreflightScreenCaptureAccess", None)  # macOS 10.15+
        if screen_access is not None and not screen_access():
            return errors.mac_screen_recording(_binary())
    elif need == "input":
        post_access = getattr(quartz, "CGPreflightPostEventAccess", None)  # macOS 10.15+
        if post_access is not None and not post_access():
            return errors.mac_accessibility(_binary())
    log.debug("desktop probe: macOS session usable (%s)", need)
    return None
