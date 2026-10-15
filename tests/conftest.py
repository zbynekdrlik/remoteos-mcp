"""Fixtures shared by several test modules: the gateway harness, the Windows provider on a fake Win32 API, and a
fake Windows console child (#48)."""

from __future__ import annotations

import ctypes
import importlib
import subprocess
import sys
import types

import pytest
from fake_console_child import FakeChild, oem_code_page
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


@pytest.fixture
def windows_python(tmp_path, monkeypatch):
    """A Windows Python install (#48 round 2); yields the path of its console ``python.exe``.

    The folder holds ``python.exe`` and ``pythonw.exe``, as python.org's installer lays them out,
    and the server runs as that ``python.exe`` (``sys.executable``). A PowerShell child starts
    under the console trampoline of the ``python.exe`` next to the server's interpreter.
    """
    folder = tmp_path / "Python312"
    folder.mkdir()
    for exe in ("python.exe", "pythonw.exe"):
        (folder / exe).write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(folder / "python.exe"))
    return folder / "python.exe"


@pytest.fixture
def windows_child(monkeypatch, windows_python):
    """A Windows console child at the OS boundary (#48); yields the ``fake_console_child.FakeChild``.

    ``subprocess.Popen`` (and so ``subprocess.run``) starts a real Python child that writes
    ``child.stdout`` / ``child.stderr`` bytes and exits ``child.returncode``. Python's Windows-only
    ``oem`` codec stands for ``child.oem``: cp437 (resolume-pp) unless the test sets another page
    before the first decode. ``windows_python`` is the install the trampoline is taken from.
    """
    child = FakeChild()
    monkeypatch.setattr(subprocess, "Popen", child)
    with oem_code_page(lambda: child.oem):
        yield child


@pytest.fixture(autouse=True)
def _linux_session_detected_at_start_up(monkeypatch):
    """A Linux Shell call reads the provider's cached session (#52).

    The server detects it at start-up, so a Shell call never has to. Each test starts from such
    a cache, holding what CI's start-up finds (no graphical session); a test that needs another
    session sets its own. Without it, the first Linux Shell call would run the host's real
    ``loginctl`` (through ``subprocess``, which some tests record), and whatever it found would
    leak into later tests.
    """
    from remoteos.platform.linux import desktop

    monkeypatch.setattr(desktop, "_SESSION", desktop.SessionInfo(mode="headless"))
