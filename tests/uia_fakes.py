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


class Control:
    def __init__(self, name="OK", kind="ButtonControl", rect=(100, 100, 200, 140), rid=(1, 2), kids=(), **state):
        self.Name, self.ControlTypeName, self.rect, self.rid = name, kind, Rect(*rect), rid
        self.kids, self.parent = list(kids), None
        for kid in self.kids:
            kid.parent = self
        self.IsEnabled, self.IsKeyboardFocusable, self.IsOffscreen = True, False, False
        self.patterns: dict = {}
        self.dead = False
        self.fails: dict = {}  # read -> the COM error it raises ("rect", "rid", "kids", "pattern", "focus")
        self.next = None
        self.focus_sets = 0
        self.__dict__.update(state)

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

    def GetFirstChildControl(self):
        if "kids" in self.fails:
            raise self.fails["kids"]
        for a, b in zip(self.kids, self.kids[1:]):
            a.next = b
        return self.kids[0] if self.kids else None

    def GetNextSiblingControl(self):
        return self.next

    def GetParentControl(self):
        return self.parent

    def GetPattern(self, pattern_id):
        if "pattern" in self.fails:
            raise self.fails["pattern"]
        return self.patterns.get(pattern_id)

    def SetFocus(self):
        if "focus" in self.fails:
            raise self.fails["focus"]
        self.focus_sets += 1
        return True


def make_auto(root, focused=None, at_point=None, window_open=True):
    """A fake ``uiautomation`` module around ``root``, window handle 50.

    ``ControlFromPoint`` answers ``at_point``, or the element itself when ``None``: pass the
    covering control to model a dialog over it. It is what a hit test really returns.
    """
    return types.SimpleNamespace(
        ControlFromHandle=lambda hwnd: root if hwnd == 50 else None,
        PatternId=types.SimpleNamespace(InvokePattern="invoke", ValuePattern="value"),
        GetFocusedControl=lambda: focused,
        ControlFromPoint=lambda x, y: at_point if at_point is not None else _at(root, x, y),
        IsWindowVisible=lambda hwnd: window_open,
    )


def _at(control, x, y):
    for kid in control.kids:
        rect = kid.rect
        if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
            return _at(kid, x, y)
    return control


def new_session(root, **auto):
    return uia.Session(make_auto(root, **auto), FakeCom, threading.Event())


def session_with(button, **auto):
    root = Control(name="App", kind="WindowControl", rect=(0, 0, 500, 500), rid=(100,), kids=[button])
    session = new_session(root, **auto)
    return session, session.tree(50).items[0].ref
