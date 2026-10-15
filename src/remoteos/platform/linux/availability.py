"""Linux desktop availability (#32): can this process see and drive the user's desktop right now?

``diagnose()`` is re-exported by ``platform.linux.desktop`` as the provider's
diagnosis. It reads that module (the cached session, the runner) at call time.
"""

from __future__ import annotations

import logging
from typing import Optional

from remoteos.errors import LOCKED

log = logging.getLogger("remoteos.platform.linux.availability")


def diagnose(capture: bool = False) -> Optional[str]:
    """Why the desktop cannot be seen or driven now, or None when it can (#32).

    No X11 session → the exact headless/Wayland stub (the cam1..4 contract). A
    locked X11 session (logind ``LockedHint``, set by the screen locker) → locked.
    ``capture`` does not matter on Linux. A lock state that cannot be read means
    "unlocked" (fail open).
    """
    from remoteos.platform.linux import desktop

    s = desktop.get_session()
    if s.mode != "x11":
        return desktop._unavailable()
    if s.session_id and _session_locked(s.session_id):
        return LOCKED
    return None


def _session_locked(session_id: str) -> bool:
    from remoteos.platform.linux import desktop

    cmd = ["loginctl", "show-session", session_id, "-p", "LockedHint", "--value"]
    rc, out, err = desktop._run_capture(cmd, timeout=5)
    if rc != 0:
        log.debug("LockedHint of session %s unreadable (rc=%s: %s): assuming unlocked", session_id, rc, err.strip())
        return False
    log.debug("LockedHint of session %s: %s", session_id, out.strip())
    return out.strip().lower() == "yes"
