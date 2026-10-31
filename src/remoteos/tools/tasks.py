"""The task manager's own tools: CancelTask, GetTaskStatus, GetRunningTasks."""

from __future__ import annotations

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from remoteos.app import mcp
from remoteos.errors import failure, reports_errors
from remoteos.taskmanager import manager as task_manager


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@reports_errors
def CancelTask(task_id: str) -> str:
    """Cancel a running or pending task by its task ID.

    Args:
        task_id: The task ID returned when the tool was invoked (e.g. from [task:abc123]).
    """
    result = task_manager.cancel_task(task_id)
    if "error" in result:
        raise failure(f"Cancel failed: {result['error']}")
    return f"Cancelled task {task_id} ({result['tool_name']})"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@reports_errors
def GetTaskStatus(task_id: str = "") -> str:
    """Get status of a specific task or list recent tasks.

    Args:
        task_id: If provided, get status of this task. If empty, list recent tasks.
    """
    import json

    if task_id:
        info = task_manager.get_task(task_id)
        if info is None:
            raise failure(f"Task {task_id} not found")
        return json.dumps(info, indent=2)
    tasks = task_manager.list_tasks()
    if not tasks:
        return "No tasks in history."
    lines = ["Recent tasks:"]
    for t in tasks[:20]:
        dur = f" ({t['duration']}s)" if t["duration"] is not None else ""
        err = f" — {t['error']}" if t.get("error") else ""
        lines.append(f"  [{t['task_id']}] {t['tool_name']} → {t['status']}{dur}{err}")
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@reports_errors
def GetRunningTasks() -> str:
    """List all currently running and pending tasks."""

    running = task_manager.list_tasks("running")
    pending = task_manager.list_tasks("pending")
    all_active = running + pending
    if not all_active:
        return "No active tasks."
    lines = [f"Active tasks ({len(all_active)}):"]
    for t in all_active:
        dur = f" ({t['duration']}s)" if t["duration"] is not None else ""
        lines.append(f"  [{t['task_id']}] {t['tool_name']} [{t['category']}] {t['status']}{dur}")
    return "\n".join(lines)
