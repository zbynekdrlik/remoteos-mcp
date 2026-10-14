"""Tool-level coordinate contract (#28): Snapshot image pixels in, native screen point out.

The real MCP tool functions (``Snapshot`` / ``Click`` / ``Move`` / ``Scroll`` /
``Type`` / ``AnnotatedSnapshot``) run end-to-end through the real Linux provider
on a FAKE 2560x1600 X11 display. Only the external process boundary is stubbed —
``_run_x`` (xdotool / scrot / wmctrl / xrandr subprocesses) and the AT-SPI
library loader — exactly like ``tests/test_linux_desktop.py``. The acceptance
case from the issue: a 2560x1600 desktop at max_width 1280 → a Click at an
IMAGE point hits the matching NATIVE point.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

import remoteos.__main__ as server
from remoteos.platform.linux import desktop as d


class _Proc:
    def __init__(self, stdout: bytes = b""):
        self.stdout = stdout
        self.returncode = 0
        self.stderr = b""


_XRANDR = (
    b"Monitors: 2\n"
    b" 0: +*eDP-1 2560/344x1600/215+0+0  eDP-1\n"
    b" 1: +HDMI-1 1920/527x1080/296+2560+0  HDMI-1\n"
)


@pytest.fixture
def fake_x11(monkeypatch):
    """A 2560x1600 X11 root with a second 1920x1080 monitor at +2560+0."""
    monkeypatch.setattr(
        d,
        "_SESSION",
        d.SessionInfo(mode="x11", display=":0", uid=1000, user="newlevel", session_id="1"),
    )
    calls: list[list[str]] = []

    def _fake_run_x(cmd, *, input_bytes=None, timeout=d._TIMEOUT, discard_output=False):
        calls.append(list(cmd))
        if cmd[:2] == ["xdotool", "getdisplaygeometry"]:
            return _Proc(b"2560 1600\n")
        if cmd[:2] == ["xrandr", "--listmonitors"]:
            return _Proc(_XRANDR)
        if cmd[:1] == ["wmctrl"]:
            return _Proc(b"0x0400000a 0 3210 100 50 800 600 host Editor\n")
        if cmd[:1] == ["scrot"]:
            if "-a" in cmd:
                _x, _y, w, h = (int(v) for v in cmd[cmd.index("-a") + 1].split(","))
            else:
                w, h = 2560, 1600
            Image.new("RGB", (w, h), (255, 255, 255)).save(Path(cmd[-1]), format="PNG")
            return _Proc()
        return _Proc()

    def _no_atspi():
        raise ImportError("no AT-SPI bus in unit tests")

    monkeypatch.setattr(d, "_run_x", _fake_run_x)
    monkeypatch.setattr(d, "_load_atspi", _no_atspi)
    return calls


def _mousemoves(calls):
    return [c[2:4] for c in calls if c[:2] == ["xdotool", "mousemove"]]


def _text(parts) -> str:
    return "\n".join(p.text for p in parts if getattr(p, "type", None) == "text")


def _image_size(parts) -> tuple[int, int]:
    (img,) = [p for p in parts if getattr(p, "type", None) == "image"]
    return Image.open(io.BytesIO(base64.b64decode(img.data))).size


# ---------------------------------------------------------------------------
# input tools take IMAGE pixels by default
# ---------------------------------------------------------------------------


def test_click_image_point_hits_native_point(fake_x11):
    out = server.Click(640, 400)
    assert _mousemoves(fake_x11) == [["1280", "800"]]
    assert "screen (1280,800)" in out


def test_click_hover_and_double_are_mapped(fake_x11):
    server.Click(100, 50, action="hover")
    server.Click(100, 50, action="double")
    assert _mousemoves(fake_x11) == [["200", "100"], ["200", "100"]]


def test_move_and_drag_start_are_mapped(fake_x11):
    server.Move(640, 400)
    server.Move(640, 400, drag=True, start_x=10, start_y=20)
    assert _mousemoves(fake_x11) == [["1280", "800"], ["20", "40"], ["1280", "800"]]


def test_scroll_and_type_points_are_mapped(fake_x11):
    server.Scroll(-3, x=300, y=200)
    server.Type("hi", x=300, y=200)
    assert _mousemoves(fake_x11) == [["600", "400"], ["600", "400"]]


def test_type_and_scroll_without_point_do_not_move(fake_x11):
    server.Type("hi")
    server.Scroll(2)
    assert _mousemoves(fake_x11) == []


def test_click_clamps_to_frame(fake_x11):
    server.Click(5000, 5000)
    assert _mousemoves(fake_x11) == [["2559", "1599"]]


def test_click_coords_screen_passes_through(fake_x11):
    out = server.Click(1280, 800, coords="screen")
    assert _mousemoves(fake_x11) == [["1280", "800"]]
    assert ["xdotool", "getdisplaygeometry"] not in fake_x11  # no frame needed for screen coords
    assert "image (" not in out


def test_click_rejects_unknown_coords(fake_x11):
    out = server.Click(1, 1, coords="pixels")
    assert "coords must be 'image' or 'screen'" in out
    assert _mousemoves(fake_x11) == []


def test_click_on_second_monitor_frame(fake_x11):
    server.Click(640, 360, monitor=2)  # 1920x1080 at +2560+0 → image 1280x720, scale 1.5
    assert _mousemoves(fake_x11) == [["3520", "540"]]


def test_click_native_frame_max_width_0(fake_x11):
    server.Click(640, 400, max_width=0)
    assert _mousemoves(fake_x11) == [["640", "400"]]


# ---------------------------------------------------------------------------
# Snapshot / AnnotatedSnapshot state the contract and list IMAGE pixels
# ---------------------------------------------------------------------------


def test_snapshot_states_contract_and_lists_image_pixels(fake_x11):
    parts = server.Snapshot()
    assert _image_size(parts) == (1280, 800)
    text = _text(parts)
    assert (
        "Image 1280x800 = screen 2560x1600 at origin (0,0), scale 2.000; "
        "input tools take IMAGE pixels of this frame (monitor=0, max_width=1280)"
    ) in text
    # wmctrl window at native 100,50 800x600 → image 50,25 400x300
    assert "[67108874] Editor (400x300 at 50,25)" in text


def test_snapshot_without_vision_still_states_contract(fake_x11):
    parts = server.Snapshot(use_vision=False)
    assert not [p for p in parts if getattr(p, "type", None) == "image"]
    assert "scale 2.000" in _text(parts)


def test_snapshot_second_monitor_reports_origin(fake_x11):
    parts = server.Snapshot(monitor=2)
    assert _image_size(parts) == (1280, 720)
    assert "screen 1920x1080 at origin (2560,0), scale 1.500" in _text(parts)
    assert ["scrot", "--overwrite", "-a", "2560,0,1920,1080"] == [c for c in fake_x11 if c[:1] == ["scrot"]][0][:4]


def test_annotated_snapshot_states_contract(fake_x11):
    parts = server.AnnotatedSnapshot()
    assert _image_size(parts) == (1280, 800)
    assert "Image 1280x800 = screen 2560x1600 at origin (0,0), scale 2.000" in _text(parts)


def test_snapshot_then_click_round_trip(fake_x11):
    # the model reads (50,25) off the Snapshot listing and clicks it → native (100,50)
    server.Snapshot()
    server.Click(50, 25)
    assert _mousemoves(fake_x11) == [["100", "50"]]
