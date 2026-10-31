"""X11 display geometry for the coordinate contract (#28).

``monitor=0`` is the whole X ROOT — exactly what a full-screen ``scrot``
captures, origin always ``(0,0)``. Its size comes from ``xrandr --current``
(``current W x H``), NOT ``xdotool getdisplaygeometry``: libxdo answers that from
``XineramaQueryScreens()[0]`` whenever Xinerama is active (always, via RandR, on
Xorg), i.e. only the FIRST head of a multi-head box. ``monitor=N`` is entry
``N-1`` of ``xrandr --listmonitors``.

The provider passes its session-aware runner in (``run(argv) -> stdout``), so
this module stays free of session handling and is testable with a fake runner.
"""

from __future__ import annotations

import re
from typing import Callable, NamedTuple

from remoteos.coords import CaptureGeometry
from remoteos.displays import DisplayInfo

# "Screen 0: minimum 8 x 8, current 2560 x 1600, maximum 32767 x 32767"
_SCREEN_RE = re.compile(r"\bcurrent\s+(\d+)\s*x\s*(\d+)")
# " 0: +*HDMI-1 1920/527x1080/296+2560+0  HDMI-1" → primary marker, name, width, height, x, y
_MONITOR_RE = re.compile(r"^\s*\d+:\s+\+?(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")


class XrandrMonitor(NamedTuple):
    x: int
    y: int
    width: int
    height: int
    name: str
    primary: bool


def parse_xrandr_screen_size(text: str) -> tuple[int, int]:
    """X root size from ``xrandr --current`` output (its ``current W x H``)."""
    m = _SCREEN_RE.search(text)
    if not m:
        raise ValueError(f"no 'current W x H' in xrandr output: {text[:120]!r}")
    width, height = int(m.group(1)), int(m.group(2))
    if width <= 0 or height <= 0:
        raise ValueError(f"empty X screen size {width}x{height}")
    return width, height


def parse_xrandr_monitors(text: str) -> list[XrandrMonitor]:
    """``xrandr --listmonitors`` → one :class:`XrandrMonitor` per line, in listing order.

    Entry ``N-1`` is ``monitor=N`` for Snapshot and the input tools.
    """
    monitors = []
    for line in text.splitlines():
        m = _MONITOR_RE.match(line)
        if m:
            primary, name = m.group(1), m.group(2)
            width, height, x, y = (int(v) for v in m.groups()[2:])
            monitors.append(XrandrMonitor(x, y, width, height, name, bool(primary)))
    return monitors


def query_geometry(monitor: int, run: Callable[[list[str]], str]) -> CaptureGeometry:
    """Capture geometry of ``monitor`` (0 = X root, N = xrandr monitor N).

    X11 input (xdotool) and capture (scrot) share one pixel space, so the
    capture size equals the bounds. Raises ``IndexError`` for an unknown monitor;
    a missing/failed ``xrandr`` surfaces as the runner's ``RuntimeError``.
    """
    if monitor <= 0:
        width, height = parse_xrandr_screen_size(run(["xrandr", "--current"]))
        return CaptureGeometry(x=0, y=0, width=width, height=height, pixel_width=width, pixel_height=height)
    monitors = parse_xrandr_monitors(run(["xrandr", "--listmonitors"]))
    if monitor > len(monitors):
        raise IndexError(f"Monitor {monitor} not found (have {len(monitors)})")
    m = monitors[monitor - 1]
    return CaptureGeometry(x=m.x, y=m.y, width=m.width, height=m.height, pixel_width=m.width, pixel_height=m.height)


def query_displays(run: Callable[[list[str]], str]) -> list[DisplayInfo]:
    """The ``monitor=N`` displays. X11 has no per-monitor scale, so ``scale`` is ``None``."""
    return [
        DisplayInfo(index, m.x, m.y, m.width, m.height, None, m.primary, m.name)
        for index, m in enumerate(parse_xrandr_monitors(run(["xrandr", "--listmonitors"])), start=1)
    ]
