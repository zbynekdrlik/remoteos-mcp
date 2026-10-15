"""Desktop diagnoses (#32): every provider's ``diagnose(capture)`` and the desktop tools' preflight.

The providers run against FAKE OS APIs, the external boundary only:

- Windows: ``ctypes.windll`` with kernel32 ``ProcessIdToSessionId``, user32
  ``GetProcessWindowStation`` / ``OpenInputDesktop`` / ``GetUserObjectInformationW``
  / ``CloseDesktop`` and wtsapi32 ``WTSQuerySessionInformationW`` /
  ``WTSFreeMemory``. The WTS buffers are laid out with the Win32 offsets as
  literals, never with the provider's own struct.
- macOS: the ``Quartz`` framework (``CGSessionCopyCurrentDictionary``,
  ``CGPreflightScreenCaptureAccess``) and the ApplicationServices library
  (``AXIsProcessTrusted``).
- Linux: the logind ``LockedHint`` query (``_run_capture``) and the cached session.

``diagnose()`` must NEVER false-positive: a healthy desktop and a probe that
fails are both "usable". Only a positive finding stops a tool.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import os
import struct
import sys
import types

import pytest
from fastmcp.exceptions import ToolError
from PIL import Image, ImageGrab

import remoteos.__main__ as server
from remoteos.platform.linux import desktop as linux_desktop

_HEADLESS = "Not available on headless Linux (no display)"
_WAYLAND = "Not available: Wayland session detected (X11 backend only — Wayland support is a separate tier)"
_LOCKED_START = "The screen is locked: screenshots show the lock screen and mouse/keyboard input goes nowhere."


def _purge(name: str) -> None:
    sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if parent in sys.modules and hasattr(sys.modules[parent], child):
        delattr(sys.modules[parent], child)


class _Fn:
    """A fake DLL function: callable, records its calls, and takes restype/argtypes like a ctypes one."""

    def __init__(self, impl):
        self.impl = impl
        self.calls: list[tuple] = []
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.impl(*args)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

_WTS_CONNECT_STATE, _WTS_SESSION_INFO_EX = 8, 25  # WTS_INFO_CLASS
_WTS_ACTIVE, _WTS_DISCONNECTED = 0, 4  # WTS_CONNECTSTATE_CLASS
_LOCK, _UNLOCK = 0, 1  # WTSINFOEX_LEVEL1_W.SessionFlags
_DESKTOP_HANDLE, _WINSTA_HANDLE = 0x1A4, 0x2C8


class _FakeWindowsSession:
    """One Windows logon session as the Win32 / WTS APIs report it."""

    def __init__(self):
        self.session_id = 1
        self.window_station = "WinSta0"
        self.connect_state = _WTS_ACTIVE
        self.input_desktop: str | None = "Default"  # None = OpenInputDesktop fails (lock screen / UAC)
        self.lock_flags: int | None = _UNLOCK  # None = WTSSessionInfoEx fails
        self.fail_session_id = False
        self.buffers: list = []  # keeps the WTS buffers alive until freed
        self.freed: list[int] = []
        self.closed: list[int] = []
        self.open_access: list[int] = []

    # kernel32
    def ProcessIdToSessionId(self, pid, sid_ref):
        if self.fail_session_id:
            raise OSError("ProcessIdToSessionId is gone")
        sid_ref._obj.value = self.session_id
        return 1

    # user32
    def OpenInputDesktop(self, flags, inherit, access):
        self.open_access.append(access)
        return _DESKTOP_HANDLE if self.input_desktop is not None else None

    def GetUserObjectInformationW(self, handle, index, buf, size, needed_ref):
        assert index == 2  # UOI_NAME
        name = {_DESKTOP_HANDLE: self.input_desktop, _WINSTA_HANDLE: self.window_station}[handle.value]
        buf.value = name
        return 1

    def CloseDesktop(self, handle):
        self.closed.append(handle.value)
        return 1

    # wtsapi32
    def WTSQuerySessionInformationW(self, server_handle, sid, info_class, buf_ref, size_ref):
        assert sid == self.session_id
        if info_class == _WTS_CONNECT_STATE:
            raw = struct.pack("<i", self.connect_state)
        elif info_class == _WTS_SESSION_INFO_EX:
            if self.lock_flags is None:
                return 0
            # WTSINFOEXW: DWORD Level; then an 8-aligned union (it holds LARGE_INTEGERs) whose
            # WTSINFOEX_LEVEL1_W starts with SessionId, SessionState, SessionFlags.
            raw = struct.pack("<IIIii", 1, 0, sid, self.connect_state, self.lock_flags) + bytes(200)
        else:
            return 0
        buf = ctypes.create_string_buffer(raw, len(raw))
        self.buffers.append(buf)
        buf_ref._obj.value = ctypes.addressof(buf)
        size_ref._obj.value = len(raw)
        return 1

    def WTSFreeMemory(self, pointer):
        self.freed.append(pointer.value if hasattr(pointer, "value") else pointer)


@pytest.fixture
def win_session(monkeypatch):
    """The Windows provider imported against a fake Win32 API; yields ``(provider, session)``."""
    session = _FakeWindowsSession()
    windll = types.SimpleNamespace(
        kernel32=types.SimpleNamespace(
            GetCurrentProcessId=_Fn(lambda: 4242),
            ProcessIdToSessionId=_Fn(session.ProcessIdToSessionId),
        ),
        user32=types.SimpleNamespace(
            GetSystemMetrics=lambda i: {76: 0, 77: 0, 78: 1920, 79: 1080}[i],
            SetProcessDpiAwarenessContext=lambda ctx: 1,
            SetProcessDPIAware=lambda: 1,
            SetCursorPos=lambda x, y: 1,
            GetProcessWindowStation=_Fn(lambda: _WINSTA_HANDLE),
            OpenInputDesktop=_Fn(session.OpenInputDesktop),
            GetUserObjectInformationW=_Fn(session.GetUserObjectInformationW),
            CloseDesktop=_Fn(session.CloseDesktop),
        ),
        wtsapi32=types.SimpleNamespace(
            WTSQuerySessionInformationW=_Fn(session.WTSQuerySessionInformationW),
            WTSFreeMemory=_Fn(session.WTSFreeMemory),
        ),
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda v: 0),
    )
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
    assert mod.diagnose(capture=True) is None
    assert mod.diagnose(capture=False) is None
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
    session.connect_state = _WTS_DISCONNECTED
    reason = mod.diagnose()
    assert "disconnected" in reason and "ReconnectSession" in reason


def test_windows_locked_screen(win_session):
    mod, session = win_session
    session.input_desktop = None  # the lock screen runs on the Winlogon desktop: OpenInputDesktop fails
    session.lock_flags = _LOCK
    assert mod.diagnose().startswith(_LOCKED_START)
    assert mod.diagnose(capture=True).startswith(_LOCKED_START)
    assert len(session.freed) == len(session.buffers)


def test_windows_secure_desktop_is_the_uac_prompt_when_the_session_is_not_locked(win_session):
    mod, session = win_session
    session.input_desktop = None
    session.lock_flags = _UNLOCK
    reason = mod.diagnose()
    assert reason.startswith("The screen shows a secure desktop (a UAC prompt, Ctrl+Alt+Del or the sign-in screen)")


def test_windows_secure_desktop_is_named_when_it_can_be_opened(win_session):
    mod, session = win_session
    session.input_desktop = "Winlogon"  # running as SYSTEM opens it
    session.lock_flags = _UNLOCK
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
        self.accessibility = True
        self.ax_library = True


@pytest.fixture
def mac_session(monkeypatch):
    state = _FakeMac()
    quartz = types.ModuleType("Quartz")
    quartz.kCGSessionOnConsoleKey = "kCGSSessionOnConsoleKey"
    quartz.CGSessionCopyCurrentDictionary = lambda: state.session
    quartz.CGPreflightScreenCaptureAccess = lambda: state.screen_recording
    quartz.CGPreflightPostEventAccess = lambda: True
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "pyautogui", types.ModuleType("pyautogui"))
    _purge("remoteos.platform.mac.desktop")
    mod = importlib.import_module("remoteos.platform.mac.desktop")

    def _application_services():
        if not state.ax_library:
            raise OSError("dlopen(ApplicationServices) failed")
        return types.SimpleNamespace(AXIsProcessTrusted=_Fn(lambda: state.accessibility))

    from remoteos.platform.mac import availability

    monkeypatch.setattr(availability, "_load_application_services", _application_services)
    yield mod, state
    _purge("remoteos.platform.mac.desktop")


def test_mac_healthy_desktop_is_no_diagnosis(mac_session):
    mod, _ = mac_session
    assert mod.diagnose(capture=True) is None
    assert mod.diagnose(capture=False) is None


def test_mac_without_screen_recording_cannot_capture(mac_session):
    mod, state = mac_session
    state.screen_recording = False
    reason = mod.diagnose(capture=True)
    assert "Screen Recording" in reason and os.path.realpath(sys.executable) in reason
    assert mod.diagnose(capture=False) is None  # input does not need Screen Recording


def test_mac_without_accessibility_cannot_send_input(mac_session):
    mod, state = mac_session
    state.accessibility = False
    reason = mod.diagnose(capture=False)
    assert "Accessibility" in reason and os.path.realpath(sys.executable) in reason
    assert mod.diagnose(capture=True) is None  # a screenshot does not need Accessibility


def test_mac_locked_screen(mac_session):
    mod, state = mac_session
    state.session = {"kCGSSessionOnConsoleKey": True, "CGSSessionScreenIsLocked": True}
    assert mod.diagnose(capture=True).startswith(_LOCKED_START)
    assert mod.diagnose(capture=False).startswith(_LOCKED_START)


def test_mac_without_a_gui_session(mac_session):
    mod, state = mac_session
    state.session = None  # CGSessionCopyCurrentDictionary: NULL outside a window-server session
    assert mod.diagnose().startswith("No macOS GUI session")


def test_mac_session_not_on_the_console(mac_session):
    mod, state = mac_session
    state.session = {"kCGSSessionOnConsoleKey": False}  # fast user switching: another user has the screen
    assert "not on the screen" in mod.diagnose()


def test_mac_unreadable_accessibility_state_is_usable(mac_session):
    mod, state = mac_session
    state.ax_library = False
    assert mod.diagnose(capture=False) is None


def test_mac_without_the_screen_capture_preflight_is_usable(mac_session, monkeypatch):
    mod, _ = mac_session
    monkeypatch.delattr(sys.modules["Quartz"], "CGPreflightScreenCaptureAccess")  # before macOS 10.15
    assert mod.diagnose(capture=True) is None


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _x11(session_id: str | None = "7"):
    return linux_desktop.SessionInfo(mode="x11", display=":0", uid=1000, user="newlevel", session_id=session_id)


@pytest.fixture
def logind(monkeypatch):
    """``loginctl show-session <id> -p LockedHint --value``: records the argv, answers ``reply``."""
    state = {"calls": [], "reply": (0, "no\n", "")}

    def _run_capture(cmd, timeout=10):
        state["calls"].append(list(cmd))
        return state["reply"]

    monkeypatch.setattr(linux_desktop, "_run_capture", _run_capture)
    return state


def test_linux_headless_and_wayland_diagnoses_are_the_unchanged_stubs(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="headless"))
    assert linux_desktop.diagnose(capture=True) == _HEADLESS
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="wayland"))
    assert linux_desktop.diagnose(capture=False) == _WAYLAND
    assert logind["calls"] == []


def test_linux_unlocked_x11_session_is_no_diagnosis(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", _x11())
    assert linux_desktop.diagnose() is None
    assert logind["calls"] == [["loginctl", "show-session", "7", "-p", "LockedHint", "--value"]]


def test_linux_locked_x11_session(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", _x11())
    logind["reply"] = (0, "yes\n", "")
    assert linux_desktop.diagnose(capture=True).startswith(_LOCKED_START)


def test_linux_unreadable_lock_hint_is_usable(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", _x11())
    logind["reply"] = (1, "", "Failed to get session: No session '7' known")
    assert linux_desktop.diagnose() is None


def test_linux_session_without_a_logind_id_is_not_probed(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", _x11(session_id=None))
    assert linux_desktop.diagnose() is None
    assert logind["calls"] == []


# ---------------------------------------------------------------------------
# The preflight: which tools run it, and with which kind
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
    """A provider whose diagnose() always finds a problem, and records how it was asked."""

    HAS_WIN32 = True

    def __init__(self):
        super().__init__(asked=[])

    def diagnose(self, capture=False):
        self.asked.append(capture)
        return f"diagnosed (capture={capture})"

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
    assert str(exc.value) == "diagnosed (capture=True)"
    assert diagnosing.asked == [True]


@pytest.mark.parametrize("name", sorted(_INPUT_TOOLS))
def test_input_tools_stop_on_an_input_diagnosis(diagnosing, name):
    with pytest.raises(ToolError) as exc:
        getattr(server, name)(**_INPUT_TOOLS[name])
    assert str(exc.value) == "diagnosed (capture=False)"
    assert diagnosing.asked == [False]


@pytest.mark.parametrize("name", sorted(_NO_PREFLIGHT_TOOLS))
def test_other_desktop_tools_do_not_run_the_preflight(diagnosing, name):
    # e.g. the clipboard and notifications work on a locked Windows session
    getattr(server, name)(**_NO_PREFLIGHT_TOOLS[name])
    assert diagnosing.asked == []


def test_the_preflight_logs_its_result(diagnosing, caplog):
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        with pytest.raises(ToolError):
            server.Click(1, 1)
    assert "desktop unavailable (input): diagnosed (capture=False)" in caplog.text


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
    return session, clicks


def test_windows_click_on_a_locked_screen_is_refused_before_any_input(windows_tools):
    session, clicks = windows_tools
    session.input_desktop, session.lock_flags = None, _LOCK
    with pytest.raises(ToolError) as exc:
        server.Click(640, 360)
    assert str(exc.value).startswith(_LOCKED_START)
    assert clicks == []


def test_windows_click_on_a_healthy_desktop_still_clicks(windows_tools):
    session, clicks = windows_tools
    assert server.Click(640, 360, max_width=0).startswith("Clicked left at (640,360)")
    assert clicks == [((640, 360), {"button": "left"})]


def _fake_session_commands(monkeypatch, session, commands):
    """``query session`` lists the server's own session as disconnected; ``tscon`` reattaches it."""

    def _run(cmd, **kwargs):
        commands.append(list(cmd))
        if cmd[:2] == ["query", "session"]:
            out = " SESSIONNAME   USERNAME   ID  STATE\n rdp-tcp#5     newlevel    1  Disc\n"
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        session.connect_state = _WTS_ACTIVE  # tscon <id> /dest:console
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server.subprocess, "run", _run)


def test_windows_snapshot_reconnects_a_disconnected_session_then_captures(monkeypatch, windows_tools):
    session, _ = windows_tools
    session.connect_state = _WTS_DISCONNECTED
    commands: list[list[str]] = []
    _fake_session_commands(monkeypatch, session, commands)
    parts = server.Snapshot()
    assert ["tscon", "1", "/dest:console"] in commands
    assert any(getattr(p, "type", None) == "image" for p in parts)


def test_windows_click_on_a_disconnected_session_is_refused_without_moving_the_session(monkeypatch, windows_tools):
    session, clicks = windows_tools
    session.connect_state = _WTS_DISCONNECTED
    commands: list[list[str]] = []
    _fake_session_commands(monkeypatch, session, commands)
    with pytest.raises(ToolError) as exc:
        server.Click(640, 360)
    assert "ReconnectSession" in str(exc.value)
    assert commands == [] and clicks == []


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
    state.accessibility = False
    with pytest.raises(ToolError) as exc:
        server.Click(100, 100)
    assert "Accessibility" in str(exc.value)
    assert clicks == []


def test_mac_click_with_accessibility_but_no_screen_recording_still_clicks(mac_tools):
    state, clicks = mac_tools
    state.screen_recording = False  # only a capture needs it
    assert server.Click(100, 100, max_width=0).startswith("Clicked left at (100,100)")
    assert clicks == [(100, 100)]


def test_linux_ocr_without_a_display_is_the_unchanged_stub(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="headless"))
    with pytest.raises(ToolError) as exc:
        server.OCR()
    assert str(exc.value) == _HEADLESS


def test_linux_click_on_a_locked_x11_session_is_refused_before_xdotool(monkeypatch, logind):
    monkeypatch.setattr(linux_desktop, "_SESSION", _x11())
    logind["reply"] = (0, "yes\n", "")

    def _no_x(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("an X command ran on a locked session")

    monkeypatch.setattr(linux_desktop, "_run_x", _no_x)
    with pytest.raises(ToolError) as exc:
        server.Click(10, 10)
    assert str(exc.value).startswith(_LOCKED_START)
