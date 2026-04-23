"""Backward-compatible re-export — delegates to detected platform."""
from remoteos.platform import get_desktop as _get_desktop

_mod = _get_desktop()

HAS_WIN32 = getattr(_mod, "HAS_WIN32", False)
WindowInfo = _mod.WindowInfo
