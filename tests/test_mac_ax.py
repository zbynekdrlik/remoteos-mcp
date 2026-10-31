"""ElementTree / ClickElement / TypeElement on macOS (#37), and the permission checks, over a fake AX.

``HIServices`` and ``CoreFoundation`` are faked with the calls pyobjc gives (signatures read from
the macOS wheel's ``_metadata.py``); the walk, refs and module are real. No real Mac ran this yet.
"""

from __future__ import annotations

import sys
import time
import types

import pytest
from test_desktop_diagnose import mac_session  # noqa: F401
from tool_surface import server
from tool_test_support import purge_module, tool_error

import remoteos.__main__ as remoteos_main
from remoteos.element_tree import StaleRef
from remoteos.platform.mac import availability, ax

OK, INVALID, NO_VALUE, CANNOT_COMPLETE, UNSUPPORTED_ACTION = 0, -25202, -25212, -25204, -25206
ILLEGAL_ARGUMENT, UNSUPPORTED_ATTRIBUTE, NOT_IMPLEMENTED, API_DISABLED = -25201, -25205, -25208, -25211


class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Size:
    def __init__(self, width, height):
        self.width, self.height = width, height


class Box:
    """An AXValue."""

    def __init__(self, value):
        self.value = value


class Element:
    """An AXUIElement: a dict of attributes."""

    def __init__(self, role, title=None, rect=None, children=(), **attributes):
        self.attributes = {"AXRole": role, "AXChildren": list(children), **attributes}
        if title is not None:
            self.attributes["AXTitle"] = title
        if rect is not None:
            left, top, width, height = rect
            self.attributes["AXPosition"] = Box(Point(left, top))
            self.attributes["AXSize"] = Box(Size(width, height))
        self.settable = set()
        self.actions = []
        self.pressed = 0
        self.press_result = OK
        self.set_result = OK
        self.invalid = False
        self.read_error = 0
        self.writes = []
        for child in children:
            child.attributes["AXParent"] = self


class FakeHI:
    """The HIServices calls ax.py makes."""

    def __init__(self):
        self.system = Element("AXSystemWide")
        self.apps = {}
        self.at_point = None
        self.timeouts = []

    def AXUIElementCreateSystemWide(self):
        return self.system

    def AXUIElementCreateApplication(self, pid):
        return self.apps[pid]

    def AXUIElementSetMessagingTimeout(self, element, seconds):
        self.timeouts.append((element, seconds))
        return OK

    def AXUIElementCopyAttributeValue(self, element, attribute, _):
        if element.invalid:
            return INVALID, None
        if element.read_error:
            return element.read_error, None
        if attribute not in element.attributes:
            return NO_VALUE, None
        return OK, element.attributes[attribute]

    def AXValueGetValue(self, value, kind, _):
        assert kind == (1 if isinstance(value.value, Point) else 2)
        return value.value

    def AXUIElementIsAttributeSettable(self, element, attribute, _):
        return (INVALID, False) if element.invalid else (OK, attribute in element.settable)

    def AXUIElementCopyActionNames(self, element, _):
        return (INVALID, None) if element.invalid else (OK, element.actions)

    def AXUIElementPerformAction(self, element, action):
        element.pressed += 1
        return element.press_result

    def AXUIElementSetAttributeValue(self, element, attribute, value):
        element.writes.append((attribute, value))
        if attribute == "AXFocused" and self.system.attributes.get("focus_follows", True):
            self.system.attributes["AXFocusedUIElement"] = element
        return element.set_result

    def AXUIElementCopyElementAtPosition(self, element, x, y, _):
        return (OK, self.at_point) if self.at_point is not None else (NO_VALUE, None)

    def AXUIElementGetPid(self, element, _):
        return OK, 42


def _failing_children(fake, window, error):
    real = fake.AXUIElementCopyAttributeValue

    def copy(element, attribute, _):
        if element is window and attribute == "AXChildren":
            return error, None
        return real(element, attribute, _)

    return copy


def button(title="Save", rect=(10, 20, 80, 24), **attributes):
    element = Element("AXButton", title, rect, **attributes)
    element.actions = ["AXPress"]
    return element


def field(title="Name", rect=(10, 60, 200, 24)):
    element = Element("AXTextField", title, rect, AXValue="secret")
    element.settable = {"AXValue", "AXFocused"}
    return element


@pytest.fixture
def hi(monkeypatch):
    fake = FakeHI()
    monkeypatch.setitem(sys.modules, "HIServices", fake)
    monkeypatch.setitem(sys.modules, "CoreFoundation", types.SimpleNamespace(CFEqual=lambda a, b: a is b))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return fake


def front(fake: FakeHI, *controls, title="Doc"):
    window = Element("AXWindow", title, (0, 0, 800, 600), controls)
    app = Element("AXApplication", "App", AXFocusedWindow=window, AXWindows=[window])
    fake.system.attributes["AXFocusedApplication"] = app
    fake.apps[42] = app
    return window


def _ref(name: str) -> str:
    return next(i.ref for i in ax.read_tree().items if i.name == name)


class _Clicks(list):
    def __call__(self, x, y):
        self.append((x, y))
        return f"Clicked left at ({x},{y})"


# ------------------------------------------------------------------ the tree


def test_the_tree_uses_the_shared_roles_and_points(hi):
    group = Element("AXGroup", None, (0, 0, 800, 100), [button()])
    off = button("Off", (100, 20, 80, 24), AXEnabled=False)
    front(hi, group, field(), off, Element("AXStaticText", "label", (0, 200, 90, 20)))
    tree = ax.read_tree()
    assert tree.title == "Doc"
    assert [(i.role, i.name, i.enabled, i.rect) for i in tree.items] == [
        ("Button", "Save", True, (10, 20, 90, 44)),
        ("Edit", "Name", True, (10, 60, 210, 84)),
        ("Button", "Off", False, (100, 20, 180, 44)),
    ]


def test_the_messaging_timeout_is_set_on_the_system_wide_element(hi):
    front(hi, button())
    ax.read_tree()
    assert hi.timeouts[0] == (hi.system, 2.0)


@pytest.mark.parametrize(
    ("role", "subrole", "expected"),
    [
        ("AXRadioButton", None, "RadioButton"),
        ("AXRadioButton", "AXTabButton", "TabItem"),
        ("AXPopUpButton", None, "ComboBox"),
        ("AXLink", None, "Hyperlink"),
        ("AXMenuItem", None, "MenuItem"),
        ("AXCheckBox", None, "CheckBox"),
        ("AXRow", None, "ListItem"),
    ],
)
def test_roles(hi, role, subrole, expected):
    extra = {"AXSubrole": subrole} if subrole else {}
    front(hi, Element(role, "x", (0, 0, 9, 9), **extra))
    assert [i.role for i in ax.read_tree().items] == [expected]


def test_a_focusable_element_of_any_role_is_listed(hi):
    custom = Element("AXGroup", "Canvas", (0, 0, 90, 90))
    custom.settable = {"AXFocused"}
    front(hi, custom)
    assert [(i.role, i.name) for i in ax.read_tree().items] == [("Group", "Canvas")]


@pytest.mark.parametrize("attribute", ["AXDescription", "AXPlaceholderValue"])
def test_an_element_without_a_title_uses_its_other_labels(hi, attribute):
    front(hi, Element("AXButton", None, (0, 0, 9, 9), **{attribute: "Close"}))
    assert [i.name for i in ax.read_tree().items] == ["Close"]


def test_a_field_s_value_is_never_read(hi):
    calls = []
    real = hi.AXUIElementCopyAttributeValue
    hi.AXUIElementCopyAttributeValue = lambda e, a, n: calls.append(a) or real(e, a, n)
    front(hi, field())
    assert "secret" not in str(ax.read_tree())
    assert "AXValue" not in calls


def test_a_vanished_element_is_skipped(hi):
    lost = button("Lost")
    lost.invalid = True
    front(hi, lost, button("Here"))
    assert [i.name for i in ax.read_tree().items] == ["Here"]


@pytest.mark.parametrize("error", [CANNOT_COMPLETE, API_DISABLED])
def test_a_hung_app_is_an_error_and_never_an_empty_tree(hi, error):
    window = front(hi)
    hi.AXUIElementCopyAttributeValue = _failing_children(hi, window, error)
    with pytest.raises(RuntimeError, match="did not answer the Accessibility API"):
        ax.read_tree()


def test_a_part_that_stops_answering_costs_that_part_and_the_reply_says_so(hi):
    stuck = button("Stuck")
    stuck.read_error = CANNOT_COMPLETE
    front(hi, button("Fine"), stuck, button("Also fine"))
    tree = ax.read_tree()
    assert [i.name for i in tree.items] == ["Fine", "Also fine"]
    assert tree.truncated == "1 parts of the window did not answer the Accessibility API"


def test_a_control_that_answers_but_not_for_its_children_is_kept(hi):
    group = button("Group")
    front(hi, group, button("Fine"))
    hi.AXUIElementCopyAttributeValue = _failing_children(hi, group, CANNOT_COMPLETE)
    tree = ax.read_tree()
    assert [i.name for i in tree.items] == ["Group", "Fine"]
    assert tree.truncated == "1 parts of the window did not answer the Accessibility API"


def test_a_window_that_goes_while_it_is_read_has_a_message(hi):
    window = front(hi, button())
    window.invalid = True
    with pytest.raises(LookupError, match="went away while it was being read"):
        ax.read_tree()


def test_a_hung_control_is_an_error_too_and_nothing_is_pressed(hi):
    save = button()
    front(hi, save)
    ref = _ref("Save")
    save.read_error = CANNOT_COMPLETE
    with pytest.raises(RuntimeError, match="did not answer the Accessibility API"):
        ax.click_element(ref, _Clicks())
    assert save.pressed == 0


def test_no_front_app_and_no_window_are_errors(hi):
    with pytest.raises(LookupError, match="No foreground window"):
        ax.read_tree()
    hi.system.attributes["AXFocusedApplication"] = Element("AXApplication")
    with pytest.raises(LookupError, match="has no window"):
        ax.read_tree()


def test_a_named_window_comes_from_its_process(hi):
    other = Element("AXWindow", "Other", (0, 0, 9, 9), [button("Other's")])
    wanted = Element("AXWindow", "Wanted", (0, 0, 9, 9), [button("Mine")])
    hi.apps[7] = Element("AXApplication", AXWindows=[other, wanted])
    tree = ax.read_tree(7, "Wanted")
    assert [i.name for i in tree.items] == ["Mine"]
    assert tree.title == "Wanted"


def test_a_process_whose_titles_differ_falls_back_to_its_focused_window(hi):
    window = Element("AXWindow", "Real title", (0, 0, 9, 9), [button("Mine")])
    hi.apps[7] = Element("AXApplication", AXWindows=[window], AXFocusedWindow=window)
    assert ax.read_tree(7, "CG title").title == "Real title"


def test_a_process_without_windows_is_an_error(hi):
    hi.apps[7] = Element("AXApplication")
    with pytest.raises(LookupError, match="shows no window"):
        ax.read_tree(7, "x")


def test_the_lists(hi):
    front(hi, *[button(f"b{i}") for i in range(5)])
    ref = _ref("b0")
    assert ax.read_elements(2)[0]["class"] == "Button"
    assert len(ax.read_elements(2)) == 2
    assert ax.click_element(ref, _Clicks()).startswith("Invoked")  # Snapshot's list ended no ref
    marked, cut = ax.read_marked(2)
    assert cut == "only the first 2 elements are listed"
    assert ax.click_element(marked[1]["ref"], _Clicks()).startswith("Invoked")
    with pytest.raises(StaleRef):
        ax.click_element(ref, _Clicks())


# ------------------------------------------------------------------ ClickElement


def test_click_presses_and_uses_no_mouse(hi):
    save = button()
    front(hi, save)
    ref, clicks = _ref("Save"), _Clicks()
    assert ax.click_element(ref, clicks) == f"Invoked {ref} ('Save')"
    assert save.pressed == 1 and clicks == []


def test_without_axpress_the_mouse_clicks_the_centre_when_the_element_is_on_top(hi):
    inner = Element("AXStaticText", "Save")
    save = button(children=[inner])
    save.actions = []
    front(hi, save)
    hi.at_point = inner  # a part of the element counts
    ref, clicks = _ref("Save"), _Clicks()
    assert ax.click_element(ref, clicks) == f"Clicked left at (50,32): {ref} ('Save') has no usable AXPress"
    assert clicks == [(50, 32)]


@pytest.mark.parametrize("error", [ILLEGAL_ARGUMENT, UNSUPPORTED_ATTRIBUTE, UNSUPPORTED_ACTION, NOT_IMPLEMENTED])
def test_a_refused_press_falls_back_to_the_mouse(hi, error):
    save = button()
    save.press_result = error
    front(hi, save)
    hi.at_point = save
    clicks = _Clicks()
    ax.click_element(_ref("Save"), clicks)
    assert clicks == [(50, 32)]


@pytest.mark.parametrize("error", [CANNOT_COMPLETE, INVALID])
def test_a_press_that_did_not_answer_is_never_followed_by_the_mouse(hi, error):
    save = button()
    save.press_result = error
    front(hi, save)
    hi.at_point = save
    ref, clicks = _ref("Save"), _Clicks()
    with pytest.raises(RuntimeError, match="may still have happened"):
        ax.click_element(ref, clicks)
    assert clicks == []


def test_something_on_top_stops_the_mouse(hi):
    save = button()
    save.actions = []
    front(hi, save)
    hi.at_point = Element("AXSheet")
    ref, clicks = _ref("Save"), _Clicks()
    with pytest.raises(RuntimeError, match="on top of element"):
        ax.click_element(ref, clicks)
    assert clicks == []


def test_an_element_with_no_place_on_screen_is_not_clicked(hi):
    save = button()
    save.actions = []
    front(hi, save)
    ref = _ref("Save")
    save.attributes["AXSize"] = Box(Size(0, 0))
    with pytest.raises(RuntimeError, match="off-screen"):
        ax.click_element(ref, _Clicks())


def test_a_disabled_element_is_refused(hi):
    save = button()
    front(hi, save)
    ref = _ref("Save")
    save.attributes["AXEnabled"] = False
    with pytest.raises(RuntimeError, match="is disabled"):
        ax.click_element(ref, _Clicks())
    assert save.pressed == 0


@pytest.mark.parametrize("change", ["invalid", "role", "name"])
def test_an_element_that_left_is_gone(hi, change):
    save = button()
    front(hi, save)
    ref = _ref("Save")
    if change == "invalid":
        save.invalid = True
    elif change == "role":
        save.attributes["AXRole"] = "AXStaticText"
    else:
        save.attributes["AXTitle"] = "Delete"  # a reused table row
    with pytest.raises(StaleRef, match="is gone: call ElementTree again"):
        ax.click_element(ref, _Clicks())
    assert save.pressed == 0


# ------------------------------------------------------------------ TypeElement


def _typed(text):
    return f"Typed {len(text)} chars"


def test_type_sets_the_value(hi):
    name = field()
    front(hi, name)
    ref = _ref("Name")
    assert ax.type_element(ref, "Žltý", _typed) == f"Set the value of {ref} ('Name') to 4 chars"
    assert name.writes == [("AXValue", "Žltý")]


def test_a_read_only_field_is_refused(hi):
    name = field()
    name.settable = set()
    front(hi, name)
    with pytest.raises(RuntimeError, match="is read-only"):
        ax.type_element(_ref("Name"), "x", lambda t: pytest.fail("typed"))
    assert name.writes == []


@pytest.mark.parametrize("error", [ILLEGAL_ARGUMENT, UNSUPPORTED_ATTRIBUTE])
def test_a_refused_value_is_typed_instead(hi, error):
    name = field()
    name.set_result = error
    front(hi, name)
    ref = _ref("Name")
    assert ax.type_element(ref, "Hi", _typed) == f"Typed 2 chars into {ref} ('Name'), at its caret"
    assert name.writes[0] == ("AXValue", "Hi")


def test_the_typed_text_is_never_logged(hi, caplog):
    marker = "secret" + "-" + "value"
    name = field()
    name.set_result = UNSUPPORTED_ACTION
    front(hi, name)
    with caplog.at_level("DEBUG", logger="remoteos"):
        ax.type_element(_ref("Name"), marker, _typed)
    assert marker not in caplog.text


def test_a_value_that_did_not_answer_may_have_happened(hi):
    name = field()
    name.set_result = CANNOT_COMPLETE
    front(hi, name)
    with pytest.raises(RuntimeError, match="may still have happened"):
        ax.type_element(_ref("Name"), "x", lambda t: pytest.fail("typed"))


def test_another_element_takes_the_focus_and_is_typed_into(hi):
    canvas = Element("AXGroup", "Canvas", (0, 0, 90, 90))
    canvas.settable = {"AXFocused"}
    front(hi, canvas)
    hi.apps[42].attributes["AXFrontmost"] = False
    ref = _ref("Canvas")
    assert ax.type_element(ref, "Hi", _typed) == f"Typed 2 chars into {ref} ('Canvas'), at its caret"
    assert ("AXFrontmost", True) in hi.apps[42].writes
    assert canvas.writes == [("AXFocused", True)]


def test_nothing_is_typed_without_the_focus(hi, monkeypatch):
    canvas = Element("AXGroup", "Canvas", (0, 0, 90, 90))
    canvas.settable = {"AXFocused"}
    front(hi, canvas)
    hi.system.attributes["focus_follows"] = False
    monkeypatch.setattr(ax, "_FOCUS_WAIT_S", 0.0)
    with pytest.raises(RuntimeError, match="did not take the keyboard focus: nothing was typed"):
        ax.type_element(_ref("Canvas"), "Hi", lambda t: pytest.fail("typed"))


# ------------------------------------------------------------------ permissions


def _trust(monkeypatch, trusted):
    monkeypatch.setitem(sys.modules, "HIServices", types.SimpleNamespace(AXIsProcessTrusted=lambda: trusted))


def test_an_untrusted_process_gets_the_settings_pane_named(mac_session, monkeypatch):  # noqa: F811
    mod, _ = mac_session
    _trust(monkeypatch, False)
    reason = mod.diagnose("accessibility")
    assert "System Settings › Privacy & Security › Accessibility" in reason
    assert "read other apps' controls" in reason


def test_a_trusted_process_is_usable(mac_session, monkeypatch):  # noqa: F811
    mod, _ = mac_session
    _trust(monkeypatch, True)
    assert mod.diagnose("accessibility") is None


def test_the_tree_reads_a_locked_screen_as_the_window_list_does(mac_session, monkeypatch):  # noqa: F811
    mod, state = mac_session
    _trust(monkeypatch, True)
    state.session["CGSSessionScreenIsLocked"] = True
    assert mod.diagnose("accessibility") is None


def test_no_session_comes_before_the_permission(mac_session, monkeypatch):  # noqa: F811
    mod, state = mac_session
    _trust(monkeypatch, False)
    state.session = None
    assert "No macOS GUI session" in mod.diagnose("accessibility")


def test_a_missing_framework_never_blocks(mac_session, monkeypatch):  # noqa: F811
    mod, _ = mac_session
    monkeypatch.setitem(sys.modules, "HIServices", None)
    assert mod.diagnose("accessibility") is None
    monkeypatch.setitem(sys.modules, "HIServices", types.SimpleNamespace())  # an older macOS
    assert mod.diagnose("accessibility") is None


def test_the_start_up_report_names_each_problem_once(mac_session, monkeypatch):  # noqa: F811
    mod, state = mac_session
    _trust(monkeypatch, False)
    state.screen_recording = False
    problems = availability.startup_problems()
    assert len(problems) == 2
    assert "Screen Recording" in problems[0]
    assert "read other apps' controls" in problems[1]
    _trust(monkeypatch, True)
    state.screen_recording = True
    assert availability.startup_problems() == []
    state.session = None
    assert len(availability.startup_problems()) == 1  # one cause, one line


def test_the_start_up_prints_the_problems_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(availability, "startup_problems", lambda: ["Allow it."])
    remoteos_main._echo_mac_problems()
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is the JSON-RPC stream under stdio
    assert "[!] Allow it." in captured.err


def test_the_tools_stop_at_the_missing_permission(monkeypatch):
    desktop = types.SimpleNamespace(
        diagnose=lambda need: "Allow it." if need == "accessibility" else None,
        element_tree=lambda window: pytest.fail("walked"),
        click_element=lambda ref: pytest.fail("clicked"),
        type_element=lambda ref, text: pytest.fail("typed"),
    )
    monkeypatch.setattr(server, "get_desktop", lambda: desktop)
    assert tool_error(lambda: server.ElementTree()) == "Allow it."
    assert tool_error(lambda: server.ClickElement("1.1")) == "Allow it."
    assert tool_error(lambda: server.TypeElement("1.1", "x")) == "Allow it."


# ------------------------------------------------------------------ the provider


@pytest.fixture
def provider(monkeypatch):
    """The macOS provider, imported against fakes of what it imports at module level."""
    quartz = types.SimpleNamespace()
    gui = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    purge_module("remoteos.platform.mac.desktop")
    import remoteos.platform.mac.desktop as mod

    yield mod
    purge_module("remoteos.platform.mac.desktop")


def _window(handle, title, pid):
    return types.SimpleNamespace(handle=handle, title=title, pid=pid)


def test_a_window_is_matched_by_title_and_read_from_its_process(provider, hi, monkeypatch):
    hi.apps[7] = Element(
        "AXApplication",
        AXWindows=[Element("AXWindow", "Budget.xlsx", (0, 0, 9, 9), [button("Mine")])],
    )
    monkeypatch.setattr(provider, "enumerate_windows", lambda: [_window(1, "Excel — Budget.xlsx", 7)])
    tree = provider.element_tree("budget")
    assert [i.name for i in tree.items] == ["Mine"]
    assert tree.title == "Budget.xlsx"


def test_a_window_that_matches_nothing_is_an_error(provider, hi, monkeypatch):
    monkeypatch.setattr(provider, "enumerate_windows", lambda: [_window(1, "Excel — Budget", 7)])
    with pytest.raises(LookupError, match="No window matching"):
        provider.element_tree("zzzzzz")


def test_a_failing_read_costs_snapshot_its_list_only(provider, hi):
    assert provider.get_interactive_elements() == []  # no front app


def test_annotated_snapshot_says_the_permission_is_missing(provider, monkeypatch):
    monkeypatch.setattr(provider, "diagnose", lambda need: "Allow it in System Settings.")
    assert provider.marked_elements() == ([], "Allow it in System Settings")


def test_annotated_snapshot_with_the_permission_reads_the_marks(provider, hi, monkeypatch):
    monkeypatch.setattr(provider, "diagnose", lambda need: None)
    front(hi, button())
    elements, cut = provider.marked_elements()
    assert elements[0]["text"] == "Save" and elements[0]["ref"] and cut == ""
    hi.system.attributes.clear()
    assert provider.marked_elements() == ([], "")


def test_the_actions_use_the_provider_s_mouse_and_keys(provider, hi, monkeypatch):
    monkeypatch.setattr(provider, "click", lambda x, y: f"Clicked left at ({x},{y})")
    monkeypatch.setattr(provider, "type_text", lambda text: "Typed 2 chars")
    save, canvas = button(), Element("AXGroup", "Canvas", (0, 0, 90, 90))
    save.actions = []
    canvas.settable = {"AXFocused"}
    front(hi, save, canvas)
    hi.at_point = save
    assert "Clicked left at (50,32)" in provider.click_element(_ref("Save"))
    assert "Typed 2 chars into" in provider.type_element(_ref("Canvas"), "Hi")
