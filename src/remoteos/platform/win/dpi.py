"""Per-monitor-v2 DPI awareness for the Windows server process (#28).

Must run BEFORE ``pyautogui`` is imported: pyautogui's import calls
``SetProcessDPIAware()`` (system-aware), and once a process has an awareness the
per-monitor upgrade is refused (``E_ACCESSDENIED``). A system-aware process gets
DPI-virtualised (scaled) coordinates on every monitor whose DPI differs from the
primary's, while ``ImageGrab`` always captures physical pixels — screenshots,
window rects and ``SetCursorPos`` then disagree. Per-monitor-v2 makes all of them
physical pixels, which the coordinate contract (:mod:`remoteos.coords`) assumes.

ctypes only — importing this module never imports pyautogui or pywin32.
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("remoteos.platform.win.dpi")

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4  # Windows 10 1703+
_PROCESS_PER_MONITOR_DPI_AWARE = 2  # shcore, Windows 8.1+
_S_OK = 0

# Mode applied to THIS process (the real ctypes.windll path only), so the second
# caller (the Windows provider, after remoteos.app) gets the same answer without
# re-calling Win32 — a repeat call would be refused and misreport the mode.
_MODE: str | None = None


_AWARENESS_NAMES = {0: "unaware", 1: "system", 2: "per-monitor"}  # DPI_AWARENESS enum


def _read_back(user32) -> str | None:
    """The process's ACTUAL awareness (Windows 10 1607+), or ``None`` if unknowable."""
    get_context = getattr(user32, "GetThreadDpiAwarenessContext", None)
    get_awareness = getattr(user32, "GetAwarenessFromDpiAwarenessContext", None)
    if get_context is None or get_awareness is None:
        return None
    try:
        # DPI_AWARENESS_CONTEXT is a handle: keep it pointer-sized, never a truncated int.
        get_context.restype = ctypes.c_void_p
        get_awareness.argtypes = [ctypes.c_void_p]
        context = get_context()
        name = _AWARENESS_NAMES.get(get_awareness(context))
        # DPI_AWARENESS reports v1 and v2 both as "per-monitor"; only a context
        # comparison tells a (manifest-)preset v2 apart.
        same = getattr(user32, "AreDpiAwarenessContextsEqual", None)
        if name == "per-monitor" and same is not None:
            same.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            if same(context, _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                return "per-monitor-v2"
        return name
    except OSError as e:
        log.warning("DPI awareness read-back failed: %s", e)
        return None


def _apply(windll) -> str:
    """Try per-monitor-v2 → v1 → system; report the awareness actually in effect.

    Below v2 the result is read back when the OS can tell, because a refused
    call (awareness already set, e.g. by a manifest) or a no-op success says
    nothing about what the process really is.
    """
    requested = _request(windll)
    if requested == "per-monitor-v2":
        return requested
    return _read_back(windll.user32) or requested


def _request(windll) -> str:
    user32 = windll.user32
    set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_context is not None:
        try:
            if set_context(ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                return "per-monitor-v2"
        except OSError as e:
            log.warning("SetProcessDpiAwarenessContext failed: %s", e)
    try:
        if windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE) == _S_OK:
            return "per-monitor"
    except (AttributeError, OSError) as e:  # shcore.dll absent before Windows 8.1
        log.warning("SetProcessDpiAwareness unavailable: %s", e)
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError) as e:
        log.warning("SetProcessDPIAware unavailable: %s", e)
    return "unchanged"


def enable_per_monitor_dpi_awareness(windll=None) -> str:
    """Request per-monitor-v2 DPI awareness, falling back to v1, then system-aware.

    Returns the awareness the process actually has: ``"per-monitor-v2"``,
    ``"per-monitor"``, ``"system"`` or ``"unaware"`` (read back from the OS on
    Windows 10 1607+); ``"unchanged"`` when every call was refused and the OS
    cannot be asked (pre-1607); ``"unavailable"`` when not on Windows.
    Idempotent for the real process. ``windll`` is injectable so the fallback
    chain is testable off Windows; an injected ``windll`` never touches the
    process-wide cache.
    """
    global _MODE
    if windll is not None:
        return _apply(windll)
    if _MODE is not None:
        return _MODE
    real = getattr(ctypes, "windll", None)
    if real is None:
        return "unavailable"
    _MODE = _apply(real)
    if _MODE == "per-monitor-v2":
        log.info("DPI awareness: %s", _MODE)
    else:
        log.warning("DPI awareness: %s (per-monitor-v2 not available) — coordinates may be scaled", _MODE)
    return _MODE
