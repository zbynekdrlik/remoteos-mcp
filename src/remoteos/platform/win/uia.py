"""Windows UI Automation (#35): the real element tree, read through the ``uiautomation`` package.

UIA objects belong to the COM apartment that made them, so one dedicated thread owns every
one of them: the ``uiautomation`` import (``import comtypes`` initializes COM on the importing
thread), the client, the controls and the ref table. Everything else talks to it through
:class:`UiaWorker`, with a timeout, and gets plain data back. A call into a hung app cannot be
cancelled, so a worker that overruns is abandoned and the next call starts a new one, with a
new client and an empty ref table. An abandoned worker's thread ends when its call returns.

The worker's thread never uninitializes COM: a control released after that would crash.

A late call must not go on: the package's functions read its process-wide client, which is the
new worker's by then. So an abandoned walk stops at its next element, an abandoned action stops
after the call that hung, an abandoned start leaves the client alone, and the client is swapped
in one assignment (no moment without one). An exception leaves the worker without its traceback,
whose frames hold controls.

Mouse and keyboard input stay on the caller's thread, so a long text cannot overrun the
worker's timeout. Nothing typed is ever logged.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from remoteos.element_tree import ElementFacts, ElementTree, RefTable, StaleRef, TreeItem, one_line, walk

log = logging.getLogger("remoteos.platform.win.uia")

TREE_TIMEOUT_S = 15.0
TREE_WALK_S = 10.0  # inside TREE_TIMEOUT_S: a big tree ends as a partial list, not as a timeout
WALK_MARGIN_S = 2.0  # a walk's reading of its results, after its deadline
SNAPSHOT_TIMEOUT_S = 6.0  # Snapshot's element list is a side dish: it must not hold up the image
SNAPSHOT_WALK_S = 3.0
ACTION_TIMEOUT_S = 10.0
ACTION_NEEDS_S = 5.0
START_TIMEOUT_S = 60.0  # the first start ever generates comtypes' wrappers, which takes seconds
MAX_STUCK_WORKERS = 8

_PARENT_HOPS = 40

# The call failed because its element or app went away, which is what a button that closes its
# own window does: UIA_E_ELEMENTNOTAVAILABLE, UIA_E_TIMEOUT, RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE.
_WENT_AWAY = frozenset({0x80040201, 0x80131505, 0x80010108, 0x800706BA})


class UiaTimeout(RuntimeError):
    """A call into UI Automation did not come back in time."""


class UiaNotAsked(UiaTimeout):
    """UI Automation is starting, busy or stuck: nothing was asked of any app."""


def _detached(error: Exception) -> Exception:
    """``error`` without its traceback and chain: their frames hold COM objects of this thread."""
    error.__traceback__ = error.__context__ = error.__cause__ = None
    return error


@dataclass
class _Job:
    fn: Callable[[Any], Any]
    done: threading.Event
    result: Any = None
    error: Optional[Exception] = None


class _Thread:
    """One worker thread: a session made by ``open_session`` and the jobs that run on it."""

    def __init__(self, open_session: Callable[[threading.Event], Any]) -> None:
        self.ready = threading.Event()
        self.failure: Optional[Exception] = None
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        self.abandoned = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, args=(open_session,), name="remoteos-uia", daemon=True)
        self.thread.start()

    def _run(self, open_session: Callable[[threading.Event], Any]) -> None:
        try:
            session = open_session(self.abandoned)
        except Exception as e:  # handed to the caller; a missing package must not kill the server
            self.failure = e
            self.ready.set()
            return
        self.ready.set()
        while not self.abandoned.is_set():
            job = self.jobs.get()
            try:
                job.result = job.fn(session)
            except Exception as e:  # the caller raises it, without this thread's frames
                job.error = _detached(e)
            job.done.set()


class UiaWorker:
    """Runs functions on the UIA thread, one at a time, and replaces a thread that hangs."""

    def __init__(self, open_session: Callable[[threading.Event], Any], start_timeout: float = START_TIMEOUT_S):
        self._open_session = open_session
        self._start_timeout = start_timeout
        self._lock = threading.Lock()
        self._current: Optional[_Thread] = None
        self._stuck: list[_Thread] = []

    def _abandon(self, worker: _Thread) -> None:
        worker.abandoned.set()
        worker.jobs.put(_Job(lambda session: None, threading.Event()))  # wakes a thread waiting for a job
        self._stuck.append(worker)
        self._current = None

    def _running(self) -> _Thread:
        if self._current is None:
            self._stuck = [w for w in self._stuck if w.thread.is_alive()]
            if len(self._stuck) >= MAX_STUCK_WORKERS:
                raise UiaNotAsked(f"UI Automation is stuck: {len(self._stuck)} calls into apps never returned")
            self._current = _Thread(self._open_session)
        return self._current

    def call(self, fn: Callable[[Any], Any], timeout: float, needs: float = 0) -> Any:
        """``fn(session)`` on the worker thread, all within ``timeout``.

        Raises what ``fn`` raises, :class:`UiaNotAsked` when ``fn`` never ran, or
        :class:`UiaTimeout` when it overran. ``needs`` = what ``fn`` may legitimately
        take: a call left less than that by the waiting is not begun, because running
        out of time would abandon a healthy worker and end every ref.
        """
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise UiaNotAsked(f"UI Automation is busy: try again in a moment (waited {timeout:.0f} s)")
        try:
            worker = self._running()
            if not worker.ready.wait(max(0.0, deadline - time.monotonic())):
                if time.monotonic() - worker.started >= self._start_timeout:
                    log.warning("UI Automation did not start in %.0f s: starting it again", self._start_timeout)
                    self._abandon(worker)
                raise UiaNotAsked(f"UI Automation is still starting: try again in a moment (waited {timeout:.0f} s)")
            if worker.failure is not None:
                self._current = None  # the next call tries again
                raise RuntimeError(f"UI Automation is not available: {worker.failure}") from worker.failure
            remaining = deadline - time.monotonic()
            if remaining < needs:
                raise UiaNotAsked("UI Automation was not free in time: try again in a moment")
            job = _Job(fn, threading.Event())
            worker.jobs.put(job)
            if not job.done.wait(remaining):
                log.warning("a UI Automation call overran %.0f s: abandoning its thread", timeout)
                self._abandon(worker)
                raise UiaTimeout(f"the window did not answer UI Automation within {timeout:.0f} s")
        finally:
            self._lock.release()
        if job.error is not None:
            raise job.error
        return job.result


@dataclass
class _Entry:
    """A listed element. Lives on the worker thread only."""

    control: Any
    runtime_id: list
    hwnd: int
    name: str


@dataclass(frozen=True)
class Activation:
    """What ClickElement did on the worker. ``point`` = the mouse must click here instead."""

    name: str
    point: Optional[tuple[int, int]] = None


class Session:
    """UI Automation on its own thread: every method runs there, through :class:`UiaWorker`."""

    def __init__(self, auto: Any, com_error: type[Exception], abandoned: threading.Event) -> None:
        self.auto = auto
        self.com_error = com_error
        self.abandoned = abandoned
        self.refs = RefTable()

    def _facts(self, control: Any) -> Optional[ElementFacts]:
        try:
            rect = control.BoundingRectangle
            return ElementFacts(
                role=control.ControlTypeName.removesuffix("Control"),
                name=control.Name or "",
                enabled=bool(control.IsEnabled),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                focusable=bool(control.IsKeyboardFocusable),
                offscreen=bool(control.IsOffscreen),
            )
        except self.com_error as e:
            log.debug("an element vanished during the walk (%s)", type(e).__name__)
            return None

    def _children(self, control: Any):
        try:
            child = control.GetFirstChildControl()
            while child is not None:
                yield child
                child = child.GetNextSiblingControl()
        except self.com_error as e:
            log.debug("a branch vanished during the walk (%s)", type(e).__name__)

    def _walk(self, hwnd: int, seconds: float, **bounds: int):
        root = self.auto.ControlFromHandle(hwnd)
        if root is None:
            raise LookupError("UI Automation does not know that window")
        deadline = time.monotonic() + seconds
        expired = lambda: self.abandoned.is_set() or time.monotonic() >= deadline  # noqa: E731
        return root, walk(root, self._facts, self._children, expired=expired, **bounds)

    def tree(self, hwnd: int) -> ElementTree:
        """Walk ``hwnd``'s window and start a new generation of refs."""
        root, result = self._walk(hwnd, TREE_WALK_S)
        entries, kept = [], []
        for control, facts in result.found:
            try:
                runtime_id = list(control.GetRuntimeId())
            except self.com_error as e:
                log.debug("a listed element vanished (%s)", type(e).__name__)
                continue
            if runtime_id:  # without one, nothing could prove later that it is the same element
                entries.append(_Entry(control, runtime_id, hwnd, one_line(facts.name)))
                kept.append(facts)
        refs = self.refs.publish(entries)
        items = tuple(TreeItem(r, f.role, f.name, f.enabled, f.rect) for r, f in zip(refs, kept))
        try:
            title = one_line(root.Name or "")
        except self.com_error:
            title = ""
        return ElementTree(title=title, items=items, truncated=result.truncated)

    def elements(self, hwnd: int, limit: int) -> list[dict]:
        """Snapshot's list: publishes no refs, so reading it ends none (a timeout ends them, as any)."""
        _, result = self._walk(hwnd, SNAPSHOT_WALK_S, limit=limit)
        return [
            {
                "index": index,
                "class": facts.role,
                "text": facts.name,
                "rect": dict(zip(("left", "top", "right", "bottom"), facts.rect)),
            }
            for index, (_, facts) in enumerate(result.found, start=1)
        ]

    def _live(self, ref: str) -> _Entry:
        """The entry behind ``ref``, proven to be that same element, still there and enabled."""
        entry = self.refs.lookup(ref)
        gone = StaleRef(f"Element {ref} ({entry.name!r}) is gone: call ElementTree again")
        if not self.auto.IsWindowVisible(entry.hwnd):  # a closed window's element may still answer
            raise gone
        try:
            if list(entry.control.GetRuntimeId()) != entry.runtime_id:
                raise gone
            enabled = entry.control.IsEnabled
        except self.com_error:
            raise gone from None
        if not enabled:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) is disabled")
        return entry

    def _center(self, ref: str, entry: _Entry) -> tuple[int, int]:
        """Where the mouse may click ``entry``, or a failure: a click never lands somewhere else."""
        try:
            rect = entry.control.BoundingRectangle
            offscreen = entry.control.IsOffscreen
        except self.com_error:
            raise StaleRef(f"Element {ref} ({entry.name!r}) is gone: call ElementTree again") from None
        if offscreen or rect.isempty():
            raise RuntimeError(f"Element {ref} ({entry.name!r}) is off-screen: bring it into view first")
        x, y = rect.xcenter(), rect.ycenter()
        try:
            hit = self._is_or_inside(self.auto.ControlFromPoint(x, y), entry)
        except self.com_error:
            hit = False
        if not hit:
            raise RuntimeError(
                f"Something else is on top of element {ref} ({entry.name!r}): a window, a dialog or a part of the page."
                " FocusWindow or close it first"
            )
        return x, y

    def _refused(self, error: Exception, ref: str, entry: _Entry) -> None:
        """Return when the app plainly refused ``ref``'s action, so that the mouse or keys may try.

        Raises when the element left during its own action, which may have worked, or when
        the caller gave up on this call and moved on: nothing more may happen in its name.
        """
        if self.abandoned.is_set():
            raise UiaTimeout("abandoned during the action")
        if (getattr(error, "hresult", 0) & 0xFFFFFFFF) in _WENT_AWAY:
            raise RuntimeError(
                f"Element {ref} ({entry.name!r}) went away during the action. That is what a button that closes its"
                " window does, so it may have happened: look before repeating it, then call ElementTree again"
            ) from None

    def activate(self, ref: str) -> Activation:
        """Invoke ``ref``; without a working Invoke pattern, the point to click.

        An Invoke that returns was accepted: only a COM error means it failed, and only
        one saying the app refused may be followed by the mouse.
        """
        entry = self._live(ref)
        pattern = entry.control.GetPattern(self.auto.PatternId.InvokePattern)
        if pattern is not None:
            try:
                pattern.Invoke(waitTime=0)
                return Activation(entry.name)
            except self.com_error as e:
                self._refused(e, ref, entry)
                log.debug("Invoke refused (%s): clicking instead", type(e).__name__)
        return Activation(entry.name, self._center(ref, entry))

    def _is_or_inside(self, control: Any, entry: _Entry) -> bool:
        """True when ``control`` is the element, or a part of it (a wrapper's inner edit)."""
        for _ in range(_PARENT_HOPS):
            if control is None:
                return False
            if list(control.GetRuntimeId()) == entry.runtime_id:
                return True
            control = control.GetParentControl()
        return False

    def set_text(self, ref: str, text: str) -> tuple[str, bool]:
        """Put ``text`` in by the Value pattern, else give ``ref`` the focus.

        Returns ``(element name, True when the value is set)``; False = the caller types.
        """
        entry = self._live(ref)
        pattern = entry.control.GetPattern(self.auto.PatternId.ValuePattern)
        if pattern is not None:
            try:
                read_only = pattern.IsReadOnly
            except self.com_error:
                raise StaleRef(f"Element {ref} ({entry.name!r}) is gone: call ElementTree again") from None
            if read_only:
                raise RuntimeError(f"Element {ref} ({entry.name!r}) is read-only")
            try:
                pattern.SetValue(text, waitTime=0)
                return entry.name, True
            except self.com_error as e:
                self._refused(e, ref, entry)
                log.debug("SetValue refused (%s): typing instead", type(e).__name__)
        entry.control.SetFocus()
        try:
            focused = self._is_or_inside(self.auto.GetFocusedControl(), entry)
        except self.com_error:
            focused = False
        if not focused:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) did not take the keyboard focus: nothing was typed")
        return entry.name, False


_retired_clients: list = []  # never released: their apartment may belong to a hung thread
_client_lock = threading.Lock()


def open_session(abandoned: threading.Event) -> Session:
    """Runs on the worker's thread: COM for this thread, a client of its own."""
    import comtypes
    import uiautomation.uiautomation as auto  # the module itself: the package's * leaves out _AutomationClient

    auto.InitializeUIAutomationInCurrentThread()
    # Its logger writes to sys.stdout (the JSON-RPC stream under stdio) and to a file in the cwd.
    auto.Logger.Write = staticmethod(lambda message, *a, **k: log.debug("uiautomation: %d chars", len(str(message))))
    client = auto._AutomationClient
    with _client_lock:
        if abandoned.is_set():  # a start that took too long: a newer worker owns the client
            raise RuntimeError("abandoned while starting")
        fresh = client()  # the class's instance() would make one only when there is none
        if client._instance is not None:
            _retired_clients.append(client._instance)
        client._instance = fresh
    return Session(auto, comtypes.COMError, abandoned)


_worker = UiaWorker(open_session)


def read_tree(hwnd: int) -> ElementTree:
    """The element tree of window ``hwnd``, with new refs."""
    return _worker.call(lambda session: session.tree(hwnd), TREE_TIMEOUT_S, needs=TREE_WALK_S + WALK_MARGIN_S)


def read_elements(hwnd: int, limit: int) -> list[dict]:
    """Up to ``limit`` elements of window ``hwnd`` for Snapshot. Existing refs stay valid."""
    needs = SNAPSHOT_WALK_S + WALK_MARGIN_S
    return _worker.call(lambda session: session.elements(hwnd, limit), SNAPSHOT_TIMEOUT_S, needs=needs)


def _after_timeout(ref: str, what: str) -> RuntimeError:
    return RuntimeError(
        f"The app did not answer {what} of element {ref} within {ACTION_TIMEOUT_S:.0f} s. It may still have happened"
        " (a dialog it opened can block the call): look before repeating it, then call ElementTree again"
    )


def click_element(ref: str, click: Callable[[int, int], str]) -> str:
    """Invoke the element, or click its centre with ``click(x, y)``. A timeout never falls through to a click."""
    try:
        done = _worker.call(lambda session: session.activate(ref), ACTION_TIMEOUT_S, needs=ACTION_NEEDS_S)
    except UiaNotAsked:
        raise
    except UiaTimeout as e:
        raise _after_timeout(ref, "the click") from e
    if done.point is None:
        return f"Invoked {ref} ({done.name!r})"
    return f"{click(*done.point)}: {ref} ({done.name!r}) has no usable Invoke pattern"


def type_element(ref: str, text: str, type_keys: Callable[[str], str]) -> str:
    """Set the element's value, or give it the focus and type with ``type_keys(text)``."""
    try:
        name, done = _worker.call(lambda session: session.set_text(ref, text), ACTION_TIMEOUT_S, needs=ACTION_NEEDS_S)
    except UiaNotAsked:
        raise
    except UiaTimeout as e:
        raise _after_timeout(ref, "the text") from e
    if done:
        return f"Set the value of {ref} ({name!r}) to {len(text)} chars"
    return f"{type_keys(text)} into {ref} ({name!r}), at its caret"
