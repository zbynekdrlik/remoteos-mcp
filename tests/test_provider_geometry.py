"""Windows / macOS provider geometry + the pyautogui input branch (#28), run on any OS.

The Windows and macOS providers are imported here against FAKE OS APIs — the
external boundary only: ``ctypes.windll`` / pywin32 / ``ImageGrab.grab`` /
pyautogui on Windows, the ``Quartz`` framework and the ``screencapture``
subprocess on macOS. Everything of ours (``capture_geometry``, ``capture_frame``,
the frame math, the tool functions) is the real code.
"""

from __future__ import annotations

import ctypes
import importlib
import sys
import types
from pathlib import Path

import pytest
from PIL import Image, ImageGrab

from remoteos.coords import image_to_screen

# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# A 1920x1080 monitor LEFT of a 2560x1600 primary → virtual screen (-1920,0) 4480x1600.
_METRICS = {76: -1920, 77: 0, 78: 4480, 79: 1600}
_MONITOR_RECTS = [(0, 0, 2560, 1600), (-1920, 0, 0, 1080)]


class _Recorder:
    def __init__(self, result=None):
        self.calls: list = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _purge(name: str) -> None:
    sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if parent in sys.modules and hasattr(sys.modules[parent], child):
        delattr(sys.modules[parent], child)


@pytest.fixture
def win(monkeypatch):
    windll = types.SimpleNamespace(
        user32=types.SimpleNamespace(
            GetSystemMetrics=lambda i: _METRICS[i],
            SetProcessDpiAwarenessContext=lambda ctx: 1,
            SetProcessDPIAware=lambda: 1,
        ),
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda v: 0),
    )
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    from remoteos.platform.win import dpi

    monkeypatch.setattr(dpi, "_MODE", None)
    win32api = types.ModuleType("win32api")
    win32api.EnumDisplayMonitors = lambda: [(i, i, rect) for i, rect in enumerate(_MONITOR_RECTS)]
    monkeypatch.setitem(sys.modules, "win32api", win32api)
    for name in ("win32clipboard", "win32con", "win32gui", "win32process"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "pyautogui", types.ModuleType("pyautogui"))

    grabs: list[dict] = []

    def _grab(bbox=None, include_layered_windows=False, all_screens=False, xdisplay=None):
        grabs.append({"bbox": bbox, "all_screens": all_screens})
        if bbox is None:
            return Image.new("RGB", (4480, 1600))
        return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]))

    monkeypatch.setattr(ImageGrab, "grab", _grab)
    _purge("remoteos.platform.win.desktop")
    mod = importlib.import_module("remoteos.platform.win.desktop")
    yield mod, grabs
    _purge("remoteos.platform.win.desktop")


def test_windows_import_sets_per_monitor_v2(win):
    from remoteos.platform.win import dpi

    assert dpi._MODE == "per-monitor-v2"


def test_windows_virtual_screen_geometry_has_negative_origin(win):
    mod, _ = win
    g = mod.capture_geometry(0)
    assert (g.x, g.y, g.width, g.height) == (-1920, 0, 4480, 1600)
    assert (g.pixel_width, g.pixel_height) == (4480, 1600)


def test_windows_capture_frame_all_monitors(win):
    mod, grabs = win
    img, frame = mod.capture_frame(monitor=0, max_width=1280)
    assert img.size == frame.image_size == (1280, 457)
    assert grabs == [{"bbox": None, "all_screens": True}]
    assert image_to_screen(frame, 0, 0) == (-1920, 0)  # image left edge = the left monitor


def test_windows_non_primary_monitor_grabs_from_all_screens(win):
    mod, grabs = win
    img, frame = mod.capture_frame(monitor=2, max_width=1280)
    # bbox of the left monitor, cut out of the WHOLE virtual screen (not a primary-only grab)
    assert grabs == [{"bbox": (-1920, 0, 0, 1080), "all_screens": True}]
    assert frame.origin == (-1920, 0)
    assert img.size == (1280, 720)
    assert image_to_screen(frame, 640, 360) == (-960, 540)


@pytest.fixture
def pyautogui_branch(monkeypatch, win):
    """The non-Linux branch of the input tools, with a recording fake pyautogui."""
    import remoteos.__main__ as server

    mod, _ = win
    fake = types.SimpleNamespace(
        click=_Recorder(),
        doubleClick=_Recorder(),
        moveTo=_Recorder(),
        drag=_Recorder(),
        position=lambda: (100, 100),
        hotkey=_Recorder(),
        press=_Recorder(),
        typewrite=_Recorder(),
        write=_Recorder(),
        scroll=_Recorder(),
        hscroll=_Recorder(),
    )
    monkeypatch.setattr(server, "pyautogui", fake, raising=False)
    monkeypatch.setattr(server, "is_linux", lambda: False)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    return server, fake


def test_pyautogui_click_gets_screen_point(pyautogui_branch):
    server, fake = pyautogui_branch
    out = server.Click(640, 228)  # virtual screen (-1920,0) 4480x1600, scale 3.5
    assert fake.click.calls == [((320, 798), {"button": "left"})]
    assert out.startswith("Clicked left at (320,798) [image (640,228) → screen (320,798)")


def test_pyautogui_drag_maps_start_and_target(pyautogui_branch):
    server, fake = pyautogui_branch
    server.Move(640, 228, drag=True, start_x=2, start_y=2)
    assert fake.moveTo.calls == [((-1913, 7), {})]
    # drag is relative to the (fake) current position (100,100)
    assert fake.drag.calls == [((220, 698), {"duration": 0.3})]


def test_pyautogui_type_and_scroll_points(pyautogui_branch):
    server, fake = pyautogui_branch
    server.Type("hi", x=640, y=228)
    server.Scroll(-2, x=640, y=228)
    assert fake.click.calls == [((320, 798), {})]
    assert fake.moveTo.calls == [((320, 798), {})]


def test_pyautogui_screen_coords_pass_through(pyautogui_branch):
    server, fake = pyautogui_branch
    server.Move(-1500, 20, coords="screen")
    assert fake.moveTo.calls == [((-1500, 20), {"duration": 0.3})]


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

# main: 1440x900 points on a 2880x1800 Retina panel; display 2: 1920x1080 @1x up-left of it.
_MAC_DISPLAYS = {1: ((0, 0, 1440, 900), (2880, 1800)), 2: ((-1920, -180, 1920, 1080), (1920, 1080))}


def _rect(x, y, w, h):
    return types.SimpleNamespace(
        origin=types.SimpleNamespace(x=float(x), y=float(y)),
        size=types.SimpleNamespace(width=float(w), height=float(h)),
    )


@pytest.fixture
def mac(monkeypatch):
    quartz = types.ModuleType("Quartz")
    quartz.CGMainDisplayID = lambda: 1
    quartz.CGGetActiveDisplayList = lambda n, a, b: (0, [1, 2], 2)
    quartz.CGDisplayBounds = lambda did: _rect(*_MAC_DISPLAYS[did][0])
    quartz.CGDisplayCopyDisplayMode = lambda did: did
    quartz.CGDisplayModeGetPixelWidth = lambda mode: _MAC_DISPLAYS[mode][1][0]
    quartz.CGDisplayModeGetPixelHeight = lambda mode: _MAC_DISPLAYS[mode][1][1]
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    _purge("remoteos.platform.mac.desktop")
    mod = importlib.import_module("remoteos.platform.mac.desktop")

    commands: list[list[str]] = []

    def _run(cmd, **kwargs):
        commands.append(list(cmd))
        display = int(cmd[cmd.index("-D") + 1]) if "-D" in cmd else 1
        Image.new("RGB", _MAC_DISPLAYS[display][1]).save(Path(cmd[-1]), format="PNG")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    yield mod, commands
    _purge("remoteos.platform.mac.desktop")


def test_mac_retina_geometry_is_points_with_pixel_capture(mac):
    mod, _ = mac
    g = mod.capture_geometry(0)
    assert (g.x, g.y, g.width, g.height) == (0, 0, 1440, 900)
    assert (g.pixel_width, g.pixel_height) == (2880, 1800)


def test_mac_capture_frame_main_display_maps_to_points(mac):
    mod, commands = mac
    img, frame = mod.capture_frame(monitor=0, max_width=1280)
    assert commands[0][:2] == ["screencapture", "-x"] and "-D" not in commands[0]
    assert img.size == (1280, 800)
    assert frame.scale == pytest.approx(1.125)
    assert image_to_screen(frame, 640, 400) == (720, 450)  # points, not Retina pixels


def test_mac_capture_frame_second_display_reports_origin(mac):
    mod, commands = mac
    img, frame = mod.capture_frame(monitor=2, max_width=1280)
    assert commands[0][:4] == ["screencapture", "-x", "-D", "2"]
    assert frame.origin == (-1920, -180)
    assert img.size == (1280, 720)
    assert image_to_screen(frame, 0, 0) == (-1920, -180)


def test_mac_unknown_monitor_raises(mac):
    mod, _ = mac
    with pytest.raises(IndexError):
        mod.capture_geometry(3)
