"""X root size parsing for the Linux ``monitor=0`` frame (#28) — pure.

``xdotool getdisplaygeometry`` reports only the FIRST Xinerama head on a
multi-head X server (libxdo's viewport), while a full-screen scrot captures the
whole root; the root size therefore comes from ``xrandr --current``. The samples
below are verbatim first lines from the fleet's X11 boxes (2026-10-14).
"""

from __future__ import annotations

import pytest

from remoteos.platform.linux.display import parse_xrandr_screen_size


@pytest.mark.parametrize(
    "first_line,expected",
    [
        ("Screen 0: minimum 320 x 200, current 1920 x 1200, maximum 16384 x 16384", (1920, 1200)),  # stream-pp
        ("Screen 0: minimum 8 x 8, current 2560 x 1600, maximum 32767 x 32767", (2560, 1600)),  # strih-snv
        ("Screen 0: minimum 8 x 8, current 4480 x 1600, maximum 32767 x 32767", (4480, 1600)),  # two heads
    ],
)
def test_parse_xrandr_screen_size(first_line, expected):
    text = first_line + "\neDP-1 connected primary 2560x1600+0+0 (normal left inverted right) 344mm x 215mm\n"
    assert parse_xrandr_screen_size(text) == expected


@pytest.mark.parametrize("text", ["", "garbage\n", "Screen 0: minimum 8 x 8, current 0 x 1600, maximum 1 x 1\n"])
def test_parse_xrandr_screen_size_rejects_bad_output(text):
    with pytest.raises(ValueError):
        parse_xrandr_screen_size(text)
