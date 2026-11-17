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

UI Automation's own wait for an app (``IUIAutomation2.TransactionTimeout``, 20 s by default)
applies to each call into the app, so ``Session._ask`` sets it to what the job has left before
every single call. An app that does not answer is then a timeout from UI Automation on a
healthy worker, not an abandoned one (#114).

Mouse and keyboard input stay on the caller's thread, so a long text cannot overrun the
worker's timeout. Nothing typed is ever logged.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Optional

from remoteos.element_tree import (
    SNAPSHOT_WALK_S,
    TREE_WALK_S,
    ElementFacts,
    ElementTree,
    RefTable,
    gone,
    listed_elements,
    marked_from,
    one_line,
    tree_from,
    walk,
)

log = logging.getLogger("remoteos.platform.win.uia")

TREE_TIMEOUT_S = 15.0
WALK_MARGIN_S = 2.0  # a walk's reading of its results, after its deadline
SNAPSHOT_TIMEOUT_S = 6.0
ACTION_TIMEOUT_S = 10.0
ACTION_NEEDS_S = 5.0
ANSWER_MARGIN_S = 1.0  # a job's reply after UI Automation gave up on the app, inside the call's budget
MIN_ANSWER_MS = 500  # UI Automation's shortest wait for an app: a responsive one answers in ms
START_TIMEOUT_S = 60.0  # the first start ever generates comtypes' wrappers, which takes seconds
MAX_STUCK_WORKERS = 8

_PARENT_HOPS = 40

# The call failed because its element or app went away, which is what a button that closes its
# own window does: UIA_E_ELEMENTNOTAVAILABLE, RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE.
_WENT_AWAY = frozenset({0x80040201, 0x80010108, 0x800706BA})
# UI Automation gave up waiting for the app: UIA_E_TIMEOUT, HRESULT_FROM_WIN32(ERROR_TIMEOUT).
_TIMED_OUT = frozenset({0x80131505, 0x800705B4})
# CUIAutomation8: the client that has IUIAutomation2, whose own wait for an app can be set.
CLSID_CUIAUTOMATION8 = "{e22ad333-b25f-460c-83d0-0581107395c9}"


class UiaTimeout(RuntimeError):
    """A call into UI Automation did not come back in time."""


class UiaNotAsked(UiaTimeout):
    """UI Automation is starting, busy or stuck: nothing was asked of any app."""


class UiaUnanswered(UiaTimeout):
    """UI Automation gave up on an app that did not answer a read: the worker lives on, no action ran."""


class UiaActionUnanswered(UiaTimeout):
    """UI Automation gave up on an app during its action (Invoke, SetValue): it may have happened."""


class UiaLookupFailed(RuntimeError):
    """UI Automation refused the window lookup (not a timeout): says which step and its HRESULT (#116)."""


def _hresult(error: Exception) -> int:
    return getattr(error, "hresult", 0) & 0xFFFFFFFF


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

    def __init__(
        self,
        open_session: Callable[[threading.Event], Any],
        start_timeout: float = START_TIMEOUT_S,
        answer_within: Optional[Callable[[Any, float], None]] = None,
    ):
        """``answer_within(session, seconds)`` runs on the worker before each job: how long the app may take."""
        self._open_session = open_session
        self._start_timeout = start_timeout
        self._answer_within = answer_within
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

    def _bounded(self, fn: Callable[[Any], Any], seconds: float) -> Callable[[Any], Any]:
        """``fn``, after the session was told that the app may take ``seconds`` of it."""
        answer_within = self._answer_within
        if answer_within is None:
            return fn

        def run(session: Any) -> Any:
            answer_within(session, seconds)
            return fn(session)

        return run

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
            job = _Job(self._bounded(fn, remaining - ANSWER_MARGIN_S), threading.Event())
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

    def __init__(self, auto: Any, com_error: type[Exception], abandoned: threading.Event, automation: Any) -> None:
        self.auto = auto
        self.com_error = com_error
        self.abandoned = abandoned
        # The client's own IUIAutomation2 and raw walker. Navigation and the pattern / focus calls go
        # through them, never the package's Control wrappers: those make two calls into the app under
        # one limit (the element, then its type), and GetPattern/SetFocus swallow every COM error.
        self.automation = automation
        self.walker = automation.RawViewWalker
        self.answer_by: Optional[float] = None
        self.refs = RefTable()

    def answer_within(self, seconds: float) -> None:
        """The next job may wait on the app for ``seconds`` in all; UI Automation then answers a timeout."""
        self.answer_by = time.monotonic() + seconds

    def _bound(self) -> None:
        """UI Automation may wait on the app only for what the job has left (a put on our own client)."""
        if self.answer_by is None:
            return
        left_ms = int((self.answer_by - time.monotonic()) * 1000)
        self.automation.TransactionTimeout = max(MIN_ANSWER_MS, left_ms)

    def _ask(self, call: Callable[[], Any]) -> Any:
        """ONE call into the app. UI Automation's timeout applies to each call, so each is bounded anew."""
        self._bound()
        return call()

    def _control(self, find: Callable[[], Any]) -> Any:
        """The control of the element ``find`` returns, or ``None``: two calls into the app, each bounded."""
        element = self._ask(find)
        if not element:  # a NULL element pointer
            return None
        return self._ask(lambda: self.auto.Control.CreateControlFromElement(element))

    def _unanswered(self, error: Exception, done: str = "nothing was done to it") -> None:
        """Raise when ``error`` is UI Automation giving up on the app: not a vanished element, a timeout."""
        if _hresult(error) in _TIMED_OUT:
            log.warning("the app did not answer UI Automation in time (%#x)", _hresult(error))
            raise UiaUnanswered(f"the window did not answer UI Automation in time: {done}") from None

    def _facts(self, control: Any) -> Optional[ElementFacts]:
        ask = self._ask
        try:
            rect = ask(lambda: control.BoundingRectangle)
            return ElementFacts(
                role=ask(lambda: control.ControlTypeName).removesuffix("Control"),
                name=ask(lambda: control.Name) or "",
                enabled=bool(ask(lambda: control.IsEnabled)),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                focusable=bool(ask(lambda: control.IsKeyboardFocusable)),
                offscreen=bool(ask(lambda: control.IsOffscreen)),
            )
        except self.com_error as e:
            self._unanswered(e)
            log.debug("an element vanished during the walk (%s)", type(e).__name__)
            return None

    def _children(self, control: Any):
        try:
            child = self._control(lambda: self.walker.GetFirstChildElement(control.Element))
            while child is not None:
                yield child
                sibling = child
                child = self._control(lambda: self.walker.GetNextSiblingElement(sibling.Element))
        except self.com_error as e:
            self._unanswered(e)
            log.debug("a branch vanished during the walk (%s)", type(e).__name__)

    def _window(self, hwnd: int) -> Any:
        """``hwnd``'s control, or ``None``. Two calls into the app; a failure says which one failed.

        A timeout is :class:`UiaUnanswered`, any other COM error :class:`UiaLookupFailed`
        with the step and its HRESULT, never the raw error (#116).
        """
        step = "finding the window"
        try:
            element = self._ask(lambda: self.automation.ElementFromHandle(hwnd))
            if not element:  # a NULL element pointer
                return None
            step = "reading the window's control type"
            return self._ask(lambda: self.auto.Control.CreateControlFromElement(element))
        except self.com_error as e:
            self._unanswered(e)
            log.warning("the window lookup failed while %s (%#010x)", step, _hresult(e))
            raise UiaLookupFailed(
                f"UI Automation failed while {step} (HRESULT {_hresult(e):#010x}): nothing was done to it"
            ) from None

    def _walk(self, hwnd: int, seconds: float, **bounds: int):
        deadline = time.monotonic() + seconds  # the walk's time includes finding the window
        root = self._window(hwnd) if hwnd else None
        if root is None:
            raise LookupError("UI Automation does not know that window")
        expired = lambda: self.abandoned.is_set() or time.monotonic() >= deadline  # noqa: E731
        return root, walk(root, self._facts, self._children, expired=expired, **bounds)

    def _entry(self, hwnd: int, control: Any, facts: ElementFacts) -> Optional[_Entry]:
        """What a ref keeps, or ``None``: without a RuntimeId nothing could prove later that it is the same element."""
        try:
            runtime_id = list(self._ask(control.GetRuntimeId))
        except self.com_error as e:
            self._unanswered(e)
            log.debug("a listed element vanished (%s)", type(e).__name__)
            return None
        return _Entry(control, runtime_id, hwnd, one_line(facts.name)) if runtime_id else None

    def tree(self, hwnd: int) -> ElementTree:
        """Walk ``hwnd``'s window and start a new generation of refs."""
        root, result = self._walk(hwnd, TREE_WALK_S)
        try:
            title = one_line(self._ask(lambda: root.Name) or "")
        except self.com_error as e:
            self._unanswered(e)
            title = ""
        return tree_from(self.refs, title, result, lambda control, facts: self._entry(hwnd, control, facts))

    def elements(self, hwnd: int, limit: int) -> list[dict]:
        """Snapshot's list: publishes no refs, so reading it ends none (a timeout ends them, as any)."""
        return listed_elements(self._walk(hwnd, SNAPSHOT_WALK_S, limit=limit)[1])

    def marked(self, hwnd: int, limit: int) -> tuple[list[dict], str]:
        """AnnotatedSnapshot's list (#36) and why it is cut: refs in a new tree, so a mark can be acted on."""
        _, result = self._walk(hwnd, SNAPSHOT_WALK_S, limit=limit)
        return marked_from(self.refs, result, lambda control, facts: self._entry(hwnd, control, facts))

    def _live(self, ref: str) -> _Entry:
        """The entry behind ``ref``, proven to be that same element, still there and enabled."""
        entry = self.refs.lookup(ref)
        stale = gone(ref, entry.name)
        if not self.auto.IsWindowVisible(entry.hwnd):  # a closed window's element may still answer
            raise stale
        try:
            if list(self._ask(entry.control.GetRuntimeId)) != entry.runtime_id:
                raise stale
            enabled = self._ask(lambda: entry.control.IsEnabled)
        except self.com_error as e:
            self._unanswered(e)
            raise stale from None
        if not enabled:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) is disabled")
        return entry

    def _center(self, ref: str, entry: _Entry) -> tuple[int, int]:
        """Where the mouse may click ``entry``, or a failure: a click never lands somewhere else."""
        try:
            rect = self._ask(lambda: entry.control.BoundingRectangle)
            offscreen = self._ask(lambda: entry.control.IsOffscreen)
        except self.com_error as e:
            self._unanswered(e)
            raise gone(ref, entry.name) from None
        if offscreen or rect.isempty():
            raise RuntimeError(f"Element {ref} ({entry.name!r}) is off-screen: bring it into view first")
        x, y = rect.xcenter(), rect.ycenter()
        try:
            at_point = self._control(lambda: self.automation.ElementFromPoint(wintypes.POINT(x, y)))
            hit = self._is_or_inside(at_point, entry)
        except self.com_error as e:
            self._unanswered(e)
            hit = False
        if not hit:
            raise RuntimeError(
                f"Something else is on top of element {ref} ({entry.name!r}): a window, a dialog or a part of the page."
                " FocusWindow or close it first"
            )
        return x, y

    def _pattern(self, entry: _Entry, pattern_id: Any) -> Any:
        """``entry``'s pattern, or ``None`` when it has none or refused it (the package's GetPattern says the same).

        An app that does not answer is :class:`UiaUnanswered`: never ``None``, which would send the
        click to the mouse and the text to the keys.
        """
        try:
            raw = self._ask(lambda: entry.control.Element.GetCurrentPattern(pattern_id))
            return self.auto.CreatePattern(pattern_id, raw) if raw else None  # QueryInterface: in-process
        except self.com_error as e:
            self._unanswered(e)
            log.debug("the pattern lookup was refused (%s)", type(e).__name__)
            return None

    def _refused(self, error: Exception, ref: str, entry: _Entry) -> None:
        """Return when the app plainly refused ``ref``'s action, so that the mouse or keys may try.

        Raises when the element left during its own action, which may have worked, or when
        the caller gave up on this call and moved on: nothing more may happen in its name.
        """
        if self.abandoned.is_set():
            raise UiaTimeout("abandoned during the action")
        if _hresult(error) in _TIMED_OUT:  # a modal dialog the action opened can hold its call: never act again
            raise UiaActionUnanswered(
                f"Element {ref} ({entry.name!r}) did not answer the action in time. It may have happened: look before"
                " repeating it, then call ElementTree again"
            ) from None
        if _hresult(error) in _WENT_AWAY:
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
        pattern = self._pattern(entry, self.auto.PatternId.InvokePattern)
        if pattern is not None:
            try:
                self._ask(lambda: pattern.Invoke(waitTime=0))
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
            if list(self._ask(control.GetRuntimeId)) == entry.runtime_id:
                return True
            current = control
            control = self._control(lambda: self.walker.GetParentElement(current.Element))
        return False

    def set_text(self, ref: str, text: str) -> tuple[str, bool]:
        """Put ``text`` in by the Value pattern, else give ``ref`` the focus.

        Returns ``(element name, True when the value is set)``; False = the caller types.
        """
        entry = self._live(ref)
        pattern = self._pattern(entry, self.auto.PatternId.ValuePattern)
        if pattern is not None:
            try:
                read_only = self._ask(lambda: pattern.IsReadOnly)
            except self.com_error as e:
                self._unanswered(e)
                raise gone(ref, entry.name) from None
            if read_only:
                raise RuntimeError(f"Element {ref} ({entry.name!r}) is read-only")
            try:
                self._ask(lambda: pattern.SetValue(text, waitTime=0))
                return entry.name, True
            except self.com_error as e:
                self._refused(e, ref, entry)
                log.debug("SetValue refused (%s): typing instead", type(e).__name__)
        maybe_focused = "nothing was typed, the focus may have moved"
        try:
            self._ask(entry.control.Element.SetFocus)
        except self.com_error as e:  # refused: the focus check below says what came of it
            self._unanswered(e, maybe_focused)
            log.debug("SetFocus refused (%s)", type(e).__name__)
        try:
            focused = self._is_or_inside(self._control(self.automation.GetFocusedElement), entry)
        except self.com_error as e:
            self._unanswered(e, maybe_focused)
            focused = False
        if not focused:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) did not take the keyboard focus: nothing was typed")
        return entry.name, False


_retired_clients: list = []  # never released: their apartment may belong to a hung thread
_client_lock = threading.Lock()


def open_session(abandoned: threading.Event) -> Session:
    """Runs on the worker's thread: COM for this thread, a client of its own."""
    import comtypes
    import comtypes.client
    import uiautomation.uiautomation as auto  # the module itself: the package's * leaves out _AutomationClient

    auto.InitializeUIAutomationInCurrentThread()
    # Its logger writes to sys.stdout (the JSON-RPC stream under stdio) and to a file in the cwd.
    auto.Logger.Write = staticmethod(lambda message, *a, **k: log.debug("uiautomation: %d chars", len(str(message))))
    client = auto._AutomationClient
    with _client_lock:
        if abandoned.is_set():  # a start that took too long: a newer worker owns the client
            raise RuntimeError("abandoned while starting")
        fresh = client()  # the class's instance() would make one only when there is none
        # IUIAutomation2 derives from IUIAutomation, so every function of the package works on it.
        fresh.IUIAutomation = comtypes.client.CreateObject(
            CLSID_CUIAUTOMATION8, interface=fresh.UIAutomationCore.IUIAutomation2
        )
        fresh.ViewWalker = fresh.IUIAutomation.RawViewWalker
        if client._instance is not None:
            _retired_clients.append(client._instance)
        client._instance = fresh
    return Session(auto, comtypes.COMError, abandoned, fresh.IUIAutomation)


_worker = UiaWorker(open_session, answer_within=Session.answer_within)


def read_tree(hwnd: int) -> ElementTree:
    """The element tree of window ``hwnd``, with new refs."""
    return _worker.call(lambda session: session.tree(hwnd), TREE_TIMEOUT_S, needs=TREE_WALK_S + WALK_MARGIN_S)


def read_elements(hwnd: int, limit: int) -> list[dict]:
    """Up to ``limit`` elements of window ``hwnd`` for Snapshot. Existing refs stay valid."""
    needs = SNAPSHOT_WALK_S + WALK_MARGIN_S
    return _worker.call(lambda session: session.elements(hwnd, limit), SNAPSHOT_TIMEOUT_S, needs=needs)


def read_marked(hwnd: int, limit: int) -> tuple[list[dict], str]:
    """AnnotatedSnapshot's elements, each with a ref: a new tree, on Snapshot's short budget (#36)."""
    needs = SNAPSHOT_WALK_S + WALK_MARGIN_S
    return _worker.call(lambda session: session.marked(hwnd, limit), SNAPSHOT_TIMEOUT_S, needs=needs)


def _after_timeout(ref: str, what: str) -> RuntimeError:
    return RuntimeError(
        f"The app did not answer {what} of element {ref} within {ACTION_TIMEOUT_S:.0f} s. It may still have happened"
        " (a dialog it opened can block the call): look before repeating it, then call ElementTree again"
    )


def click_element(ref: str, click: Callable[[int, int], str]) -> str:
    """Invoke the element, or click its centre with ``click(x, y)``. A timeout never falls through to a click."""
    try:
        done = _worker.call(lambda session: session.activate(ref), ACTION_TIMEOUT_S, needs=ACTION_NEEDS_S)
    except (UiaNotAsked, UiaUnanswered, UiaActionUnanswered):  # each already says what happened
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
    except (UiaNotAsked, UiaUnanswered, UiaActionUnanswered):  # each already says what happened
        raise
    except UiaTimeout as e:
        raise _after_timeout(ref, "the text") from e
    if done:
        return f"Set the value of {ref} ({name!r}) to {len(text)} chars"
    return f"{type_keys(text)} into {ref} ({name!r}), at its caret"
