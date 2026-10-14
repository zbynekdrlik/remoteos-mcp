"""TaskManager: cancelling and the tool wrapper's status writes are serialized by the manager lock (#31).

Without the lock, the status check-and-set in ``TaskInfo.cancel`` races the
stale-task auto-cancel in ``_cleanup_old``, which runs under the lock. The real
TaskManager and TaskInfo run here; the task's cancel event is a stdlib
``threading.Event`` that also records whether the manager's lock was held at the
moment the task was cancelled.
"""

from __future__ import annotations

import threading

from remoteos.taskmanager import TaskManager, TaskStatus


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

    out = tm.wrap_sync_tool("Shell", tool)()
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
