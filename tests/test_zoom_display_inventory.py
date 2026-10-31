"""Zoom and DisplayInventory (#33): the real tools through the real providers.

Linux runs on the fake X11 display of ``test_coordinate_contract_tools`` (only ``_run_x`` is
stubbed), Windows and macOS on the fake OS APIs of ``test_provider_geometry``, so the argv / bbox
each OS really gets is what these tests read. A provider's ``capture_region`` is only reached
through Zoom here on purpose: its image must also pass ``fit_to_frame``.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import sys
import types
from pathlib import Path

import pytest
import test_coordinate_contract_tools as linux_contract
import test_provider_geometry as provider_geometry
from fastmcp.exceptions import ToolError
from PIL import Image
from test_coordinate_contract_tools import _install_fake_x11, _mousemoves, _text
from test_provider_geometry import _flaky_grab
from tool_surface import server
from tool_test_support import tool_error

from remoteos.coords import CaptureGeometry
from remoteos.displays import DisplayInfo, inventory_text
from remoteos.platform.linux import session as linux_session
from remoteos.platform.linux import x11_capture
from remoteos.platform.linux.display import parse_xrandr_monitors

# The fixtures of the two modules these tests extend.
fake_x11, dual_x11 = linux_contract.fake_x11, linux_contract.dual_x11
win, mac = provider_geometry.win, provider_geometry.mac
pyautogui_branch, mac_tool_layer = provider_geometry.pyautogui_branch, provider_geometry.mac_tool_layer


def _image(parts) -> Image.Image:
    (img,) = [p for p in parts if getattr(p, "type", None) == "image"]
    assert img.mime_type == "image/jpeg"
    return Image.open(io.BytesIO(base64.b64decode(img.data)))


def _scrots(calls) -> list[str]:
    return [c[c.index("-a") + 1] for c in calls if c[:1] == ["scrot"] and "-a" in c]


# ---------------------------------------------------------------------------
# Zoom on Linux
# ---------------------------------------------------------------------------


def test_zoom_maps_image_pixels_and_captures_native(fake_x11):
    parts = server.Zoom(100, 50, 320, 200)  # 2560x1600 at max_width 1280: scale 2
    assert _scrots(fake_x11) == ["200,100,640,400"]
    assert _image(parts).size == (640, 400)
    assert "Zoom image 640x400 = screen 640x400 at origin (200,100), scale 1.000" in _text(parts)


def test_zoom_maps_through_the_frame_of_the_given_max_width(fake_x11):
    parts = server.Zoom(10, 10, 50, 25, max_width=640)  # parent frame at scale 4
    assert _scrots(fake_x11) == ["40,40,200,100"]
    assert _image(parts).size == (200, 100)


def test_zoom_downscales_only_above_max_width(fake_x11):
    parts = server.Zoom(0, 0, 1280, 800)
    assert _scrots(fake_x11) == ["0,0,2560,1600"]
    assert _image(parts).size == (1280, 800)
    assert "scale 2.000" in _text(parts)


def test_zoom_native_wide_region_with_max_width_zero(fake_x11):
    parts = server.Zoom(0, 0, 2000, 100, max_width=0, coords="screen")
    assert _image(parts).size == (2000, 100)


def test_zoom_screen_coordinates_bypass_the_frame_mapping(fake_x11):
    server.Zoom(7, 9, 110, 130, coords="screen")
    assert _scrots(fake_x11) == ["7,9,110,130"]


def test_zoom_on_a_monitor_keeps_the_monitors_origin(dual_x11):
    parts = server.Zoom(100, 100, 200, 100, monitor=2)  # 1920x1080 at +2560+0, scale 1.5
    assert _scrots(dual_x11) == ["2710,150,300,150"]
    assert "at origin (2710,150)" in _text(parts)


def test_zoom_cuts_the_region_to_the_frame_and_reports_what_it_captured(fake_x11):
    parts = server.Zoom(1200, 700, 400, 400)
    assert _scrots(fake_x11) == ["2400,1400,160,200"]
    assert "screen 160x200 at origin (2400,1400)" in _text(parts)


def test_a_point_converted_with_the_zoom_reply_clicks_that_screen_point(fake_x11):
    server.Zoom(100, 50, 320, 200)
    # image point (10,20) of the zoom: screen = origin (200,100) + 1.0 * point
    server.Click(210, 120, coords="screen")
    assert _mousemoves(fake_x11) == [["210", "120"]]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((0, 0, 0, 10), "Zoom error: region size must be positive, got 0x10"),
        ((5000, 0, 10, 10), "Zoom error: region 10x10 at (5000,0) is outside the frame"),
    ],
)
def test_a_bad_rectangle_is_an_error_and_captures_nothing(fake_x11, args, message):
    assert tool_error(lambda: server.Zoom(*args)) == message
    assert _scrots(fake_x11) == []


def test_a_bad_coords_value_is_an_error_before_any_query(fake_x11):
    message = tool_error(lambda: server.Zoom(0, 0, 10, 10, coords="pixels"))
    assert message == "Zoom error: coords must be 'image' or 'screen', got 'pixels'"
    assert fake_x11 == []


def test_an_unknown_monitor_is_an_error(fake_x11):
    assert tool_error(lambda: server.Zoom(0, 0, 10, 10, monitor=5)) == "Zoom error: Monitor 5 not found (have 1)"


def test_zoom_encodes_above_snapshots_quality_for_small_text(monkeypatch, fake_x11):
    qualities = []
    real = server.encode_jpeg

    def _encode(img, quality):
        qualities.append(quality)
        return real(img, quality)

    monkeypatch.setattr(server, "encode_jpeg", _encode)
    server.Zoom(100, 50, 320, 200)
    server.Snapshot()
    assert qualities == [85, 65]


def test_zoom_warns_when_the_grabber_disagrees_with_the_geometry(monkeypatch, fake_x11, caplog):
    def _small_png(bbox):
        buf = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr(x11_capture, "capture_png", _small_png)
    with caplog.at_level(logging.WARNING, logger="remoteos.snapshot"):
        parts = server.Zoom(100, 50, 320, 200)
    assert _image(parts).size == (640, 400)
    assert "capture is 10x10 but the display geometry says 640x400" in caplog.text


# ---------------------------------------------------------------------------
# Zoom on Windows and macOS
# ---------------------------------------------------------------------------


def test_windows_zoom_grabs_the_bbox_from_all_screens(win, pyautogui_branch):
    _, grabs = win
    parts = server.Zoom(100, 100, 200, 100)  # virtual screen (-1920,0) 4480x1600, scale 3.5
    assert grabs == [{"bbox": (-1570, 350, -870, 701), "all_screens": True}]
    assert _image(parts).size == (700, 351)
    assert "at origin (-1570,350)" in _text(parts)


def test_windows_zoom_retries_a_failed_grab_like_snapshot(monkeypatch, win, pyautogui_branch):
    monkeypatch.setattr(server, "is_windows", lambda: True)
    monkeypatch.setattr(server, "_ensure_session_connected", lambda: "reattached")
    _flaky_grab(monkeypatch, failures=1)
    assert _image(server.Zoom(100, 100, 200, 100)).size == (700, 351)


def test_mac_zoom_uses_points_and_returns_retina_pixels(monkeypatch, mac, mac_tool_layer):
    mod, commands = mac

    def _run(cmd, **kwargs):
        commands.append(list(cmd))
        _x, _y, w, h = (int(v) for v in cmd[cmd.index("-R") + 1].split(","))
        Image.new("RGB", (w * 2, h * 2)).save(Path(cmd[-1]), format="PNG")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    parts = server.Zoom(100, 100, 200, 100)  # main display 1440x900 pt at scale 1.125
    assert commands[0][:4] == ["screencapture", "-x", "-R", "112,112,226,113"]
    assert _image(parts).size == (452, 226)
    assert "screen 226x113 at origin (112,112), scale 0.500" in _text(parts)


# ---------------------------------------------------------------------------
# DisplayInventory
# ---------------------------------------------------------------------------


def test_parse_xrandr_keeps_the_name_and_the_primary_marker():
    text = "Monitors: 2\n 0: +*HDMI-1 2560/597x1600/336+0+0  HDMI-1\n 1: +DP-2 1920/527x1080/296+2560+0  DP-2\n"
    first, second = parse_xrandr_monitors(text)
    assert (first.name, first.primary) == ("HDMI-1", True)
    assert (second.name, second.primary) == ("DP-2", False)
    assert second[:4] == (2560, 0, 1920, 1080)


def test_parse_xrandr_reads_a_user_defined_monitor_without_the_plus():
    (monitor,) = parse_xrandr_monitors("Monitors: 1\n 0: *LEFT 1280/300x1024/200+-1280+0  LEFT\n")
    assert monitor == (-1280, 0, 1280, 1024, "LEFT", True)


def test_parse_xrandr_reads_negative_offsets_and_any_atom_name():
    text = "Monitors: 2\n 0: +*DP_1 1920/527x1080/296+-1920+-180  DP_1\n 1: MY MONÍTOR 800/300x600/200+0+0  x\n"
    assert parse_xrandr_monitors(text) == [
        (-1920, -180, 1920, 1080, "DP_1", True),
        (0, 0, 800, 600, "MY MONÍTOR", False),
    ]


def test_inventory_text_renders_every_field():
    root = CaptureGeometry(-1920, 0, 4480, 1600, 4480, 1600)
    displays = [
        DisplayInfo(1, 0, 0, 2560, 1600, 1.5, True, "ONE"),
        DisplayInfo(2, -1920, 0, 1920, 1080, None, False, "TWO"),
    ]
    assert inventory_text("all of it", root, displays).splitlines() == [
        "**monitor=0:** all of it: 4480x1600 at origin (-1920,0)",
        "",
        "**Displays (monitor=N):**",
        "  [1] ONE: 2560x1600 at origin (0,0), scale 1.500, primary",
        "  [2] TWO: 1920x1080 at origin (-1920,0), scale n/a",
    ]


def test_linux_inventory_lists_the_tools_monitor_numbers(dual_x11):
    assert server.DisplayInventory().splitlines() == [
        "**monitor=0:** the X root, all heads: 4480x1600 at origin (0,0)",
        "",
        "**Displays (monitor=N):**",
        "  [1] HEAD-0: 2560x1600 at origin (0,0), scale n/a",
        "  [2] HEAD-1: 1920x1080 at origin (2560,0), scale n/a",
    ]


def test_linux_inventory_index_is_the_display_a_monitor_argument_reaches(dual_x11):
    assert "[2] HEAD-1: 1920x1080 at origin (2560,0)" in server.DisplayInventory()
    server.Zoom(0, 0, 10, 10, monitor=2)
    assert _scrots(dual_x11)[-1].startswith("2560,0,")


def test_linux_inventory_with_no_randr_monitor_is_an_empty_list(monkeypatch):
    calls = _install_fake_x11(monkeypatch, root=(800, 600), heads=[(0, 0, 800, 600)])
    real = linux_session._run_x

    def _no_monitors(cmd, **kwargs):
        if "--listmonitors" in cmd:
            return types.SimpleNamespace(stdout=b"Monitors: 0\n")
        return real(cmd, **kwargs)

    monkeypatch.setattr(linux_session, "_run_x", _no_monitors)
    assert server.DisplayInventory().splitlines()[-1] == "**Displays (monitor=N):**"
    assert ["xrandr", "--current"] in calls


@pytest.fixture
def win_displays(monkeypatch, win, pyautogui_branch):
    """pywin32's GetMonitorInfo and shcore's GetDpiForMonitor for the fixture's two monitors."""
    sys.modules["win32api"].GetMonitorInfo = lambda hmon: {
        "Monitor": (),
        "Work": (),
        "Flags": 1 if hmon == 0 else 0,
        "Device": f"DISPLAY{hmon + 1}",
    }
    asked: list[tuple] = []

    def _dpi(hmon, kind, dpi_x, dpi_y):
        handle = hmon.value or 0  # a NULL c_void_p reads None
        asked.append((handle, kind))
        dpi_x._obj.value = dpi_y._obj.value = {0: 144, 1: 96}[handle]
        return 0

    monkeypatch.setattr(ctypes.windll.shcore, "GetDpiForMonitor", _dpi, raising=False)
    return asked


def test_windows_inventory(win_displays):
    assert server.DisplayInventory().splitlines() == [
        "**monitor=0:** the whole virtual screen, all monitors: 4480x1600 at origin (-1920,0)",
        "",
        "**Displays (monitor=N):**",
        "  [1] DISPLAY1: 2560x1600 at origin (0,0), scale 1.500, primary",
        "  [2] DISPLAY2: 1920x1080 at origin (-1920,0), scale 1.000",
    ]
    assert win_displays == [(0, 0), (1, 0)]  # MDT_EFFECTIVE_DPI = 0


def test_windows_inventory_without_a_dpi_answer_says_so_and_warns(monkeypatch, win_displays, caplog):
    monkeypatch.setattr(ctypes.windll.shcore, "GetDpiForMonitor", lambda *a: -2147024809)  # E_INVALIDARG
    with caplog.at_level(logging.WARNING, logger="remoteos.platform.win.desktop"):
        text = server.DisplayInventory()
    assert "[1] DISPLAY1: 2560x1600 at origin (0,0), scale n/a, primary" in text
    assert "GetDpiForMonitor failed" in caplog.text


def test_windows_inventory_before_8_1_has_no_scale(monkeypatch, win_displays, caplog):
    monkeypatch.delattr(ctypes.windll.shcore, "GetDpiForMonitor")
    with caplog.at_level(logging.WARNING, logger="remoteos.platform.win.desktop"):
        assert "scale n/a, primary" in server.DisplayInventory()
    assert "GetDpiForMonitor unavailable (AttributeError)" in caplog.text


def test_mac_inventory_main_display_and_retina_scale(mac, mac_tool_layer):
    assert server.DisplayInventory().splitlines() == [
        "**monitor=0:** the main display: 1440x900 at origin (0,0)",
        "",
        "**Displays (monitor=N):**",
        "  [1] display 1: 1440x900 at origin (0,0), scale 2.000, primary",
        "  [2] display 2: 1920x1080 at origin (-1920,-180), scale 1.000",
    ]


def test_mac_inventory_failure_is_an_error(monkeypatch, mac, mac_tool_layer):
    monkeypatch.setattr(sys.modules["Quartz"], "CGGetActiveDisplayList", lambda n, a, b: (1001, [], 0))
    assert tool_error(server.DisplayInventory) == "DisplayInventory error: CGGetActiveDisplayList failed (1001)"


def test_inventory_runs_the_window_preflight_not_the_capture_one(monkeypatch):
    asked = []

    def _diagnose(need):
        asked.append(need)
        return f"diagnosed ({need})"

    monkeypatch.setattr(server, "get_desktop", lambda: types.SimpleNamespace(diagnose=_diagnose))
    with pytest.raises(ToolError) as exc:
        server.DisplayInventory()
    assert str(exc.value) == "diagnosed (windows)" and asked == ["windows"]


def test_new_tools_are_gated_like_snapshot_and_categorised():
    from remoteos.taskmanager import TOOL_CATEGORIES, ToolCategory
    from remoteos.tiers import LINUX_DISPLAY_TOOLS, TOOL_TIERS

    assert {"Zoom", "DisplayInventory"} <= LINUX_DISPLAY_TOOLS
    assert {"Zoom", "DisplayInventory"} <= TOOL_TIERS["tier1"]
    assert TOOL_CATEGORIES["Zoom"] is ToolCategory.DESKTOP  # a capture: exclusive, like Snapshot
    assert TOOL_CATEGORIES["DisplayInventory"] is ToolCategory.QUERY
