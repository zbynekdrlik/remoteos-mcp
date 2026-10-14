"""TaskManager: cancelling and the tool wrapper's status writes are serialized by the manager lock (#31).

Without the lock, the status check-and-set in ``TaskInfo.cancel`` races the
stale-task auto-cancel in ``_cleanup_old``, which runs under the lock. The real
TaskManager and TaskInfo run here; the task's cancel event is a stdlib
``threading.Event`` that also records whether the manager's lock was held at the
moment the task was cancelled.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from fastmcp.exceptions import ToolError

from remoteos.taskmanager import TaskManager, TaskStatus, ToolCategory


class _LockProbeEvent(threading.Event):
    def __init__(self, lock):
        super().__init__()
        self._probe_lock = lock
        self.lock_held_on_set: list[bool] = []

    def set(self):
        self.lock_held_on_set.append(self._probe_lock.locked())
        super().set()


def test_cancel_task_holds_the_lock_while_it_cancels():
    tm = TaskManager()
    task = tm.create_task("Shell")
    probe = _LockProbeEvent(tm._lock)
    task._cancel_event = probe
    assert tm.cancel_task(task.task_id) == {"status": "cancelled", "task_id": task.task_id, "tool_name": "Shell"}
    assert probe.lock_held_on_set == [True]
    assert task.status is TaskStatus.CANCELLED
    assert not tm._lock.locked()  # released again afterwards


def test_cancel_task_unknown_id_is_an_error():
    tm = TaskManager()
    assert tm.cancel_task("nope") == {"error": "Task nope not found"}
    assert not tm._lock.locked()


class _CancelAtTheCompletionCheck(threading.Event):
    """The task's cancel event. Once armed, the wrapper's post-run "was it cancelled?"
    check fires ``cancel_task`` from another thread and gives it up to 1 s before the
    answer is returned, so the cancel lands exactly between that check and the
    wrapper's status write, unless the manager lock makes the two one step."""

    def __init__(self, tm):
        super().__init__()
        self.tm = tm
        self.task_id = ""
        self.armed = False
        self.reply: dict | None = None
        self.thread: threading.Thread | None = None

    def is_set(self):
        answer = super().is_set()
        if self.armed:
            self.armed = False

            def _cancel():
                self.reply = self.tm.cancel_task(self.task_id)

            self.thread = threading.Thread(target=_cancel)
            self.thread.start()
            self.thread.join(timeout=1.0)
        return answer


def test_a_cancel_racing_the_completion_never_leaves_a_cancelled_reply_on_a_completed_task():
    tm = TaskManager()
    probe = _CancelAtTheCompletionCheck(tm)

    def tool():
        [task] = tm._tasks.values()  # the wrapper registered it before running the tool
        probe.task_id = task.task_id
        task._cancel_event = probe
        probe.armed = True
        return "ok"

    try:
        out = tm.wrap_sync_tool("Shell", tool)()
    except ToolError as e:  # a cancelled task is an error result (#32)
        out = str(e)
    probe.thread.join(timeout=5)
    task = tm._tasks[probe.task_id]
    # cancel_task's reply and the task's final state must tell the same story
    if probe.reply == {"status": "cancelled", "task_id": probe.task_id, "tool_name": "Shell"}:
        assert task.status is TaskStatus.CANCELLED
        assert out == f"[task:{probe.task_id}] Cancelled during execution"
    else:
        assert probe.reply == {"error": f"Task {probe.task_id} is already completed"}
        assert task.status is TaskStatus.COMPLETED
        assert out == f"[task:{probe.task_id}] ok"


def test_cancel_task_twice_reports_the_task_already_cancelled():
    tm = TaskManager()
    task = tm.create_task("Shell")
    assert tm.cancel_task(task.task_id)["status"] == "cancelled"
    assert tm.cancel_task(task.task_id) == {"error": f"Task {task.task_id} is already cancelled"}
    assert not tm._lock.locked()


# ---------------------------------------------------------------------------
# #32: every failure path of the wrapper raises a ToolError that keeps [task:<id>]
# ---------------------------------------------------------------------------


def _only_task(tm: TaskManager):
    [task] = tm._tasks.values()
    return task


def test_an_unexpected_exception_becomes_a_tool_error_and_a_failed_task(caplog):
    tm = TaskManager()

    def tool():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="remoteos.taskmanager"):
        with pytest.raises(ToolError) as exc:
            tm.wrap_sync_tool("Shell", tool)()
    task = _only_task(tm)
    assert str(exc.value) == f"[task:{task.task_id}] Error in Shell: boom"
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert task.status is TaskStatus.FAILED
    assert task.error == "boom"
    assert "Traceback" in caplog.text  # an unexpected failure is logged with its traceback


def test_a_tool_error_keeps_its_text_behind_the_task_prefix(caplog):
    tm = TaskManager()

    def tool():
        raise ToolError("Click error: no such window")

    with caplog.at_level(logging.WARNING, logger="remoteos.taskmanager"):
        with pytest.raises(ToolError) as exc:
            tm.wrap_sync_tool("Click", tool)()
    task = _only_task(tm)
    assert str(exc.value) == f"[task:{task.task_id}] Click error: no such window"
    assert task.status is TaskStatus.FAILED
    assert task.error == "Click error: no such window"
    assert "Click error: no such window" in caplog.text
    assert "Traceback" not in caplog.text  # an expected failure is one log line


class _NeverAvailable:
    """A category semaphore that is never free: acquire() reports the lock wait timed out."""

    def __init__(self):
        self.timeouts: list[float] = []

    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        return False

    def release(self):  # pragma: no cover - a lock that was never acquired is never released
        raise AssertionError("released a lock that was never acquired")


def test_the_lock_wait_timeout_is_a_tool_error_and_the_tool_never_runs():
    tm = TaskManager()
    busy = _NeverAvailable()
    tm._thread_semaphores[ToolCategory.DESKTOP] = busy
    ran = []
    with pytest.raises(ToolError) as exc:
        tm.wrap_sync_tool("Click", lambda: ran.append(1))()
    task = _only_task(tm)
    assert str(exc.value) == (
        f"[task:{task.task_id}] Timeout waiting for desktop lock (another desktop task is running)"
    )
    assert busy.timeouts == [60]
    assert ran == []
    assert task.status is TaskStatus.FAILED


def test_a_task_cancelled_while_it_waits_for_the_lock_is_a_tool_error_and_never_runs():
    tm = TaskManager()
    sem = tm._thread_semaphores[ToolCategory.DESKTOP]
    assert sem.acquire(timeout=1)  # another desktop task holds the lock
    ran: list[int] = []
    outcome: dict = {}

    def _call():
        try:
            outcome["result"] = tm.wrap_sync_tool("Click", lambda: ran.append(1) or "clicked")()
        except ToolError as e:
            outcome["error"] = e

    worker = threading.Thread(target=_call)
    worker.start()
    deadline = time.monotonic() + 5
    while not tm._tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    task = _only_task(tm)
    assert tm.cancel_task(task.task_id)["status"] == "cancelled"
    sem.release()
    worker.join(timeout=5)
    assert "result" not in outcome
    assert str(outcome["error"]) == f"[task:{task.task_id}] Cancelled before execution"
    assert ran == []
    assert task.status is TaskStatus.CANCELLED


def test_a_task_cancelled_while_it_runs_is_a_tool_error():
    tm = TaskManager()

    def tool():
        tm.cancel_task(_only_task(tm).task_id)
        return "done"

    with pytest.raises(ToolError) as exc:
        tm.wrap_sync_tool("Shell", tool)()
    task = _only_task(tm)
    assert str(exc.value) == f"[task:{task.task_id}] Cancelled during execution"
    assert task.status is TaskStatus.CANCELLED


def test_a_successful_tool_still_returns_its_result_with_the_task_prefix():
    tm = TaskManager()
    out = tm.wrap_sync_tool("Shell", lambda: "ok")()
    assert out == f"[task:{_only_task(tm).task_id}] ok"
    assert _only_task(tm).status is TaskStatus.COMPLETED
