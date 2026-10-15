"""Windows / macOS provider geometry, the pyautogui input branch (#28), the wheel (#29)
and Unicode typing (#30).

The Windows and macOS providers are imported here against FAKE OS APIs — the
external boundary only: ``ctypes.windll`` (user32 ``SendInput`` / ``SetCursorPos``
included) / pywin32 (``win32clipboard`` included) / ``ImageGrab.grab`` / pyautogui
on Windows, the ``Quartz`` framework (event creation + posting included) and the
``screencapture`` / ``pbcopy`` / ``pbpaste`` subprocesses on macOS. Everything of
ours (``capture_geometry``, ``capture_frame``, ``scroll``, ``type_unicode``, the
frame math, the tool functions) is the real code.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import struct
import sys
import types
from pathlib import Path

import pytest
from fake_windows_session import WTS_DISCONNECTED, FakeWindowsSession
from fastmcp.exceptions import ToolError
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


def _tool_error(call) -> str:
    """The text of the ToolError a failing tool raises (#32: a failure is never a success string)."""
    with pytest.raises(ToolError) as exc:
        call()
    return str(exc.value)


def _purge(name: str) -> None:
    sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if parent in sys.modules and hasattr(sys.modules[parent], child):
        delattr(sys.modules[parent], child)


class _FakeInput:
    """user32 cursor + ``SendInput``: records every call in order, reading the INPUT array.

    ``raw`` keeps each call's INPUT array as raw bytes, so keyboard events are decoded
    with the Win32 ``KEYBDINPUT`` layout itself, not with the provider's field names.
    """

    def __init__(self):
        self.log: list[tuple] = []
        self.raw: list[tuple[int, bytes]] = []  # (cbSize, the INPUT array's bytes) per SendInput call
        self.inserted: int | None = None  # None = all events accepted
        self.accepted_calls: int | None = None  # N = every SendInput call after the N-th inserts 0
        self.cursor_ok = True

    def SetCursorPos(self, x, y):
        self.log.append(("SetCursorPos", x, y))
        return 1 if self.cursor_ok else 0

    def SendInput(self, count, events, size):
        entries = [(e.type, e.u.mi.dwFlags, e.u.mi.mouseData, e.u.mi.dx, e.u.mi.dy) for e in events[:count]]
        self.log.append(("SendInput", count, entries, size))
        self.raw.append((size, bytes(events)))
        if self.accepted_calls is not None and len(self.raw) > self.accepted_calls:
            return 0
        return count if self.inserted is None else self.inserted


@pytest.fixture
def win(monkeypatch):
    user_input = _FakeInput()
    session = FakeWindowsSession()  # a HEALTHY session: the desktop tools' preflight (#32) passes for real
    windll = types.SimpleNamespace(
        user32=types.SimpleNamespace(
            GetSystemMetrics=lambda i: _METRICS[i],
            SetProcessDpiAwarenessContext=lambda ctx: 1,
            SetProcessDPIAware=lambda: 1,
            SetCursorPos=user_input.SetCursorPos,
            SendInput=user_input.SendInput,
        ),
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda v: 0),
        input=user_input,  # not a DLL: the tests' handle on the recorded input
        session=session,  # not a DLL: the tests' handle on the fake logon session
    )
    session.add_to(windll)
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
    monkeypatch.setattr(mod, "pyautogui", fake, raising=False)  # the provider's own typing path (#30)
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


def test_pyautogui_type_and_drag_start_on_column_zero(pyautogui_branch):
    # x=0 is the frame's left column, not "no point" (#31): image (0,y) → screen (-1920, …)
    server, fake = pyautogui_branch
    server.Type("hi", x=0, y=228)
    server.Move(640, 228, drag=True, start_x=0, start_y=2)
    assert fake.click.calls == [((-1920, 798), {})]
    assert fake.moveTo.calls == [((-1920, 7), {})]


def test_pyautogui_type_point(pyautogui_branch):
    server, fake = pyautogui_branch
    server.Type("hi", x=640, y=228)
    assert fake.click.calls == [((320, 798), {})]


def test_pyautogui_screen_coords_pass_through(pyautogui_branch):
    server, fake = pyautogui_branch
    server.Move(-1500, 20, coords="screen")
    assert fake.moveTo.calls == [((-1500, 20), {"duration": 0.3})]


# --- Windows: the mouse wheel is one SendInput event of clicks × WHEEL_DELTA (#29) ---

_INPUT_MOUSE, _WHEEL, _HWHEEL = 0, 0x0800, 0x1000
# sizeof(INPUT): SendInput rejects any other cbSize (x64: 40, x86: 28).
_SIZEOF_INPUT = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28


def _sent(win_input):
    """The (type, dwFlags, mouseData) of every SendInput event, and the cbSizes it was given."""
    calls = [entry for entry in win_input.log if entry[0] == "SendInput"]
    return [e[:3] for _, _, entries, _ in calls for e in entries], {size for *_, size in calls}


@pytest.mark.parametrize(
    ("direction", "clicks", "flags", "data"),
    [
        ("up", 3, _WHEEL, 360),
        ("down", 3, _WHEEL, -360),
        ("right", 2, _HWHEEL, 240),
        ("left", 2, _HWHEEL, -240),
        ("down", 500, _WHEEL, -12000),  # clamped to 100 clicks
    ],
)
def test_windows_scroll_sends_one_wheel_event(win, direction, clicks, flags, data):
    mod, _ = win
    mod.scroll(direction, clicks)
    events, sizes = _sent(ctypes.windll.input)
    assert events == [(_INPUT_MOUSE, flags, data)]
    assert sizes == {_SIZEOF_INPUT}


def test_windows_scroll_puts_the_cursor_on_the_point_first(win):
    mod, _ = win
    mod.scroll("down", 1, x=-1500, y=0)  # y=0 is a real screen row, not "no point"
    log = ctypes.windll.input.log
    assert log[0] == ("SetCursorPos", -1500, 0)
    assert log[1][0] == "SendInput"


def test_windows_scroll_without_point_leaves_the_cursor(win):
    mod, _ = win
    assert mod.scroll("up", 2) == "Scrolled 2 clicks up"
    assert [entry[0] for entry in ctypes.windll.input.log] == ["SendInput"]


def test_windows_blocked_sendinput_is_an_error(win):
    mod, _ = win
    ctypes.windll.input.inserted = 0  # a locked or disconnected session (a UIPI block is silent)
    # no GetLastError in the text: windll's last error is unreliable, and kernel32 is not faked here
    with pytest.raises(RuntimeError, match=r"^SendInput inserted 0 of 1 wheel events — input is blocked"):
        mod.scroll("up", 1)


def test_windows_failed_setcursorpos_is_an_error_and_sends_nothing(win):
    mod, _ = win
    ctypes.windll.input.cursor_ok = False
    with pytest.raises(RuntimeError, match=r"^SetCursorPos\(-1500,0\) failed — no interactive desktop"):
        mod.scroll("down", 1, x=-1500, y=0)
    assert [entry[0] for entry in ctypes.windll.input.log] == ["SetCursorPos"]


def test_windows_scroll_tool_maps_the_point_and_sends_the_wheel(pyautogui_branch):
    server, fake = pyautogui_branch
    out = server.Scroll(-2, x=640, y=228)  # legacy signed amount → down 2
    assert ctypes.windll.input.log[0] == ("SetCursorPos", 320, 798)
    assert _sent(ctypes.windll.input)[0] == [(_INPUT_MOUSE, _WHEEL, -240)]
    assert out.startswith("Scrolled 2 clicks down [image (640,228) → screen (320,798)")
    assert fake.scroll.calls == [] and fake.moveTo.calls == []  # pyautogui's wheel is never used


def test_windows_legacy_horizontal_scroll_is_horizontal(pyautogui_branch):
    server, fake = pyautogui_branch
    replies = [server.Scroll(2, horizontal=True), server.Scroll(-3, horizontal="true")]
    # pyautogui 0.9.54's hscroll was a VERTICAL raw-delta event on Windows; this is the real tilt wheel
    assert _sent(ctypes.windll.input)[0] == [(_INPUT_MOUSE, _HWHEEL, 240), (_INPUT_MOUSE, _HWHEEL, -360)]
    assert fake.hscroll.calls == [] and fake.scroll.calls == []
    assert replies == ["Scrolled 2 clicks right", "Scrolled 3 clicks left"]


def test_windows_scroll_direction_wins_over_sign_and_horizontal(pyautogui_branch):
    server, _ = pyautogui_branch
    assert server.Scroll(-3, direction="up", horizontal=True) == "Scrolled 3 clicks up"
    assert _sent(ctypes.windll.input)[0] == [(_INPUT_MOUSE, _WHEEL, 360)]


def test_windows_scroll_unknown_direction_sends_nothing(pyautogui_branch):
    server, _ = pyautogui_branch
    out = _tool_error(lambda: server.Scroll(3, direction="sideways"))
    assert out == "Scroll error: direction must be one of up, down, left, right (got 'sideways')"
    assert ctypes.windll.input.log == []


# --- Windows: typing is SendInput + KEYEVENTF_UNICODE, one call per character (#30) ---

_INPUT_KEYBOARD, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 1, 0x0002, 0x0004
_VK_RETURN, _VK_TAB = 0x0D, 0x09
# INPUT's union starts at offset 8 on x64 / 4 on x86 (it holds a ULONG_PTR). KEYBDINPUT inside
# it: wVk WORD @+0, wScan WORD @+2, dwFlags DWORD @+4.
_UNION_OFFSET = ctypes.sizeof(ctypes.c_void_p)


def _key_calls(win_input):
    """Per SendInput call, the (type, wVk, wScan, dwFlags) of each INPUT, decoded from raw bytes."""
    calls = []
    for size, raw in win_input.raw:
        assert size == _SIZEOF_INPUT
        events = []
        for start in range(0, len(raw), _SIZEOF_INPUT):
            (kind,) = struct.unpack_from("<I", raw, start)
            vk, scan, flags = struct.unpack_from("<HHI", raw, start + _UNION_OFFSET)
            events.append((kind, vk, scan, flags))
        calls.append(events)
    return calls


def _packet(unit):
    """The down + up of one UTF-16 code unit sent as a Unicode packet."""
    return [
        (_INPUT_KEYBOARD, 0, unit, _KEYEVENTF_UNICODE),
        (_INPUT_KEYBOARD, 0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP),
    ]


def _vkey(code):
    return [(_INPUT_KEYBOARD, code, 0, 0), (_INPUT_KEYBOARD, code, 0, _KEYEVENTF_KEYUP)]


_SLOVAK = "Žltý kôň ďakuje"  # 15 characters, all in the BMP: one code unit each


def test_windows_type_unicode_sends_a_unicode_packet_pair_per_character(win):
    mod, _ = win
    assert mod.type_unicode(_SLOVAK) == 15
    calls = _key_calls(ctypes.windll.input)
    assert calls[0] == _packet(0x017D)  # Ž
    assert calls[9] == _packet(0x010F)  # ď
    assert calls == [_packet(ord(char)) for char in _SLOVAK]


def test_windows_type_unicode_emoji_is_one_call_with_a_surrogate_pair(win):
    mod, _ = win
    assert mod.type_unicode("😀") == 1
    assert _key_calls(ctypes.windll.input) == [_packet(0xD83D) + _packet(0xDE00)]


def test_windows_type_unicode_enter_and_tab_are_virtual_keys(win):
    mod, _ = win
    assert mod.type_unicode("a\tb\r\n") == 5  # CRLF = one Enter, counted as its two characters
    assert _key_calls(ctypes.windll.input) == [_packet(0x61), _vkey(_VK_TAB), _packet(0x62), _vkey(_VK_RETURN)]


def test_windows_type_unicode_waits_a_little_between_characters(win, monkeypatch):
    mod, _ = win
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    mod.type_unicode("abc")
    assert len(sleeps) == 2 and all(0 < s <= 0.05 for s in sleeps)


def test_windows_short_sendinput_is_an_error_naming_what_got_through(win):
    mod, _ = win
    ctypes.windll.input.accepted_calls = 3  # the 4th SendInput inserts 0: the session locked mid-text
    with pytest.raises(
        RuntimeError, match=r"^SendInput inserted 0 of 2 key events after 3 of 8 chars — input is blocked"
    ):
        mod.type_unicode("Žltý kôň")
    assert len(_key_calls(ctypes.windll.input)) == 4  # stopped at the refused character


def test_windows_type_tool_sends_every_slovak_character(pyautogui_branch):
    server, fake = pyautogui_branch
    out = server.Type(_SLOVAK)
    assert _key_calls(ctypes.windll.input) == [_packet(ord(char)) for char in _SLOVAK]
    assert out == "Typed 15 chars"
    assert fake.typewrite.calls == [] and fake.write.calls == []  # pyautogui's ASCII-only key map is never used


def test_windows_type_tool_reports_blocked_input_as_an_error_not_typed(pyautogui_branch):
    server, _ = pyautogui_branch
    ctypes.windll.input.inserted = 0
    out = _tool_error(lambda: server.Type("ab"))
    assert out.startswith("Type error: SendInput inserted 0 of 2 key events after 0 of 2 chars")
    assert "Typed" not in out


def test_windows_type_tool_clicks_clears_types_and_presses_enter(pyautogui_branch):
    server, fake = pyautogui_branch
    out = server.Type("ď", x=640, y=228, clear=True, press_enter=True)
    assert fake.click.calls == [((320, 798), {})]
    assert fake.hotkey.calls == [(("ctrl", "a"), {})]
    assert fake.press.calls == [(("delete",), {}), (("enter",), {})]
    assert _key_calls(ctypes.windll.input) == [_packet(0x010F)]
    assert out.startswith("Typed 1 chars [image (640,228) → screen (320,798)")


def test_type_tool_unknown_method_is_an_error_and_does_nothing(pyautogui_branch):
    server, fake = pyautogui_branch
    out = _tool_error(lambda: server.Type("x", x=640, y=228, method="clipboard"))
    assert out == "Type error: method must be 'keys' or 'paste' (got 'clipboard')"
    assert fake.click.calls == [] and ctypes.windll.input.raw == []


class _FakeClipboard:
    """pywin32 ``win32clipboard`` with one CF_UNICODETEXT slot (``None`` = no text on it)."""

    def __init__(self, text):
        self.text = text
        self.writes: list[str] = []

    def OpenClipboard(self):
        pass

    def CloseClipboard(self):
        pass

    def IsClipboardFormatAvailable(self, fmt):
        return self.text is not None

    def EmptyClipboard(self):
        self.text = None

    def GetClipboardData(self, fmt):
        if self.text is None:
            raise TypeError("Specified clipboard format is not available")
        return self.text

    def SetClipboardText(self, text, fmt):
        self.text = text
        self.writes.append(text)


@pytest.fixture
def win_clipboard(monkeypatch, pyautogui_branch):
    """``method="paste"`` on Windows: a fake clipboard, and the paste keys see what it holds."""
    server, fake = pyautogui_branch
    clip = _FakeClipboard("old clip")
    module = sys.modules["win32clipboard"]
    for name in (
        "OpenClipboard",
        "CloseClipboard",
        "IsClipboardFormatAvailable",
        "EmptyClipboard",
        "GetClipboardData",
        "SetClipboardText",
    ):
        monkeypatch.setattr(module, name, getattr(clip, name), raising=False)
    monkeypatch.setattr(sys.modules["win32con"], "CF_UNICODETEXT", 13, raising=False)
    pressed: list[tuple] = []
    fake.hotkey = lambda *keys: pressed.append((keys, clip.text))
    return server, clip, pressed


def test_windows_paste_method_pastes_then_restores_the_clipboard(win_clipboard):
    server, clip, pressed = win_clipboard
    out = server.Type("Žltý kôň", method="paste")
    assert pressed == [(("ctrl", "v"), "Žltý kôň")]  # the text was on the clipboard at Ctrl+V
    assert clip.writes == ["Žltý kôň", "old clip"] and clip.text == "old clip"
    assert out == "Pasted 8 chars (clipboard restored)"
    assert ctypes.windll.input.raw == []  # no keystrokes


def test_windows_paste_method_says_so_when_the_clipboard_held_no_text(win_clipboard):
    server, clip, pressed = win_clipboard
    clip.text = None
    out = server.Type("ďakujem", method="paste")
    assert pressed == [(("ctrl", "v"), "ďakujem")]
    assert out == "Pasted 7 chars (the clipboard held no text, so it was not restored)"


def test_windows_paste_method_refused_clipboard_is_an_error_before_the_paste_keys(win_clipboard, monkeypatch):
    server, _, pressed = win_clipboard

    def _refuse(text, fmt):
        raise OSError("OpenClipboard failed")

    monkeypatch.setattr(sys.modules["win32clipboard"], "SetClipboardText", _refuse)
    out = _tool_error(lambda: server.Type("x", method="paste"))
    assert out == "Type error: could not put the text on the clipboard: OpenClipboard failed"
    assert pressed == []


def test_windows_paste_reports_a_failed_restore(win_clipboard, monkeypatch):
    server, clip, pressed = win_clipboard
    write = clip.SetClipboardText

    def _second_write_refused(text, fmt):
        if clip.writes:  # the restore is the second write
            raise OSError("OpenClipboard failed")
        write(text, fmt)

    monkeypatch.setattr(sys.modules["win32clipboard"], "SetClipboardText", _second_write_refused)
    out = server.Type("ďakujem", method="paste")
    assert pressed == [(("ctrl", "v"), "ďakujem")]
    assert out == "Pasted 7 chars, but restoring the clipboard failed: OpenClipboard failed"


def test_windows_paste_restores_the_clipboard_when_the_paste_keys_fail(win_clipboard, pyautogui_branch):
    server, clip, _ = win_clipboard
    _, fake = pyautogui_branch

    def _broken_keyboard(*keys):
        raise OSError("keyboard is gone")

    fake.hotkey = _broken_keyboard
    out = _tool_error(lambda: server.Type("ďakujem", method="paste"))
    assert out == "Type error: keyboard is gone"
    assert clip.writes == ["ďakujem", "old clip"] and clip.text == "old clip"


def test_windows_provider_type_text_clicks_a_point_on_row_zero(pyautogui_branch):
    server, fake = pyautogui_branch
    mod = server.get_desktop()
    assert mod.type_text("ď", x=-1500, y=0) == "Typed 1 chars"
    assert fake.click.calls == [((-1500, 0), {})]  # y=0 is a real screen row, not "no point"
    assert _key_calls(ctypes.windll.input) == [_packet(0x010F)]


def test_type_tool_logs_the_count_never_the_text(pyautogui_branch, caplog):
    server, _ = pyautogui_branch
    with caplog.at_level(logging.DEBUG, logger="remoteos.mcp"):
        assert server.Type("tajné heslo 123") == "Typed 15 chars"
    assert "Type 15 chars by keys" in caplog.text
    assert "tajné" not in caplog.text and "heslo" not in caplog.text


# --- Windows: a failed grab is retried once after reconnecting the RDP session ---


def _flaky_grab(monkeypatch, failures: int):
    real = ImageGrab.grab  # the fixture's fake grab
    state = {"left": failures}

    def _grab(*args, **kwargs):
        if state["left"]:
            state["left"] -= 1
            raise OSError("screen grab failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(ImageGrab, "grab", _grab)


@pytest.fixture
def windows_tool_layer(monkeypatch, win):
    import remoteos.__main__ as server

    mod, _ = win
    monkeypatch.setattr(server, "is_windows", lambda: True)
    monkeypatch.setattr(server, "is_linux", lambda: False)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)  # tscon's 1 s settle wait
    commands: list[list[str]] = []
    return server, commands


def _session_tools(monkeypatch, server, commands, readable=True):
    """The server's own session (id 2) is disconnected; ``tscon 2 /dest:console`` attaches it.

    The session is read through ProcessIdToSessionId + WTSConnectState (#32 review round 1),
    never parsed out of ``query session``: a real disconnected session's line there has a
    blank SESSIONNAME (``>   drlikzbynek   2  Disc``), which the old parser skipped.
    """
    session = ctypes.windll.session
    session.session_id, session.connect_state = 2, WTS_DISCONNECTED
    session.fail_session_id = not readable

    def _run(cmd, **kwargs):
        commands.append(list(cmd))
        return session.tscon(cmd)  # only tscon may run

    monkeypatch.setattr(server.subprocess, "run", _run)


def test_windows_capture_retries_after_session_reconnect(monkeypatch, windows_tool_layer):
    server, commands = windows_tool_layer
    _session_tools(monkeypatch, server, commands)
    _flaky_grab(monkeypatch, failures=1)
    img, frame = server._capture_snapshot_frame(0, 1280)
    assert img.size == frame.image_size == (1280, 457)
    assert commands == [["tscon", "2", "/dest:console"]]


def test_windows_capture_error_surfaces_when_not_a_session_issue(monkeypatch, windows_tool_layer):
    server, commands = windows_tool_layer
    _session_tools(monkeypatch, server, commands, readable=False)  # session state unreadable → no reconnect
    _flaky_grab(monkeypatch, failures=1)
    with pytest.raises(OSError, match="screen grab failed"):
        server._capture_snapshot_frame(0, 1280)
    assert commands == []


def test_windows_capture_reports_failed_retry(monkeypatch, windows_tool_layer):
    server, commands = windows_tool_layer
    _session_tools(monkeypatch, server, commands)
    _flaky_grab(monkeypatch, failures=2)
    with pytest.raises(RuntimeError, match="after session reconnect: screen grab failed"):
        server._capture_snapshot_frame(0, 1280)


def test_session_reconnect_helper_is_defined_once():
    # A second module-level def silently replaced the first, leaving a dead copy (#31).
    import ast

    import remoteos.__main__ as server

    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_session_connected"]
    assert len(defs) == 1
    assert [a.arg for a in defs[0].args.args] == ["force"]  # the effective copy (ReconnectSession's force)


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
    # Quartz event API (#29): created events are plain dicts, every post is recorded in order.
    quartz.kCGScrollEventUnitLine, quartz.kCGHIDEventTap = 1, 0
    quartz.kCGEventMouseMoved, quartz.kCGMouseButtonLeft = 5, 0
    quartz.posted = []
    quartz.CGEventCreateScrollWheelEvent = lambda src, unit, count, *wheels: {
        "scroll": (unit, count, *wheels),
        "location": None,
    }
    quartz.CGEventCreateMouseEvent = lambda src, kind, point, button: {"mouse": (kind, tuple(point), button)}
    quartz.CGEventSetLocation = lambda event, point: event.update(location=tuple(point))
    # Keyboard events (#30): (keycode, key down?) and the Unicode string set on the event.
    quartz.CGEventCreateKeyboardEvent = lambda src, keycode, down: {"key": (keycode, down), "unicode": None}
    quartz.CGEventKeyboardSetUnicodeString = lambda event, length, text: event.update(unicode=(length, text))
    quartz.CGPreflightPostEventAccess = lambda: True  # the process may post events (Privacy & Security)
    quartz.CGEventPost = lambda tap, event: quartz.posted.append((tap, event))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "pyautogui", types.ModuleType("pyautogui"))
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


# --- macOS: the wheel is Quartz line events, one line per click (#29) ---


def _scrolls(quartz):
    return [event["scroll"] for _, event in quartz.posted if "scroll" in event]


@pytest.mark.parametrize(
    ("direction", "clicks", "wheels"),
    [
        ("up", 3, [(1, 2, 3, 0)]),  # line units, 2 wheels, wheel1 = vertical (+ = up)
        ("down", 3, [(1, 2, -3, 0)]),
        ("left", 2, [(1, 2, 0, 2)]),  # Quartz wheel2: POSITIVE = LEFT
        ("right", 2, [(1, 2, 0, -2)]),
        ("down", 25, [(1, 2, -10, 0), (1, 2, -10, 0), (1, 2, -5, 0)]),  # at most 10 lines per event
    ],
)
def test_mac_scroll_posts_line_events(mac, direction, clicks, wheels):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    assert mod.scroll(direction, clicks) == f"Scrolled {clicks} clicks {direction}"
    assert _scrolls(quartz) == wheels
    assert {tap for tap, _ in quartz.posted} == {0}  # the HID event tap
    assert all(event["location"] is None for _, event in quartz.posted)


def test_mac_scroll_at_a_point_moves_there_and_locates_every_event(mac):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    mod.scroll("up", 12, x=720, y=450)
    first, *scrolls = [event for _, event in quartz.posted]
    assert first == {"mouse": (5, (720, 450), 0)}  # a mouse-moved event to the point first
    assert [e["scroll"] for e in scrolls] == [(1, 2, 10, 0), (1, 2, 2, 0)]
    assert [e["location"] for e in scrolls] == [(720, 450), (720, 450)]


@pytest.mark.parametrize("factory", ["CGEventCreateMouseEvent", "CGEventCreateScrollWheelEvent"])
def test_mac_failed_event_creation_is_an_error_and_posts_nothing(mac, monkeypatch, factory):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    monkeypatch.setattr(quartz, factory, lambda *args: None)
    with pytest.raises(RuntimeError, match=f"^{factory} returned no event"):
        mod.scroll("up", 3, x=720, y=450)
    assert quartz.posted == []


# --- macOS: typing is keyboard events carrying a Unicode string, <= 20 UTF-16 units each (#30) ---

_KVK_RETURN, _KVK_TAB = 0x24, 0x30


def _keys_posted(quartz):
    """((keycode, down), (length, unicode string) | None) of every posted keyboard event."""
    return [(event["key"], event["unicode"]) for _, event in quartz.posted if "key" in event]


def _typed_strings(quartz):
    return [unicode for (_, down), unicode in _keys_posted(quartz) if down]


def test_mac_type_unicode_posts_the_text_and_a_real_return(mac):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    assert mod.type_unicode("Žltý kôň\n") == 9
    assert _keys_posted(quartz) == [
        ((0, True), (8, "Žltý kôň")),
        ((0, False), (8, "Žltý kôň")),
        ((_KVK_RETURN, True), None),
        ((_KVK_RETURN, False), None),
    ]
    assert {tap for tap, _ in quartz.posted} == {0}  # the HID event tap


def test_mac_type_unicode_tab_is_the_tab_keycode(mac):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    mod.type_unicode("a\tb")
    assert _keys_posted(quartz) == [
        ((0, True), (1, "a")),
        ((0, False), (1, "a")),
        ((_KVK_TAB, True), None),
        ((_KVK_TAB, False), None),
        ((0, True), (1, "b")),
        ((0, False), (1, "b")),
    ]


def test_mac_type_unicode_chunks_at_20_units_without_splitting_a_surrogate_pair(mac):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    assert mod.type_unicode("a" * 19 + "😀b") == 21
    # the emoji is 2 UTF-16 units: 19 + 2 > 20, so it starts the next event, whole
    assert _typed_strings(quartz) == [(19, "a" * 19), (3, "😀b")]


def test_mac_failed_keyboard_event_is_an_error_and_posts_nothing(mac, monkeypatch):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    monkeypatch.setattr(quartz, "CGEventCreateKeyboardEvent", lambda *args: None)
    with pytest.raises(RuntimeError, match="^CGEventCreateKeyboardEvent returned no event"):
        mod.type_unicode("ab\ncd")
    assert quartz.posted == []


def test_mac_type_unicode_without_the_posting_permission_is_an_error_and_posts_nothing(mac, monkeypatch):
    mod, _ = mac
    quartz = sys.modules["Quartz"]
    # CGEventPost returns nothing: without this permission macOS drops the events silently
    monkeypatch.setattr(quartz, "CGPreflightPostEventAccess", lambda: False)
    with pytest.raises(RuntimeError, match=r"^macOS does not let this process post keyboard events"):
        mod.type_unicode("Žltý")
    assert quartz.posted == []


@pytest.fixture
def mac_tool_layer(monkeypatch, mac):
    import remoteos.__main__ as server

    mod, _ = mac
    fake = types.SimpleNamespace(
        scroll=_Recorder(),
        hscroll=_Recorder(),
        moveTo=_Recorder(),
        click=_Recorder(),
        hotkey=_Recorder(),
        press=_Recorder(),
        typewrite=_Recorder(),
        write=_Recorder(),
    )
    monkeypatch.setattr(server, "pyautogui", fake, raising=False)
    monkeypatch.setattr(mod, "pyautogui", fake, raising=False)  # the provider's own typing path (#30)
    monkeypatch.setattr(server, "is_linux", lambda: False)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    return server, fake, sys.modules["Quartz"]


def test_mac_legacy_horizontal_scroll_is_horizontal(mac_tool_layer):
    server, fake, quartz = mac_tool_layer
    assert server.Scroll(2, horizontal=True) == "Scrolled 2 clicks right"
    assert _scrolls(quartz) == [(1, 2, 0, -2)]
    assert fake.hscroll.calls == [] and fake.scroll.calls == []


def test_mac_scroll_tool_maps_the_image_point_to_points(mac_tool_layer):
    server, _, quartz = mac_tool_layer
    server.Scroll(3, direction="down", x=640, y=400)  # main display 1440x900 pt, image 1280x800
    _, first = quartz.posted[0]
    assert first == {"mouse": (5, (720, 450), 0)}
    assert _scrolls(quartz) == [(1, 2, -3, 0)]


def test_mac_type_tool_types_slovak_text_through_quartz(mac_tool_layer):
    server, fake, quartz = mac_tool_layer
    assert server.Type(_SLOVAK) == "Typed 15 chars"
    assert _typed_strings(quartz) == [(15, _SLOVAK)]
    assert fake.typewrite.calls == [] and fake.write.calls == []  # pyautogui's ASCII-only key map is never used


def test_mac_clear_selects_all_with_command_not_ctrl(mac_tool_layer):
    server, fake, _ = mac_tool_layer
    server.Type("x", clear=True)
    # Ctrl+A in a Cocoa text field moves the caret to the paragraph start; forward-delete then ate one character
    assert fake.hotkey.calls == [(("command", "a"), {})]
    assert fake.press.calls == [(("delete",), {})]


def _mac_pasteboard(monkeypatch, server, fake, text):
    """``pbpaste`` / ``pbcopy`` over one text slot; the paste keys record what it holds when pressed."""
    board = {"text": text, "writes": [], "pressed": []}

    def _run(cmd, **kwargs):
        if cmd == ["pbpaste"]:
            return types.SimpleNamespace(returncode=0, stdout=board["text"], stderr="")
        if cmd == ["pbcopy"]:
            board["text"] = kwargs["input"].decode("utf-8")
            board["writes"].append(board["text"])
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(server.get_desktop().subprocess, "run", _run)
    fake.hotkey = lambda *keys: board["pressed"].append((keys, board["text"]))
    return board


def test_mac_paste_method_uses_command_v_and_restores_the_clipboard(mac_tool_layer, monkeypatch):
    server, fake, quartz = mac_tool_layer
    board = _mac_pasteboard(monkeypatch, server, fake, "old clip")
    out = server.Type("Žltý kôň", method="paste")
    assert board["pressed"] == [(("command", "v"), "Žltý kôň")]
    assert board["writes"] == ["Žltý kôň", "old clip"]
    assert out == "Pasted 8 chars (clipboard restored)"
    assert _keys_posted(quartz) == []


def test_mac_paste_with_an_empty_or_non_text_clipboard_says_it_was_not_restored(mac_tool_layer, monkeypatch):
    server, fake, _ = mac_tool_layer
    # pbpaste prints "" for an empty clipboard AND for an image: there is no text to put back
    board = _mac_pasteboard(monkeypatch, server, fake, "")
    out = server.Type("Žltý", method="paste")
    assert board["writes"] == ["Žltý"]  # no "restore" that would only wipe the clipboard
    assert out == "Pasted 4 chars (the clipboard held no text, so it was not restored)"


def test_mac_paste_without_the_posting_permission_is_an_error_and_touches_nothing(mac_tool_layer, monkeypatch):
    server, fake, quartz = mac_tool_layer
    board = _mac_pasteboard(monkeypatch, server, fake, "old clip")
    monkeypatch.setattr(quartz, "CGPreflightPostEventAccess", lambda: False)
    # pyautogui's Command+V is a CGEventPost too: macOS would drop it silently behind "Pasted 4 chars"
    out = _tool_error(lambda: server.Type("Žltý", x=640, y=400, clear=True, method="paste"))
    # the Type tool's preflight (#32) refuses first, with the same CGPreflightPostEventAccess predicate
    assert out.startswith("macOS does not let this process control the mouse and keyboard")
    assert board["writes"] == [] and board["pressed"] == []
    assert fake.click.calls == [] and fake.press.calls == []


def test_mac_keys_without_the_posting_permission_clicks_and_clears_nothing(mac_tool_layer, monkeypatch):
    server, fake, quartz = mac_tool_layer
    monkeypatch.setattr(quartz, "CGPreflightPostEventAccess", lambda: False)
    out = _tool_error(lambda: server.Type("Žltý", x=640, y=400, clear=True, press_enter=True))
    # the Type tool's preflight (#32) refuses first, with the same CGPreflightPostEventAccess predicate
    assert out.startswith("macOS does not let this process control the mouse and keyboard")
    assert fake.click.calls == [] and fake.hotkey.calls == [] and fake.press.calls == []
    assert quartz.posted == []


def test_mac_type_tool_clicks_clears_pastes_and_presses_enter_in_order(mac_tool_layer, monkeypatch):
    server, fake, _ = mac_tool_layer
    board = _mac_pasteboard(monkeypatch, server, fake, "old clip")
    order = []
    fake.click = lambda *point: order.append(("click", point))
    fake.hotkey = lambda *keys: order.append(("hotkey", keys, board["text"]))
    fake.press = lambda *keys: order.append(("press", keys))
    server.Type("ď", x=640, y=400, clear=True, press_enter=True, method="paste")
    assert order == [
        ("click", (720, 450)),  # image (640,400) on the 1440x900-point main display
        ("hotkey", ("command", "a"), "old clip"),
        ("press", ("delete",)),
        ("hotkey", ("command", "v"), "ď"),
        ("press", ("enter",)),
    ]
    assert board["text"] == "old clip"


def test_mac_provider_type_text_clicks_a_point_on_column_zero(mac_tool_layer):
    server, fake, quartz = mac_tool_layer
    mod = server.get_desktop()
    assert mod.type_text("ď", x=0, y=10) == "Typed 1 chars"
    assert fake.click.calls == [((0, 10), {})]  # x=0 is a real screen column, not "no point"
    assert _typed_strings(quartz) == [(1, "ď")]
