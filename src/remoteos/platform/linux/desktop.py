"""Linux desktop provider.

Runtime session detection decides behavior:

* **X11 session** (a graphical seat is active)  → real implementations backed by
  ``xdotool`` / ``scrot`` / ``wmctrl`` / ``xclip`` / ``notify-send`` / ``loginctl``.
* **Wayland session**                           → not yet implemented (separate tier);
  every function raises an honest "unavailable" message and never crashes.
* **Headless** (no graphical session, e.g. cam1..4) → the original stub message,
  byte-for-byte unchanged, so headless boxes see zero regression.

The non-X11 message is RAISED as :class:`remoteos.errors.DesktopUnavailable`
(#32): the tool layer passes its text through verbatim as an MCP error
(``isError: true``) instead of a successful result. Every other failure raises
too (``RuntimeError`` from :func:`_run_x`, ``LookupError`` for a window that is
not there); no function returns error text.

The MCP service may run either as *root* (borrowing the logged-in user's X session)
or as the *session user itself* (the default systemd unit ``User=`` on our targets).
Both are handled: when running as root we re-run each X command via ``sudo -u <user>``
with the session's ``DISPLAY`` / ``XAUTHORITY`` / D-Bus address; when already running
as the session user we simply inject those variables into the environment.

``loginctl show-session`` frequently reports an **empty** ``Display=`` for X11
sessions, so the reliable source of ``DISPLAY`` + ``XAUTHORITY`` is a scan of
``/proc/<pid>/environ`` for a process owned by the session user (e.g. the shell /
window manager). That is what :func:`_scan_proc_for_x_env` does.
"""

from __future__ import annotations

# The provider interface. The code lives by concern: session.py, windows.py, x11_capture.py, x11_input.py (#49).
from remoteos.platform.linux.session import (  # noqa: F401
    SessionInfo,
    _unavailable,
    get_session,
    lock_screen,
    show_notification,
)
from remoteos.platform.linux.windows import (  # noqa: F401
    WindowInfo,
    enumerate_windows,
    focus_window,
    get_interactive_elements,
    launch_app,
    minimize_all,
    resize_window,
)
from remoteos.platform.linux.x11_capture import (  # noqa: F401
    MONITOR_ZERO,
    capture_frame,
    capture_geometry,
    capture_png,
    capture_region,
    list_displays,
    record_screen,
)
from remoteos.platform.linux.x11_input import (  # noqa: F401
    click,
    cursor_position,
    get_clipboard,
    key_down,
    key_up,
    move,
    scroll,
    set_clipboard,
    shortcut,
    type_text,
    type_unicode,
)

HAS_WIN32 = False  # compatibility flag — always False on Linux

# The provider's diagnose() (#32). Imported last: availability reads this module at call time.
from remoteos.platform.linux.availability import diagnose  # noqa: E402,F401
