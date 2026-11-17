"""Fakes of the ``uiautomation`` package's controls and patterns, for the UIA worker tests (#35, #114).

Only the package is faked: the session, the worker and their threads are the real ones.
"""

from __future__ import annotations

import threading
import types

from remoteos.platform.win import uia


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
    """Real semantics: a failed HRESULT raises, so a False return (S_FALSE) was accepted."""

    def __init__(self, returns=True, error=None):
        self.returns, self.error, self.calls, self.waits = returns, error, 0, []

    def Invoke(self, waitTime):
        self.calls += 1
        self.waits.append(waitTime)
        if self.error:
            raise self.error
        return self.returns


class Value:
    def __init__(self, read_only=False, returns=True, error=None):
        self.IsReadOnly, self.returns, self.error, self.value, self.waits = read_only, returns, error, None, []

    def SetValue(self, value, waitTime):
        self.waits.append(waitTime)
        if self.error:
            raise self.error
        self.value = value
        return self.returns


class Element:
    """A control's ``IUIAutomationElement``: the raw calls the session makes itself (#114)."""

    def __init__(self, control):
        self.control = control

    def GetCurrentPattern(self, pattern_id):
        self.control._touch("pattern")
        return self.control.patterns.get(pattern_id)

    def SetFocus(self):
        self.control._touch("focus")
        self.control.focus_sets += 1
        return 0


class Control:
    """A control of the package. Its navigation and its GetPattern/SetFocus are the package's
    wrappers: they SWALLOW a COM error (None / False), as uiautomation 2.0.29 does, so the session
    goes to the raw ``Element`` and the client's walker for them instead."""

    def __init__(self, name="OK", kind="ButtonControl", rect=(100, 100, 200, 140), rid=(1, 2), kids=(), **state):
        self.Name, self.ControlTypeName, self.rect, self.rid = name, kind, Rect(*rect), rid
        self.kids, self.parent = list(kids), None
        for kid in self.kids:
            kid.parent = self
        self.IsEnabled, self.IsKeyboardFocusable, self.IsOffscreen = True, False, False
        self.patterns: dict = {}
        self.dead = False
        self.fails: dict = {}  # call -> its COM error: rect rid kids sibling parent type pattern focus
        self.next = None
        self.focus_sets = 0
        self.__dict__.update(state)

    @property
    def Element(self):
        return Element(self)

    def _touch(self, what):
        if what in self.fails:
            raise self.fails[what]
        if self.dead:
            raise FakeCom("element not available")

    @property
    def BoundingRectangle(self):
        self._touch("rect")
        return self.rect

    def GetRuntimeId(self):
        self._touch("rid")
        return self.rid

    def first_child(self):
        if "kids" in self.fails:
            raise self.fails["kids"]
        for a, b in zip(self.kids, self.kids[1:]):
            a.next = b
        return self.kids[0] if self.kids else None

    def GetPattern(self, pattern_id):
        try:
            return self.Element.GetCurrentPattern(pattern_id)
        except FakeCom:
            return None

    def SetFocus(self):
        try:
            return self.Element.SetFocus() == 0
        except FakeCom:
            return False


class Walker:
    """The client's raw view walker: one call into the app each, an element or None.

    ``fails["sibling"]`` / ``fails["parent"]`` on the element walked FROM make that step fail. A
    ``dead`` element still navigates here (a known limit: the real walker may refuse it), which is
    what lets a dead element be skipped while the walk goes on to its siblings.
    """

    def GetFirstChildElement(self, element):
        child = element.control.first_child()
        return child.Element if child is not None else None

    def GetNextSiblingElement(self, element):
        if "sibling" in element.control.fails:
            raise element.control.fails["sibling"]
        return element.control.next.Element if element.control.next is not None else None

    def GetParentElement(self, element):
        if "parent" in element.control.fails:
            raise element.control.fails["parent"]
        return element.control.parent.Element if element.control.parent is not None else None


class Automation:
    """The client's IUIAutomation2 around ``root``, window handle 50; records each TransactionTimeout put.

    ``ElementFromPoint`` answers ``at_point``, or the element itself when ``None``: pass the
    covering control to model a dialog over it. It is what a hit test really returns.
    """

    def __init__(self, root, focused=None, at_point=None):
        self.root, self.focused, self.at_point = root, focused, at_point
        self.RawViewWalker = Walker()
        self.puts: list[int] = []

    @property
    def TransactionTimeout(self):
        return self.puts[-1]

    @TransactionTimeout.setter
    def TransactionTimeout(self, ms):
        self.puts.append(ms)

    def ElementFromHandle(self, hwnd):
        return self.root.Element if hwnd == 50 else None

    def ElementFromPoint(self, point):
        hit = self.at_point if self.at_point is not None else _at(self.root, point.x, point.y)
        return hit.Element

    def GetFocusedElement(self):
        return self.focused.Element if self.focused is not None else None


def make_auto(window_open=True):
    """A fake ``uiautomation`` module: what the session uses of it besides the client."""
    return types.SimpleNamespace(
        PatternId=types.SimpleNamespace(InvokePattern="invoke", ValuePattern="value"),
        Control=types.SimpleNamespace(CreateControlFromElement=_control_of),
        CreatePattern=_pattern_of,
        IsWindowVisible=lambda hwnd: window_open,
    )


def _control_of(element):
    """The package reads the element's ``CurrentControlType``: a call into the app, which can fail."""
    if not element:
        return None
    if "type" in element.control.fails:
        raise element.control.fails["type"]
    return element.control


def _pattern_of(pattern_id, raw):
    """The package's ``CreatePattern``: a QueryInterface of the raw pattern, which can be refused."""
    if getattr(raw, "refuses_interface", None) is not None:
        raise raw.refuses_interface
    return raw


def _at(control, x, y):
    for kid in control.kids:
        rect = kid.rect
        if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
            return _at(kid, x, y)
    return control


def new_session(root, window_open=True, abandoned=None, **client):
    """A session on ``root`` with its own fake client (``focused``, ``at_point``)."""
    automation = Automation(root, **client)
    return uia.Session(make_auto(window_open), FakeCom, abandoned or threading.Event(), automation)


def session_with(button, **auto):
    root = Control(name="App", kind="WindowControl", rect=(0, 0, 500, 500), rid=(100,), kids=[button])
    session = new_session(root, **auto)
    return session, session.tree(50).items[0].ref
