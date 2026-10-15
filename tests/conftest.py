"""Fixtures shared by several test modules: the gateway harness, and the Windows provider on a fake Win32 API."""

from __future__ import annotations

import ctypes
import importlib
import sys
import types

import pytest
from fake_windows_session import FakeWindowsSession
from gateway_harness import MACHINE, OTHER_MACHINE, FakeMachine, Harness, _default_registry, _machines_file, _write_json
from PIL import Image, ImageGrab
from tool_test_support import purge_module


@pytest.fixture
def gw(tmp_path) -> Harness:
    h = Harness(
        tmp=tmp_path,
        machine=FakeMachine(),
        registry_path=tmp_path / "cfg" / "registry.json",
        machines_path=tmp_path / "claude" / "remoteos-mcp.mcp.json",
        access_log_path=tmp_path / "state" / "access.jsonl",
    )
    _write_json(h.registry_path, _default_registry())
    _machines_file(h.machines_path, [MACHINE, OTHER_MACHINE])
    return h


@pytest.fixture
def win_session(monkeypatch):
    """The Windows provider imported against a fake Win32 API; yields ``(provider, session)`` (#32).

    ``session`` is a ``fake_windows_session.FakeWindowsSession``, HEALTHY by default
    (session 1, WinSta0, active, input desktop "Default"). One 1920x1080 monitor, no
    windows, an empty fake pywin32/pyautogui. Only the OS boundary is faked.
    """
    session = FakeWindowsSession()
    windll = types.SimpleNamespace(
        user32=types.SimpleNamespace(
            GetSystemMetrics=lambda i: {76: 0, 77: 0, 78: 1920, 79: 1080}[i],
            SetProcessDpiAwarenessContext=lambda ctx: 1,
            SetProcessDPIAware=lambda: 1,
            SetCursorPos=lambda x, y: 1,
        ),
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda v: 0),
    )
    session.add_to(windll)
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    from remoteos.platform.win import dpi

    monkeypatch.setattr(dpi, "_MODE", None)
    win32api = types.ModuleType("win32api")
    win32api.EnumDisplayMonitors = lambda: [(0, 0, (0, 0, 1920, 1080))]
    monkeypatch.setitem(sys.modules, "win32api", win32api)
    win32gui = types.ModuleType("win32gui")
    win32gui.EnumWindows = lambda cb, extra: None
    win32gui.GetForegroundWindow = lambda: 0
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    for name in ("win32clipboard", "win32con", "win32process"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "pyautogui", types.ModuleType("pyautogui"))
    monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None, all_screens=False, **kw: Image.new("RGB", (1920, 1080)))
    purge_module("remoteos.platform.win.desktop")
    mod = importlib.import_module("remoteos.platform.win.desktop")
    yield mod, session
    purge_module("remoteos.platform.win.desktop")
