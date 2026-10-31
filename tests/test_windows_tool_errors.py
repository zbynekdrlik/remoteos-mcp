"""The Windows provider and tools report a failure as an error, and only a failure (#32).

Moved here from ``test_provider_geometry.py`` (review round 6: that module crossed
the ~1000-line budget with tests that are not about geometry). The provider runs
against the fake Win32 API of ``conftest.win_session``; only the OS boundary is
faked: pywin32's clipboard, the screen grab, ``subprocess.run``.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

import pytest
from fake_console_child import popen_answered_by, run_result
from fake_windows_session import FakeClipboard
from PIL import ImageGrab
from tool_surface import server


@pytest.fixture
def clipboard_tool(monkeypatch, win_session):
    mod, _ = win_session
    clip = FakeClipboard()
    module = sys.modules["win32clipboard"]
    for name in ("OpenClipboard", "CloseClipboard", "IsClipboardFormatAvailable", "GetClipboardData"):
        monkeypatch.setattr(module, name, getattr(clip, name), raising=False)
    monkeypatch.setattr(sys.modules["win32con"], "CF_UNICODETEXT", 13, raising=False)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    return clip


def test_windows_clipboard_without_text_reads_as_empty_not_an_error(clipboard_tool):
    # an empty clipboard, or one holding only an image, is "no text" (#32 review round 1)
    clipboard_tool.text = None
    assert server.GetClipboard() == ""
    clipboard_tool.text = "Error: this is just text"  # a failure is a raise, so text like this is ordinary text
    assert server.GetClipboard() == "Error: this is just text"


@pytest.mark.parametrize(
    "call",
    [lambda mod: mod.launch_app("zz_no_such_app_zz"), lambda mod: mod.show_notification("t", "m")],
    ids=["launch_app", "show_notification"],
)
def test_windows_powershell_failure_is_an_error_not_a_success(win_session, windows_python, monkeypatch, call):
    # live on win-resolume: Start-Process of a missing app exits 1 (#32 review round 1)
    mod, _ = win_session
    stderr = "This command cannot be run: file not found."
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(lambda cmd, **kwargs: run_result(kwargs, 1, "", stderr)))
    with pytest.raises(RuntimeError, match="file not found"):
        call(mod)


def test_windows_launch_app_success(win_session, windows_python, monkeypatch):
    mod, _ = win_session
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(lambda cmd, **kwargs: run_result(kwargs)))
    assert mod.launch_app("notepad") == "Launched notepad"


@pytest.mark.parametrize(("locked", "outcome"), [(1, "Screen locked"), (0, "LockWorkStation failed")])
def test_windows_lock_screen_reports_a_refused_lock_as_an_error(win_session, monkeypatch, locked, outcome):
    # review round 8: LockWorkStation returns 0 when it cannot lock (e.g. no interactive desktop)
    mod, _ = win_session
    monkeypatch.setattr(ctypes.windll.user32, "LockWorkStation", lambda: locked, raising=False)
    if locked:
        assert mod.lock_screen() == outcome
    else:
        with pytest.raises(RuntimeError, match=f"^{outcome} "):
            mod.lock_screen()


def test_windows_capture_on_an_attached_session_retries_once_and_says_so(monkeypatch, win_session):
    # review round 2: no reconnect happened, so neither the log nor the error may claim one
    mod, _ = win_session  # the default fake session is attached
    monkeypatch.setattr(server, "is_windows", lambda: True)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    commands: list[list[str]] = []
    monkeypatch.setattr(server.subprocess, "Popen", popen_answered_by(lambda cmd, **kw: commands.append(list(cmd))))
    grabs: list[bool] = []

    def _failing_grab(bbox=None, all_screens=False, **kwargs):
        grabs.append(all_screens)
        raise OSError("screen grab failed")

    monkeypatch.setattr(ImageGrab, "grab", _failing_grab)
    with pytest.raises(RuntimeError, match="^after a retry: screen grab failed"):
        server._capture_snapshot_frame(0, 1280)
    assert len(grabs) == 2  # the grab and ONE retry
    assert commands == []  # no tscon
