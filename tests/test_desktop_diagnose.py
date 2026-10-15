"""Desktop diagnoses (#32): every provider's ``diagnose(need)`` and the desktop tools' preflight.

The providers run against FAKE OS APIs, the external boundary only:

- Windows: ``tests/fake_windows_session.py`` (kernel32 / user32 / wtsapi32 of one
  session, WTS buffers with literal Win32 offsets) and a faked ``tscon``.
- macOS: the ``Quartz`` framework (``CGSessionCopyCurrentDictionary``,
  ``CGPreflightScreenCaptureAccess``, ``CGPreflightPostEventAccess``).
- Linux: the cached session (and a guard that no subprocess runs).

``diagnose()`` must NEVER false-positive: a healthy desktop and a probe that
fails are both "usable". Only a positive finding stops a tool.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import os
import sys
import types

import pytest
from fake_windows_session import LOCK, UNLOCK, WTS_ACTIVE, WTS_DISCONNECTED, FakeWindowsSession
from fastmcp.exceptions import ToolError
from PIL import Image, ImageGrab

import remoteos.__main__ as server
from remoteos.platform.linux import desktop as linux_desktop

_HEADLESS = "Not available on headless Linux (no display)"
_WAYLAND = "Not available: Wayland session detected (X11 backend only — Wayland support is a separate tier)"
_LOCKED_START = "The screen is locked: screenshots show the lock screen and mouse/keyboard input goes nowhere."
_DESKTOP_HANDLE = 0x1A4


def _purge(name: str) -> None:
    sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if parent in sys.modules and hasattr(sys.modules[parent], child):
        delattr(sys.modules[parent], child)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


@pytest.fixture
def win_session(monkeypatch):
    """The Windows provider imported against a fake Win32 API; yields ``(provider, session)``."""
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
    _purge("remoteos.platform.win.desktop")
    mod = importlib.import_module("remoteos.platform.win.desktop")
    yield mod, session
    _purge("remoteos.platform.win.desktop")


def test_windows_healthy_desktop_is_no_diagnosis(win_session):
    mod, session = win_session
    assert mod.diagnose("capture") is None
    assert mod.diagnose("input") is None
    assert session.closed == [_DESKTOP_HANDLE, _DESKTOP_HANDLE]  # the input desktop handle is closed again
    assert session.open_access == [0x0001, 0x0001]  # DESKTOP_READOBJECTS: enough to read its name
    assert len(session.freed) == len(session.buffers)  # every WTS buffer is freed


def test_windows_session_zero_says_how_to_start_it_in_the_user_session(win_session):
    mod, session = win_session
    session.session_id = 0
    reason = mod.diagnose()
    assert "session 0" in reason
    assert "`schtasks /Run /TN RemoteOSMCP`" in reason


def test_windows_non_interactive_window_station_is_no_interactive_desktop(win_session):
    mod, session = win_session
    session.window_station = "Service-0x0-3e7$"  # a task set to run whether the user is logged on or not
    reason = mod.diagnose()
    assert "'Service-0x0-3e7$'" in reason and "WinSta0" in reason


def test_windows_disconnected_session_points_to_reconnect_session(win_session):
    mod, session = win_session
    session.connect_state = WTS_DISCONNECTED
    reason = mod.diagnose()
    assert "disconnected" in reason and "ReconnectSession" in reason


def test_windows_locked_screen(win_session):
    mod, session = win_session
    session.input_desktop = None  # the lock screen runs on the Winlogon desktop: OpenInputDesktop fails
    session.lock_flags = LOCK
    assert mod.diagnose().startswith(_LOCKED_START)
    assert mod.diagnose("capture").startswith(_LOCKED_START)
    assert len(session.freed) == len(session.buffers)


def test_windows_secure_desktop_is_the_uac_prompt_when_the_session_is_not_locked(win_session):
    mod, session = win_session
    session.input_desktop = None
    session.lock_flags = UNLOCK
    reason = mod.diagnose()
    assert reason.startswith("The screen shows a secure desktop (a UAC prompt, Ctrl+Alt+Del or the sign-in screen)")


def test_windows_secure_desktop_is_named_when_it_can_be_opened(win_session):
    mod, session = win_session
    session.input_desktop = "Winlogon"  # running as SYSTEM opens it
    session.lock_flags = UNLOCK
    assert mod.diagnose().startswith("The screen shows the 'Winlogon' desktop")
    assert session.closed == [_DESKTOP_HANDLE]


def test_windows_unknown_lock_state_still_reports_the_blocked_desktop(win_session):
    mod, session = win_session
    session.input_desktop = None
    session.lock_flags = None  # WTSSessionInfoEx refused
    assert mod.diagnose().startswith("The screen shows a secure desktop")


def test_windows_failing_probe_is_usable_and_logged(win_session, caplog):
    mod, session = win_session
    session.fail_session_id = True
    with caplog.at_level(logging.WARNING, logger="remoteos"):
        assert mod.diagnose() is None  # fail open: never block a desktop the probe could not read
    assert "desktop probe failed" in caplog.text


def test_windows_window_list_needs_only_the_users_desktop(win_session):
    # review round 3: the window list (Snapshot without vision) works on a disconnected or locked
    # session, but not from session 0 or a non-interactive window station: those are no user desktop
    mod, session = win_session
    session.connect_state = WTS_DISCONNECTED
    assert mod.diagnose("windows") is None
    session.connect_state, session.input_desktop, session.lock_flags = WTS_ACTIVE, None, LOCK
    assert mod.diagnose("windows") is None
    session.window_station = "Service-0x0-3e7$"
    assert "'Service-0x0-3e7$'" in mod.diagnose("windows")
    session.session_id = 0
    assert "`schtasks /Run /TN RemoteOSMCP`" in mod.diagnose("windows")


def test_windows_wtsinfoex_head_keeps_the_win32_offsets():
    from remoteos.platform.win import availability

    head = availability._WTSINFOEX_HEAD
    assert (head.SessionId.offset, head.SessionState.offset, head.SessionFlags.offset) == (8, 12, 16)


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


class _FakeMac:
    def __init__(self):
        self.session: dict | None = {"kCGSSessionOnConsoleKey": True}
        self.screen_recording = True
        self.post_events = True  # CGPreflightPostEventAccess: Privacy & Security › Accessibility


@pytest.fixture
def mac_session(monkeypatch):
    state = _FakeMac()
    quartz = types.ModuleType("Quartz")
    quartz.kCGSessionOnConsoleKey = "kCGSSessionOnConsoleKey"
    quartz.CGSessionCopyCurrentDictionary = lambda: state.session
    quartz.CGPreflightScreenCaptureAccess = lambda: state.screen_recording
    quartz.CGPreflightPostEventAccess = lambda: state.post_events
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "pyautogui", types.ModuleType("pyautogui"))
    _purge("remoteos.platform.mac.desktop")
    mod = importlib.import_module("remoteos.platform.mac.desktop")
    yield mod, state
    _purge("remoteos.platform.mac.desktop")


def test_mac_healthy_desktop_is_no_diagnosis(mac_session):
    mod, _ = mac_session
    assert mod.diagnose("capture") is None
    assert mod.diagnose("input") is None


def test_mac_without_screen_recording_cannot_capture(mac_session):
    mod, state = mac_session
    state.screen_recording = False
    reason = mod.diagnose("capture")
    assert "Screen Recording" in reason and os.path.realpath(sys.executable) in reason
    assert mod.diagnose("input") is None  # input does not need Screen Recording


def test_mac_without_accessibility_cannot_send_input(mac_session):
    # the same predicate the event-posting code gates on (CGPreflightPostEventAccess), review round 1
    mod, state = mac_session
    state.post_events = False
    reason = mod.diagnose("input")
    assert "Accessibility" in reason and os.path.realpath(sys.executable) in reason
    assert mod.diagnose("capture") is None  # a screenshot does not need Accessibility


def test_mac_locked_screen(mac_session):
    mod, state = mac_session
    state.session = {"kCGSSessionOnConsoleKey": True, "CGSSessionScreenIsLocked": True}
    assert mod.diagnose("capture").startswith(_LOCKED_START)
    assert mod.diagnose("input").startswith(_LOCKED_START)


def test_mac_without_a_gui_session(mac_session):
    mod, state = mac_session
    state.session = None  # CGSessionCopyCurrentDictionary: NULL outside a window-server session
    assert mod.diagnose().startswith("No macOS GUI session")


def test_mac_session_not_on_the_console(mac_session):
    mod, state = mac_session
    state.session = {"kCGSSessionOnConsoleKey": False}  # fast user switching: another user has the screen
    assert "not on the screen" in mod.diagnose()


def test_mac_without_the_post_event_preflight_is_usable(mac_session, monkeypatch):
    mod, _ = mac_session
    monkeypatch.delattr(sys.modules["Quartz"], "CGPreflightPostEventAccess")  # before macOS 10.15
    assert mod.diagnose("input") is None


def test_mac_window_list_needs_only_a_gui_session(mac_session):
    mod, state = mac_session
    state.session = {"kCGSSessionOnConsoleKey": True, "CGSSessionScreenIsLocked": True}
    state.screen_recording = state.post_events = False
    assert mod.diagnose("windows") is None  # the window list works without either permission
    state.session = None
    assert mod.diagnose("windows").startswith("No macOS GUI session")


def test_mac_without_the_screen_capture_preflight_is_usable(mac_session, monkeypatch):
    mod, _ = mac_session
    monkeypatch.delattr(sys.modules["Quartz"], "CGPreflightScreenCaptureAccess")  # before macOS 10.15
    assert mod.diagnose("capture") is None


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _x11(session_id: str | None = "7"):
    return linux_desktop.SessionInfo(mode="x11", display=":0", uid=1000, user="newlevel", session_id=session_id)


@pytest.fixture
def no_subprocess(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("the Linux diagnosis must not run a subprocess")

    monkeypatch.setattr(linux_desktop, "_run_capture", _boom)
    monkeypatch.setattr(linux_desktop, "_run_x", _boom)


def test_linux_headless_and_wayland_diagnoses_are_the_unchanged_stubs(monkeypatch, no_subprocess):
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="headless"))
    assert linux_desktop.diagnose("capture") == _HEADLESS
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="wayland"))
    assert linux_desktop.diagnose("input") == _WAYLAND


def test_linux_x11_session_is_no_diagnosis_even_when_a_locker_runs(monkeypatch, no_subprocess):
    # review round 1: a locked X11 screen is no silent failure. The screenshot shows the locker
    # and xdotool input reaches it (the only remote way to unlock), so it is never refused.
    monkeypatch.setattr(linux_desktop, "_SESSION", _x11())
    assert linux_desktop.diagnose("capture") is None
    assert linux_desktop.diagnose("input") is None


# ---------------------------------------------------------------------------
# The preflight: which tools run it, with which kind, and that it fails open
# ---------------------------------------------------------------------------

_CAPTURE_TOOLS = {
    "Snapshot": {},
    "AnnotatedSnapshot": {},
    "OCR": {},
    "ScreenRecord": {"duration": 0.1},
}
_INPUT_TOOLS = {
    "Click": {"x": 1, "y": 1},
    "Move": {"x": 1, "y": 1},
    "Type": {"text": "x"},
    "Scroll": {"amount": 1, "direction": "down"},
    "Shortcut": {"keys": "ctrl+c"},
    "MinimizeAll": {},
}
_NO_PREFLIGHT_TOOLS = {
    "FocusWindow": {"title": "Editor"},
    "App": {"name": "editor"},
    "GetClipboard": {},
    "SetClipboard": {"text": "x"},
    "LockScreen": {},
    "Notification": {"message": "x"},
}


class _DiagnosingProvider(types.SimpleNamespace):
    """A provider whose diagnose() always finds a problem (or raises), and records how it was asked."""

    HAS_WIN32 = True

    def __init__(self, explode: bool = False):
        super().__init__(asked=[], explode=explode)

    def diagnose(self, need="input"):
        self.asked.append(need)
        if self.explode:
            raise RuntimeError("probe exploded")
        return f"diagnosed ({need})"

    def __getattr__(self, name):  # every other provider function "works"
        return lambda *args, **kwargs: "ok"


@pytest.fixture
def diagnosing(monkeypatch):
    provider = _DiagnosingProvider()
    monkeypatch.setattr(server, "get_desktop", lambda: provider)
    return provider


@pytest.mark.parametrize("name", sorted(_CAPTURE_TOOLS))
def test_capture_tools_stop_on_a_capture_diagnosis(diagnosing, name):
    with pytest.raises(ToolError) as exc:
        getattr(server, name)(**_CAPTURE_TOOLS[name])
    assert str(exc.value) == "diagnosed (capture)"
    assert diagnosing.asked == ["capture"]


@pytest.mark.parametrize("name", sorted(_INPUT_TOOLS))
def test_input_tools_stop_on_an_input_diagnosis(diagnosing, name):
    with pytest.raises(ToolError) as exc:
        getattr(server, name)(**_INPUT_TOOLS[name])
    assert str(exc.value) == "diagnosed (input)"
    assert diagnosing.asked == ["input"]


@pytest.mark.parametrize("name", sorted(_NO_PREFLIGHT_TOOLS))
def test_other_desktop_tools_do_not_run_the_preflight(diagnosing, name):
    # e.g. the clipboard and notifications work on a locked Windows session
    getattr(server, name)(**_NO_PREFLIGHT_TOOLS[name])
    assert diagnosing.asked == []


def test_the_preflight_logs_its_result(diagnosing, caplog):
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        with pytest.raises(ToolError):
            server.Click(1, 1)
    assert "desktop unavailable (input): diagnosed (input)" in caplog.text


def test_a_diagnose_that_raises_never_blocks_the_tool(monkeypatch, caplog):
    # fail open at the one shared call site too, whatever a provider's diagnose() does (review round 1)
    provider = _DiagnosingProvider(explode=True)
    monkeypatch.setattr(server, "get_desktop", lambda: provider)
    with caplog.at_level(logging.WARNING, logger="remoteos"):
        assert server.Shortcut("ctrl+c") == "ok"
    assert provider.asked == ["input"]
    assert "desktop probe failed" in caplog.text


# ---------------------------------------------------------------------------
# The preflight end to end on each OS
# ---------------------------------------------------------------------------


@pytest.fixture
def windows_tools(monkeypatch, win_session):
    mod, session = win_session
    clicks: list = []
    fake_gui = types.SimpleNamespace(
        click=lambda *a, **k: clicks.append((a, k)),
        moveTo=lambda *a, **k: clicks.append(("move", a)),
        hotkey=lambda *a: clicks.append(("hotkey", a)),
    )
    monkeypatch.setattr(server, "pyautogui", fake_gui, raising=False)
    monkeypatch.setattr(server, "is_windows", lambda: True)
    monkeypatch.setattr(server, "is_linux", lambda: False)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    commands: list[list[str]] = []

    def _run(cmd, **kwargs):  # only tscon may run
        commands.append(list(cmd))
        return session.tscon(cmd)

    monkeypatch.setattr(server.subprocess, "run", _run)
    return session, clicks, commands


def test_windows_click_on_a_locked_screen_is_refused_before_any_input(windows_tools):
    session, clicks, _ = windows_tools
    session.input_desktop, session.lock_flags = None, LOCK
    with pytest.raises(ToolError) as exc:
        server.Click(640, 360)
    assert str(exc.value).startswith(_LOCKED_START)
    assert clicks == []


def test_windows_click_on_a_healthy_desktop_still_clicks(windows_tools):
    _, clicks, _ = windows_tools
    assert server.Click(640, 360, max_width=0).startswith("Clicked left at (640,360)")
    assert clicks == [((640, 360), {"button": "left"})]


def test_windows_tools_on_a_disconnected_session_are_refused_without_moving_the_session(windows_tools):
    # review round 1: no tool moves a session onto the physical console on its own
    session, clicks, commands = windows_tools
    session.connect_state = WTS_DISCONNECTED
    for call in (server.Snapshot, lambda: server.Click(640, 360)):
        with pytest.raises(ToolError) as exc:
            call()
        assert "ReconnectSession" in str(exc.value)
    assert commands == [] and clicks == []


def test_reconnect_session_attaches_the_servers_own_session_to_the_console(windows_tools):
    # the server's OWN session (ProcessIdToSessionId), never one parsed out of `query session`
    session, _, commands = windows_tools
    session.session_id, session.connect_state = 2, WTS_DISCONNECTED
    (reply,) = server.ReconnectSession()
    assert reply.text == "Session connected to console"
    assert commands == [["tscon", "2", "/dest:console"]]
    assert session.connect_state == WTS_ACTIVE
    assert server.Snapshot()  # and the desktop is usable again


def test_reconnect_session_that_leaves_the_session_disconnected_is_an_error(windows_tools):
    session, _, commands = windows_tools
    session.connect_state, session.tscon_attaches = WTS_DISCONNECTED, False
    with pytest.raises(ToolError) as exc:
        server.ReconnectSession()
    assert str(exc.value).startswith("ReconnectSession failed:")
    assert "still disconnected" in str(exc.value)
    assert commands == [["tscon", "1", "/dest:console"]]


def test_reconnect_session_on_a_connected_session_does_nothing_and_says_so(windows_tools):
    # review round 2: an attached session (console OR remote desktop) is never "connected to console"
    _, _, commands = windows_tools
    (reply,) = server.ReconnectSession()
    assert reply.text == "Session already attached (console or remote desktop): nothing to do"
    assert commands == []


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda s: setattr(s, "session_id", 0), "`schtasks /Run /TN RemoteOSMCP`"),
        (lambda s: setattr(s, "window_station", "Service-0x0-3e7$"), "'Service-0x0-3e7$'"),
    ],
    ids=["session-0", "non-interactive-window-station"],
)
def test_snapshot_without_vision_outside_the_users_desktop_is_an_error(windows_tools, setup, expected):
    # review round 3: there the window list is a hollow success (an empty list), never the user's windows
    session, _, _ = windows_tools
    setup(session)
    with pytest.raises(ToolError) as exc:
        server.Snapshot(use_vision=False)
    assert expected in str(exc.value)


def test_snapshot_without_vision_still_lists_windows_on_a_disconnected_session(windows_tools, monkeypatch):
    # review round 2: use_vision=False captures nothing, so the capture preflight must not refuse it
    # (live on win-pz-server: a disconnected session still answers EnumWindows and GetSystemMetrics)
    session, _, commands = windows_tools
    session.connect_state = WTS_DISCONNECTED
    monkeypatch.setattr(sys.modules["win32gui"], "EnumWindows", lambda cb, extra: cb(0x10, None))
    monkeypatch.setattr(sys.modules["win32gui"], "IsWindowVisible", lambda hwnd: True, raising=False)
    monkeypatch.setattr(sys.modules["win32gui"], "GetWindowText", lambda hwnd: "Editor", raising=False)
    monkeypatch.setattr(sys.modules["win32gui"], "GetWindowRect", lambda hwnd: (0, 0, 640, 480), raising=False)
    monkeypatch.setattr(sys.modules["win32process"], "GetWindowThreadProcessId", lambda hwnd: (1, 4242), raising=False)
    (text,) = server.Snapshot(use_vision=False)
    assert "Editor" in text.text and "screen 1920x1080" in text.text
    assert commands == []


@pytest.fixture
def mac_tools(monkeypatch, mac_session):
    mod, state = mac_session
    clicks: list = []
    fake_gui = types.SimpleNamespace(click=lambda *a, **k: clicks.append(a))
    monkeypatch.setattr(server, "pyautogui", fake_gui, raising=False)  # not imported on Linux
    monkeypatch.setattr(server, "is_linux", lambda: False)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    quartz = sys.modules["Quartz"]
    quartz.CGMainDisplayID = lambda: 1
    quartz.CGDisplayBounds = lambda did: types.SimpleNamespace(
        origin=types.SimpleNamespace(x=0.0, y=0.0), size=types.SimpleNamespace(width=1440.0, height=900.0)
    )
    quartz.CGDisplayCopyDisplayMode = lambda did: None
    return state, clicks


def test_mac_click_without_accessibility_is_refused_before_any_input(mac_tools):
    state, clicks = mac_tools
    state.post_events = False
    with pytest.raises(ToolError) as exc:
        server.Click(100, 100)
    assert "Accessibility" in str(exc.value)
    assert clicks == []


def test_mac_click_with_accessibility_but_no_screen_recording_still_clicks(mac_tools):
    state, clicks = mac_tools
    state.screen_recording = False  # only a capture needs it
    assert server.Click(100, 100, max_width=0).startswith("Clicked left at (100,100)")
    assert clicks == [(100, 100)]


def test_linux_ocr_without_a_display_is_the_unchanged_stub(monkeypatch, no_subprocess):
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="headless"))
    with pytest.raises(ToolError) as exc:
        server.OCR()
    assert str(exc.value) == _HEADLESS
