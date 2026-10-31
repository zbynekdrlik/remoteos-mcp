"""Network tools, and EventLog, which comes right after them in the tool list."""

from __future__ import annotations

try:
    from mcp.types import ToolAnnotations
except ImportError:
    from fastmcp.tools import ToolAnnotations

from remoteos import network
from remoteos.app import mcp
from remoteos.errors import reports_errors
from remoteos.platform import get_services


@mcp.tool(
    annotations=ToolAnnotations(
        title="Ping",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
@reports_errors
def Ping(host: str, count: int = 4) -> str:
    """Ping a host.

    Args:
        host: Hostname or IP address.
        count: Number of ping requests (default 4).
    """
    return network.ping(host, count)


@mcp.tool(
    annotations=ToolAnnotations(
        title="PortCheck",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
@reports_errors
def PortCheck(host: str, port: int, timeout: float = 5.0) -> str:
    """Check if a TCP port is open.

    Args:
        host: Hostname or IP address.
        port: Port number.
        timeout: Connection timeout in seconds (default 5).
    """
    return network.port_check(host, port, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        title="NetConnections",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def NetConnections(filter: str = "", limit: int = 50) -> str:
    """List network connections.

    Args:
        filter: Filter connections by local/remote address, status, or PID.
        limit: Maximum number of connections to return (default 50).
    """
    return network.net_connections(filter, limit=limit)


# ============================ EVENT LOG ====================================


@mcp.tool(
    annotations=ToolAnnotations(
        title="EventLog",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
@reports_errors
def EventLog(log_name: str = "System", count: int = 20, level: str = "") -> str:
    """Read system event log entries.

    Args:
        log_name: Log name (System, Application, Security, etc.).
        count: Number of entries to retrieve (default 20).
        level: Filter by level: critical, error, warning, information, verbose.
    """
    return get_services().event_log(log_name, count, level)
