"""UI Automation gives up on an app inside the call's budget (#114), on fake controls.

UI Automation's own wait for an app (TransactionTimeout) is 20 s by default, longer than every
call budget, so an app that did not answer used to abandon the worker. Now the worker bounds it
per job and the session re-bounds it before each read; its timeout HRESULTs are timeouts, never
a vanished element, a gone ref, or a refusal that the mouse or the keys would act on again.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest
from uia_fakes import Control, FakeCom, Invoke, Value, make_auto, new_session, session_with

from remoteos.element_tree import StaleRef
from remoteos.platform.win import uia

# UI Automation gave up waiting for the app: UIA_E_TIMEOUT, HRESULT_FROM_WIN32(ERROR_TIMEOUT), signed (comtypes).
TIMEOUTS = [v - 2**32 for v in (0x80131505, 0x800705B4)]


def timed_out(hresult: int) -> FakeCom:
    error = FakeCom("the app did not answer")
    error.hresult = hresult
    return error


# -- the worker and the session's bound ---------------------------------------------------------


def test_each_job_lets_ui_automation_wait_only_for_what_its_call_has_left():
    seen: list = []

    def answer_within(session, seconds):
        seen.append(("bound", session, seconds, threading.current_thread()))

    worker = uia.UiaWorker(lambda abandoned: "session", answer_within=answer_within)
    assert worker.call(lambda s: seen.append(("job", threading.current_thread())) or "done", 8) == "done"
    (_, session, seconds, bound_on), (_, job_on) = seen
    assert session == "session" and bound_on is job_on is not threading.current_thread()
    assert 8 - uia.ANSWER_MARGIN_S - 1 < seconds <= 8 - uia.ANSWER_MARGIN_S


class Automation:
    """The client's IUIAutomation2: records each TransactionTimeout put."""

    def __init__(self):
        self.puts: list[int] = []

    @property
    def TransactionTimeout(self):
        return self.puts[-1]

    @TransactionTimeout.setter
    def TransactionTimeout(self, ms):
        self.puts.append(ms)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_every_read_gets_only_what_the_job_has_left_never_less_than_the_floor(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(uia, "time", clock)
    automation = Automation()

    class Slow(Control):
        @property
        def BoundingRectangle(self):
            clock.now += 3  # each element's read takes 3 s of the job
            return self.rect

    kids = [Slow(name=f"b{i}", rid=(i + 1,)) for i in range(3)]
    root = Control(name="App", kind="WindowControl", kids=kids)
    session = uia.Session(new_session(root).auto, FakeCom, threading.Event(), automation=automation)
    session.answer_within(7.5)
    session.tree(50)
    assert automation.puts[0] == 7500, "the job's whole share before its first read"
    assert automation.puts == sorted(automation.puts, reverse=True), "what is left only shrinks"
    assert 4500 in automation.puts and 1500 in automation.puts, "re-set before each element's read"
    assert automation.puts[-1] == uia.MIN_ANSWER_MS, "a spent job still lets a responsive app answer"


def test_a_session_with_no_bound_leaves_ui_automation_alone():
    automation = Automation()
    session = uia.Session(
        new_session(Control(name="App", kids=[Control()])).auto, FakeCom, threading.Event(), automation=automation
    )
    session.tree(50)
    assert automation.puts == []


# -- every single call into the app is bounded anew --------------------------------------------

PROPERTIES = {"BoundingRectangle", "ControlTypeName", "Name", "IsEnabled", "IsKeyboardFocusable", "IsOffscreen"}
METHODS = {
    "GetRuntimeId",
    "GetFirstChildControl",
    "GetNextSiblingControl",
    "GetParentControl",
    "GetPattern",
    "SetFocus",
}


class Ledger:
    """Each call into the app checks that the limit in force is what the job had left at that call."""

    def __init__(self, monkeypatch):
        self.clock = Clock()
        monkeypatch.setattr(uia, "time", self.clock)
        self.automation = Automation()
        self.session = None
        self.calls: list[str] = []

    def call(self, name: str) -> None:
        left = max(uia.MIN_ANSWER_MS, int((self.session.answer_by - self.clock.now) * 1000))
        assert self.automation.puts[-1] == left, f"{name}: a stale limit, not re-set before this call"
        self.calls.append(name)
        self.clock.now += 0.25  # every call takes time


def ticking(ledger: Ledger, base: type) -> type:
    class Ticking(base):
        def __getattribute__(self, name):
            value = super().__getattribute__(name)
            if name in PROPERTIES:
                ledger.call(name)
            elif name in METHODS or name in ("Invoke", "SetValue"):

                def called(*args, **kwargs):
                    ledger.call(name)
                    return value(*args, **kwargs)

                return called
            elif name == "IsReadOnly":
                ledger.call(name)
            return value

    return Ticking


@pytest.fixture
def ledger(monkeypatch):
    return Ledger(monkeypatch)


def _app(ledger, field_patterns=None, button_patterns=None, focused_field=False):
    tick = ticking(ledger, Control)
    label = tick(name="", kind="TextControl", rid=(9,))  # the part of the button a hit test lands on
    button = tick(name="Save", rid=(2,), kids=[label])
    field = tick(name="Name", kind="EditControl", rect=(100, 200, 300, 240), rid=(3,))
    button.patterns.update(button_patterns or {})
    field.patterns.update(field_patterns or {})
    root = tick(name="App", kind="WindowControl", rect=(0, 0, 500, 500), rid=(100,), kids=[button, field])
    auto = make_auto(root, focused=field if focused_field else None)
    for name in ("ControlFromHandle", "ControlFromPoint", "GetFocusedControl"):
        real = getattr(auto, name)
        setattr(auto, name, lambda *a, _real=real, _name=name: ledger.call(_name) or _real(*a))
    ledger.session = uia.Session(auto, FakeCom, threading.Event(), automation=ledger.automation)
    ledger.session.answer_within(100)
    refs = {item.name: item.ref for item in ledger.session.tree(50).items}
    ledger.session.answer_within(100)
    return refs


def test_every_call_of_a_walk_is_bounded_anew(ledger):
    _app(ledger)
    assert {"ControlFromHandle", "BoundingRectangle", "IsOffscreen", "GetNextSiblingControl", "Name"} <= set(
        ledger.calls
    )
    assert len(ledger.calls) > 20


def test_every_call_of_an_invoke_is_bounded_anew(ledger):
    refs = _app(ledger, button_patterns={"invoke": ticking(ledger, Invoke)()})
    del ledger.calls[:]
    ledger.session.activate(refs["Save"])
    assert ledger.calls == ["GetRuntimeId", "IsEnabled", "GetPattern", "Invoke"]


def test_every_call_of_a_click_s_hit_test_is_bounded_anew(ledger):
    refs = _app(ledger)
    del ledger.calls[:]
    ledger.session.activate(refs["Save"])
    hit_test = ["ControlFromPoint", "GetRuntimeId", "GetParentControl", "GetRuntimeId"]
    assert ledger.calls[3:] == ["BoundingRectangle", "IsOffscreen", *hit_test]


def test_every_call_of_a_value_set_is_bounded_anew(ledger):
    refs = _app(ledger, field_patterns={"value": ticking(ledger, Value)()})
    del ledger.calls[:]
    ledger.session.set_text(refs["Name"], "x")
    assert ledger.calls == ["GetRuntimeId", "IsEnabled", "GetPattern", "IsReadOnly", "SetValue"]


def test_every_call_of_a_focus_is_bounded_anew(ledger):
    refs = _app(ledger, focused_field=True)
    del ledger.calls[:]
    ledger.session.set_text(refs["Name"], "x")
    assert ledger.calls[3:] == ["SetFocus", "GetFocusedControl", "GetRuntimeId"]


# -- a timeout is a timeout wherever the session reads the app ----------------------------------


def _lookup(error):
    session = new_session(Control())

    def unanswered(hwnd):
        raise error

    session.auto.ControlFromHandle = unanswered
    return lambda: session.tree(50)


def _walk_read(what):
    def setup(error):
        slow = Control(name="slow", rid=(2,))
        slow.fails[what] = error
        root = Control(name="App", kind="WindowControl", kids=[slow, Control(name="alive", rid=(3,))])
        return lambda: new_session(root).tree(50)

    return setup


def _title(error):
    class Silent(Control):
        @property
        def Name(self):
            raise error

        @Name.setter
        def Name(self, value):
            pass

    root = Silent(kind="WindowControl", kids=[Control(rid=(3,))])
    return lambda: new_session(root).tree(50)


def _after_tree(what, act="activate"):
    def setup(error):
        button = Control(kind="EditControl")
        session, ref = session_with(button)
        button.fails[what] = error
        return lambda: getattr(session, act)(ref) if act == "activate" else session.set_text(ref, "x")

    return setup


def _hit_test(error):
    session, ref = session_with(Control())

    def unanswered(x, y):
        raise error

    session.auto.ControlFromPoint = unanswered
    return lambda: session.activate(ref)


def _read_only(error):
    class Asking:
        @property
        def IsReadOnly(self):
            raise error

    field = Control(kind="EditControl")
    field.patterns["value"] = Asking()
    session, ref = session_with(field)
    return lambda: session.set_text(ref, "x")


def _focus_check(error):
    field = Control(kind="EditControl")
    session, ref = session_with(field)

    def unanswered():
        raise error

    session.auto.GetFocusedControl = unanswered
    return lambda: session.set_text(ref, "x")


NOTHING, FOCUS = "nothing was done to it", "nothing was typed, the focus may have moved"
READS = {
    "window lookup": (_lookup, NOTHING),
    "element read": (_walk_read("rect"), NOTHING),
    "children": (_walk_read("kids"), NOTHING),
    "runtime id of a listed element": (_walk_read("rid"), NOTHING),
    "window title": (_title, NOTHING),
    "ref check": (_after_tree("rid"), NOTHING),
    "centre for the mouse": (_after_tree("rect"), NOTHING),
    "pattern lookup": (_after_tree("pattern"), NOTHING),
    "hit test": (_hit_test, NOTHING),
    "read-only check": (_read_only, NOTHING),
    "focus": (_after_tree("focus", act="set_text"), FOCUS),
    "focus check": (_focus_check, FOCUS),
}


@pytest.mark.parametrize("hresult", TIMEOUTS)
@pytest.mark.parametrize("read", READS)
def test_a_read_the_app_did_not_answer_is_a_timeout_that_says_what_was_done(read, hresult):
    setup, done = READS[read]
    with pytest.raises(uia.UiaUnanswered, match=f"did not answer UI Automation in time: {done}$") as caught:
        setup(timed_out(hresult))()
    assert not isinstance(caught.value, StaleRef)


@pytest.mark.parametrize("hresult", TIMEOUTS)
def test_an_invoke_the_app_did_not_answer_may_have_happened_and_is_never_clicked(hresult):
    button = Control()
    button.patterns["invoke"] = pattern = Invoke(error=timed_out(hresult))
    session, ref = session_with(button)
    with pytest.raises(uia.UiaActionUnanswered, match="may have happened"):
        session.activate(ref)
    assert pattern.calls == 1


@pytest.mark.parametrize("hresult", TIMEOUTS)
def test_a_value_set_the_app_did_not_answer_may_have_happened_and_is_never_typed(hresult):
    field = Control(kind="EditControl")
    field.patterns["value"] = Value(error=timed_out(hresult))
    session, ref = session_with(field)
    with pytest.raises(uia.UiaActionUnanswered, match="may have happened"):
        session.set_text(ref, "x")
    assert field.focus_sets == 0


class Raises:
    def __init__(self, error):
        self.error = error

    def call(self, fn, timeout, needs=0):
        raise self.error


@pytest.mark.parametrize("error", [uia.UiaUnanswered("nothing was done to it"), uia.UiaActionUnanswered("it may have")])
def test_click_and_type_pass_on_what_the_session_said_happened(monkeypatch, error):
    monkeypatch.setattr(uia, "_worker", Raises(error))
    with pytest.raises(type(error), match=str(error)):
        uia.click_element("1.1", lambda x, y: pytest.fail("clicked"))
    with pytest.raises(type(error), match=str(error)):
        uia.type_element("1.1", "x", lambda text: pytest.fail("typed"))


# -- open_session against a fake package --------------------------------------------------------


def test_a_session_gets_its_own_client_and_silences_the_package_s_logger(monkeypatch):
    calls = []

    class Client:
        _instance = None

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
    client_module = types.ModuleType("comtypes.client")
    eight = types.SimpleNamespace(RawViewWalker="raw walker of the IUIAutomation2 object")

    def create_object(clsid, interface):
        calls.append(("create", clsid, interface))
        return eight

    client_module.CreateObject = create_object
    comtypes.client = client_module
    Client.UIAutomationCore = types.SimpleNamespace(IUIAutomation2="IUIAutomation2")
    monkeypatch.setitem(sys.modules, "uiautomation", package)
    monkeypatch.setitem(sys.modules, "uiautomation.uiautomation", module)
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", client_module)
    monkeypatch.setattr(uia, "_retired_clients", [])
    old = Client._instance = Client()

    session = uia.open_session(threading.Event())

    # CUIAutomation8 as IUIAutomation2: the client whose own wait for an app can be set (#114)
    assert calls == ["coinit", ("create", "{e22ad333-b25f-460c-83d0-0581107395c9}", "IUIAutomation2")]
    assert isinstance(Client._instance, Client) and Client._instance is not old
    assert Client._instance.IUIAutomation is eight and Client._instance.ViewWalker == eight.RawViewWalker
    assert session.automation is eight
    assert uia._retired_clients == [old], "a hung apartment's client must never be released"
    assert session.com_error is FakeCom
    module.Logger.Write("a line", 1, writeToFile=True)

    abandoned = threading.Event()
    abandoned.set()
    live = Client._instance
    with pytest.raises(RuntimeError, match="abandoned"):
        uia.open_session(abandoned)
    assert Client._instance is live, "a start that was given up on must not take the client of the new worker"
