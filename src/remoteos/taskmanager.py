"""Task manager with concurrency control, error resilience, and cancellation."""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from remoteos.errors import ToolError, stack_frames

logger = logging.getLogger("remoteos.taskmanager")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCategory(str, Enum):
    """Tool concurrency categories."""

    # Desktop interaction — mouse/keyboard/screen capture. Must be exclusive.
    DESKTOP = "desktop"
    # File system operations — can run concurrently with each other.
    FILE = "file"
    # System queries — read-only, fully concurrent.
    QUERY = "query"
    # Shell commands — semi-concurrent (limited pool).
    SHELL = "shell"
    # Network tools — fully concurrent.
    NETWORK = "network"


# Which tools belong to which category
TOOL_CATEGORIES: dict[str, ToolCategory] = {
    # Desktop — exclusive lock
    "Snapshot": ToolCategory.DESKTOP,
    "AnnotatedSnapshot": ToolCategory.DESKTOP,
    "Click": ToolCategory.DESKTOP,
    "Type": ToolCategory.DESKTOP,
    "Scroll": ToolCategory.DESKTOP,
    "Move": ToolCategory.DESKTOP,
    "Shortcut": ToolCategory.DESKTOP,
    "FocusWindow": ToolCategory.DESKTOP,
    "MinimizeAll": ToolCategory.DESKTOP,
    "App": ToolCategory.DESKTOP,
    "OCR": ToolCategory.DESKTOP,
    "ScreenRecord": ToolCategory.DESKTOP,
    "LockScreen": ToolCategory.DESKTOP,
    "Wait": ToolCategory.DESKTOP,
    # File operations
    "FileRead": ToolCategory.FILE,
    "FileWrite": ToolCategory.FILE,
    "FileList": ToolCategory.FILE,
    "FileSearch": ToolCategory.FILE,
    "FileDownload": ToolCategory.FILE,
    "FileUpload": ToolCategory.FILE,
    # System queries — concurrent
    "GetSystemInfo": ToolCategory.QUERY,
    "GetClipboard": ToolCategory.QUERY,
    "SetClipboard": ToolCategory.QUERY,
    "ListProcesses": ToolCategory.QUERY,
    "KillProcess": ToolCategory.QUERY,
    "Notification": ToolCategory.QUERY,
    "RegRead": ToolCategory.QUERY,
    "RegWrite": ToolCategory.QUERY,
    "ServiceList": ToolCategory.QUERY,
    "ServiceStart": ToolCategory.QUERY,
    "ServiceStop": ToolCategory.QUERY,
    "TaskList": ToolCategory.QUERY,
    "TaskCreate": ToolCategory.QUERY,
    "TaskDelete": ToolCategory.QUERY,
    "EventLog": ToolCategory.QUERY,
    # Shell
    "Shell": ToolCategory.SHELL,
    "Scrape": ToolCategory.SHELL,
    # Network
    "Ping": ToolCategory.NETWORK,
    "PortCheck": ToolCategory.NETWORK,
    "NetConnections": ToolCategory.NETWORK,
}

# A RUNNING task older than this is taken as stuck and auto-cancelled (unless it declared a longer timeout).
STALE_AFTER_S = 300
# Headroom over a tool's own `timeout` argument: its kill-and-reap path takes a few seconds more.
_TIMEOUT_HEADROOM_S = 60


def _declared_runtime(kwargs: dict) -> float | None:
    """The runtime a call declares through a numeric ``timeout`` argument (e.g. Shell), plus headroom."""
    timeout = kwargs.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout) + _TIMEOUT_HEADROOM_S
    return None


# Max concurrent tasks per category
CATEGORY_LIMITS: dict[ToolCategory, int] = {
    ToolCategory.DESKTOP: 1,  # Exclusive — only one at a time
    ToolCategory.FILE: 5,
    ToolCategory.QUERY: 10,
    ToolCategory.SHELL: 3,
    ToolCategory.NETWORK: 5,
}


@dataclass
class TaskInfo:
    """Tracks a running task."""

    task_id: str
    tool_name: str
    category: ToolCategory
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: Any = None
    error: str | None = None
    # how long it may run before the stale sweep cancels it (None = STALE_AFTER_S)
    max_runtime: float | None = None
    _cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return round(end - self.started_at, 2)

    def cancel(self) -> bool:
        """Request cancellation."""
        if self.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            self._cancel_event.set()
            self.status = TaskStatus.CANCELLED
            self.completed_at = time.time()
            return True
        return False

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "category": self.category.value,
            "status": self.status.value,
            "duration": self.duration,
            "error": self.error,
        }


class TaskManager:
    """Manages task execution with concurrency control and cancellation."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        # Semaphores per category for concurrency control
        self._semaphores: dict[ToolCategory, asyncio.Semaphore] = {}
        # Thread-based semaphores for sync tools
        self._thread_semaphores: dict[ToolCategory, threading.Semaphore] = {
            cat: threading.Semaphore(limit) for cat, limit in CATEGORY_LIMITS.items()
        }
        self._lock = threading.Lock()
        # Keep max N completed tasks in history
        self._max_history = 100

    def _get_semaphore(self, category: ToolCategory) -> asyncio.Semaphore:
        """Get or create async semaphore for a category."""
        if category not in self._semaphores:
            limit = CATEGORY_LIMITS.get(category, 5)
            self._semaphores[category] = asyncio.Semaphore(limit)
        return self._semaphores[category]

    def create_task(self, tool_name: str, max_runtime: float | None = None) -> TaskInfo:
        """Register a new task (``max_runtime``: a longer stale limit than ``STALE_AFTER_S``)."""
        category = TOOL_CATEGORIES.get(tool_name, ToolCategory.QUERY)
        task = TaskInfo(
            task_id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            category=category,
            max_runtime=max_runtime,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._cleanup_old()
        return task

    def cancel_task(self, task_id: str) -> dict:
        """Cancel a task by ID.

        The lookup and the cancel run under the manager lock (#31), the same lock
        the tool wrapper's status writes (``_transition``) and ``_cleanup_old``'s
        stale-task auto-cancel take, so the reply always matches the final status.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {"error": f"Task {task_id} not found"}
            if task.cancel():
                logger.info("Cancelled task %s (%s)", task_id, task.tool_name)
                return {"status": "cancelled", "task_id": task_id, "tool_name": task.tool_name}
            return {"error": f"Task {task_id} is already {task.status.value}"}

    def list_tasks(self, status: str | None = None) -> list[dict]:
        """List tasks, optionally filtered by status."""
        with self._lock:
            tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status.value == status]
        # Most recent first
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in tasks[:50]]

    def get_task(self, task_id: str) -> dict | None:
        """Get task info by ID."""
        with self._lock:
            task = self._tasks.get(task_id)
        return task.to_dict() if task else None

    def _cleanup_old(self) -> None:
        """Remove old completed tasks beyond max_history, and cancel stale running tasks."""
        now = time.time()
        # Cancel tasks running longer than 5 minutes (they're likely stuck), or longer than their own
        # declared runtime: a cancelled task's reply is an error (#32), so a command that is still
        # inside its own timeout must never be swept (review round 2).
        for t in list(self._tasks.values()):
            limit = max(STALE_AFTER_S, t.max_runtime or 0)
            if t.status == TaskStatus.RUNNING and t.started_at and (now - t.started_at) > limit:
                elapsed = now - t.started_at
                logger.warning("Auto-cancelling stale task %s (%s) after %.0fs", t.task_id, t.tool_name, elapsed)
                t.cancel()

        completed = [
            t
            for t in self._tasks.values()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ]
        if len(completed) > self._max_history:
            completed.sort(key=lambda t: t.created_at)
            for t in completed[: len(completed) - self._max_history]:
                self._tasks.pop(t.task_id, None)

    def _transition(self, task: TaskInfo, status: TaskStatus, error: str | None = None) -> bool:
        """Move ``task`` to ``status`` unless it was cancelled; False = it was cancelled.

        The "was it cancelled?" check and the status write are one step under the
        manager lock (#31): a ``cancel_task`` landing between them used to reply
        "cancelled" while the wrapper then wrote COMPLETED over it.
        """
        with self._lock:
            if task.is_cancelled:
                return False
            task.status = status
            if status is TaskStatus.RUNNING:
                task.started_at = time.time()
            else:
                task.completed_at = time.time()
                task.error = error
            return True

    def wrap_sync_tool(self, tool_name: str, func: Callable) -> Callable:
        """Wrap a synchronous tool function with error handling + concurrency control.

        A successful text result gets the ``[task:<id>]`` prefix. Every failure
        RAISES a ``ToolError`` with the same prefix (#32), which FastMCP returns
        to the client as ``isError: true``: the tool's own ``ToolError`` keeps its
        text, an unexpected exception becomes ``Error in <tool>: …`` (its
        traceback is logged), and the lock-wait timeout and a cancel are errors
        too. The task is FAILED or CANCELLED in each case.
        """
        category = TOOL_CATEGORIES.get(tool_name, ToolCategory.QUERY)
        sem = self._thread_semaphores.get(category)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            task = self.create_task(tool_name, max_runtime=_declared_runtime(kwargs))
            prefix = f"[task:{task.task_id}]"

            # Try to acquire semaphore — wait up to 60s
            if sem and not sem.acquire(timeout=60):
                error = f"Timeout waiting for {category.value} lock (another {category.value} task is running)"
                if not self._transition(task, TaskStatus.FAILED, error):
                    raise ToolError(f"{prefix} Cancelled before execution", log_level=logging.WARNING)
                logger.warning("Tool %s [%s]: %s", tool_name, task.task_id, error)
                raise ToolError(f"{prefix} {error}", log_level=logging.WARNING)

            try:
                if not self._transition(task, TaskStatus.RUNNING):
                    raise ToolError(f"{prefix} Cancelled before execution", log_level=logging.WARNING)

                # A failure is logged payload-free (#31): its text can echo a tool argument, so
                # the log gets the tool, the task and the error type; the client and the task
                # record get the text.
                try:
                    result = func(*args, **kwargs)
                except ToolError as e:
                    if self._transition(task, TaskStatus.FAILED, str(e)):
                        logger.warning("Tool %s [%s] failed", tool_name, task.task_id)
                        raise ToolError(f"{prefix} {e}", log_level=e.log_level) from e
                    raise ToolError(f"{prefix} Cancelled during execution", log_level=logging.WARNING) from e
                except Exception as e:
                    if self._transition(task, TaskStatus.FAILED, str(e)):
                        logger.error(
                            "Tool %s [%s] failed: %s\n%s", tool_name, task.task_id, type(e).__name__, stack_frames(e)
                        )
                        raise ToolError(f"{prefix} Error in {tool_name}: {e}") from e
                    raise ToolError(f"{prefix} Cancelled during execution", log_level=logging.WARNING) from e

                if not self._transition(task, TaskStatus.COMPLETED):
                    raise ToolError(f"{prefix} Cancelled during execution", log_level=logging.WARNING)

                # Prepend task_id to text results
                if isinstance(result, str):
                    return f"{prefix} {result}"
                if isinstance(result, list):
                    # For multi-content results (images + text), prepend task_id to first text
                    injected = False
                    for item in result:
                        if hasattr(item, "text") and not injected:
                            item.text = f"{prefix} {item.text}"
                            injected = True
                    if not injected:
                        # Add task_id as first element
                        from mcp.types import TextContent

                        result.insert(0, TextContent(type="text", text=prefix))
                    return result
                return result

            finally:
                if sem:
                    sem.release()

        return wrapper


# Global task manager instance
manager = TaskManager()
