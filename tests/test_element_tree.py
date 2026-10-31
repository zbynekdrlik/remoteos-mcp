"""The element tree's OS-free part and the UIA worker (#35), on fake elements.

``remoteos.element_tree`` and ``snapshot.tree_lines`` are pure. ``platform/win/uia.py`` runs its
real threads here; only the ``uiautomation`` package is faked, at its controls and patterns.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest
from tool_surface import server
from tool_test_support import tool_error

from remoteos.coords import CaptureGeometry, frame_for
from remoteos.element_tree import ElementFacts, RefTable, StaleRef, TreeItem, Walk, listed, walk
from remoteos.platform.win import uia
from remoteos.snapshot import tree_lines

BUTTON = dict(role="Button", name="OK", enabled=True, rect=(10, 10, 110, 40))


def facts(**changes) -> ElementFacts:
    return ElementFacts(**{**BUTTON, **changes})


class Node:
    def __init__(self, fact=None, kids=()):
        self.fact = fact or facts()
        self.kids = list(kids)


def walked(kids, **bounds):
    return walk(Node(kids=kids), lambda n: n.fact, lambda n: n.kids, **bounds)


# -- what is listed ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, True),
        ({"enabled": False}, True),  # a disabled button is worth seeing
        ({"role": "Pane"}, False),
        ({"role": "Pane", "focusable": True}, True),
        ({"offscreen": True}, False),
        ({"rect": (10, 10, 10, 40)}, False),
        ({"rect": (10, 10, 110, 10)}, False),
    ],
)
def test_only_interactive_visible_elements_are_listed(changes, expected):
    assert listed(facts(**changes)) is expected


def test_a_hidden_container_is_walked_but_not_listed_and_the_root_is_not_listed():
    inner = Node(facts(name="inner"))
    result = walked([Node(facts(role="Pane", offscreen=True), [inner])])
    assert [f.name for _, f in result.found] == ["inner"]
    assert result.truncated == ""


def test_the_walk_is_in_reading_order():
    tree = [Node(facts(name="a"), [Node(facts(name="b"))]), Node(facts(name="c"))]
    assert [f.name for _, f in walked(tree).found] == ["a", "b", "c"]


def test_exactly_the_limit_is_complete_one_more_is_truncated():
    assert walked([Node() for _ in range(3)], limit=3).truncated == ""
    result = walked([Node() for _ in range(4)], limit=3)
    assert len(result.found) == 3
    assert result.truncated == "only the first 3 elements are listed"


def test_the_depth_limit_reports_what_it_cuts():
    chain = Node(facts(name="3"), [Node(facts(name="4"))])
    tree = [Node(facts(name="1"), [Node(facts(name="2"), [chain])])]
    result = walked(tree, max_depth=3)
    assert [f.name for _, f in result.found] == ["1", "2", "3"]
    assert "deeper than 3 levels" in result.truncated
    assert walked(tree, max_depth=4).truncated == ""


def test_the_visit_budget_counts_unlisted_elements_too():
    result = walked([Node(facts(role="Pane")) for _ in range(10)], max_visited=4)
    assert result.found == []
    assert result.truncated == "the walk stopped after 4 elements"


def test_running_out_of_time_keeps_what_was_found_and_says_so():
    ticks = iter([False, False, True])
    result = walked([Node(), Node(), Node()], expired=lambda: next(ticks))
    assert len(result.found) == 2
    assert result.truncated == "the walk ran out of time after 2 elements"


def test_an_element_that_vanished_under_the_walk_is_skipped():
    gone, kept = Node(facts(name="gone")), Node(facts(name="kept"))
    result = walk(Node(kids=[gone, kept]), lambda n: None if n is gone else n.fact, lambda n: n.kids)
    assert [f.name for _, f in result.found] == ["kept"]


# -- refs ---------------------------------------------------------------------------------------


def test_a_ref_of_an_older_tree_never_names_an_element_of_the_new_one():
    table = RefTable()
    old = table.publish(["old first", "old second"])
    new = table.publish(["new first", "new second"])
    assert not set(old) & set(new)
    assert table.lookup(new[0]) == "new first"
    with pytest.raises(StaleRef, match="call ElementTree again"):
        table.lookup(old[0])


def test_a_ref_from_before_a_restart_does_not_match_the_new_server():
    first_trees = {RefTable().publish(["x"])[0] for _ in range(50)}
    assert len(first_trees) > 1


@pytest.mark.parametrize("ref", ["", "1", "x", "1.1.1"])
def test_a_ref_that_never_was_one_is_refused(ref):
    table = RefTable()
    table.publish(["x"])
    with pytest.raises(StaleRef):
        table.lookup(ref)


# -- rendering ----------------------------------------------------------------------------------


FRAME = frame_for(0, 1280, CaptureGeometry(x=-1920, y=0, width=3840, height=1080, pixel_width=3840, pixel_height=1080))


def test_a_line_has_role_name_state_and_bounds_in_image_pixels():
    item = TreeItem("7.1", "Button", "OK", True, (-1620, 300, -1320, 390))
    assert tree_lines(FRAME, [item]) == ['  [7.1] Button "OK" enabled (100x30 at 100,100)']


def test_a_disabled_element_says_so():
    item = TreeItem("7.2", "Edit", "", False, (-1620, 300, -1320, 390))
    assert tree_lines(FRAME, [item]) == ['  [7.2] Edit "" disabled (100x30 at 100,100)']


def test_a_name_stays_on_one_short_line_and_its_quotes_are_marked():
    item = TreeItem("7.3", "Text", 'say\n"hi"  ' + "x" * 200, True, (-1620, 300, -1320, 390))
    line = tree_lines(FRAME, [item])[0]
    assert line.startswith('  [7.3] Text "say \\"hi\\" xxx')
    kept = 80 - len('say "hi" ') - 1  # the cut name is 80 long, its last character the ellipsis
    assert "x" * kept + "…" in line and "x" * (kept + 1) not in line
    assert "\n" not in line


def test_an_element_outside_the_frame_is_marked_not_moved():
    item = TreeItem("7.4", "Button", "far", True, (2100, 300, 2400, 390))
    assert tree_lines(FRAME, [item]) == ['  [7.4] Button "far" enabled (100x30 at 1340,100) (off-frame)']


# -- the worker ---------------------------------------------------------------------------------


def worker_of(sessions: list | None = None, **kwargs) -> uia.UiaWorker:
    made = sessions if sessions is not None else []

    def open_session():
        made.append(threading.current_thread())
        return len(made)

    return uia.UiaWorker(open_session, **kwargs)


def test_every_call_runs_on_the_one_worker_thread():
    threads: list = []
    worker = worker_of(threads)
    assert worker.call(lambda s: threading.current_thread(), 5) is threads[0]
    assert worker.call(lambda s: threading.current_thread(), 5) is threads[0]
    assert threads[0] is not threading.current_thread()
    assert len(threads) == 1


def test_a_failure_on_the_worker_reaches_the_caller_and_the_worker_lives_on():
    worker = worker_of()

    def fail(session):
        raise ValueError("no such thing")

    with pytest.raises(ValueError, match="no such thing"):
        worker.call(fail, 5)
    assert worker.call(lambda s: s, 5) == 1


def test_a_hung_call_times_out_and_the_next_call_gets_a_new_worker():
    threads: list = []
    worker = worker_of(threads)
    release = threading.Event()
    with pytest.raises(uia.UiaTimeout):
        worker.call(lambda s: release.wait(10), 0.05)
    assert worker.call(lambda s: s, 5) == 2  # a new session: new client, new ref table
    release.set()
    threads[0].join(5)
    assert not threads[0].is_alive(), "an abandoned worker must end when its call returns"


def test_stuck_workers_are_bounded():
    worker = worker_of()
    release = threading.Event()
    for _ in range(uia.MAX_STUCK_WORKERS):
        with pytest.raises(uia.UiaTimeout):
            worker.call(lambda s: release.wait(10), 0.01)
    with pytest.raises(uia.UiaTimeout, match="stuck"):
        worker.call(lambda s: s, 5)
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            assert worker.call(lambda s: s, 5)
            return
        except uia.UiaTimeout:
            time.sleep(0.01)
    pytest.fail("the worker never came back after the stuck calls returned")


def test_a_session_that_cannot_open_is_reported_and_tried_again():
    attempts = []

    def open_session():
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("No module named 'uiautomation'")
        return "session"

    worker = uia.UiaWorker(open_session)
    with pytest.raises(RuntimeError, match="UI Automation is not available: No module named 'uiautomation'"):
        worker.call(lambda s: s, 5)
    assert worker.call(lambda s: s, 5) == "session"


def test_a_slow_start_is_not_a_hung_worker():
    release = threading.Event()
    started = []

    def open_session():
        started.append(1)
        release.wait(10)
        return "session"

    worker = uia.UiaWorker(open_session)
    with pytest.raises(uia.UiaStarting):
        worker.call(lambda s: s, 0.01)
    release.set()
    assert worker.call(lambda s: s, 5) == "session"
    assert len(started) == 1, "still starting must keep the worker that is starting"


def test_a_start_that_never_ends_is_replaced():
    release = threading.Event()
    started = []

    def open_session():
        started.append(1)
        if len(started) == 1:
            release.wait(10)
        return "session"

    worker = uia.UiaWorker(open_session, start_timeout=0)
    with pytest.raises(uia.UiaStarting):
        worker.call(lambda s: s, 0.01)
    assert worker.call(lambda s: s, 5) == "session"
    release.set()


# -- the session, on fake controls --------------------------------------------------------------


class FakeCom(Exception):
    pass


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def isempty(self):
        return self.right == self.left or self.bottom == self.top

    def xcenter(self):
        return (self.left + self.right) // 2

    def ycenter(self):
        return (self.top + self.bottom) // 2


class Invoke:
    def __init__(self, ok=True, error=None):
        self.ok, self.error, self.calls, self.waits = ok, error, 0, []

    def Invoke(self, waitTime):
        self.calls += 1
        self.waits.append(waitTime)
        if self.error:
            raise self.error
        return self.ok


class Value:
    def __init__(self, read_only=False, ok=True, error=None):
        self.IsReadOnly, self.ok, self.error, self.value, self.waits = read_only, ok, error, None, []

    def SetValue(self, value, waitTime):
        self.waits.append(waitTime)
        if self.error:
            raise self.error
        self.value = value
        return self.ok


class Control:
    def __init__(self, name="OK", kind="ButtonControl", rect=(100, 100, 200, 140), rid=(1, 2), kids=(), **state):
        self.Name, self.ControlTypeName, self.rect, self.rid = name, kind, Rect(*rect), rid
        self.kids, self.parent = list(kids), None
        for kid in self.kids:
            kid.parent = self
        self.IsEnabled, self.IsKeyboardFocusable, self.IsOffscreen = True, False, False
        self.patterns: dict = {}
        self.dead = False
        self.next = None
        self.focus_sets = 0
        self.__dict__.update(state)

    def _touch(self):
        if self.dead:
            raise FakeCom("element not available")

    @property
    def BoundingRectangle(self):
        self._touch()
        return self.rect

    def GetRuntimeId(self):
        self._touch()
        return self.rid

    def GetFirstChildControl(self):
        for a, b in zip(self.kids, self.kids[1:]):
            a.next = b
        return self.kids[0] if self.kids else None

    def GetNextSiblingControl(self):
        return self.next

    def GetParentControl(self):
        return self.parent

    def GetPattern(self, pattern_id):
        return self.patterns.get(pattern_id)

    def SetFocus(self):
        self.focus_sets += 1
        return True


def make_auto(root, focused=None, over=50):
    """A fake ``uiautomation`` module around ``root``: window handle 50 is its own root owner."""
    return types.SimpleNamespace(
        ControlFromHandle=lambda hwnd: root if hwnd == 50 else None,
        PatternId=types.SimpleNamespace(InvokePattern="invoke", ValuePattern="value"),
        GetFocusedControl=lambda: focused,
        WindowFromPoint=lambda x, y: over,
        GetAncestor=lambda hwnd, flag: hwnd if flag == 3 else None,
    )


def session_with(button, **auto):
    root = Control(name="App", kind="WindowControl", kids=[button])
    session = uia.Session(make_auto(root, **auto), FakeCom)
    return session, session.tree(50).items[0].ref


def test_the_tree_has_the_window_title_roles_and_screen_rects():
    tree = uia.Session(make_auto(Control(name="App", kind="WindowControl", kids=[Control()])), FakeCom).tree(50)
    assert tree.title == "App"
    item = tree.items[0]
    assert (item.role, item.name, item.enabled, item.rect) == ("Button", "OK", True, (100, 100, 200, 140))


def test_an_unknown_window_is_a_failure():
    with pytest.raises(LookupError):
        uia.Session(make_auto(Control()), FakeCom).tree(51)


def test_an_element_that_dies_during_the_walk_is_left_out():
    dead, alive = Control(name="dead", dead=True), Control(name="alive", rid=(3,))
    root = Control(name="App", kind="WindowControl", kids=[dead, alive])
    assert [i.name for i in uia.Session(make_auto(root), FakeCom).tree(50).items] == ["alive"]


def test_snapshot_s_list_keeps_the_shape_of_the_consumers_and_ends_no_ref():
    session, ref = session_with(Control())
    assert session.elements(50) == [
        {"index": 1, "class": "Button", "text": "OK", "rect": {"left": 100, "top": 100, "right": 200, "bottom": 140}}
    ]
    assert session.activate(ref).point is not None  # no Invoke here: the ref itself still resolves


def test_invoke_is_used_when_there_is_one_and_no_mouse_moves():
    button = Control()
    button.patterns["invoke"] = pattern = Invoke()
    session, ref = session_with(button)
    assert session.activate(ref) == uia.Activation("OK")
    assert pattern.calls == 1
    assert pattern.waits == [0], "the package sleeps 0.5 s by default"


def test_no_invoke_pattern_means_a_click_at_the_centre():
    session, ref = session_with(Control())
    assert session.activate(ref) == uia.Activation("OK", (150, 120))


def test_a_refused_invoke_falls_back_to_the_click():
    button = Control()
    button.patterns["invoke"] = Invoke(error=FakeCom("nope"))
    session, ref = session_with(button)
    assert session.activate(ref).point == (150, 120)


def test_an_element_that_changed_identity_is_gone_not_clicked():
    button = Control()
    session, ref = session_with(button)
    button.rid = (9, 9)
    with pytest.raises(StaleRef, match="gone: call ElementTree again"):
        session.activate(ref)


def test_a_dead_element_is_gone_not_clicked():
    button = Control()
    session, ref = session_with(button)
    button.dead = True
    with pytest.raises(StaleRef, match="gone: call ElementTree again"):
        session.activate(ref)


def test_a_disabled_element_is_refused():
    button = Control()
    button.patterns["invoke"] = pattern = Invoke()
    session, ref = session_with(button)
    button.IsEnabled = False
    with pytest.raises(RuntimeError, match="is disabled"):
        session.activate(ref)
    assert pattern.calls == 0


def test_a_new_tree_ends_the_old_refs():
    session, ref = session_with(Control())
    session.tree(50)
    with pytest.raises(StaleRef, match="not in the latest tree"):
        session.activate(ref)


def test_a_covered_element_is_refused_rather_than_clicking_what_covers_it():
    session, ref = session_with(Control(), over=77)
    with pytest.raises(RuntimeError, match="Another window covers"):
        session.activate(ref)


@pytest.mark.parametrize("change", [{"IsOffscreen": True}, {"rect": Rect(5, 5, 5, 5)}])
def test_an_element_that_left_the_screen_is_refused(change):
    button = Control()
    session, ref = session_with(button)
    button.__dict__.update(change)
    with pytest.raises(RuntimeError, match="off-screen"):
        session.activate(ref)


def test_text_goes_through_the_value_pattern_without_the_keyboard():
    field = Control(name="Name", kind="EditControl")
    field.patterns["value"] = pattern = Value()
    session, ref = session_with(field)
    assert session.set_text(ref, "Žltý kôň") == ("Name", True)
    assert pattern.value == "Žltý kôň"
    assert pattern.waits == [0]
    assert field.focus_sets == 0


def test_a_read_only_value_is_refused_and_nothing_is_typed():
    field = Control(name="Name", kind="EditControl")
    field.patterns["value"] = Value(read_only=True)
    session, ref = session_with(field)
    with pytest.raises(RuntimeError, match="read-only"):
        session.set_text(ref, "x")
    assert field.focus_sets == 0


@pytest.mark.parametrize("pattern", [None, Value(error=FakeCom("nope")), Value(ok=False)], ids=["none", "raises", "no"])
def test_no_usable_value_means_focus_and_keystrokes(pattern):
    field = Control(name="Doc", kind="DocumentControl", IsKeyboardFocusable=True)
    if pattern:
        field.patterns["value"] = pattern
    session, ref = session_with(field, focused=field)
    assert session.set_text(ref, "x") == ("Doc", False)
    assert field.focus_sets == 1


def test_the_focus_on_an_inner_control_counts():
    inner = Control(name="inner", kind="EditControl", rid=(7,))
    field = Control(name="Box", kind="ComboBoxControl", kids=[inner])
    session, ref = session_with(field, focused=inner)
    assert session.set_text(ref, "x") == ("Box", False)


def test_a_focus_that_went_elsewhere_types_nothing():
    other = Control(name="other", rid=(8,))
    session, ref = session_with(Control(kind="EditControl"), focused=other)
    with pytest.raises(RuntimeError, match="did not take the keyboard focus"):
        session.set_text(ref, "x")


def test_no_focus_anywhere_types_nothing():
    session, ref = session_with(Control(kind="EditControl"), focused=None)
    with pytest.raises(RuntimeError, match="did not take the keyboard focus"):
        session.set_text(ref, "x")


# -- click_element / type_element around the worker ---------------------------------------------


class Raises:
    """A worker whose call ends in ``error``, as ``UiaWorker.call`` does for a hung Invoke."""

    def __init__(self, error):
        self.error = error

    def call(self, fn, timeout):
        raise self.error


def test_a_hung_invoke_never_falls_through_to_a_click(monkeypatch):
    monkeypatch.setattr(uia, "_worker", Raises(uia.UiaTimeout("hung")))
    clicks = []
    with pytest.raises(RuntimeError, match="may still have happened"):
        uia.click_element("1.1", lambda x, y: clicks.append((x, y)))
    assert clicks == []


def test_a_hung_set_value_is_never_typed_as_well(monkeypatch):
    monkeypatch.setattr(uia, "_worker", Raises(uia.UiaTimeout("hung")))
    typed = []
    with pytest.raises(RuntimeError, match="may still have happened"):
        uia.type_element("1.1", "x", typed.append)
    assert typed == []


def test_a_start_timeout_says_nothing_happened(monkeypatch):
    monkeypatch.setattr(uia, "_worker", Raises(uia.UiaStarting("still starting")))
    with pytest.raises(uia.UiaStarting):
        uia.click_element("1.1", lambda x, y: "clicked")


# -- open_session against a fake package --------------------------------------------------------


def test_a_session_gets_its_own_client_and_silences_the_package_s_logger(monkeypatch):
    calls = []

    class Client:
        _instance = "the old worker's client"

        @classmethod
        def instance(cls):
            cls._instance = "this thread's client"
            return cls._instance

    class Logger:
        @staticmethod
        def Write(*args, **kwargs):
            raise AssertionError("the package's logger writes to sys.stdout, the JSON-RPC stream under stdio")

    module = types.ModuleType("uiautomation.uiautomation")
    module._AutomationClient, module.Logger = Client, Logger
    module.InitializeUIAutomationInCurrentThread = lambda: calls.append("coinit")
    package = types.ModuleType("uiautomation")
    package.uiautomation = module
    comtypes = types.ModuleType("comtypes")
    comtypes.COMError = FakeCom
    monkeypatch.setitem(sys.modules, "uiautomation", package)
    monkeypatch.setitem(sys.modules, "uiautomation.uiautomation", module)
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setattr(uia, "_retired_clients", [])

    session = uia.open_session()

    assert calls == ["coinit"]
    assert Client._instance == "this thread's client"
    assert uia._retired_clients == ["the old worker's client"], "a hung apartment's client must never be released"
    assert session.com_error is FakeCom
    module.Logger.Write("a line", 1, writeToFile=True)


# -- the tools ----------------------------------------------------------------------------------


@pytest.fixture
def tool_box(monkeypatch, win_session):
    """The real Windows provider and tools; a real worker running a session on fake controls."""
    mod, _ = win_session
    button = Control(name="Save", rect=(40, 40, 140, 80))
    field = Control(name="Name", kind="EditControl", rect=(40, 100, 140, 140), rid=(5,))
    root = Control(name="Editor", kind="WindowControl", kids=[button, field])
    worker = uia.UiaWorker(lambda: uia.Session(make_auto(root), FakeCom))
    monkeypatch.setattr(uia, "_worker", worker)
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: 50)
    monkeypatch.setattr(server, "get_desktop", lambda: mod)
    return types.SimpleNamespace(mod=mod, button=button, field=field)


def ref_of(reply: str, name: str) -> str:
    line = next(line for line in reply.split("\n") if f'"{name}"' in line)
    return line.split("[", 1)[1].split("]", 1)[0]


def test_the_tree_tool_prints_the_contract_and_the_elements(tool_box):
    reply = server.ElementTree()
    assert reply.startswith("**Coordinates:** Image 1280x")
    assert "**Elements of 'Editor':** 2" in reply
    assert f'[{ref_of(reply, "Save")}] Button "Save" enabled' in reply
    assert "Incomplete" not in reply


def test_the_tree_tool_says_when_the_list_is_incomplete(tool_box, monkeypatch):
    monkeypatch.setattr(uia, "walk", lambda root, read, children, expired: Walk([], "cut short"))
    assert "**Incomplete:** cut short." in server.ElementTree()


def test_the_tree_tool_finds_a_window_by_title(tool_box, monkeypatch):
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: 0)
    monkeypatch.setattr(tool_box.mod, "enumerate_windows", lambda: [types.SimpleNamespace(handle=50, title="Editor")])
    assert "Editor" in server.ElementTree(window="Edit")
    assert "No foreground window" in tool_error(lambda: server.ElementTree())
    assert "No window matching" in tool_error(lambda: server.ElementTree(window="zzzzzzzz"))


def test_click_element_invokes(tool_box):
    tool_box.button.patterns["invoke"] = pattern = Invoke()
    ref = ref_of(server.ElementTree(), "Save")
    assert server.ClickElement(ref) == f"Invoked {ref} ('Save')"
    assert pattern.calls == 1


def test_click_element_clicks_the_centre_through_the_provider(tool_box, monkeypatch):
    clicks = []
    monkeypatch.setattr(tool_box.mod, "click", lambda x, y: clicks.append((x, y)) or f"Clicked left at ({x},{y})")
    ref = ref_of(server.ElementTree(), "Save")
    reply = server.ClickElement(ref)
    assert clicks == [(90, 60)]
    assert reply == f"Clicked left at (90,60): {ref} ('Save') has no usable Invoke pattern"


def test_a_stale_ref_is_a_tool_error_asking_for_a_new_tree(tool_box):
    ref = ref_of(server.ElementTree(), "Save")
    server.ElementTree()
    assert "call ElementTree again" in tool_error(lambda: server.ClickElement(ref))


def test_type_element_sets_the_value(tool_box):
    tool_box.field.patterns["value"] = pattern = Value()
    ref = ref_of(server.ElementTree(), "Name")
    assert server.TypeElement(ref, "Žltý") == f"Set the value of {ref} ('Name') to 4 chars"
    assert pattern.value == "Žltý"


def test_type_element_types_when_there_is_no_value(tool_box, monkeypatch):
    uia._worker.call(lambda s: setattr(s.auto, "GetFocusedControl", lambda: tool_box.field), 5)
    typed = []
    monkeypatch.setattr(tool_box.mod, "type_text", lambda text: typed.append(text) or "Typed 1 chars")
    ref = ref_of(server.ElementTree(), "Name")
    assert server.TypeElement(ref, "x") == f"Typed 1 chars into {ref} ('Name'), at its caret"
    assert typed == ["x"]


def test_snapshot_lists_the_uia_elements(tool_box):
    assert tool_box.mod.get_interactive_elements()[0]["text"] == "Save"


def test_a_failing_uia_costs_snapshot_its_list_only(tool_box, monkeypatch, caplog):
    def down(fn, timeout):
        raise uia.UiaTimeout("the window did not answer")

    monkeypatch.setattr(uia._worker, "call", down)
    assert tool_box.mod.get_interactive_elements() == []
    assert "no UI Automation element list (UiaTimeout)" in caplog.text


def test_no_foreground_window_is_an_empty_list(win_session):
    mod, _ = win_session
    assert mod.get_interactive_elements() == []


@pytest.mark.parametrize(
    "call",
    [lambda: server.ElementTree(), lambda: server.ClickElement("1.1"), lambda: server.TypeElement("1.1", "x")],
    ids=["ElementTree", "ClickElement", "TypeElement"],
)
def test_the_other_oses_report_not_available_yet(monkeypatch, call):
    monkeypatch.setattr(server, "get_desktop", lambda: types.SimpleNamespace(diagnose=lambda need: None))
    assert "is not available on this OS yet" in tool_error(call)
