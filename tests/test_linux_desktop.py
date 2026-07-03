"""Tests for the Linux desktop provider.

Three concerns:

1. Pure session-detection logic (env matrix, loginctl parsing, session choice).
2. Headless regression contract — on a box with no display EVERY desktop function
   still returns the stub message / empty result and NEVER crashes (protects the
   headless cam1..4 boxes from regression).
3. X11 command construction — the right ``xdotool`` / ``wmctrl`` / ``xclip`` argv
   is built, without needing a real display (``_run_x`` is stubbed).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from remoteos.platform.linux import desktop as d

_MSG = "Not available on headless Linux (no display)"


# ---------------------------------------------------------------------------
# 1) Pure session detection logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, None),
        ({"DISPLAY": ":0"}, "x11"),
        ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, "x11"),
        ({"XDG_SESSION_TYPE": "x11"}, "x11"),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
        ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
        # XWayland: compositor is Wayland even though DISPLAY is also set → wayland
        ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, "wayland"),
        ({"XDG_SESSION_TYPE": "tty"}, None),
        ({"SSH_CONNECTION": "x"}, None),
    ],
)
def test_mode_from_env(env, expected):
    assert d._mode_from_env(env) == expected


def test_parse_show_session():
    text = "Id=1\nUser=1000\nName=newlevel\nType=x11\nClass=user\nActive=yes\nState=active\nDisplay=\n"
    props = d._parse_show_session(text)
    assert props == {
        "Id": "1",
        "User": "1000",
        "Name": "newlevel",
        "Type": "x11",
        "Class": "user",
        "Active": "yes",
        "State": "active",
        "Display": "",
    }


def _sess(sid, stype, sclass, active, state="active", user="1000"):
    return {"Id": sid, "User": user, "Type": stype, "Class": sclass, "Active": active, "State": state}


def test_choose_session_picks_active_x11_user():
    # Mirrors the real imag-nb layout: an x11 user session (active), a tty user
    # session, and an inactive gdm greeter. Must pick the x11 user session.
    sessions = [
        _sess("1", "x11", "user", "yes"),
        _sess("75", "tty", "user", "yes"),
        _sess("c1", "x11", "greeter", "no", state="online", user="120"),
    ]
    chosen = d._choose_session(sessions)
    assert chosen is not None
    assert chosen["Id"] == "1"
    assert chosen["Type"] == "x11"


def test_choose_session_prefers_wayland_when_no_x11():
    sessions = [_sess("3", "wayland", "user", "yes"), _sess("9", "tty", "user", "yes")]
    chosen = d._choose_session(sessions)
    assert chosen is not None and chosen["Id"] == "3"


def test_choose_session_none_when_only_tty_and_greeter():
    sessions = [
        _sess("9", "tty", "user", "yes"),
        _sess("c1", "x11", "greeter", "no", state="online", user="120"),
    ]
    assert d._choose_session(sessions) is None


def test_choose_session_skips_inactive_x11():
    sessions = [_sess("1", "x11", "user", "no", state="closing")]
    assert d._choose_session(sessions) is None


def test_parse_wmctrl_windows():
    # wmctrl -lpG columns: winid desktop pid x y w h host title
    text = (
        "0x03000007  0 2776 0    0    1920 1080 imag-pc Desktop\n"
        "0x04200003  0 3210 100  50   800  600  imag-pc gedit — file.txt\n"
        "malformed line\n"
    )
    wins = d._parse_wmctrl_windows(text)
    assert len(wins) == 2
    assert wins[0].handle == 0x03000007
    assert wins[0].pid == 2776
    assert wins[0].rect == (0, 0, 1920, 1080)
    assert wins[0].title == "Desktop"
    assert wins[1].title == "gedit — file.txt"
    assert wins[1].rect == (100, 50, 900, 650)
    assert wins[1].width == 800 and wins[1].height == 600


# ---------------------------------------------------------------------------
# 2) Headless regression contract (cam1..4 must never change)
# ---------------------------------------------------------------------------


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", d.SessionInfo(mode="headless"))
    return d


def test_headless_enumerate_windows_empty(headless):
    assert headless.enumerate_windows() == []


def test_headless_interactive_elements_empty(headless):
    assert headless.get_interactive_elements() == []


def test_headless_screenshot_raises_stub_message(headless):
    with pytest.raises(RuntimeError) as exc:
        headless.take_screenshot()
    assert str(exc.value) == _MSG


@pytest.mark.parametrize(
    "call",
    [
        lambda m: m.focus_window(title="x"),
        lambda m: m.minimize_all(),
        lambda m: m.launch_app("firefox"),
        lambda m: m.resize_window(1, 800, 600),
        lambda m: m.get_clipboard(),
        lambda m: m.set_clipboard("x"),
        lambda m: m.lock_screen(),
        lambda m: m.show_notification("t", "b"),
        lambda m: m.click(10, 10),
        lambda m: m.type_text("hi"),
        lambda m: m.scroll(3),
        lambda m: m.move(5, 5),
        lambda m: m.shortcut("ctrl+c"),
    ],
)
def test_headless_all_functions_return_stub(headless, call):
    result = call(headless)
    assert result == _MSG  # exact stub message, no crash


def test_headless_never_shells_out(headless, monkeypatch):
    # Guard: no desktop function may spawn a subprocess on a headless box.
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("headless path must not run subprocesses")

    monkeypatch.setattr(d, "_run_x", _boom)
    monkeypatch.setattr(d, "_run_capture", _boom)
    assert headless.click(1, 1) == _MSG
    assert headless.get_clipboard() == _MSG
    assert headless.enumerate_windows() == []


# ---------------------------------------------------------------------------
# 3) X11 command construction (no real display needed)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def x11(monkeypatch):
    monkeypatch.setattr(
        d,
        "_SESSION",
        d.SessionInfo(
            mode="x11",
            display=":0",
            xauthority="/run/user/1000/gdm/Xauthority",
            uid=1000,
            user="newlevel",
            session_id="1",
        ),
    )
    calls: list[list[str]] = []

    def _fake_run_x(cmd, *, input_bytes=None, timeout=d._TIMEOUT, discard_output=False):
        calls.append(cmd)
        # xclip -o (get_clipboard) returns fake clipboard content
        if cmd[:1] == ["xclip"] and "-o" in cmd:
            return _FakeProc(stdout=b"clip-content")
        if cmd[:1] == ["wmctrl"] and "-lpG" in cmd:
            return _FakeProc(stdout=b"0x0400000a 0 3210 100 50 800 600 host gedit\n")
        return _FakeProc()

    monkeypatch.setattr(d, "_run_x", _fake_run_x)
    return d, calls


def test_x11_click_builds_xdotool_argv(x11):
    mod, calls = x11
    assert mod.click(120, 340, button="left", action="click") == "Clicked left at (120,340)"
    assert calls[-1] == ["xdotool", "mousemove", "120", "340", "click", "1"]


def test_x11_right_double_click(x11):
    mod, calls = x11
    mod.click(5, 6, button="right", action="double")
    assert calls[-1] == ["xdotool", "mousemove", "5", "6", "click", "--repeat", "2", "--delay", "120", "3"]


def test_x11_type_text_with_clear_and_enter(x11):
    mod, calls = x11
    assert mod.type_text("hello", clear=True, press_enter=True) == "Typed 5 chars"
    argvs = [c for c in calls if c[:1] == ["xdotool"]]
    assert ["xdotool", "key", "--clearmodifiers", "ctrl+a"] in argvs
    assert ["xdotool", "type", "--delay", "20", "--", "hello"] in argvs
    assert ["xdotool", "key", "Return"] in argvs


def test_x11_scroll_down(x11):
    mod, calls = x11
    mod.scroll(-3)
    assert calls[-1] == ["xdotool", "click", "--repeat", "3", "5"]  # 5 = wheel down


def test_x11_scroll_up(x11):
    mod, calls = x11
    mod.scroll(2)
    assert calls[-1] == ["xdotool", "click", "--repeat", "2", "4"]  # 4 = wheel up


def test_x11_shortcut_maps_win_and_enter(x11):
    mod, calls = x11
    mod.shortcut("win+e")
    assert calls[-1] == ["xdotool", "key", "super+e"]
    mod.shortcut("ctrl+shift+Escape")
    assert calls[-1] == ["xdotool", "key", "ctrl+shift+Escape"]


def test_x11_move_drag(x11):
    mod, calls = x11
    mod.move(200, 200, drag=True, start_x=10, start_y=10)
    assert ["xdotool", "mousedown", "1"] in calls
    assert ["xdotool", "mouseup", "1"] in calls


def test_x11_set_clipboard(x11):
    mod, calls = x11
    assert mod.set_clipboard("hello") == "Clipboard set"
    assert calls[-1] == ["xclip", "-selection", "clipboard"]


def test_x11_get_clipboard(x11):
    mod, _calls = x11
    assert mod.get_clipboard() == "clip-content"


def test_x11_enumerate_windows(x11):
    mod, _calls = x11
    wins = mod.enumerate_windows()
    assert len(wins) == 1
    assert wins[0].title == "gedit"
    assert wins[0].pid == 3210


def test_x11_resize_window(x11):
    mod, calls = x11
    mod.resize_window(0x0400000A, 640, 480)
    assert calls[-1] == ["xdotool", "windowsize", str(0x0400000A), "640", "480"]


# ---------------------------------------------------------------------------
# _build_invocation: env-injection vs sudo-wrap
# ---------------------------------------------------------------------------


def _x11_session():
    return d.SessionInfo(
        mode="x11",
        display=":0",
        xauthority="/run/user/1000/gdm/Xauthority",
        uid=1000,
        user="newlevel",
        session_id="1",
    )


def test_set_clipboard_uses_discard_output(monkeypatch):
    # Regression guard: xclip forks to stay resident holding the selection, so a
    # captured pipe blocks subprocess.run until timeout. set_clipboard MUST route
    # through _run_x with discard_output=True. (Found live on imag-nb.)
    monkeypatch.setattr(d, "_SESSION", _x11_session())
    captured: dict = {}

    def _rec(cmd, *, input_bytes=None, timeout=d._TIMEOUT, discard_output=False):
        captured.update(cmd=cmd, discard=discard_output, input=input_bytes)
        return _FakeProc()

    monkeypatch.setattr(d, "_run_x", _rec)
    assert d.set_clipboard("hi") == "Clipboard set"
    assert captured["cmd"] == ["xclip", "-selection", "clipboard"]
    assert captured["discard"] is True
    assert captured["input"] == b"hi"


def test_build_invocation_same_user_injects_env(monkeypatch):
    monkeypatch.setattr(d.os, "geteuid", lambda: 1000)
    argv, env = d._build_invocation(_x11_session(), ["xdotool", "key", "a"])
    assert argv == ["xdotool", "key", "a"]  # no sudo wrap
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/run/user/1000/gdm/Xauthority"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


def test_build_invocation_root_wraps_sudo(monkeypatch):
    monkeypatch.setattr(d.os, "geteuid", lambda: 0)
    argv, _env = d._build_invocation(_x11_session(), ["xdotool", "key", "a"])
    assert argv[:5] == ["sudo", "-n", "-u", "newlevel", "env"]
    assert "DISPLAY=:0" in argv
    assert argv[-3:] == ["xdotool", "key", "a"]


def test_unavailable_message_wayland(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", d.SessionInfo(mode="wayland"))
    msg = d._unavailable()
    assert "Wayland" in msg and "separate tier" in msg


def test_unavailable_message_headless(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", d.SessionInfo(mode="headless"))
    assert d._unavailable() == _MSG


# ---------------------------------------------------------------------------
# #8 — ScreenRecord via ffmpeg x11grab
# ---------------------------------------------------------------------------


def test_ffmpeg_argv_fullscreen():
    argv = d._ffmpeg_x11grab_argv(":0", "/tmp/out.gif", duration=3, fps=5, region=None, max_width=800)
    assert argv[:9] == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "x11grab",
        "-framerate",
        "5",
    ]
    assert "-video_size" not in argv  # full screen → x11grab auto-detects size
    assert argv[argv.index("-i") + 1] == ":0"
    assert argv[argv.index("-t") + 1] == "3"
    vf = argv[argv.index("-vf") + 1]
    assert "fps=5" in vf and "scale=800:-2" in vf
    assert argv[-1] == "/tmp/out.gif"


def test_ffmpeg_argv_region_and_no_scale():
    argv = d._ffmpeg_x11grab_argv(":0", "/tmp/o.gif", duration=2, fps=8, region=(100, 50, 900, 650), max_width=0)
    assert argv[argv.index("-video_size") + 1] == "800x600"
    assert argv[argv.index("-i") + 1] == ":0+100,50"
    assert argv[argv.index("-vf") + 1] == "fps=8"  # max_width=0 → no scale filter


def test_ffmpeg_argv_clamps_fps_and_duration():
    argv = d._ffmpeg_x11grab_argv(":0", "/tmp/o.gif", duration=99, fps=99, region=None, max_width=800)
    assert argv[argv.index("-framerate") + 1] == "10"  # fps clamped to 10
    assert argv[argv.index("-t") + 1] == "10"  # duration clamped to 10


def test_record_screen_headless_raises_stub_and_never_shells_out(headless, monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("headless record_screen must not run subprocesses")

    monkeypatch.setattr(d, "_run_x", _boom)
    with pytest.raises(RuntimeError) as exc:
        headless.record_screen(duration=1)
    assert str(exc.value) == _MSG


def test_record_screen_x11_runs_ffmpeg_and_returns_b64(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", _x11_session())
    calls: list[list[str]] = []

    def _fake_run_x(cmd, *, input_bytes=None, timeout=d._TIMEOUT, discard_output=False):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"GIF89a-fake-frames")  # ffmpeg writes the out file
        return _FakeProc()

    monkeypatch.setattr(d, "_run_x", _fake_run_x)
    b64 = d.record_screen(duration=1, fps=5)
    assert base64.b64decode(b64) == b"GIF89a-fake-frames"
    assert calls and calls[0][0] == "ffmpeg" and "x11grab" in calls[0]
    assert not Path(calls[0][-1]).exists()  # temp file cleaned up


def test_record_screen_x11_empty_output_raises(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", _x11_session())

    def _fake_run_x(cmd, *, input_bytes=None, timeout=d._TIMEOUT, discard_output=False):
        Path(cmd[-1]).write_bytes(b"")  # ffmpeg "succeeded" but produced nothing
        return _FakeProc()

    monkeypatch.setattr(d, "_run_x", _fake_run_x)
    with pytest.raises(RuntimeError) as exc:
        d.record_screen(duration=1)
    assert "empty recording" in str(exc.value)


# ---------------------------------------------------------------------------
# #7 — AnnotatedSnapshot via the AT-SPI accessibility tree
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Synthetic stand-in for _AtspiAdapter over dict nodes (no real AT-SPI)."""

    def children(self, n):
        return n.get("children", [])

    def role_name(self, n):
        return n.get("role", "")

    def name(self, n):
        return n.get("name", "")

    def is_showing(self, n):
        return n.get("showing", True)

    def extents(self, n):
        return n.get("ext")


def _node(role="", name="", ext=None, showing=True, children=None):
    return {"role": role, "name": name, "ext": ext, "showing": showing, "children": children or []}


def test_collect_actionable_maps_only_actionable_visible_valid():
    tree = _node(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[
            _node(
                "panel",
                "",
                (0, 0, 800, 600),
                children=[
                    _node("push button", "OK", (10, 20, 110, 60)),
                    _node("entry", "name field", (10, 80, 300, 110)),
                    _node("label", "just text", (10, 120, 200, 140)),  # not actionable
                    _node("push button", "", (-1, -1, -2, -2)),  # invalid extents
                    _node("check box", "Agree", (10, 160, 40, 190), showing=False),  # hidden
                ],
            )
        ],
    )
    els = d._collect_actionable(tree, _FakeAdapter())
    assert [e["class"] for e in els] == ["push button", "entry"]
    assert els[0] == {
        "index": 1,
        "class": "push button",
        "text": "OK",
        "rect": {"left": 10, "top": 20, "right": 110, "bottom": 60},
    }
    assert els[1]["index"] == 2 and els[1]["text"] == "name field"


def test_collect_actionable_respects_max_elements():
    kids = [_node("push button", f"b{i}", (0, 0, 10, 10)) for i in range(10)]
    root = _node("frame", "W", (0, 0, 100, 100), children=kids)
    els = d._collect_actionable(root, _FakeAdapter(), max_elements=3)
    assert [e["index"] for e in els] == [1, 2, 3]


def test_get_interactive_elements_atspi_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", _x11_session())

    def _boom():
        raise RuntimeError("gi/Atspi not importable")

    monkeypatch.setattr(d, "_load_atspi", _boom)
    assert d.get_interactive_elements() == []


def test_get_interactive_elements_no_frame_returns_empty(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", _x11_session())
    monkeypatch.setattr(d, "_load_atspi", lambda: object())
    monkeypatch.setattr(d, "_atspi_active_frame", lambda atspi: None)
    assert d.get_interactive_elements() == []


def test_get_interactive_elements_collects_from_active_frame(monkeypatch):
    monkeypatch.setattr(d, "_SESSION", _x11_session())
    frame = _node("frame", "W", (0, 0, 800, 600), children=[_node("push button", "Go", (5, 5, 55, 35))])
    monkeypatch.setattr(d, "_load_atspi", lambda: object())
    monkeypatch.setattr(d, "_atspi_active_frame", lambda atspi: frame)
    monkeypatch.setattr(d, "_AtspiAdapter", lambda atspi: _FakeAdapter())
    els = d.get_interactive_elements()
    assert len(els) == 1 and els[0]["text"] == "Go" and els[0]["class"] == "push button"
