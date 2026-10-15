"""Linux desktop availability (#32): can this process see and drive the user's desktop right now?

``diagnose()`` is re-exported by ``platform.linux.desktop`` as the provider's
diagnosis. It reads that module's cached session at call time.

A locked X11 screen is deliberately NOT a diagnosis (review round 1): it is no
silent failure. The screenshot truthfully shows the locker, and xdotool input
reaches it, which is the only remote way to unlock.
"""

from __future__ import annotations

from typing import Optional


def diagnose(capture: bool = False) -> Optional[str]:
    """Why the desktop cannot be seen or driven now, or None when it can (#32).

    No X11 session → the exact headless/Wayland stub (the cam1..4 contract).
    ``capture`` does not matter on Linux. No subprocess, no side effects.
    """
    from remoteos.platform.linux import desktop

    if desktop.get_session().mode != "x11":
        return desktop._unavailable()
    return None
