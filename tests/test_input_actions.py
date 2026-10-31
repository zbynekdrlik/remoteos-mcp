"""Computer-use actions (#34): the real tools through the real providers.

Linux runs on the fake X11 display of ``test_coordinate_contract_tools`` (only ``_run_x`` is
stubbed), Windows and macOS on the fake OS APIs of ``test_provider_geometry``, so the argv and the
events each OS really gets are what these tests read. Expected codes are literals.
"""

from __future__ import annotations

import collections.abc
import ctypes
import struct
import types

import pytest
import test_coordinate_contract_tools as linux_contract
import test_provider_geometry as provider_geometry
from PIL import Image
from tool_surface import server
from tool_test_support import tool_error

from remoteos import input_keys, ocr
from remoteos.platform.linux import session as linux_session
from remoteos.taskmanager import TOOL_CATEGORIES, ToolCategory
from remoteos.tiers import LINUX_DISPLAY_TOOLS, TOOL_TIERS

# The fixtures of the two modules these tests extend.
fake_x11 = linux_contract.fake_x11
win, mac = provider_geometry.win, provider_geometry.mac
pyautogui_branch, mac_tool_layer = provider_geometry.pyautogui_branch, provider_geometry.mac_tool_layer


def _xdotool(calls) -> list[list[str]]:
    return [c[1:] for c in calls if c[:1] == ["xdotool"]]


class _Desktop:
    """Records ``key_down`` / ``key_up``; a key in ``refuse`` fails on the way down or up."""

    def __init__(self, refuse_down=(), refuse_up=()):
        self.log: list[tuple[str, str]] = []
        self.refuse_down, self.refuse_up = refuse_down, refuse_up

    def check_key(self, key):
        if key == "unknown":
            raise ValueError("Unknown key")

    def key_down(self, key):
        if key in self.refuse_down:
            raise RuntimeError("no down")
        self.log.append(("down", key))

    def key_up(self, key):
        if key in self.refuse_up:
            raise RuntimeError("no up")
        self.log.append(("up", key))


# ---------------------------------------------------------------------------
# held_keys: the guaranteed release
# ---------------------------------------------------------------------------


def test_held_keys_presses_in_order_and_releases_in_reverse():
    desktop = _Desktop()
    with input_keys.held_keys(["ctrl", "shift"], desktop):
        desktop.log.append(("body", ""))
    assert desktop.log == [("down", "ctrl"), ("down", "shift"), ("body", ""), ("up", "shift"), ("up", "ctrl")]


def test_held_keys_releases_when_the_body_fails_and_reports_the_body():
    desktop = _Desktop()
    with pytest.raises(ValueError, match="body"):
        with input_keys.held_keys(["ctrl"], desktop):
            raise ValueError("body")
    assert desktop.log == [("down", "ctrl"), ("up", "ctrl")]


def test_held_keys_releases_what_was_pressed_when_a_later_press_fails():
    desktop = _Desktop(refuse_down=("shift",))
    with pytest.raises(RuntimeError, match="no down"):
        with input_keys.held_keys(["ctrl", "shift"], desktop):
            pytest.fail("the body must not run")
    assert desktop.log == [("down", "ctrl"), ("up", "ctrl")]


def test_held_keys_with_an_unknown_key_presses_nothing():
    desktop = _Desktop()
    with pytest.raises(ValueError, match="Unknown key"):
        with input_keys.held_keys(["ctrl", "unknown"], desktop):
            pytest.fail("the body must not run")
    assert desktop.log == []


def test_held_keys_a_failed_release_still_releases_the_rest_and_raises():
    desktop = _Desktop(refuse_up=("shift",))
    with pytest.raises(RuntimeError, match="could not release shift"):
        with input_keys.held_keys(["ctrl", "shift"], desktop):
            pass
    assert ("up", "ctrl") in desktop.log


def test_held_keys_a_failed_release_never_hides_the_body_failure():
    desktop = _Desktop(refuse_up=("ctrl",))
    with pytest.raises(ValueError, match="body"):
        with input_keys.held_keys(["ctrl"], desktop):
            raise ValueError("body")


@pytest.mark.parametrize(
    ("given", "keys"),
    [
        ("", []),
        (" ", []),
        ("ctrl+shift", ["ctrl", "shift"]),
        ("Control + WIN", ["ctrl", "super"]),
        ("CTRL", ["ctrl"]),
        ("cmd", ["super"]),
        ("Page_Up", ["Page_Up"]),  # an X keysym keeps its case
    ],
)
def test_split_keys(given, keys):
    assert input_keys.split_keys(given) == keys


@pytest.mark.parametrize("bad", ["ctrl+", "+", "a++b"])
def test_split_keys_refuses_an_empty_part(bad):
    with pytest.raises(ValueError):
        input_keys.split_keys(bad)


def test_modifiers_must_be_modifiers():
    assert input_keys.check_modifiers("option+shift") == ["alt", "shift"]
    with pytest.raises(ValueError, match="Not a modifier: a"):
        input_keys.check_modifiers("ctrl+a")


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def test_linux_triple_click_is_three_clicks_at_the_mapped_point(fake_x11):
    out = server.Click(640, 400, action="triple")
    assert _xdotool(fake_x11) == [["mousemove", "1280", "800", "click", "--repeat", "3", "--delay", "120", "1"]]
    assert out.startswith("Triple-clicked left at (1280,800)")


@pytest.mark.parametrize(("action", "verb", "word"), [("down", "mousedown", "Pressed"), ("up", "mouseup", "Released")])
def test_linux_mouse_down_and_up(fake_x11, action, verb, word):
    out = server.Click(100, 50, button="right", action=action)
    assert _xdotool(fake_x11) == [["mousemove", "200", "100", verb, "3"]]
    assert out.startswith(f"{word} right at (200,100)")


def test_linux_click_modifiers_wrap_the_click(fake_x11):
    server.Click(100, 50, modifiers="ctrl+shift")
    assert _xdotool(fake_x11) == [
        ["keydown", "ctrl"],
        ["keydown", "shift"],
        ["mousemove", "200", "100", "click", "1"],
        ["keyup", "shift"],
        ["keyup", "ctrl"],
    ]


def test_linux_modifiers_are_released_when_the_click_fails(fake_x11, monkeypatch):
    real = linux_session._run_x

    def _failing_click(cmd, **kwargs):
        if "click" in cmd:
            fake_x11.append(list(cmd))
            raise RuntimeError("xdotool failed")
        return real(cmd, **kwargs)

    monkeypatch.setattr(linux_session, "_run_x", _failing_click)
    assert "xdotool failed" in tool_error(lambda: server.Click(100, 50, modifiers="ctrl"))
    assert _xdotool(fake_x11)[-1] == ["keyup", "ctrl"]


def test_linux_scroll_modifiers_wrap_the_wheel(fake_x11):
    server.Scroll(2, direction="up", modifiers="ctrl")
    assert _xdotool(fake_x11) == [["keydown", "ctrl"], ["click", "--repeat", "2", "4"], ["keyup", "ctrl"]]


def test_linux_shortcut_repeat(fake_x11):
    assert server.Shortcut("down", repeat=3) == "Executed shortcut: down x3"
    server.Shortcut("ctrl+c")
    assert _xdotool(fake_x11) == [["key", "--repeat", "3", "Down"], ["key", "ctrl+c"]]


def test_linux_hold_key_holds_for_the_time_and_releases(fake_x11, monkeypatch):
    slept = []
    monkeypatch.setattr(server.time, "sleep", lambda s: slept.append((s, list(_xdotool(fake_x11)))))
    assert server.HoldKey("shift+F5", seconds=2) == "Held shift+F5 for 2s"
    assert slept == [(2, [["keydown", "shift"], ["keydown", "F5"]])]
    assert _xdotool(fake_x11)[2:] == [["keyup", "F5"], ["keyup", "shift"]]


def test_linux_a_key_xdotool_does_not_know_is_an_error(fake_x11, monkeypatch):
    # xdotool prints "No such key name" and exits 0 (real xdotool 3.20160805)
    real = linux_session._run_x

    def _ignoring(cmd, **kwargs):
        reply = real(cmd, **kwargs)
        if cmd[1:2] == ["keydown"] and cmd[2] == "nosuchkey":
            reply.stderr = b"(symbol) No such key name 'nosuchkey'. Ignoring it."
        return reply

    monkeypatch.setattr(linux_session, "_run_x", _ignoring)
    assert "Unknown key: nosuchkey" in tool_error(lambda: server.HoldKey("ctrl+nosuchkey"))
    assert _xdotool(fake_x11)[-1] == ["keyup", "ctrl"]


def test_linux_hold_key_keeps_a_keysyms_case(fake_x11, monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server.HoldKey("Page_Up", seconds=1)
    assert _xdotool(fake_x11) == [["keydown", "Page_Up"], ["keyup", "Page_Up"]]


def test_linux_hold_key_of_a_capital_adds_no_shift(fake_x11, monkeypatch):
    # `xdotool keydown A` holds Shift too; Windows and macOS hold only the letter
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server.HoldKey("ctrl+W", seconds=1)
    assert _xdotool(fake_x11) == [["keydown", "ctrl"], ["keydown", "w"], ["keyup", "w"], ["keyup", "ctrl"]]


@pytest.mark.parametrize(("key", "keysym"), [("pgup", "Prior"), ("pgdn", "Next"), ("enter", "Return"), ("f5", "F5")])
def test_linux_a_key_name_means_the_same_in_shortcut_and_hold_key(fake_x11, monkeypatch, key, keysym):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server.Shortcut(key)
    server.HoldKey(key, seconds=1)
    assert _xdotool(fake_x11) == [["key", keysym], ["keydown", keysym], ["keyup", keysym]]


def test_linux_held_keys_are_not_logged():
    assert linux_session._loggable(["xdotool", "keydown", "p"]) == ["xdotool", "keydown", "<key>"]
    assert linux_session._loggable(["sudo", "xdotool", "keyup", "p"]) == ["sudo", "xdotool", "keyup", "<key>"]


def test_linux_cursor_position_reports_both_spaces(fake_x11):
    out = server.CursorPosition()
    assert out == "Cursor at image (640,400), screen (1280,800) (monitor=0, max_width=1280)"


def test_linux_cursor_position_marks_a_cursor_off_the_frame(fake_x11, monkeypatch):
    monkeypatch.setattr(server.get_desktop(), "cursor_position", lambda: (9000, 800))
    assert "(off-frame)" in server.CursorPosition()


def test_linux_cursor_position_without_an_answer_is_an_error(fake_x11, monkeypatch):
    monkeypatch.setattr(
        linux_session, "_run_x", lambda cmd, **kw: types.SimpleNamespace(stdout=b"", stderr=b"", returncode=0)
    )
    assert "no position" in tool_error(server.CursorPosition)


@pytest.mark.parametrize(
    ("call", "fragment"),
    [
        (lambda: server.Click(1, 1, action="quadruple"), "Unknown action"),
        (lambda: server.Click(1, 1, button="side"), "Unknown button"),
        (lambda: server.Click(1, 1, modifiers="a"), "Not a modifier"),
        (lambda: server.Scroll(1, direction="up", modifiers="a"), "Not a modifier"),
        (lambda: server.Shortcut("a", repeat=0), "repeat must be"),
        (lambda: server.Shortcut("a", repeat=101), "repeat must be"),
        (lambda: server.HoldKey("", seconds=1), "No keys"),
        (lambda: server.HoldKey("a", seconds=0), "seconds must be"),
    ],
)
def test_a_bad_argument_is_refused_before_any_input(fake_x11, call, fragment):
    assert fragment in tool_error(call)
    assert _xdotool(fake_x11) == []


def test_hold_key_is_capped_at_30_seconds(fake_x11, monkeypatch):
    slept = []
    monkeypatch.setattr(server.time, "sleep", slept.append)
    assert server.HoldKey("a", seconds=90) == "Held a for 30.0s (capped at 30s)"
    assert slept == [30.0]
    assert _xdotool(fake_x11) == [["keydown", "a"], ["keyup", "a"]]


# ---------------------------------------------------------------------------
# Windows: SendInput
# ---------------------------------------------------------------------------


def _key_events() -> list[tuple[int, int]]:
    """``(wVk, dwFlags)`` per SendInput call, decoded with the Win32 offsets (x64: union at 8)."""
    out = []
    for _size, raw in ctypes.windll.input.raw:
        kind, vk, _scan, flags = struct.unpack_from("<I4xHHI", raw)
        assert kind == 1  # INPUT_KEYBOARD
        out.append((vk, flags))
    return out


def _mouse_flags() -> list[int]:
    return [entries[0][1] for _name, _count, entries, _size in _sends()]


def _sends():
    return [entry for entry in ctypes.windll.input.log if entry[0] == "SendInput"]


def test_windows_triple_click_goes_through_pyautogui(pyautogui_branch):
    server_, fake = pyautogui_branch
    out = server_.Click(640, 228, action="triple")
    assert fake.tripleClick.calls == [((320, 798), {"button": "left"})]
    assert out.startswith("Triple-clicked left at (320,798)")


@pytest.mark.parametrize(
    ("button", "action", "flag"),
    [
        ("left", "down", 0x0002),
        ("left", "up", 0x0004),
        ("right", "down", 0x0008),
        ("right", "up", 0x0010),
        ("middle", "down", 0x0020),
        ("middle", "up", 0x0040),
    ],
)
def test_windows_mouse_buttons_are_sendinput_at_the_point(pyautogui_branch, button, action, flag):
    server_, _ = pyautogui_branch
    server_.Click(640, 228, button=button, action=action)
    assert ctypes.windll.input.log[0] == ("SetCursorPos", 320, 798)
    assert _mouse_flags() == [flag]


def test_windows_mouse_button_on_a_blocked_desktop_is_an_error(pyautogui_branch):
    server_, _ = pyautogui_branch
    ctypes.windll.input.inserted = 0
    assert "input is blocked" in tool_error(lambda: server_.Click(640, 228, action="down"))


def test_windows_modifiers_are_sendinput_keys_around_the_click(pyautogui_branch):
    server_, fake = pyautogui_branch
    order = []
    fake.click = lambda *a, **k: order.append(list(_key_events()))
    server_.Click(640, 228, modifiers="ctrl+shift")
    assert order == [[(0x11, 0), (0x10, 0)]]
    assert _key_events() == [(0x11, 0), (0x10, 0), (0x10, 2), (0x11, 2)]


def test_windows_modifier_is_released_when_the_click_fails(pyautogui_branch):
    server_, fake = pyautogui_branch

    def _broken(*a, **k):
        raise OSError("no click")

    fake.click = _broken
    assert "no click" in tool_error(lambda: server_.Click(640, 228, modifiers="ctrl"))
    assert _key_events() == [(0x11, 0), (0x11, 2)]


@pytest.mark.parametrize(
    ("key", "vk", "extended"),
    [("a", 0x41, 0), ("7", 0x37, 0), ("f5", 0x74, 0), ("left", 0x25, 1), ("win", 0x5B, 1), ("enter", 0x0D, 0)],
)
def test_windows_hold_key_codes(pyautogui_branch, key, vk, extended):
    server_, _ = pyautogui_branch
    assert server_.HoldKey(key, seconds=1) == f"Held {key} for 1s"
    assert _key_events() == [(vk, extended), (vk, extended | 2)]


def test_windows_unknown_key_in_a_combination_sends_nothing(pyautogui_branch):
    server_, _ = pyautogui_branch
    assert "Unknown key: ?" in tool_error(lambda: server_.HoldKey("ctrl+?"))
    assert _sends() == []


def test_windows_key_names_are_case_insensitive(pyautogui_branch):
    server_, _ = pyautogui_branch
    server_.HoldKey("F5", seconds=1)
    assert _key_events() == [(0x74, 0), (0x74, 2)]


def test_windows_shortcut_repeat(pyautogui_branch):
    server_, fake = pyautogui_branch
    assert server_.Shortcut("ctrl+z", repeat=3) == "Executed shortcut: ctrl+z x3"
    assert fake.hotkey.calls == [(("ctrl", "z"), {})] * 3


def test_windows_cursor_position_maps_through_the_frame(pyautogui_branch):
    server_, _ = pyautogui_branch
    assert server_.CursorPosition().startswith("Cursor at image (640,228), screen (320,798)")


def test_windows_cursor_position_on_a_blocked_desktop_is_an_error(pyautogui_branch):
    server_, _ = pyautogui_branch
    ctypes.windll.input.cursor_ok = False
    assert "GetCursorPos failed" in tool_error(server_.CursorPosition)


# ---------------------------------------------------------------------------
# macOS: Quartz
# ---------------------------------------------------------------------------


def _quartz():
    import sys

    return sys.modules["Quartz"]


def _posted_mouse() -> list[dict]:
    return [event for _, event in _quartz().posted if "mouse" in event]


def test_mac_plain_click_stays_pyautogui(mac_tool_layer):
    server_, fake, *_ = mac_tool_layer
    server_.Click(720, 450, coords="screen")
    assert fake.click.calls == [((720, 450), {"button": "left"})]
    assert _quartz().posted == []


@pytest.mark.parametrize(("action", "states"), [("double", [1, 1, 2, 2]), ("triple", [1, 1, 2, 2, 3, 3])])
def test_mac_plain_double_and_triple_click_count_for_the_app(mac_tool_layer, action, states):
    # pyautogui sets no kCGMouseEventClickState: the app would see single clicks
    server_, fake, *_ = mac_tool_layer
    server_.Click(720, 450, action=action, coords="screen")
    assert fake.doubleClick.calls == [] and fake.tripleClick.calls == []
    assert [e.get("field1") for e in _posted_mouse()][1:] == states


def test_mac_modified_triple_click_carries_the_flags_and_the_click_count(mac_tool_layer):
    server_, fake, *_ = mac_tool_layer
    out = server_.Click(720, 450, action="triple", coords="screen", modifiers="cmd+shift")
    assert fake.tripleClick.calls == [] and fake.click.calls == []
    kinds = [e["mouse"][0] for e in _posted_mouse()]
    assert kinds == [5, 1, 2, 1, 2, 1, 2]
    assert [e.get("field1") for e in _posted_mouse()][1:] == [1, 1, 2, 2, 3, 3]
    assert {e["flags"] for e in _posted_mouse()} == {0x100000 | 0x20000}
    assert out.startswith("Triple-clicked left at (720,450)")


def test_mac_modifier_key_is_posted_around_the_click_and_flags_end_with_it(mac_tool_layer):
    server_, *_ = mac_tool_layer
    server_.Click(720, 450, coords="screen", modifiers="alt")
    keys = [e["key"] for _, e in _quartz().posted if "key" in e]
    assert keys == [(0x3A, True), (0x3A, False)]
    assert _quartz().posted[-1][1].get("flags") is None  # the release carries no flag any more
    server_.Scroll(1, direction="up")
    assert "flags" not in _quartz().posted[-1][1]


def test_mac_scroll_under_a_modifier_carries_the_flag(mac_tool_layer):
    server_, *_ = mac_tool_layer
    server_.Scroll(2, direction="up", modifiers="ctrl")
    wheel = [e for _, e in _quartz().posted if "scroll" in e]
    assert [e["flags"] for e in wheel] == [0x40000]


@pytest.mark.parametrize(
    ("button", "code", "down", "up", "dragged"),
    [("left", 0, 1, 2, 6), ("right", 1, 3, 4, 7), ("middle", 2, 25, 26, 27)],
)
def test_mac_down_move_up_is_a_real_drag(mac_tool_layer, button, code, down, up, dragged):
    server_, fake, *_ = mac_tool_layer
    server_.Click(10, 20, button=button, action="down", coords="screen")
    server_.Move(30, 40, coords="screen")
    server_.Click(30, 40, button=button, action="up", coords="screen")
    server_.Move(50, 60, coords="screen")
    assert [e["mouse"] for e in _posted_mouse()] == [
        (5, (10.0, 20.0), code),
        (down, (10.0, 20.0), code),
        (dragged, (30.0, 40.0), code),
        (dragged, (30.0, 40.0), code),
        (up, (30.0, 40.0), code),
    ]
    assert fake.moveTo.calls == [((50, 60), {"duration": 0.3})]  # released: an ordinary move again


def test_mac_up_elsewhere_and_hover_drag_while_a_button_is_held(mac_tool_layer):
    server_, fake, *_ = mac_tool_layer
    server_.Click(10, 20, action="down", coords="screen")
    server_.Click(30, 40, action="hover", coords="screen")
    server_.Click(50, 60, action="up", coords="screen")
    assert [e["mouse"][0] for e in _posted_mouse()] == [5, 1, 6, 6, 2]
    assert fake.moveTo.calls == []


def test_mac_a_single_click_while_a_button_is_held_drags_there(mac_tool_layer):
    server_, fake, *_ = mac_tool_layer
    server_.Click(10, 20, action="down", coords="screen")
    server_.Click(30, 40, button="right", coords="screen")
    assert fake.click.calls == []
    assert [e["mouse"][0] for e in _posted_mouse()] == [5, 1, 6, 3, 4]


def test_mac_releasing_another_button_keeps_the_held_one(mac_tool_layer):
    server_, fake, *_ = mac_tool_layer
    server_.Click(10, 20, action="down", coords="screen")
    server_.Click(10, 20, button="right", action="up", coords="screen")
    server_.Move(30, 40, coords="screen")
    assert _posted_mouse()[-1]["mouse"][0] == 6  # the left button is still down: a drag
    assert fake.moveTo.calls == []


def test_mac_unknown_key_in_a_combination_posts_nothing(mac_tool_layer):
    server_, *_ = mac_tool_layer
    assert "Unknown key: ?" in tool_error(lambda: server_.HoldKey("ctrl+?"))
    assert _quartz().posted == []


def test_mac_without_posting_permission_nothing_is_held_or_pressed(mac_tool_layer):
    server_, *_ = mac_tool_layer
    _quartz().CGPreflightPostEventAccess = lambda: False
    assert "Accessibility" in tool_error(lambda: server_.Click(10, 20, action="down", coords="screen"))
    assert _quartz().posted == []


@pytest.mark.parametrize(
    ("key", "code"), [("w", 13), ("6", 22), ("5", 23), ("i", 34), ("m", 46), ("f5", 0x60), ("up", 0x7E), ("esc", 0x35)]
)
def test_mac_hold_key_codes(mac_tool_layer, key, code):
    server_, *_ = mac_tool_layer
    server_.HoldKey(key, seconds=1)
    assert [e["key"] for _, e in _quartz().posted] == [(code, True), (code, False)]


def test_mac_cursor_position_is_points(mac_tool_layer):
    server_, *_ = mac_tool_layer
    assert server_.CursorPosition().startswith("Cursor at image (640,400), screen (720,450)")


# ---------------------------------------------------------------------------
# Wait and WaitFor
# ---------------------------------------------------------------------------


def test_wait_is_capped_at_300_seconds(monkeypatch):
    slept = []
    monkeypatch.setattr(server.time, "sleep", slept.append)
    assert server.Wait(seconds=301) == "Waited 300.0s (capped at 300s)"
    assert server.Wait(seconds=2.5) == "Waited 2.5s"
    assert slept == [300.0, 2.5]


def test_waiting_never_takes_the_desktop_lock():
    assert TOOL_CATEGORIES["Wait"] is ToolCategory.WAIT
    assert TOOL_CATEGORIES["WaitFor"] is ToolCategory.WAIT


def test_new_tools_are_gated_and_categorised():
    assert {"HoldKey", "CursorPosition", "WaitFor"} <= LINUX_DISPLAY_TOOLS
    assert "Wait" not in LINUX_DISPLAY_TOOLS  # a headless box keeps it
    assert "HoldKey" in TOOL_TIERS["tier2"]
    assert {"CursorPosition", "WaitFor"} <= TOOL_TIERS["tier1"]
    assert TOOL_CATEGORIES["HoldKey"] is ToolCategory.DESKTOP  # input: exclusive
    assert TOOL_CATEGORIES["CursorPosition"] is ToolCategory.QUERY


def test_wait_for_does_not_wait_behind_a_running_desktop_tool():
    from remoteos.taskmanager import TaskManager

    tm = TaskManager()
    desktop_slot = tm._thread_semaphores[ToolCategory.DESKTOP]
    assert desktop_slot.acquire(timeout=1)  # a Click is running
    assert tm.wrap_sync_tool("WaitFor", lambda: "done")().endswith("done")
    assert tm.wrap_sync_tool("Wait", lambda: "done")().endswith("done")


@pytest.fixture
def screen(fake_x11, monkeypatch):
    """The OCR text per poll; the last one stays. Records every sleep."""
    state = types.SimpleNamespace(texts=["nothing"], regions=[], sleeps=[])

    def _run_ocr(**region):
        state.regions.append(region)
        return state.texts.pop(0) if len(state.texts) > 1 else state.texts[0]

    monkeypatch.setattr(server.ocr, "run_ocr", _run_ocr)
    monkeypatch.setattr(server.time, "sleep", state.sleeps.append)
    return state


def test_wait_for_text_polls_until_it_shows(screen):
    screen.texts = ["Loading", "Loading", "Header\n  Save  FINISHED ok\nFooter"]
    out = server.WaitFor(text="finished  OK", timeout=5)
    assert len(screen.regions) == 3
    assert screen.sleeps == [0.5, 0.5]
    assert out.startswith("Found after") and out.endswith("text on monitor 0, line: Save  FINISHED ok")


def test_wait_for_text_across_an_ocr_line_break_still_matches(screen):
    screen.texts = ["Save\nchanges now"]
    assert "text on monitor 0" in server.WaitFor(text="save changes", timeout=0)


def test_wait_for_text_on_a_monitor_reads_only_that_monitor(screen, fake_x11):
    screen.texts = ["ready"]
    server.WaitFor(text="ready", monitor=1, timeout=0)
    assert screen.regions == [{"left": 0, "top": 0, "right": 2560, "bottom": 1600}]


def test_wait_for_timeout_is_a_clear_error_without_the_tool_prefix(screen):
    assert tool_error(lambda: server.WaitFor(text="absent", timeout=0)) == (
        "WaitFor timed out after 0s: no text 'absent'"
    )


def test_wait_for_really_stops_at_the_deadline(fake_x11, monkeypatch):
    monkeypatch.setattr(server.ocr, "run_ocr", lambda **region: "nothing")
    error = tool_error(lambda: server.WaitFor(text="absent", timeout=0.02))
    assert "timed out" in error


def test_wait_for_window_reports_its_rect_in_image_pixels(screen):
    out = server.WaitFor(window="edit", timeout=0)
    assert out.endswith("window (image pixels):\n  [67108874] Editor (400x300 at 50,25)")
    assert screen.regions == []  # no OCR for a window


def test_wait_for_a_failing_window_list_is_an_error_at_once(screen, monkeypatch):
    def _no_wmctrl(cmd, **kwargs):
        if cmd[:1] == ["wmctrl"]:
            raise RuntimeError("wmctrl not installed")
        return real(cmd, **kwargs)

    real = linux_session._run_x
    monkeypatch.setattr(linux_session, "_run_x", _no_wmctrl)
    assert "wmctrl not installed" in tool_error(lambda: server.WaitFor(window="x", timeout=60))
    assert screen.sleeps == []


@pytest.mark.parametrize("monitor", [-1, 9])
def test_wait_for_a_monitor_that_does_not_exist_fails_before_waiting(screen, monitor):
    assert tool_error(lambda: server.WaitFor(window="edit", monitor=monitor)).startswith("WaitFor error:")
    assert screen.sleeps == []


def test_wait_for_window_rect_uses_the_frame_at_the_match(screen, monkeypatch):
    frames = []
    real = server._current_frame
    monkeypatch.setattr(server, "_current_frame", lambda *a: frames.append(a) or real(*a))
    server.WaitFor(window="edit", max_width=640, timeout=0)
    assert frames == [(0, 640), (0, 640)]  # the early check, then the frame of the reply


def test_wait_for_window_timeout(screen):
    assert "no window titled 'Absent'" in tool_error(lambda: server.WaitFor(window="Absent", timeout=0))


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"text": "  "}, {"text": "a", "window": "b"}, {"text": "a", "timeout": -1}, {"text": "a", "timeout": 301}],
)
def test_wait_for_bad_arguments(screen, kwargs):
    assert tool_error(lambda: server.WaitFor(**kwargs)).startswith("WaitFor error:")
    assert screen.regions == []


def test_wait_for_ocr_failure_ends_the_wait_at_once(screen, monkeypatch):
    def _broken(**region):
        raise RuntimeError("OCR failed")

    monkeypatch.setattr(server.ocr, "run_ocr", _broken)
    assert "OCR failed" in tool_error(lambda: server.WaitFor(text="a", timeout=60))
    assert screen.sleeps == []


@pytest.mark.parametrize(("kwargs", "need"), [({"text": "a"}, "capture"), ({"window": "a"}, "windows")])
def test_wait_for_preflight_follows_what_it_waits_for(screen, monkeypatch, kwargs, need):
    asked = []
    desktop = types.SimpleNamespace(diagnose=lambda n: asked.append(n) or f"diagnosed ({n})")
    monkeypatch.setattr(server, "get_desktop", lambda: desktop)
    assert tool_error(lambda: server.WaitFor(**kwargs)) == f"diagnosed ({need})"
    assert asked == [need]


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def test_ocr_of_another_monitor_grabs_from_all_screens(monkeypatch):
    # without all_screens PIL crops the bbox out of a primary-only grab: a black region
    grabs = []

    def _grab(bbox=None, all_screens=False):
        grabs.append((bbox, all_screens))
        return Image.new("RGB", (8, 8))

    monkeypatch.setattr(ocr, "ImageGrab", types.SimpleNamespace(grab=_grab))
    ocr._screenshot_region(-1920, 0, 0, 1080)
    ocr._screenshot_region()
    assert grabs == [((-1920, 0, 0, 1080), True), (None, True)]


class _VanishingEnviron(collections.abc.Mapping):
    """``os.environ`` while ``_load_atspi`` pops a key in another thread: listed, then gone."""

    def __iter__(self):
        return iter(["PATH", "DISPLAY"])

    def __len__(self):
        return 2

    def __getitem__(self, key):
        if key == "DISPLAY":
            raise KeyError(key)
        return "/bin"


def test_an_x_command_survives_a_key_popped_while_the_environment_is_copied(monkeypatch):
    # Wait and WaitFor run outside the desktop lock (#34), next to the AT-SPI window
    fake_os = types.SimpleNamespace(environ=_VanishingEnviron(), geteuid=lambda: 1000)
    monkeypatch.setattr(linux_session, "os", fake_os)
    info = linux_session.SessionInfo(mode="x11", display=":0", uid=1000, user="newlevel", session_id="1")
    argv, env = linux_session._build_invocation(info, ["wmctrl", "-lpG"])
    assert argv == ["wmctrl", "-lpG"]
    assert env["PATH"] == "/bin" and env["DISPLAY"] == ":0"
