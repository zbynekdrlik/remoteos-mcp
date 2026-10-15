"""Network diagnostic tools.

A failure raises and the tool layer reports it as an MCP error (#32, upstream
winremote-mcp PR 83): ping cannot run or hangs, PortCheck's host does not
resolve, psutil is refused. Ping's own output (also for an unknown or silent
host) and a port that is closed or does not answer are results.
"""

from __future__ import annotations

import socket
import subprocess
import sys

from remoteos.platform.command import console_decoding


def ping(host: str, count: int = 4) -> str:
    """Ping a host: ping's own output, also when the host does not answer."""
    flag = "-n" if sys.platform == "win32" else "-c"
    timeout = count * 5 + 10
    try:
        result = subprocess.run(
            ["ping", flag, str(count), host],
            capture_output=True,
            timeout=timeout,
            **console_decoding(),  # Windows ping writes the console's OEM code page (#32)
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ping timed out after {timeout}s") from None
    return (result.stdout or "").strip() or (result.stderr or "").strip() or "(no output)"


def port_check(host: str, port: int, timeout: float = 5.0) -> str:
    """Check if a TCP port is open (a closed or silent port is an answer, not a failure)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
    except socket.timeout:
        return f"Port {port} on {host} — connection timed out ({timeout}s)"
    finally:
        sock.close()
    if result == 0:
        return f"Port {port} on {host} is OPEN"
    return f"Port {port} on {host} is CLOSED (code {result})"


def net_connections(filter_str: str = "", limit: int = 50) -> str:
    """List network connections using psutil."""
    import psutil
    from tabulate import tabulate

    conns = psutil.net_connections(kind="inet")
    rows = []
    for c in conns:
        local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
        remote = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
        status = c.status
        pid = c.pid or ""

        if filter_str:
            searchable = f"{local} {remote} {status} {pid}"
            if filter_str.lower() not in searchable.lower():
                continue

        rows.append([local, remote, status, pid])

    if not rows:
        return "No connections found."
    rows = rows[:limit]
    return tabulate(rows, headers=["Local", "Remote", "Status", "PID"], tablefmt="simple")
