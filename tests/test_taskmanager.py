"""TaskManager: cancelling and the tool wrapper's status writes are serialized by the manager lock (#31).

Without the lock, the status check-and-set in ``TaskInfo.cancel`` races the
stale-task auto-cancel in ``_cleanup_old``, which runs under the lock. The real
TaskManager and TaskInfo run here; the task's cancel event is a stdlib
``threading.Event`` that also records whether the manager's lock was held at the
moment the task was cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from tool_surface import server

from remoteos.taskmanager import TaskManager, TaskStatus, ToolCategory
from remoteos.taskmanager import manager as task_manager


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

    secret = "boom-" + "secret"  # built at run time: the logged frames show source lines, never values

    def tool():
        raise RuntimeError(secret)

    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        with pytest.raises(ToolError) as exc:
            tm.wrap_sync_tool("Shell", tool)()
    task = _only_task(tm)
    assert str(exc.value) == f"[task:{task.task_id}] Error in Shell: boom-secret"
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert task.status is TaskStatus.FAILED
    assert task.error == "boom-secret"
    # an unexpected failure is logged with its traceback (where it broke) and its type,
    # never its message, which can echo a tool argument (#31)
    assert "Traceback" in caplog.text and "in tool" in caplog.text and "RuntimeError" in caplog.text
    assert "boom-secret" not in caplog.text


def test_a_tool_error_keeps_its_text_behind_the_task_prefix(caplog):
    tm = TaskManager()

    def tool():
        raise ToolError("Click error: no such window")

    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        with pytest.raises(ToolError) as exc:
            tm.wrap_sync_tool("Click", tool)()
    task = _only_task(tm)
    assert str(exc.value) == f"[task:{task.task_id}] Click error: no such window"
    assert task.status is TaskStatus.FAILED
    assert task.error == "Click error: no such window"
    # one payload-free line: the error text can echo a tool argument (#31: logs never carry those)
    assert f"Tool Click [{task.task_id}] failed" in caplog.text
    assert "no such window" not in caplog.text
    assert "Traceback" not in caplog.text


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


# ---------------------------------------------------------------------------
# Review round 2: the stale-task auto-cancel respects a tool's own longer timeout
# ---------------------------------------------------------------------------


def _runs_for(tm: TaskManager, seconds: float, result="done"):
    """A tool body that has 'been running' ``seconds`` when another task is created (the stale sweep)."""

    def tool(**kwargs):
        [task] = [t for t in tm._tasks.values() if t.status is TaskStatus.RUNNING]
        task.started_at = time.time() - seconds
        tm.create_task("GetSystemInfo")  # every new task runs _cleanup_old
        return result

    return tool


def test_a_long_shell_within_its_own_timeout_is_not_auto_cancelled():
    # a Shell(timeout=600) that finishes after 400 s completed: its output is the reply, not an error
    tm = TaskManager()
    out = tm.wrap_sync_tool("Shell", _runs_for(tm, 400))(command="sleep 400", timeout=600)
    [task] = [t for t in tm._tasks.values() if t.tool_name == "Shell"]
    assert out == f"[task:{task.task_id}] done"
    assert task.status is TaskStatus.COMPLETED


def test_a_long_wait_within_its_own_seconds_is_not_auto_cancelled():
    # review round 8: Wait declares its runtime through `seconds`, not `timeout`; a Wait(seconds=400)
    # that finishes after 400 s completed, so its reply is the answer, never "Cancelled during execution"
    tm = TaskManager()
    out = tm.wrap_sync_tool("Wait", _runs_for(tm, 400))(seconds=400)
    [task] = [t for t in tm._tasks.values() if t.tool_name == "Wait"]
    assert out == f"[task:{task.task_id}] done"
    assert task.status is TaskStatus.COMPLETED


def test_a_short_declared_runtime_still_gets_the_300_s_floor():
    # review round 9: every call now declares a runtime (FastMCP fills in Wait's default
    # seconds=1.0, i.e. 61 s), and the sweep must still wait STALE_AFTER_S before cancelling it
    tm = TaskManager()
    out = tm.wrap_sync_tool("Wait", _runs_for(tm, 200))(seconds=1)
    [task] = [t for t in tm._tasks.values() if t.tool_name == "Wait"]
    assert task.max_runtime == 61  # the precondition: a real (short) declaration, not None
    assert out == f"[task:{task.task_id}] done"
    assert task.status is TaskStatus.COMPLETED


def test_the_mcp_client_path_declares_waits_seconds_as_its_runtime(monkeypatch):
    # review round 9: the sweep reads keyword arguments, so this locks in that FastMCP passes
    # Wait's `seconds` by keyword (coerced to float) through the real in-process client
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)

    async def _go():
        async with Client(server.mcp) as client:
            return await client.call_tool("Wait", {"seconds": 250})

    (block,) = asyncio.run(_go()).content
    match = re.match(r"^\[task:([0-9a-f]{12})\] Waited 250\.0s$", block.text)
    assert match, block.text
    assert task_manager._tasks[match.group(1)].max_runtime == 310


def test_a_wait_past_its_own_seconds_and_the_stale_limit_is_auto_cancelled():
    tm = TaskManager()
    with pytest.raises(ToolError, match="Cancelled during execution"):
        tm.wrap_sync_tool("Wait", _runs_for(tm, 400))(seconds=30)


def test_a_task_past_the_stale_limit_is_still_auto_cancelled():
    tm = TaskManager()
    with pytest.raises(ToolError, match="Cancelled during execution"):
        tm.wrap_sync_tool("Shell", _runs_for(tm, 400))(command="x", timeout=30)
    with pytest.raises(ToolError, match="Cancelled during execution"):
        tm.wrap_sync_tool("Click", _runs_for(tm, 400))(x=1, y=1)
