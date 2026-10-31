"""System tools: preferences/registry, services, SelfUpgrade, scheduled tasks."""

from __future__ import annotations

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from remoteos import selfupgrade
from remoteos.app import mcp
from remoteos.errors import reports_errors
from remoteos.platform import get_services, get_system

# ============================== REGISTRY ===================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="RegRead",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def RegRead(key: str, value_name: str) -> str:
    """Read system preferences (Windows registry / macOS defaults).

    Args:
        key: Registry key path, e.g. "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion".
        value_name: Name of the value to read.
    """
    return get_system().reg_read(key, value_name)


@mcp.tool(
    annotations=ToolAnnotations(
        title="RegWrite",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def RegWrite(key: str, value_name: str, data: str, reg_type: str = "REG_SZ") -> str:
    """Write system preferences (Windows registry / macOS defaults).

    Args:
        key: Registry key path, e.g. "HKCU\\SOFTWARE\\MyApp".
        value_name: Name of the value to write.
        data: Value data. For REG_DWORD/REG_QWORD pass as string number. For REG_MULTI_SZ use | separator.
        reg_type: Registry type: REG_SZ, REG_EXPAND_SZ, REG_DWORD, REG_QWORD, REG_BINARY, REG_MULTI_SZ.
    """
    return get_system().reg_write(key, value_name, data, reg_type)


# ============================= SERVICES ====================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="ServiceList",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def ServiceList(filter: str = "") -> str:
    """List system services.

    Args:
        filter: Filter by service name or display name (substring match).
    """
    return get_services().service_list(filter)


@mcp.tool(
    annotations=ToolAnnotations(
        title="SelfUpgrade",
        destructiveHint=True,
        openWorldHint=True,
    )
)
@reports_errors
def SelfUpgrade(ref: str = "") -> str:
    """Upgrade this box to a git ref: start the OS's installer detached from this server, return at once.

    The installer proves the new version and rolls back on a failure. It restarts this server,
    so the reply is only "started": poll /health for the new version, and read the log the reply
    names (root-owned on Linux, reachable with sudo).

    Args:
        ref: A commit sha, tag or branch of remoteos-mcp (default main).
    """
    return selfupgrade.start(selfupgrade.upgrade_plan(ref))


@mcp.tool(
    annotations=ToolAnnotations(
        title="ServiceStart",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def ServiceStart(name: str) -> str:
    """Start a system service.

    Args:
        name: Service name.
    """
    return get_services().service_start(name)


@mcp.tool(
    annotations=ToolAnnotations(
        title="ServiceStop",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def ServiceStop(name: str) -> str:
    """Stop a system service.

    Args:
        name: Service name.
    """
    return get_services().service_stop(name)


# ========================= SCHEDULED TASKS =================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="TaskList",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def TaskList(filter: str = "") -> str:
    """List scheduled tasks.

    Args:
        filter: Filter by task name (substring match).
    """
    return get_services().task_list(filter)


@mcp.tool(
    annotations=ToolAnnotations(
        title="TaskCreate",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def TaskCreate(name: str, command: str, schedule: str) -> str:
    """Create a scheduled task.

    Args:
        name: Task name.
        command: Command to execute.
        schedule: Schedule type (ONCE, DAILY, WEEKLY, MONTHLY, ONSTART, ONLOGON, ONIDLE).
    """
    return get_services().task_create(name, command, schedule)


@mcp.tool(
    annotations=ToolAnnotations(
        title="TaskDelete",
        destructiveHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def TaskDelete(name: str) -> str:
    """Delete a scheduled task.

    Args:
        name: Task name.
    """
    return get_services().task_delete(name)
