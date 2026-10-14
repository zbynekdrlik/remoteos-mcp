"""X11 display geometry parsing for the coordinate contract (#28) — pure, no I/O.

The Linux provider runs ``xdotool getdisplaygeometry`` (the X root = what a
full-screen ``scrot`` captures, origin always ``(0,0)``) and ``xrandr
--listmonitors`` (per-monitor bounds inside that root) through its session-aware
``_run_x``; these functions only parse their output.
"""

from __future__ import annotations

import re

# " 1: +HDMI-1 1920/527x1080/296+2560+0  HDMI-1" → width, height, x, y
_MONITOR_RE = re.compile(r"^\s*\d+:\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")


def parse_display_geometry(text: str) -> tuple[int, int]:
    """``"2560 1600"`` (``xdotool getdisplaygeometry``) → ``(2560, 1600)``."""
    parts = text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"unexpected xdotool getdisplaygeometry output: {text!r}")
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"empty X display geometry: {text!r}")
    return width, height


def parse_xrandr_monitors(text: str) -> list[tuple[int, int, int, int]]:
    """``xrandr --listmonitors`` → ``[(x, y, width, height), ...]`` in listing order.

    Entry ``N-1`` is ``monitor=N`` for Snapshot and the input tools.
    """
    monitors = []
    for line in text.splitlines():
        m = _MONITOR_RE.match(line)
        if m:
            width, height, x, y = (int(v) for v in m.groups())
            monitors.append((x, y, width, height))
    return monitors
