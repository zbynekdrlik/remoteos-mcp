"""``remoteos-call``: run one MCP tool on a fleet machine from a script (#70, part b of #67).

    remoteos-call --list
    remoteos-call linux-stream-snv Shell 'sudo -n true; echo ok' [--timeout 60] [--cwd /tmp]
    remoteos-call linux-stream-snv put ./pkg.deb /tmp/pkg.deb
    remoteos-call linux-stream-snv get /var/log/syslog ./syslog
    remoteos-call win-resolume-pp GetSystemInfo --json '{}'

The machine's ``url`` and ``headers.Authorization`` come from an MCP config: ``--config PATH``,
else ``--project NAME`` (``~/.claude/mcp-configs/NAME.mcp.json``, what ``remoteos-gateway
sync-configs`` writes), else ``./.mcp.json``. The token is read from that file only. It never
goes into an argv, an output line or a log record.

Exit codes:

- ``Shell``: the remote command's exit code (``[Exit code: N]`` in Shell's reply, none = 0).
  A code outside 0..255 is reported as 255 (a negative POSIX signal code ``-S`` as 128+S),
  with one ``remoteos-call:`` line on stderr that names the real code.
- 0: put/get/any other tool succeeded.
- 125: the tool itself failed (the MCP result has ``isError``), or a put/get size check failed.
- 126: no call was possible: a missing/broken config, an unknown machine, a transport or auth
  failure (connection refused, 401/403/404/413, a timeout).
- 2: a usage error (click).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import click

from remoteos import __version__

log = logging.getLogger("remoteos.cli_call")

EXIT_TOOL_ERROR = 125
EXIT_NO_CALL = 126

# The fleet's MCP names: ``win-…``, ``linux-…``, ``mac-…`` (an http entry with another name, e.g.
# a project's own n8n server, is not a machine this CLI talks to).
_FLEET_NAME_RE = re.compile(r"(?:win|linux|mac)-[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Every wrapped tool's reply starts with the task id (taskmanager.wrap_sync_tool).
_TASK_PREFIX_RE = re.compile(r"\A\[task:[^\]\s]+\] ?")
_EXIT_CODE_RE = re.compile(r"(?:\A|\n)\[Exit code: (-?\d+)\]\Z")
_STDERR_MARK = "[STDERR] "
_NO_OUTPUT = "(no output)"  # ChildOutput.reply() when there is nothing at all
_DOWNLOAD_RE = re.compile(r"\Abase64:(\d+)bytes:", re.ASCII)
_UPLOAD_RE = re.compile(r"\AWritten (\d+) bytes to ", re.ASCII)

# A Shell call may run up to its own timeout; the client waits that long plus this margin (the
# server's own stale-task sweep allows the declared runtime + 60 s, taskmanager._RUNTIME_ARGS).
SHELL_WAIT_MARGIN_S = 60
DEFAULT_WAIT_S = 300.0  # put/get/other tools: a 12 MB transfer over a slow tailnet link fits
CONNECT_TIMEOUT_S = 30.0


class CallFailed(Exception):
    """A failure with its exit code and ONE line for stderr (never the token)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- the config ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Machine:
    name: str
    url: str
    authorization: str | None = field(default=None, repr=False)  # "Bearer <token>": never printed


def config_path(config: Path | None, project: str | None, cwd: Path | None = None) -> Path:
    """``--config``, else ``--project`` (``~/.claude/mcp-configs/NAME.mcp.json``), else ``./.mcp.json``."""
    if config is not None:
        return config.expanduser()
    if project is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", project):
            raise CallFailed(EXIT_NO_CALL, f"invalid project name {project!r}")
        return Path.home() / ".claude" / "mcp-configs" / f"{project}.mcp.json"
    return (cwd or Path.cwd()) / ".mcp.json"


def load_servers(path: Path) -> dict:
    """The ``mcpServers`` object of the config at *path*."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CallFailed(EXIT_NO_CALL, f"no MCP config at {path} (use --config PATH or --project NAME)") from None
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot read the MCP config {path}: {exc.strerror}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:  # the message names a position only, never the content
        raise CallFailed(EXIT_NO_CALL, f"the MCP config {path} is not valid JSON (line {exc.lineno})") from None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise CallFailed(EXIT_NO_CALL, f"the MCP config {path} has no mcpServers object")
    return servers


def fleet_machines(servers: dict) -> list[str]:
    """The fleet machine names in a config, sorted: ``win-``/``linux-``/``mac-`` entries with an url."""
    return sorted(
        name
        for name, entry in servers.items()
        if _FLEET_NAME_RE.fullmatch(name) and isinstance(entry, dict) and isinstance(entry.get("url"), str)
    )


def resolve_machine(servers: dict, name: str, path: Path) -> Machine:
    if not _FLEET_NAME_RE.fullmatch(name) or name not in servers:
        known = ", ".join(fleet_machines(servers)) or "none"
        raise CallFailed(EXIT_NO_CALL, f"unknown machine {name!r} in {path} (known: {known})")
    entry = servers[name]
    url = entry.get("url") if isinstance(entry, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise CallFailed(EXIT_NO_CALL, f"machine {name!r} in {path} has no http(s) url")
    headers = entry.get("headers")
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    if authorization is not None and not isinstance(authorization, str):
        raise CallFailed(EXIT_NO_CALL, f"machine {name!r} in {path} has a non-string Authorization header")
    return Machine(name=name, url=url, authorization=authorization)


# -- the replies ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShellReply:
    stdout: str
    stderr: str
    exit_code: int


def strip_task_prefix(text: str) -> str:
    return _TASK_PREFIX_RE.sub("", text, count=1)


def parse_shell_reply(text: str) -> ShellReply:
    """Split Shell's reply (``ChildOutput.reply()``) back into stdout, stderr and the exit code.

    The reply is ``stdout``, then ``\\n[STDERR] <stderr>`` (non-empty stderr), then
    ``\\n[Exit code: N]`` (N != 0), stripped as a whole, ``(no output)`` when empty. The split is
    at the FIRST ``[STDERR]`` marker: a stdout line that itself starts with ``[STDERR] `` cannot
    be told apart (documented limitation). The strip also loses leading whitespace of stdout and
    the trailing newlines of each part.
    """
    text = strip_task_prefix(text)
    exit_code = 0
    match = _EXIT_CODE_RE.search(text)
    if match:
        exit_code = int(match.group(1))
        text = text[: match.start()]
    if text == _NO_OUTPUT:
        return ShellReply("", "", exit_code)
    if text.startswith(_STDERR_MARK):  # no stdout at all (the strip removed the newline)
        return ShellReply("", text[len(_STDERR_MARK) :].rstrip("\n"), exit_code)
    stdout, mark, stderr = text.partition("\n" + _STDERR_MARK)
    return ShellReply(stdout.rstrip("\n"), stderr.rstrip("\n") if mark else "", exit_code)


def process_exit_code(remote: int) -> int:
    """The remote exit code as a process exit code (0..255)."""
    if 0 <= remote <= 255:
        return remote
    if -127 <= remote < 0:  # POSIX Popen: killed by signal -remote, the shell's 128+S convention
        return 128 - remote
    return 255  # a Windows 32-bit code (4294967295 = exit -1) or anything else out of range


def result_text(result) -> str:
    """The text blocks of a ``CallToolResult``, joined; other blocks are named, never dumped."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(f"[{getattr(block, 'type', 'content')} block: {getattr(block, 'mimeType', '?')}]")
    return "\n".join(parts)


# -- the call ------------------------------------------------------------------------------


def _scrub(text: str, machine: Machine) -> str:
    """Remove the token from any text before it is shown (exception messages may echo a request)."""
    if machine.authorization:
        text = text.replace(machine.authorization, "<authorization>")
        token = machine.authorization.split(" ", 1)[-1]
        if token:
            text = text.replace(token, "<token>")
    return text


def _innermost(exc: BaseException) -> BaseException:
    """The first leaf of an (anyio) exception group, else the deepest cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    seen = set()
    while exc.__cause__ is not None and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.__cause__
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
    return exc


_HTTP_REASONS = {
    401: "authentication refused (401): the token in the config is not accepted",
    403: "not allowed (403): this project's token may not reach this machine",
    404: "not found (404): the gateway does not expose this machine",
    413: "request too large (413): the gateway caps a request at 16 MiB (about 12 MB of file)",
}


class _HttpErrors:
    """An httpx response hook: the first HTTP error status of the call.

    The MCP SDK turns a non-2xx answer to a request into a JSON-RPC error with a fixed text
    ("Server returned an error response"), so the status (401, 403, 413) is read here instead.
    """

    def __init__(self) -> None:
        self.status: int | None = None

    async def __call__(self, response) -> None:
        if response.status_code >= 400 and self.status is None:
            self.status = response.status_code


def _transport_failure(exc: BaseException, machine: Machine, status: int | None = None) -> CallFailed:
    """One stderr line for a call that never produced a tool result (exit 126)."""
    import httpx2
    from mcp.shared.exceptions import MCPError
    from mcp.types import REQUEST_TIMEOUT

    leaf = _innermost(exc)
    if isinstance(leaf, httpx2.HTTPStatusError):
        status = leaf.response.status_code
    if status is not None:
        reason = _HTTP_REASONS.get(status, f"HTTP {status}")
    elif isinstance(leaf, httpx2.TimeoutException) or (isinstance(leaf, MCPError) and leaf.code == REQUEST_TIMEOUT):
        reason = "timed out waiting for the reply"
    elif isinstance(leaf, httpx2.ConnectError):
        reason = "cannot connect (connection refused or host unreachable)"
    else:
        detail = str(leaf).splitlines()[0] if str(leaf) else ""
        reason = f"{type(leaf).__name__}: {detail}" if detail else type(leaf).__name__
    return CallFailed(EXIT_NO_CALL, _scrub(f"{machine.name}: {reason} ({_display_url(machine.url)})", machine))


def _protocol_failure(exc: BaseException, machine: Machine) -> CallFailed | None:
    """A JSON-RPC error the server itself answered (HTTP 200) to the tool call: exit 125.

    None when the failure is not such an answer (a timeout, a dropped connection: exit 126).
    """
    from mcp.shared.exceptions import MCPError
    from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

    leaf = _innermost(exc)
    if isinstance(leaf, MCPError) and leaf.code not in (REQUEST_TIMEOUT, CONNECTION_CLOSED):
        return CallFailed(EXIT_TOOL_ERROR, _scrub(f"{machine.name}: {leaf.message}", machine))
    return None


def _display_url(url: str) -> str:
    """The url without a query string or userinfo (neither should hold a secret, but never show one)."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


async def call_tool(machine: Machine, tool: str, arguments: dict, wait_s: float):
    """One ``tools/call`` on *machine*; returns fastmcp's ``CallToolResult`` (``is_error`` set, not raised)."""
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    http_errors = _HttpErrors()

    def http_client(headers=None, timeout=None, auth=None, **_ignored) -> httpx2.AsyncClient:
        # fastmcp's default client, plus the status hook. Redirects stay off (httpx's default, as
        # in fastmcp's own default client): the SDK follows same-origin redirects itself.
        client = httpx2.AsyncClient(
            headers=headers, auth=auth, timeout=timeout or httpx2.Timeout(CONNECT_TIMEOUT_S, read=wait_s)
        )
        client.event_hooks["response"].append(http_errors)
        return client

    headers = {"Authorization": machine.authorization} if machine.authorization else {}
    transport = StreamableHttpTransport(machine.url, headers=headers, httpx_client_factory=http_client)
    # "legacy" = the plain initialize handshake every fleet machine (old and new fastmcp) and the
    # gateway's own probe use; "auto" would first probe server/discover on every call.
    client = Client(transport, timeout=wait_s, init_timeout=CONNECT_TIMEOUT_S, mode="legacy")
    log.debug("calling %s on %s", tool, machine.name)  # never the arguments, never the token
    connected = False
    try:
        async with client:
            connected = True
            return await client.call_tool(tool, arguments, raise_on_error=False, timeout=wait_s)
    except Exception as exc:  # no tool result exists
        log.debug("call to %s failed: %s", machine.name, type(exc).__name__)
        answered = _protocol_failure(exc, machine) if connected and http_errors.status is None else None
        raise (answered or _transport_failure(exc, machine, http_errors.status)) from None


def _run(machine: Machine, tool: str, arguments: dict, wait_s: float):
    return asyncio.run(call_tool(machine, tool, arguments, wait_s))


def _tool_failed(result) -> CallFailed:
    return CallFailed(EXIT_TOOL_ERROR, strip_task_prefix(result_text(result)))


def run_shell(machine: Machine, command: str, timeout: int, cwd: str | None) -> int:
    arguments: dict = {"command": command, "timeout": timeout}
    if cwd:
        arguments["cwd"] = cwd
    result = _run(machine, "Shell", arguments, timeout + SHELL_WAIT_MARGIN_S)
    if result.is_error:
        raise _tool_failed(result)
    reply = parse_shell_reply(result_text(result))
    if reply.stdout:
        click.echo(reply.stdout)
    if reply.stderr:
        click.echo(reply.stderr, err=True)
    code = process_exit_code(reply.exit_code)
    if code != reply.exit_code:
        click.echo(f"remoteos-call: remote exit code {reply.exit_code}, reported as {code}", err=True)
    return code


def run_put(machine: Machine, local: Path, remote: str, wait_s: float) -> int:
    try:
        data = local.read_bytes()
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot read {local}: {exc.strerror}") from None
    result = _run(machine, "FileUpload", {"path": remote, "data_base64": base64.b64encode(data).decode()}, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    reply = strip_task_prefix(result_text(result))
    match = _UPLOAD_RE.match(reply)
    if not match:
        raise CallFailed(EXIT_TOOL_ERROR, f"put: unexpected FileUpload reply: {reply[:200]}")
    written = int(match.group(1))
    if written != len(data):
        raise CallFailed(EXIT_TOOL_ERROR, f"put: size mismatch, sent {len(data)} bytes, remote wrote {written}")
    return 0


def run_get(machine: Machine, remote: str, local: Path, wait_s: float) -> int:
    result = _run(machine, "FileDownload", {"path": remote}, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    reply = strip_task_prefix(result_text(result))
    match = _DOWNLOAD_RE.match(reply)
    if not match:
        raise CallFailed(EXIT_TOOL_ERROR, f"get: unexpected FileDownload reply: {reply[:200]}")
    declared = int(match.group(1))
    try:
        data = base64.b64decode(reply[match.end() :], validate=True)
    except ValueError as exc:
        raise CallFailed(EXIT_TOOL_ERROR, f"get: the FileDownload reply is not valid base64 ({exc})") from None
    if len(data) != declared:
        raise CallFailed(EXIT_TOOL_ERROR, f"get: size mismatch, remote sent {declared} bytes, received {len(data)}")
    _write_atomically(local, data)
    return 0


def _write_atomically(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a temp file in the same directory: no half-written file."""
    directory = path.parent if str(path.parent) else Path(".")
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".part")
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot write into {directory}: {exc.strerror}") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise CallFailed(EXIT_NO_CALL, f"cannot write {path}: {exc.strerror}") from None


def run_tool(machine: Machine, tool: str, arguments: dict, wait_s: float) -> int:
    result = _run(machine, tool, arguments, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    text = strip_task_prefix(result_text(result))
    if text:
        click.echo(text)
    return 0


# -- the command ---------------------------------------------------------------------------


def _parse_json_arguments(raw: str) -> dict:
    try:
        arguments = json.loads(raw)
    except ValueError as exc:
        raise click.UsageError(f"--json is not valid JSON ({exc.msg} at position {exc.pos})") from None
    if not isinstance(arguments, dict):
        raise click.UsageError("--json must be a JSON object")
    return arguments


def _plan(tool: str, args: tuple[str, ...], timeout, cwd, json_args) -> Callable[[Machine], int]:
    """Check the command line's shape BEFORE the config is read; returns the call to make.

    A usage error is exit 2 whatever the config holds, and nothing is called.
    """
    wait = DEFAULT_WAIT_S if timeout is None else timeout
    if json_args is not None:
        if args or cwd is not None:
            raise click.UsageError("--json carries all of the tool's arguments: no positional arguments, no --cwd")
        arguments = _parse_json_arguments(json_args)
        return lambda machine: run_tool(machine, tool, arguments, wait)
    if tool == "Shell":
        if len(args) != 1:
            raise click.UsageError("Shell takes exactly one COMMAND (quote it)")
        shell_timeout = 30 if timeout is None else timeout
        return lambda machine: run_shell(machine, args[0], shell_timeout, cwd)
    if cwd is not None:
        raise click.UsageError("--cwd applies to Shell only")
    if tool == "put":
        if len(args) != 2:
            raise click.UsageError("put takes exactly two paths: LOCAL REMOTE")
        return lambda machine: run_put(machine, Path(args[0]), args[1], wait)
    if tool == "get":
        if len(args) != 2:
            raise click.UsageError("get takes exactly two paths: REMOTE LOCAL")
        return lambda machine: run_get(machine, args[0], Path(args[1]), wait)
    if args:
        raise click.UsageError(f"{tool}: pass its arguments with --json '{{...}}'")
    return lambda machine: run_tool(machine, tool, {}, wait)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("machine", required=False)
@click.argument("tool", required=False)
@click.argument("args", nargs=-1)
@click.option("--config", type=click.Path(dir_okay=False, path_type=Path), help="The MCP config to read.")
@click.option("--project", help="Read ~/.claude/mcp-configs/PROJECT.mcp.json.")
@click.option("--list", "list_machines", is_flag=True, help="List the fleet machines in the config.")
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    help="Shell: the command's timeout in seconds (default 30). put/get/--json: how long to wait (default 300).",
)
@click.option("--cwd", help="Shell: the remote working directory.")
@click.option("--json", "json_args", help="The tool's arguments as a JSON object (any tool).")
@click.version_option(__version__, prog_name="remoteos-call")
def main(machine, tool, args, config, project, list_machines, timeout, cwd, json_args) -> None:
    """Run TOOL on MACHINE over MCP: Shell COMMAND, put LOCAL REMOTE, get REMOTE LOCAL, or TOOL --json.

    Exit codes: Shell = the remote exit code; 0 = success; 125 = the tool failed; 126 = no call
    was possible (config, unknown machine, transport, auth); 2 = usage.
    """
    if config is not None and project is not None:
        raise click.UsageError("--config and --project are exclusive")
    try:
        path = config_path(config, project)
        if list_machines:
            if machine or tool:
                raise click.UsageError("--list takes no MACHINE or TOOL")
            for name in fleet_machines(load_servers(path)):
                click.echo(name)
            sys.exit(0)
        if not machine or not tool:
            raise click.UsageError("MACHINE and TOOL are required (or --list)")
        call = _plan(tool, args, timeout, cwd, json_args)
        sys.exit(call(resolve_machine(load_servers(path), machine, path)))
    except CallFailed as failure:
        click.echo(f"remoteos-call: {failure.message}", err=True)
        sys.exit(failure.code)


if __name__ == "__main__":
    main()
