"""ElementTree / ClickElement / TypeElement on AT-SPI (#38): the provider over a fake a11y bus.

The fake answers the same calls as ``gi.repository.Atspi`` (checked against the real library with
GTK, Qt and Chrome on a private Xvfb, see ``.claude/rules/accessibility-providers.md``). Only the library, the X
helpers (``session._run_x``) and the clock are faked; the walk, refs and tools are the real ones.
"""

from __future__ import annotations

import os
import sys
import time
import types

import pytest
from test_coordinate_contract_tools import _install_fake_x11, fake_x11  # noqa: F401
from tool_surface import server
from tool_test_support import tool_error

from remoteos.element_tree import StaleRef
from remoteos.errors import DesktopUnavailable
from remoteos.platform.linux import desktop as d
from remoteos.platform.linux import session as linux_session
from remoteos.platform.linux import windows as linux_windows

_real_load_atspi = linux_windows._load_atspi
WID = 0x0400000A  # the window of the fake wmctrl and xdotool
PID = 3210

State = types.SimpleNamespace(
    **{s: s for s in ("ACTIVE", "DEFUNCT", "EDITABLE", "ENABLED", "FOCUSABLE", "FOCUSED", "SHOWING")}
)
BASE = {"ENABLED", "SHOWING"}


class _States:
    def __init__(self, names):
        self.names = names

    def contains(self, name):
        return name in self.names


class _Rect:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class Node:
    """An ``Atspi.Accessible``: role, name, states, extents (window-relative), children, actions."""

    def __init__(self, role, name="", ext=(0, 0, 0, 0), states=BASE, children=(), actions=(), interfaces=None):
        self.role, self.name, self.ext = role, name, ext
        self.states, self.kids = set(states), list(children)
        self.actions = list(actions)
        self.interfaces = interfaces if interfaces is not None else (["Action"] if actions else [])
        self.pid, self.toolkit = PID, "Qt"
        self.done, self.text = [], None
        self.fails = False  # the app is gone, or a call into it timed out
        self.refuses = False
        self.grabs = []

    def _call(self):
        if self.fails:
            raise RuntimeError("GDBus timeout")

    def get_role_name(self):
        self._call()
        return self.role

    def get_name(self):
        self._call()
        return self.name

    def get_state_set(self):
        self._call()
        return _States(self.states)

    def get_child_count(self):
        self._call()
        return len(self.kids)

    def get_child_at_index(self, index):
        return self.kids[index]

    def get_extents(self, coord):
        self._call()
        if self.role == "application":
            return _Rect(-1, -1, -1, -1)  # what the real library gives, no exception
        return _Rect(*self.ext)

    def get_process_id(self):
        return self.pid

    def get_toolkit_name(self):
        return self.toolkit

    def get_interfaces(self):
        return self.interfaces

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[index]

    def do_action(self, index):
        self._call()
        self.done.append(self.actions[index])
        return not self.refuses

    def set_text_contents(self, text):
        self._call()
        self.text = text
        return not self.refuses

    def grab_focus(self):
        self._call()
        self.grabs.append(True)
        self.states.add("FOCUSED")
        return True


def fake_atspi(*apps):
    desktop = Node("desktop frame", children=apps)
    return types.SimpleNamespace(
        StateType=State,
        CoordType=types.SimpleNamespace(WINDOW="window", SCREEN="screen"),
        get_desktop=lambda n: desktop,
    )


def window_tree(*controls, name="Editor", states=BASE | {"ACTIVE"}, ext=(0, 0, 800, 600)):
    frame = Node("frame", name, ext, states, controls)
    return Node("application", "app", children=[frame]), frame


@pytest.fixture
def x11(fake_x11, monkeypatch):  # noqa: F811
    """The fake X of the coordinate tests, with an active window at (100, 50), 800x600."""
    calls = fake_x11
    real = linux_session._run_x

    def run_x(cmd, **kwargs):
        if cmd[:2] == ["xdotool", "getactivewindow"]:
            calls.append(list(cmd))
            return types.SimpleNamespace(stdout=str(run_x.active).encode(), returncode=0, stderr=b"")
        if cmd[:2] == ["xdotool", "getwindowgeometry"]:
            calls.append(list(cmd))
            return types.SimpleNamespace(stdout=b"X=100\nY=50\nWIDTH=800\nHEIGHT=600\n", returncode=0, stderr=b"")
        if cmd[:2] == ["xdotool", "getwindowname"]:
            return types.SimpleNamespace(stdout=run_x.name, returncode=0, stderr=b"")
        if cmd[:2] == ["xdotool", "getwindowpid"]:
            return types.SimpleNamespace(stdout=run_x.pid, returncode=0, stderr=b"")
        return real(cmd, **kwargs)

    run_x.active, run_x.name, run_x.pid = WID, b"", b"0"
    monkeypatch.setattr(linux_session, "_run_x", run_x)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return types.SimpleNamespace(calls=calls, run_x=run_x)


def bus(monkeypatch, *apps):
    atspi = fake_atspi(*apps)
    monkeypatch.setattr(linux_windows, "_load_atspi", lambda: atspi)
    return atspi


def _ref(name: str) -> str:
    return next(i.ref for i in d.element_tree().items if i.name == name)


# ------------------------------------------------------------------ the tree


def test_the_tree_uses_the_shared_roles_and_screen_coordinates(x11, monkeypatch):
    app, _ = window_tree(
        Node("panel", children=[Node("push button", "Save", (10, 20, 100, 30), BASE)]),
        Node("entry", "Name", (10, 60, 200, 24), BASE | {"EDITABLE"}),
        Node("label", "just text", (10, 90, 100, 20)),
        Node("check box", "Off", (10, 120, 100, 20), {"SHOWING"}),
        Node("push button", "Hidden", (10, 150, 100, 20), {"ENABLED"}),
        Node("push button", "No size", (-1, -1, 0, 0)),
    )
    bus(monkeypatch, app)
    tree = d.element_tree()
    assert tree.title == "Editor"
    assert [(i.role, i.name, i.enabled, i.rect) for i in tree.items] == [
        ("Button", "Save", True, (110, 70, 210, 100)),
        ("Edit", "Name", True, (110, 110, 310, 134)),
        ("CheckBox", "Off", False, (110, 170, 210, 190)),
    ]
    assert all(item.ref.count(".") == 1 for item in tree.items)


@pytest.mark.parametrize(
    ("role", "states", "expected"),
    [
        ("text", BASE | {"EDITABLE"}, ["Edit"]),  # GTK 3's entry
        ("text", BASE, []),  # the same role as static text
        ("text", BASE | {"FOCUSABLE"}, ["Text"]),  # a read-only field
        ("page tab", BASE, ["TabItem"]),
        ("link", BASE, ["Hyperlink"]),
        ("menu item", BASE, ["MenuItem"]),
        ("switch", BASE, ["CheckBox"]),
        ("table cell", BASE, ["DataItem"]),
        ("slider", BASE, ["Slider"]),
    ],
)
def test_roles(x11, monkeypatch, role, states, expected):
    bus(monkeypatch, window_tree(Node(role, "x", (0, 0, 9, 9), states))[0])
    assert [i.role for i in d.element_tree().items] == expected


def test_a_control_deep_under_hidden_containers_is_found(x11, monkeypatch):
    # Electron nests a control dozens of levels down, under nodes that report nothing
    node = Node("push button", "Deep", (1, 1, 9, 9))
    for _ in range(30):
        node = Node("section", states={"ENABLED"}, children=[node])
    bus(monkeypatch, window_tree(node)[0])
    assert [i.name for i in d.element_tree().items] == ["Deep"]


def test_the_window_s_process_names_the_app_even_when_no_frame_says_active(x11, monkeypatch):
    # Qt frames often report neither ACTIVE nor SHOWING. Another app's frame is ACTIVE: not ours.
    other, _ = window_tree(Node("push button", "Theirs", (0, 0, 9, 9)), name="Other")
    other.pid = 99
    mine, _ = window_tree(Node("push button", "Mine", (0, 0, 9, 9)), name="Not the title", states={"ENABLED"})
    bus(monkeypatch, other, mine)
    assert [i.name for i in d.element_tree().items] == ["Mine"]


def test_an_application_with_two_windows_gives_the_one_with_the_title(x11, monkeypatch):
    app, _ = window_tree(Node("push button", "First", (0, 0, 9, 9)), name="Other window")
    second = Node("frame", "Editor", (0, 0, 800, 600), {"ENABLED"}, [Node("push button", "Second", (0, 0, 9, 9))])
    app.kids.append(second)
    bus(monkeypatch, app)
    assert [i.name for i in d.element_tree("Editor").items] == ["Second"]


def _two_frames():
    first = Node("frame", "Alpha", (0, 0, 800, 600), {"ENABLED", "SHOWING", "ACTIVE"})
    first.kids = [Node("push button", "Alpha's", (0, 0, 9, 9))]
    second = Node("frame", "Beta as AT-SPI says", (0, 0, 800, 600), {"ENABLED", "SHOWING"})
    second.kids = [Node("push button", "Beta's", (0, 0, 9, 9))]
    return Node("application", "app", children=[first, second])


def test_a_background_window_is_never_replaced_by_its_app_s_active_frame(x11, monkeypatch):
    # real GTK 3: "Beta" was listed with Alpha's elements, under Beta's title and at Beta's offset
    bus(monkeypatch, _two_frames())
    x11.run_x.active = 0x777
    assert tool_error(lambda: server.ElementTree("Editor")).endswith("FocusWindow it first")


def test_a_background_app_s_only_frame_is_its_window_whatever_its_title(x11, monkeypatch):
    app, _ = window_tree(Node("push button", "Mine", (0, 0, 9, 9)), name="AT-SPI's own title")
    bus(monkeypatch, app)
    x11.run_x.active = 0x777
    assert [i.name for i in d.element_tree("Editor").items] == ["Mine"]


def test_the_active_window_may_use_its_app_s_active_frame(x11, monkeypatch):
    bus(monkeypatch, _two_frames())
    assert [i.name for i in d.element_tree().items] == ["Alpha's"]


def test_a_window_with_no_process_is_found_by_its_exact_title_only(x11, monkeypatch):
    monkeypatch.setattr(linux_windows, "list_windows", lambda: [d.WindowInfo(WID, "Editor", (0, 0, 9, 9), True, 0)])
    other, _ = window_tree(Node("push button", "Wrong", (0, 0, 9, 9)), name="Something else")
    bus(monkeypatch, other)
    assert d.element_tree().items == ()
    mine, _ = window_tree(Node("push button", "Right", (0, 0, 9, 9)))
    bus(monkeypatch, other, mine)
    assert [i.name for i in d.element_tree().items] == ["Right"]


def _silent_frame(name, button, ext=(0, 0, 800, 600)):
    return Node("frame", name, ext, {"ENABLED"}, [button])


def test_an_app_s_only_frame_is_its_window_whatever_it_says(x11, monkeypatch):
    button = Node("push button", "In", (10, 20, 100, 30))
    bus(monkeypatch, Node("application", "app", children=[_silent_frame("Not the title", button)]))
    assert [(i.name, i.rect) for i in d.element_tree().items] == [("In", (110, 70, 210, 100))]


def test_an_app_of_several_silent_frames_is_walked_whole_by_screen_coordinates(x11, monkeypatch):
    first = Node("push button", "One", (10, 20, 100, 30))
    second = Node("push button", "Two", (10, 60, 100, 30))
    frames = [_silent_frame("Not the title", first, (5, 5, 400, 300)), _silent_frame("Nor this", second)]
    bus(monkeypatch, Node("application", "app", children=frames))
    assert [(i.name, i.rect) for i in d.element_tree().items] == [
        ("One", (10, 20, 110, 50)),
        ("Two", (10, 60, 110, 90)),
    ]


def test_pixels_nobody_can_vouch_for_are_never_clicked(x11, monkeypatch):
    # several silent frames, none with a SCREEN position: the list is best effort, the mouse refuses
    first, second = Node("push button", "One", (10, 20, 100, 30)), Node("push button", "Two", (10, 60, 100, 30))
    frames = [_silent_frame("Not the title", first), _silent_frame("Nor this", second)]
    bus(monkeypatch, Node("application", "app", children=frames))
    with pytest.raises(RuntimeError, match="screen position .* is unknown: nothing was clicked"):
        d.click_element(_ref("One"))
    assert _mouse_clicks(x11) == []


def _unvouched(monkeypatch):
    frames = [_silent_frame("Not the title", Node("push button", "One", (10, 20, 100, 30)))]
    frames.append(_silent_frame("Nor this", Node("push button", "Two", (10, 60, 100, 30))))
    bus(monkeypatch, Node("application", "app", children=frames))


def test_pixels_nobody_can_vouch_for_are_withheld_or_flagged(x11, monkeypatch):
    _unvouched(monkeypatch)
    assert d.get_interactive_elements() == []
    elements, cut = d.marked_elements()
    assert [e["text"] for e in elements] == ["One", "Two"]
    assert cut.startswith("the screen positions of these elements are unknown")
    assert "**Hint:** the screen positions of these elements are unknown" in server.ElementTree()


def test_a_cut_list_keeps_the_flag_on_its_pixels(x11, monkeypatch):
    _unvouched(monkeypatch)
    elements, cut = d.marked_elements(limit=1)
    assert len(elements) == 1
    assert cut.startswith("only the first 1 elements are listed")
    assert "the screen positions of these elements are unknown" in cut


@pytest.mark.parametrize(
    ("found", "cut", "pixels_known", "expected"),
    [
        (True, "", True, ""),
        (True, "cut", True, ""),
        (True, "", False, linux_windows._UNKNOWN_PIXELS),
        (True, "cut", False, linux_windows._UNKNOWN_PIXELS),
        (False, "", True, "HINT"),
        (False, "", False, "HINT"),
        (False, "cut", True, ""),  # a list that stopped says nothing of whether the app shows controls
        (False, "cut", False, ""),
    ],
)
def test_what_a_reply_adds(found, cut, pixels_known, expected):
    reading = linux_windows._Reading(None, None, None, None, pixels_known)
    hint = linux_windows._enable_hint("")
    assert reading.note(found, cut) == (hint if expected == "HINT" else expected)


def test_a_frame_that_is_not_the_only_one_is_not_taken_for_the_window(x11, monkeypatch):
    # one sized frame among several: its size says nothing about the target's X window
    sized = _silent_frame("Hidden", Node("push button", "One", (10, 20, 100, 30)))
    empty = _silent_frame("Target", Node("push button", "Two", (10, 60, 100, 30)), ext=(0, 0, 0, 0))
    atspi = bus(monkeypatch, Node("application", "app", children=[sized, empty]))
    assert linux_windows._atspi_coord_strategy(atspi, atspi.get_desktop(0).kids[0], (100, 50, 800, 600)) is None


def test_a_frame_that_stops_answering_stops_the_mouse_too(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30))
    app, frame = window_tree(button)
    bus(monkeypatch, app)
    ref = _ref("Go")
    monkeypatch.setattr(frame, "get_extents", lambda coord: (_ for _ in ()).throw(RuntimeError("busy")))
    with pytest.raises(RuntimeError, match="screen position"):
        d.click_element(ref)
    assert _mouse_clicks(x11) == []


def test_a_vanished_element_is_skipped(x11, monkeypatch):
    gone_one = Node("push button", "Gone", (0, 0, 9, 9))
    gone_one.fails = True
    defunct = Node("push button", "Defunct", (0, 0, 9, 9), BASE | {"DEFUNCT"})
    bus(monkeypatch, window_tree(gone_one, defunct, Node("push button", "Here", (0, 0, 9, 9)))[0])
    assert [i.name for i in d.element_tree().items] == ["Here"]


def test_an_unlisted_window_with_no_title_never_names_another_app_s_untitled_frame(x11, monkeypatch):
    x11.run_x.active = 0x777
    x11.run_x.name, x11.run_x.pid = b"\n", b"0\n"
    untitled = Node("frame", "", (0, 0, 9, 9), BASE, [Node("push button", "Theirs", (0, 0, 9, 9))])
    other = Node("application", "app", children=[untitled])
    bus(monkeypatch, other)
    assert d.element_tree().items == ()


def test_an_untitled_window_names_no_untitled_frame_of_its_own_app_either(x11, monkeypatch):
    monkeypatch.setattr(linux_windows, "list_windows", lambda: [d.WindowInfo(WID, "", (0, 0, 9, 9), True, PID)])
    monkeypatch.setattr(linux_windows, "enumerate_windows", linux_windows.list_windows)
    app, frame = window_tree(Node("push button", "Mine", (0, 0, 9, 9)), name="")
    bus(monkeypatch, app)
    assert [i.name for i in d.element_tree().items] == ["Mine"]  # its only frame, not by the empty title
    assert linux_windows._frame_titled([frame], "") is None


def test_a_window_with_no_pid_keeps_the_title_xdotool_gave(x11, monkeypatch):
    x11.run_x.name, x11.run_x.pid = b"Editor\n", b"\n"
    window = linux_windows._unlisted_window(WID)
    assert (window.title, window.pid) == ("Editor", 0)


def test_a_box_without_wmctrl_still_finds_the_active_window(x11, monkeypatch):
    monkeypatch.setattr(linux_windows, "enumerate_windows", lambda: [])
    x11.run_x.name, x11.run_x.pid = b"Editor\n", f"{PID}\n".encode()
    bus(monkeypatch, window_tree(Node("push button", "Mine", (0, 0, 9, 9)))[0])
    tree = d.element_tree()
    assert tree.title == "Editor" and [i.name for i in tree.items] == ["Mine"]


def test_a_big_tree_ends_as_a_partial_list_that_says_so(x11, monkeypatch):
    kids = [Node("push button", f"b{i}", (0, 0, 9, 9)) for i in range(320)]
    bus(monkeypatch, window_tree(*kids)[0])
    tree = d.element_tree()
    assert len(tree.items) == 300
    assert tree.truncated == "only the first 300 elements are listed"


# ------------------------------------------------------------------ hints


@pytest.mark.parametrize(
    ("toolkit", "wanted", "unwanted"),
    [
        ("Qt", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1", "force-renderer"),
        ("Chromium", "--force-renderer-accessibility", "QT_LINUX"),
        ("Electron", "ACCESSIBILITY_ENABLED=1", "QT_LINUX"),
    ],
)
def test_a_toolkit_that_shows_nothing_gets_its_own_hint(x11, monkeypatch, toolkit, wanted, unwanted):
    app, frame = window_tree()
    frame.toolkit = toolkit
    bus(monkeypatch, app)
    hint = d.element_tree().hint
    assert wanted in hint and unwanted not in hint


def test_an_app_not_on_the_bus_gets_both_hints(x11, monkeypatch):
    bus(monkeypatch)
    tree = d.element_tree()
    assert tree.items == () and tree.title == "Editor"
    assert "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1" in tree.hint
    assert "--force-renderer-accessibility" in tree.hint
    assert "ACCESSIBILITY_ENABLED=1" in tree.hint


def test_gtk_has_no_switch_so_no_hint(x11, monkeypatch):
    app, frame = window_tree()
    frame.toolkit = "gtk"
    bus(monkeypatch, app)
    assert d.element_tree().hint == ""


def test_a_tree_with_elements_has_no_hint(x11, monkeypatch):
    bus(monkeypatch, window_tree(Node("push button", "Go", (0, 0, 9, 9)))[0])
    assert d.element_tree().hint == ""


def test_the_tool_prints_the_hint(x11, monkeypatch):
    bus(monkeypatch)
    assert "**Hint:** this app shows no controls to AT-SPI" in server.ElementTree()


def test_annotated_snapshot_reports_the_hint_and_keeps_its_image(x11, monkeypatch):
    bus(monkeypatch)
    assert d.marked_elements() == ([], linux_windows._enable_hint(""))


# ------------------------------------------------------------------ refs and lists


def test_a_new_tree_ends_the_old_refs(x11, monkeypatch):
    bus(monkeypatch, window_tree(Node("push button", "Go", (0, 0, 9, 9), actions=["click"]))[0])
    old = _ref("Go")
    d.element_tree()
    with pytest.raises(StaleRef, match="not in the latest tree"):
        d.click_element(old)


def test_snapshot_s_list_leaves_the_refs_alone_and_annotated_snapshot_starts_a_tree(x11, monkeypatch):
    button = Node("push button", "Go", (0, 0, 9, 9), actions=["click"])
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    assert d.get_interactive_elements()[0] == {
        "index": 1,
        "class": "Button",
        "text": "Go",
        "rect": {"left": 100, "top": 50, "right": 109, "bottom": 59},
    }
    assert d.click_element(ref) == f"Invoked {ref} ('Go')"
    elements, cut = d.marked_elements()
    assert cut == ""
    with pytest.raises(StaleRef):
        d.click_element(ref)
    assert d.click_element(elements[0]["ref"]).startswith("Invoked")


def test_the_lists_stop_at_their_limit(x11, monkeypatch):
    bus(monkeypatch, window_tree(*[Node("push button", f"b{i}", (0, 0, 9, 9)) for i in range(5)])[0])
    assert len(d.get_interactive_elements(limit=2)) == 2
    elements, cut = d.marked_elements(limit=2)
    assert len(elements) == 2 and cut == "only the first 2 elements are listed"


def test_a_failing_a11y_bus_costs_the_lists_but_is_an_error_for_the_tree(x11, monkeypatch):
    def no_bus():
        raise RuntimeError("no a11y bus")

    monkeypatch.setattr(linux_windows, "_load_atspi", no_bus)
    assert d.get_interactive_elements() == []
    assert d.marked_elements() == ([], "")
    assert tool_error(server.ElementTree) == "ElementTree error: no a11y bus"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda: d.get_interactive_elements(), []),
        (lambda: d.marked_elements(), ([], "")),
    ],
)
def test_headless_lists_are_empty_without_touching_anything(monkeypatch, call, expected):
    monkeypatch.setattr(linux_session, "_SESSION", d.SessionInfo(mode="headless"))
    monkeypatch.setattr(linux_windows, "_load_atspi", lambda: pytest.fail("touched AT-SPI"))
    assert call() == expected


def test_headless_tree_is_the_stub(monkeypatch):
    monkeypatch.setattr(linux_session, "_SESSION", d.SessionInfo(mode="headless"))
    with pytest.raises(DesktopUnavailable, match="headless"):
        d.element_tree()


# ------------------------------------------------------------------ ClickElement


def _mouse_clicks(x11) -> list[list[str]]:
    return [c for c in x11.calls if "click" in c]


def test_click_uses_the_press_action_and_no_mouse(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["SetFocus", "ShowMenu", "Press"])
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    assert d.click_element(ref) == f"Invoked {ref} ('Go')"
    assert button.done == ["Press"]
    assert _mouse_clicks(x11) == []


def test_click_prefers_click_to_gtk2_s_half_press(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["press", "release", "click"])
    bus(monkeypatch, window_tree(button)[0])
    d.click_element(_ref("Go"))
    assert button.done == ["click"]


def test_activate_on_a_text_field_is_enter_so_the_mouse_clicks_it(x11, monkeypatch):
    # real GTK 3: the entry's only action, and it fires the form's default button
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"}, actions=["activate"])
    bus(monkeypatch, window_tree(field)[0])
    d.click_element(_ref("Name"))
    assert field.done == []
    assert _mouse_clicks(x11) == [["xdotool", "mousemove", "160", "85", "click", "1"]]


@pytest.mark.parametrize("interfaces", [["Action"], ["Action", "EditableText"]])
def test_activate_on_a_read_only_field_is_enter_too(x11, monkeypatch, interfaces):
    # real GTK 3: no EDITABLE state, the same single action, the same default button
    field = Node("text", "Name", (10, 20, 100, 30), BASE | {"FOCUSABLE"}, actions=["activate"], interfaces=interfaces)
    bus(monkeypatch, window_tree(field)[0])
    d.click_element(_ref("Name"))
    assert field.done == []
    assert len(_mouse_clicks(x11)) == 1


@pytest.mark.parametrize(
    ("states", "interfaces"),
    [(BASE | {"EDITABLE"}, ["Action"]), (BASE, ["Action", "EditableText"])],
    ids=["editable-state", "editable-interface"],
)
def test_a_field_is_known_by_more_than_its_role(x11, monkeypatch, states, interfaces):
    # real GTK 3: a spin button has only activate, and its role is no text role
    spin = Node("spin button", "Count", (10, 20, 100, 30), states, actions=["activate"], interfaces=interfaces)
    bus(monkeypatch, window_tree(spin)[0])
    d.click_element(_ref("Count"))
    assert spin.done == []
    assert len(_mouse_clicks(x11)) == 1


def test_a_node_whose_role_stops_answering_gets_no_action(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["activate"])
    atspi = bus(monkeypatch, window_tree(button)[0])
    monkeypatch.setattr(button, "get_role_name", lambda: (_ for _ in ()).throw(RuntimeError("busy")))
    assert linux_windows._is_field(atspi, button) is True


def test_a_text_field_s_real_click_action_is_used(x11, monkeypatch):
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"}, actions=["activate", "click"])
    bus(monkeypatch, window_tree(field)[0])
    d.click_element(_ref("Name"))
    assert field.done == ["click"]


def test_an_action_that_means_something_else_is_never_taken(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["SetFocus", "ShowMenu"])
    bus(monkeypatch, window_tree(button)[0])
    reply = d.click_element(_ref("Go"))
    assert button.done == []
    assert _mouse_clicks(x11) == [["xdotool", "mousemove", "160", "85", "click", "1"]]
    assert reply.endswith("has no usable action")


def test_a_refused_action_falls_back_to_the_mouse(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["click"])
    button.refuses = True
    bus(monkeypatch, window_tree(button)[0])
    d.click_element(_ref("Go"))
    assert len(_mouse_clicks(x11)) == 1


def test_an_action_that_does_not_answer_is_never_followed_by_the_mouse(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["click"])
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    monkeypatch.setattr(button, "do_action", lambda index: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(RuntimeError, match="may still have happened"):
        d.click_element(ref)
    assert _mouse_clicks(x11) == []


def test_a_mouse_click_needs_its_window_in_front(x11, monkeypatch):
    bus(monkeypatch, window_tree(Node("push button", "Go", (10, 20, 100, 30)))[0])
    ref = _ref("Go")
    x11.run_x.active = 0x777
    with pytest.raises(RuntimeError, match="on top of element"):
        d.click_element(ref)
    assert _mouse_clicks(x11) == []


@pytest.mark.parametrize("states", [{"ENABLED"}, BASE])
def test_a_mouse_click_needs_an_element_on_the_screen(x11, monkeypatch, states):
    button = Node("push button", "Go", (10, 20, 100, 30))
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    button.states = set(states)
    button.ext = (0, 0, 0, 0) if states == BASE else button.ext
    with pytest.raises(RuntimeError, match="off-screen"):
        d.click_element(ref)


def test_a_ref_of_an_unknown_window_is_not_stale_for_it(x11, monkeypatch):
    # no active X window at the walk: handle 0, which no window list holds
    atspi = fake_atspi()
    button = Node("push button", "Go", (0, 0, 9, 9), actions=["click"])
    entry = linux_windows._Entry(button, "push button", "Go", 0, button, atspi)
    [ref] = linux_windows._refs.publish([entry])
    assert d.click_element(ref).startswith("Invoked")


def test_a_box_without_wmctrl_can_still_act_on_a_ref(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["click"])
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    monkeypatch.setattr(linux_windows, "enumerate_windows", lambda: [])
    assert d.click_element(ref).startswith("Invoked")


def test_a_disabled_element_is_refused(x11, monkeypatch):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["click"])
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    button.states.discard("ENABLED")
    with pytest.raises(RuntimeError, match="is disabled"):
        d.click_element(ref)
    assert button.done == []


@pytest.mark.parametrize("change", ["defunct", "fails", "role", "name", "window"])
def test_an_element_that_left_is_gone_and_nothing_happens(x11, monkeypatch, change):
    button = Node("push button", "Go", (10, 20, 100, 30), actions=["click"])
    bus(monkeypatch, window_tree(button)[0])
    ref = _ref("Go")
    if change == "defunct":
        button.states.add("DEFUNCT")
    elif change == "fails":
        button.fails = True
    elif change == "role":
        button.role = "label"  # a node id reused for something else
    elif change == "name":
        button.name = "Delete"  # a list row reused for other content
    else:
        monkeypatch.setattr(linux_windows, "enumerate_windows", lambda: [d.WindowInfo(1, "Other", (0, 0, 9, 9), True)])
    with pytest.raises(StaleRef, match="is gone: call ElementTree again"):
        d.click_element(ref)
    assert button.done == []


# ------------------------------------------------------------------ TypeElement


def test_type_replaces_the_text_through_editable_text(x11, monkeypatch):
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"}, interfaces=["EditableText", "Text"])
    bus(monkeypatch, window_tree(field)[0])
    ref = _ref("Name")
    assert d.type_element(ref, "Žltý") == f"Set the value of {ref} ('Name') to 4 chars"
    assert field.text == "Žltý"
    assert not [c for c in x11.calls if "type" in c]


def test_a_read_only_field_is_refused_and_nothing_is_typed(x11, monkeypatch):
    field = Node("text", "Name", (10, 20, 100, 30), BASE | {"FOCUSABLE"}, interfaces=["EditableText"])
    bus(monkeypatch, window_tree(field)[0])
    with pytest.raises(RuntimeError, match="is read-only"):
        d.type_element(_ref("Name"), "x")
    assert field.text is None and not [c for c in x11.calls if "type" in c]


def test_text_that_does_not_answer_may_have_happened(x11, monkeypatch):
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"}, interfaces=["EditableText"])
    bus(monkeypatch, window_tree(field)[0])
    ref = _ref("Name")
    monkeypatch.setattr(field, "set_text_contents", lambda text: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(RuntimeError, match="may still have happened"):
        d.type_element(ref, "x")
    assert not [c for c in x11.calls if "type" in c]


@pytest.mark.parametrize("refused", [False, True])
def test_without_editable_text_it_takes_the_focus_and_types(x11, monkeypatch, refused):
    interfaces = ["EditableText"] if refused else []
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"}, interfaces=interfaces)
    field.refuses = refused
    bus(monkeypatch, window_tree(field)[0])
    ref = _ref("Name")
    assert d.type_element(ref, "Hi") == f"Typed 2 chars into {ref} ('Name'), at its caret"
    assert field.grabs == [True]
    activate = ["wmctrl", "-i", "-a", hex(WID)]
    typed = ["xdotool", "type", "--delay", "20", "--", "Hi"]
    assert x11.calls.index(activate) < x11.calls.index(typed)


def test_the_focus_may_land_inside_a_wrapper(x11, monkeypatch):
    inner = Node("entry", "", (10, 20, 100, 30), BASE | {"FOCUSED"})
    wrapper = Node("combo box", "Name", (10, 20, 100, 30), children=[inner])
    monkeypatch.setattr(wrapper, "grab_focus", lambda: True)
    bus(monkeypatch, window_tree(wrapper)[0])
    assert "Typed 2 chars" in d.type_element(_ref("Name"), "Hi")


def test_nothing_is_typed_without_the_focus(x11, monkeypatch):
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"})
    monkeypatch.setattr(field, "grab_focus", lambda: False)
    bus(monkeypatch, window_tree(field)[0])
    ref = _ref("Name")
    monkeypatch.setattr(linux_windows, "_FOCUS_WAIT_S", 0.0)
    with pytest.raises(RuntimeError, match="did not take the keyboard focus: nothing was typed"):
        d.type_element(ref, "Hi")
    assert not [c for c in x11.calls if "type" in c]


def test_a_table_is_not_searched_for_the_focus_on_every_poll(x11, monkeypatch):
    rows = [Node("table row", states=BASE | {"FOCUSED"}) for _ in range(33)]
    table = Node("table", "Rows", (0, 0, 9, 9), BASE | {"FOCUSABLE"}, rows)
    monkeypatch.setattr(table, "grab_focus", lambda: True)
    bus(monkeypatch, window_tree(table)[0])
    ref = _ref("Rows")
    monkeypatch.setattr(linux_windows, "_FOCUS_WAIT_S", 0.0)
    with pytest.raises(RuntimeError, match="did not take the keyboard focus"):
        d.type_element(ref, "Hi")


def test_nothing_is_typed_when_its_window_stays_in_the_back(x11, monkeypatch):
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE", "FOCUSED"})
    bus(monkeypatch, window_tree(field)[0])
    ref = _ref("Name")
    x11.run_x.active = 0x777
    monkeypatch.setattr(linux_windows, "_FOCUS_WAIT_S", 0.0)
    with pytest.raises(RuntimeError, match="did not take the keyboard focus"):
        d.type_element(ref, "Hi")
    assert not [c for c in x11.calls if "type" in c]


def test_a_role_cannot_forge_a_reply_line(x11, monkeypatch):
    forged = 'custom\n  [1.1] Button "Pay" enabled'
    bus(monkeypatch, window_tree(Node(forged, "Go", (0, 0, 9, 9), BASE | {"FOCUSABLE"}))[0])
    assert "\n  [1.1]" not in server.ElementTree()


def test_the_env_of_the_process_is_left_as_it_was(x11, monkeypatch):
    # Regression (#10 fix 1/5): a full walk through the real _load_atspi leaves os.environ alone
    session = d.SessionInfo(
        mode="x11",
        display=":0",
        xauthority="/run/user/1000/gdm/Xauthority",
        uid=1000,
        user="newlevel",
        runtime_dir="/run/user/1000",
        dbus_address="unix:path=/run/user/1000/bus",
    )
    monkeypatch.setattr(linux_session, "_SESSION", session)
    keys = ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    atspi = fake_atspi(window_tree(Node("push button", "Go", (0, 0, 9, 9)))[0])
    monkeypatch.setitem(sys.modules, "gi", types.SimpleNamespace(require_version=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "gi.repository", types.SimpleNamespace(Atspi=atspi))
    monkeypatch.setattr(linux_windows, "_load_atspi", _real_load_atspi)
    assert [e["text"] for e in d.get_interactive_elements()] == ["Go"]  # the walk ran end to end
    assert {key: os.environ.get(key) for key in keys} == dict.fromkeys(keys)


def test_the_typed_text_is_never_logged(x11, monkeypatch, caplog):
    marker = "secret" + "-" + "value"
    field = Node("entry", "Name", (10, 20, 100, 30), BASE | {"EDITABLE"}, interfaces=["EditableText"])
    bus(monkeypatch, window_tree(field)[0])
    with caplog.at_level("DEBUG", logger="remoteos"):
        d.type_element(_ref("Name"), marker)
    assert marker not in caplog.text


# ------------------------------------------------------------------ the tools


def test_element_tree_tool_prints_refs_in_image_pixels(x11, monkeypatch):
    bus(monkeypatch, window_tree(Node("push button", "Go", (100, 50, 200, 100)))[0])
    text = server.ElementTree()
    assert "**Elements of 'Editor':** 1" in text
    assert 'Button "Go" enabled (100x50 at 100,50)' in text  # 2560 wide at max_width 1280


def test_click_and_type_tools_reach_the_provider(x11, monkeypatch):
    button = Node("push button", "Go", (0, 0, 9, 9), actions=["click"])
    field = Node("entry", "Name", (0, 20, 9, 9), BASE | {"EDITABLE"}, interfaces=["EditableText"])
    bus(monkeypatch, window_tree(button, field)[0])
    tree = {i.name: i.ref for i in d.element_tree().items}
    assert server.ClickElement(tree["Go"]).startswith("Invoked")
    assert server.TypeElement(tree["Name"], "x").startswith("Set the value")
