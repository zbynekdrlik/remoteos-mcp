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
from fastmcp.exceptions import ToolError
from PIL import Image

import remoteos.__main__ as server
from remoteos.platform.linux import desktop as d


class _Proc:
    def __init__(self, stdout: bytes = b""):
        self.stdout = stdout
        self.returncode = 0
        self.stderr = b""


def _install_fake_x11(monkeypatch, root, heads, capture_size=None, xrandr_installed=True):
    """Fake X11 world: ``root`` = (W, H) of the X screen, ``heads`` = [(x, y, w, h), ...].

    Mirrors the real tools: ``xrandr --current`` reports the ROOT size, ``xrandr
    --listmonitors`` the heads, and ``xdotool getdisplaygeometry`` only the FIRST
    head (libxdo's Xinerama viewport — the trap a root-size query must avoid). A
    full-screen scrot captures the whole root (``capture_size`` overrides that).
    ``xrandr_installed=False`` makes xrandr fail the way ``_run_x`` reports a
    missing binary.
    """
    monkeypatch.setattr(
        d,
        "_SESSION",
        d.SessionInfo(mode="x11", display=":0", uid=1000, user="newlevel", session_id="1"),
    )
    calls: list[list[str]] = []
    listing = f"Monitors: {len(heads)}\n" + "".join(
        f" {i}: +HEAD-{i} {w}/500x{h}/300+{x}+{y}  HEAD-{i}\n" for i, (x, y, w, h) in enumerate(heads)
    )

    def _fake_run_x(cmd, *, input_bytes=None, timeout=d._TIMEOUT, discard_output=False):
        calls.append(list(cmd))
        if cmd[:1] == ["xrandr"] and not xrandr_installed:
            raise RuntimeError("xrandr not installed: [Errno 2] No such file or directory: 'xrandr'")
        if cmd[:2] == ["xdotool", "getdisplaygeometry"]:
            return _Proc(f"{heads[0][2]} {heads[0][3]}\n".encode())
        if cmd[:2] == ["xrandr", "--current"]:
            return _Proc(
                f"Screen 0: minimum 8 x 8, current {root[0]} x {root[1]}, maximum 32767 x 32767\n"
                f"HEAD-0 connected primary {heads[0][2]}x{heads[0][3]}+0+0\n".encode()
            )
        if cmd[:2] == ["xrandr", "--listmonitors"]:
            return _Proc(listing.encode())
        if cmd[:1] == ["wmctrl"]:
            return _Proc(b"0x0400000a 0 3210 100 50 800 600 host Editor\n")
        if cmd[:1] == ["scrot"]:
            if "-a" in cmd:
                _x, _y, w, h = (int(v) for v in cmd[cmd.index("-a") + 1].split(","))
            else:
                w, h = capture_size or root
            Image.new("RGB", (w, h), (255, 255, 255)).save(Path(cmd[-1]), format="PNG")
            return _Proc()
        return _Proc()

    def _no_atspi():
        raise ImportError("no AT-SPI bus in unit tests")

    def _fake_run_capture(cmd, timeout=10):
        # the desktop tools' preflight (#32) runs no helper command on an X11 session
        raise AssertionError(f"unexpected helper command {cmd}")

    monkeypatch.setattr(d, "_run_x", _fake_run_x)
    monkeypatch.setattr(d, "_run_capture", _fake_run_capture)
    monkeypatch.setattr(d, "_load_atspi", _no_atspi)
    return calls


@pytest.fixture
def fake_x11(monkeypatch):
    """The issue's acceptance case: one 2560x1600 display."""
    return _install_fake_x11(monkeypatch, root=(2560, 1600), heads=[(0, 0, 2560, 1600)])


@pytest.fixture
def dual_x11(monkeypatch):
    """Two heads side by side: 2560x1600 at +0+0 and 1920x1080 at +2560+0 → root 4480x1600."""
    return _install_fake_x11(monkeypatch, root=(4480, 1600), heads=[(0, 0, 2560, 1600), (2560, 0, 1920, 1080)])


def _mousemoves(calls):
    return [c[2:4] for c in calls if c[:2] == ["xdotool", "mousemove"]]


def _text(parts) -> str:
    return "\n".join(p.text for p in parts if getattr(p, "type", None) == "text")


def _image_size(parts) -> tuple[int, int]:
    (img,) = [p for p in parts if getattr(p, "type", None) == "image"]
    return Image.open(io.BytesIO(base64.b64decode(img.data))).size


def _tool_error(call) -> str:
    """The text of the ToolError a failing tool raises (#32: a failure is never a success string)."""
    with pytest.raises(ToolError) as exc:
        call()
    return str(exc.value)


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


def _wheel_clicks(calls):
    return [c[2:] for c in calls if c[:2] == ["xdotool", "click"]]


def test_scroll_direction_and_clicks_at_a_mapped_point(fake_x11):
    out = server.Scroll(4, direction="left", x=300, y=200)
    assert _mousemoves(fake_x11) == [["600", "400"]]
    assert _wheel_clicks(fake_x11) == [["--repeat", "4", "6"]]  # 6 = wheel left
    assert out.startswith("Scrolled 4 clicks left [image (300,200) → screen (600,400)")


def test_scroll_legacy_horizontal_routes_to_the_horizontal_buttons(fake_x11):
    assert server.Scroll(3, horizontal=True) == "Scrolled 3 clicks right"
    assert server.Scroll(-2, horizontal="true") == "Scrolled 2 clicks left"
    assert _wheel_clicks(fake_x11) == [["--repeat", "3", "7"], ["--repeat", "2", "6"]]


def test_scroll_legacy_signed_amount_and_direction_wins(fake_x11):
    server.Scroll(-5)
    server.Scroll(-5, direction="up", horizontal=True)
    assert _wheel_clicks(fake_x11) == [["--repeat", "5", "5"], ["--repeat", "5", "4"]]


def test_scroll_unknown_direction_is_an_error_and_sends_nothing(fake_x11):
    out = _tool_error(lambda: server.Scroll(1, direction="sideways"))
    assert out == "Scroll error: direction must be one of up, down, left, right (got 'sideways')"
    assert _wheel_clicks(fake_x11) == []


def test_type_and_scroll_without_point_do_not_move(fake_x11):
    server.Type("hi")
    server.Scroll(2)
    assert _mousemoves(fake_x11) == []


def test_type_scroll_and_drag_start_reach_column_and_row_zero(fake_x11):
    # x=0 alone / y=0 alone is the frame's left column / top row, never "no point" (#31)
    server.Type("hi", x=0, y=200)
    server.Scroll(-3, x=300, y=0)
    server.Move(640, 400, drag=True, start_x=0, start_y=20)
    assert _mousemoves(fake_x11) == [["0", "400"], ["600", "0"], ["0", "40"], ["1280", "800"]]


def test_drag_without_start_point_drags_from_the_current_position(fake_x11):
    # only 0/0 (the default) means "no start point"
    server.Move(640, 400, drag=True)
    assert _mousemoves(fake_x11) == [["1280", "800"]]
    assert ["xdotool", "mousedown", "1"] in fake_x11


def test_type_paste_method_clicks_the_mapped_point_then_pastes(fake_x11, monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)  # the paste settle wait
    out = server.Type("Žltý", x=300, y=200, method="paste")
    assert _mousemoves(fake_x11) == [["600", "400"]]
    assert ["xdotool", "key", "--clearmodifiers", "ctrl+v"] in fake_x11
    assert not [c for c in fake_x11 if c[:2] == ["xdotool", "type"]]
    # the fake X11 clipboard is empty: there is no text to put back (#30 review round 1)
    assert out.startswith(
        "Pasted 4 chars (the clipboard held no text, so it was not restored) [image (300,200) → screen (600,400)"
    )


def test_type_unknown_method_is_an_error_and_sends_nothing(fake_x11):
    out = _tool_error(lambda: server.Type("x", x=300, y=200, method="clipboard"))
    assert out == "Type error: method must be 'keys' or 'paste' (got 'clipboard')"
    assert [c for c in fake_x11 if c[:1] == ["xdotool"]] == []


def test_click_clamps_to_frame(fake_x11):
    server.Click(5000, 5000)
    assert _mousemoves(fake_x11) == [["2559", "1599"]]


def test_click_coords_screen_passes_through(fake_x11):
    out = server.Click(1280, 800, coords="screen")
    assert _mousemoves(fake_x11) == [["1280", "800"]]
    geometry_queries = [c for c in fake_x11 if c[:1] == ["xrandr"] or c[:2] == ["xdotool", "getdisplaygeometry"]]
    assert geometry_queries == []  # no frame needed for screen coords
    assert "image (" not in out


def test_click_rejects_unknown_coords(fake_x11):
    out = _tool_error(lambda: server.Click(1, 1, coords="pixels"))
    assert "coords must be 'image' or 'screen'" in out
    assert _mousemoves(fake_x11) == []


def test_click_on_second_monitor_frame(dual_x11):
    server.Click(640, 360, monitor=2)  # 1920x1080 at +2560+0 → image 1280x720, scale 1.5
    assert _mousemoves(dual_x11) == [["3520", "540"]]


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


def test_snapshot_second_monitor_reports_origin(dual_x11):
    parts = server.Snapshot(monitor=2)
    assert _image_size(parts) == (1280, 720)
    assert "screen 1920x1080 at origin (2560,0), scale 1.500" in _text(parts)
    assert ["scrot", "--overwrite", "-a", "2560,0,1920,1080"] == [c for c in dual_x11 if c[:1] == ["scrot"]][0][:4]


def test_snapshot_all_monitors_covers_the_whole_x_root(dual_x11):
    # monitor=0 is the X ROOT (both heads), not the first Xinerama head
    parts = server.Snapshot()
    assert _image_size(parts) == (1280, 457)
    assert "Image 1280x457 = screen 4480x1600 at origin (0,0), scale 3.500" in _text(parts)


def test_click_all_monitors_reaches_the_second_head(dual_x11):
    server.Click(1000, 100)  # image x 1000 * 3.5 = 3500 → on the 1920 head at +2560
    assert _mousemoves(dual_x11) == [["3500", "350"]]


def test_capture_size_mismatch_is_logged(monkeypatch, caplog):
    # A capture that does not match the reported geometry is unexpected: it is
    # still fitted to the frame, but never silently.
    _install_fake_x11(monkeypatch, root=(2560, 1600), heads=[(0, 0, 2560, 1600)], capture_size=(2000, 1000))
    with caplog.at_level("WARNING"):
        parts = server.Snapshot()
    assert _image_size(parts) == (1280, 800)
    assert "capture is 2000x1000 but the display geometry says 2560x1600" in caplog.text


def test_matching_capture_logs_no_mismatch_warning(fake_x11, caplog):
    with caplog.at_level("WARNING"):
        parts = server.Snapshot()
    assert _image_size(parts) == (1280, 800)  # the capture really happened
    assert "capture is" not in caplog.text


def test_missing_xrandr_fails_loudly_never_a_wrong_frame(monkeypatch):
    calls = _install_fake_x11(monkeypatch, root=(2560, 1600), heads=[(0, 0, 2560, 1600)], xrandr_installed=False)
    assert _tool_error(server.Snapshot).startswith("Snapshot error: xrandr not installed")
    assert _tool_error(lambda: server.Click(640, 400)).startswith("Click error: xrandr not installed")
    assert _mousemoves(calls) == []  # no click at an unmapped / guessed point


def test_unknown_monitor_index_is_an_error(dual_x11):
    assert _tool_error(lambda: server.Click(1, 1, monitor=3)) == "Click error: Monitor 3 not found (have 2)"
    assert _mousemoves(dual_x11) == []


def test_headless_input_tool_raises_the_exact_provider_stub(monkeypatch):
    # the stub TEXT is the cam1..4 contract and stays byte-for-byte; it now arrives as an error (#32)
    monkeypatch.setattr(d, "_SESSION", d.SessionInfo(mode="headless"))
    stub = "Not available on headless Linux (no display)"
    assert _tool_error(lambda: server.Click(10, 10)) == stub
    assert _tool_error(lambda: server.Move(10, 10)) == stub
    assert _tool_error(lambda: server.Scroll(3, direction="down", x=10, y=10)) == stub
    assert _tool_error(lambda: server.Scroll(-3)) == stub


def test_annotated_snapshot_states_contract(fake_x11):
    parts = server.AnnotatedSnapshot()
    assert _image_size(parts) == (1280, 800)
    assert "Image 1280x800 = screen 2560x1600 at origin (0,0), scale 2.000" in _text(parts)


def test_snapshot_then_click_round_trip(fake_x11):
    # the model reads (50,25) off the Snapshot listing and clicks it → native (100,50)
    server.Snapshot()
    server.Click(50, 25)
    assert _mousemoves(fake_x11) == [["100", "50"]]
