"""TaskManager.cancel_task: the lookup and the cancel are one step under the manager lock (#31).

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


def test_cancel_task_twice_reports_the_task_already_cancelled():
    tm = TaskManager()
    task = tm.create_task("Shell")
    assert tm.cancel_task(task.task_id)["status"] == "cancelled"
    assert tm.cancel_task(task.task_id) == {"error": f"Task {task.task_id} is already cancelled"}
    assert not tm._lock.locked()
