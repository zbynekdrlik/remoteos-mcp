"""Windows desktop availability (#32): can this process see and drive the user's desktop right now?

Read-only ctypes probes (kernel32, user32, wtsapi32), no pywin32 and no side
effects. ``diagnose()`` and ``reconnect_to_console()`` are re-exported by
``platform.win.desktop`` for the tools.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from typing import Optional

from remoteos import errors
from remoteos.platform.command import run_command

log = logging.getLogger("remoteos.platform.win.availability")

_DESKTOP_READOBJECTS = 0x0001  # enough to read the input desktop's name
_UOI_NAME = 2
_WTS_CONNECT_STATE = 8  # WTS_INFO_CLASS.WTSConnectState → an INT (WTS_CONNECTSTATE_CLASS)
_WTS_SESSION_INFO_EX = 25  # WTS_INFO_CLASS.WTSSessionInfoEx → WTSINFOEXW
_WTS_DISCONNECTED = 4  # WTS_CONNECTSTATE_CLASS.WTSDisconnected
_WTS_SESSIONSTATE_LOCK = 0  # WTSINFOEX_LEVEL1_W.SessionFlags (Windows 7 / 2008 R2 report it inverted)


class _WTSINFOEX_HEAD(ctypes.Structure):
    """The start of WTSINFOEXW (Level 1).

    Its union holds LARGE_INTEGERs, so it is 8-aligned: SessionId is at offset 8,
    SessionState at 12, SessionFlags at 16, on x86 and x64 alike.
    """

    _fields_ = [
        ("Level", ctypes.c_uint32),
        ("_align", ctypes.c_uint32),
        ("SessionId", ctypes.c_uint32),
        ("SessionState", ctypes.c_int32),
        ("SessionFlags", ctypes.c_int32),
    ]


def _session_id() -> int:
    sid = ctypes.c_uint32()
    kernel32 = ctypes.windll.kernel32
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid)):
        raise OSError("ProcessIdToSessionId failed")
    return sid.value


def _object_name(handle: int) -> Optional[str]:
    """The name of a window station or desktop handle (UOI_NAME), or None when unreadable."""
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    needed = ctypes.c_uint32()
    if not user32.GetUserObjectInformationW(
        ctypes.c_void_p(handle), _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
    ):
        return None
    return buf.value


def _input_desktop() -> tuple[bool, Optional[str]]:
    """``(opened, name)`` of the desktop that receives input now.

    A user process cannot open the secure "Winlogon" desktop (lock screen, UAC
    prompt, Ctrl+Alt+Del), so ``opened`` is False exactly when that one is up.
    """
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = ctypes.c_void_p  # HDESK: pointer-sized
    handle = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not handle:
        return False, None
    try:
        return True, _object_name(handle)
    finally:
        user32.CloseDesktop(ctypes.c_void_p(handle))


def _wts_query(session_id: int, info_class: int, ctype):
    """A copy of ``WTSQuerySessionInformationW(info_class)`` as ``ctype``, or None when refused."""
    wtsapi32 = ctypes.windll.wtsapi32
    buf = ctypes.c_void_p()
    size = ctypes.c_uint32()
    if not wtsapi32.WTSQuerySessionInformationW(None, session_id, info_class, ctypes.byref(buf), ctypes.byref(size)):
        return None
    try:
        if not buf.value or size.value < ctypes.sizeof(ctype):
            return None
        return ctype.from_buffer_copy(ctypes.string_at(buf.value, ctypes.sizeof(ctype)))
    finally:
        wtsapi32.WTSFreeMemory(buf)


def diagnose(need: str = "input") -> Optional[str]:
    """Why the desktop cannot be seen or driven now, or None when it can (#32).

    Cheap, read-only probes with no side effects, in this order: session 0, a
    non-interactive window station, a disconnected session, then the input
    desktop (the secure desktop = locked screen, UAC prompt or Ctrl+Alt+Del).
    ``need``:
    - ``"capture"`` and ``"input"``: every check, because each of these blocks
      screenshots and input alike.
    - ``"windows"`` (the window list, Snapshot without vision) and
      ``"accessibility"`` (the element tree): only session 0 and the window
      station. A disconnected or locked session still lists the user's windows
      (live on win-pz-server); outside the user's desktop the list would be a
      hollow, empty success.

    Only a positive finding is a diagnosis. A probe that fails is logged and the
    desktop is taken as usable (fail open), never blocked.
    """
    try:
        return _diagnose(need)
    except Exception as e:
        log.warning("desktop probe failed (%s: %s): assuming the desktop is usable", type(e).__name__, e)
        return None


def _diagnose(need: str) -> Optional[str]:
    session = _session_id()
    if session == 0:
        return errors.SESSION_ZERO
    user32 = ctypes.windll.user32
    user32.GetProcessWindowStation.restype = ctypes.c_void_p  # HWINSTA, not to be closed
    station = _object_name(user32.GetProcessWindowStation())
    if station is not None and station.lower() != "winsta0":
        return errors.non_interactive_window_station(station)
    if need in ("windows", "accessibility"):  # UI Automation reads a locked desktop, as the window list does
        return None
    if _disconnected(session):
        return errors.DISCONNECTED
    opened, name = _input_desktop()
    log.debug("desktop probe: session %s, input desktop %s", session, name if opened else "(cannot open)")
    if opened and (name is None or name.lower() == "default"):
        return None
    info = _wts_query(session, _WTS_SESSION_INFO_EX, _WTSINFOEX_HEAD)
    if info is not None and info.Level == 1 and info.SessionFlags == _WTS_SESSIONSTATE_LOCK:
        return errors.LOCKED
    return errors.secure_desktop(name)


def _disconnected(session: int) -> bool:
    """WTSConnectState of ``session`` is WTSDisconnected (an unreadable state is "connected")."""
    state = _wts_query(session, _WTS_CONNECT_STATE, ctypes.c_int32)
    return state is not None and state.value == _WTS_DISCONNECTED


def reconnect_to_console(force: bool = False) -> str:
    """Attach THIS server's own session to the console (``tscon <id> /dest:console``).

    Returns ``"reattached"`` when tscon ran and the session is attached now, or
    ``"attached"`` when it already was (console or remote desktop) and nothing
    ran. Raises RuntimeError when the state cannot be read or the session is
    still disconnected after tscon (#32 review rounds 1-2).

    Only the ReconnectSession tool and the capture retry of Snapshot and
    AnnotatedSnapshot call it, never a preflight: it puts the user's desktop on
    the machine's physical screen.
    The session is the server's own (ProcessIdToSessionId), never one parsed
    out of ``query session``, whose line for a disconnected session has a blank
    SESSIONNAME.
    """
    try:
        session = _session_id()
        disconnected = _disconnected(session)
    except Exception as e:
        raise RuntimeError(f"cannot read the session state: {type(e).__name__}: {e}") from e
    if not disconnected and not force:
        log.debug("session %s is attached: nothing to reconnect", session)
        return "attached"
    log.warning("attaching session %s to the console (tscon)", session)
    try:
        run_command(["tscon", str(session), "/dest:console"], timeout=10, check=True, label="tscon")
    except subprocess.TimeoutExpired:
        raise RuntimeError("tscon timed out") from None
    time.sleep(1)  # the session needs a moment to attach
    if _disconnected(session):
        raise RuntimeError(f"session {session} is still disconnected after tscon")
    log.info("session %s attached to the console", session)
    return "reattached"
