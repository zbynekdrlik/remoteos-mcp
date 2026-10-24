"""``remoteos-call``: run one MCP tool on a fleet machine from a script (#70, part b of #67).

    remoteos-call --list
    remoteos-call linux-stream-snv Shell 'sudo -n true; echo ok' [--timeout 60] [--cwd /tmp]
    remoteos-call linux-stream-snv put ./pkg.deb /tmp/pkg.deb
    remoteos-call linux-stream-snv get /var/log/syslog ./syslog
    remoteos-call win-resolume-pp GetSystemInfo --json '{}'

The machine's ``url`` and ``Authorization`` header come from an MCP config: ``--config PATH``,
else ``--project NAME`` (``~/.claude/mcp-configs/NAME.mcp.json``, what ``remoteos-gateway
sync-configs`` writes), else ``./.mcp.json``. The token is read from that file only. It never
goes into an argv, an output line or a log record.

Exit codes:

- ``Shell``: the remote command's exit code (``[Exit code: N]`` in Shell's reply, none = 0).
  A code outside 0..255 is reported as 255 (a negative POSIX signal code ``-S`` as 128+S),
  with one ``remoteos-call:`` line on stderr that names the real code.
- 0: put/get/any other tool succeeded.
- 125: the tool itself failed (the MCP result has ``isError``, or the server answered the call
  with a JSON-RPC error), or a put/get size check failed.
- 126: the CLI got no result: a missing/broken config, an unknown machine, a refused connection,
  401/403/404/413, no reply in time, a dropped connection, a local file it cannot read/write.
  After a timeout or a dropped connection the remote command MAY have run (or still run).
- 141: stdout was closed (a broken pipe, e.g. ``| head``).
- 2: a usage error (click).

The CLI's own failures are always ONE stderr line starting with ``remoteos-call:``; a remote
command that itself exits 125 or 126 prints no such line.
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
from remoteos.gateway.registry import MachineSourceError, authorization_header, is_http_url, read_json_file
from remoteos.taskmanager import declared_runtime

log = logging.getLogger("remoteos.cli_call")

EXIT_TOOL_ERROR = 125
EXIT_NO_CALL = 126
EXIT_BROKEN_PIPE = 141  # 128 + SIGPIPE, what a shell reports for a writer whose reader went away

# The fleet's MCP names: ``win-…``, ``linux-…``, ``mac-…`` (an http entry with another name, e.g.
# a project's own n8n server, is not a machine this CLI talks to).
_FLEET_NAME_RE = re.compile(r"(?:win|linux|mac)-[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # as gateway/manifest.py
# Every wrapped tool's reply starts with the task id (taskmanager.wrap_sync_tool).
_TASK_PREFIX_RE = re.compile(r"\A\[task:[^\]\s]+\] ?")
_EXIT_CODE_RE = re.compile(r"(?:\A|\n)\[Exit code: (-?\d+)\]\Z")
_STDERR_MARK = "[STDERR] "
_NO_OUTPUT = "(no output)"  # ChildOutput.reply() when there is nothing at all
_DOWNLOAD_RE = re.compile(r"\Abase64:(\d+)bytes:", re.ASCII)
_UPLOAD_RE = re.compile(r"\AWritten (\d+) bytes to ", re.ASCII)

SHELL_DEFAULT_TIMEOUT_S = 30  # Shell's own default
DEFAULT_WAIT_S = 300.0  # put/get/--json: how long the client waits for the reply
CONNECT_TIMEOUT_S = 30.0
# One reply is ONE server-sent event. httpx2's EventSource refuses an event over 1 MiB by default,
# which a FileDownload of ~780 KB or a Shell printing 1 MB already exceeds. The largest legitimate
# reply is a FileDownload of a big file (its base64), so the CLI allows this much.
MAX_REPLY_EVENT_BYTES = 256 * 1024 * 1024
# Libraries whose loggers the CLI silences: their DEBUG lines carry tool arguments and replies,
# and any line they print would land in the remote command's stderr stream.
_LIBRARY_LOGGERS = ("fastmcp", "mcp", "httpx2", "httpcore2")


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
        return Path.home() / ".claude" / "mcp-configs" / f"{project}.mcp.json"
    return (cwd or Path.cwd()) / ".mcp.json"


def load_servers(path: Path) -> dict:
    """The ``mcpServers`` object of the config at *path* (the gateway's reader: never echoes content)."""
    if not path.exists():
        raise CallFailed(EXIT_NO_CALL, f"no MCP config at {path} (use --config PATH or --project NAME)")
    try:
        data = read_json_file(path, "the MCP config")
    except MachineSourceError as exc:
        raise CallFailed(EXIT_NO_CALL, str(exc)) from None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise CallFailed(EXIT_NO_CALL, f"the MCP config {path} has no mcpServers object")
    return servers


def _usable(entry: object) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("url"), str) and is_http_url(entry["url"])


def fleet_machines(servers: dict) -> list[str]:
    """The fleet machine names in a config, sorted: ``win-``/``linux-``/``mac-`` entries with an http(s) url."""
    return sorted(name for name, entry in servers.items() if _FLEET_NAME_RE.fullmatch(name) and _usable(entry))


def resolve_machine(servers: dict, name: str, path: Path) -> Machine:
    if not _FLEET_NAME_RE.fullmatch(name) or name not in servers:
        known = ", ".join(fleet_machines(servers)) or "none"
        raise CallFailed(EXIT_NO_CALL, f"unknown machine {name!r} in {path} (known: {known})")
    entry = servers[name]
    if not _usable(entry):
        raise CallFailed(EXIT_NO_CALL, f"machine {name!r} in {path} has no valid http(s) url")
    # the gateway's own reading: any spelling of the header name, the last one wins
    return Machine(name=name, url=entry["url"], authorization=authorization_header(entry))


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
    ``\\n[Exit code: N]`` (N != 0), stripped as a whole, ``(no output)`` when empty. Plain text
    cannot round-trip everything (README "Scripts: remoteos-call"): the split is at the FIRST
    ``[STDERR]`` marker, a final ``[Exit code: N]`` line the command printed itself reads as its
    exit code, a literal ``(no output)`` reads as nothing, and the strip loses leading whitespace
    of stdout and trailing whitespace of the last part.
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
            parts.append(f"[{getattr(block, 'type', 'content')} block: {getattr(block, 'mime_type', None) or '?'}]")
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
    # the machine's MCP transport takes 4 MiB per request (mcp DEFAULT_MAX_REQUEST_BODY_SIZE), the
    # gateway 16 MiB (gateway/app.py MAX_BODY_BYTES): the machine's limit is the one a put meets
    413: "request too large (413): a machine takes at most 4 MiB per request, so a put carries about 3 MB",
}


class _HttpErrors:
    """An httpx response hook: the first HTTP error status of a POST (an MCP request) of the call.

    The MCP SDK turns a non-2xx answer to a request into a JSON-RPC error with a fixed text
    ("Server returned an error response"), so the status (401, 403, 413) is read here instead.
    """

    def __init__(self) -> None:
        self.status: int | None = None

    async def __call__(self, response) -> None:
        if response.status_code >= 400 and self.status is None and response.request.method == "POST":
            self.status = response.status_code


def _transport_failure(exc: BaseException, machine: Machine, status: int | None = None) -> CallFailed:
    """One stderr line for a call that produced no tool result (exit 126)."""
    import httpx2
    from mcp.shared.exceptions import MCPError
    from mcp.types import REQUEST_TIMEOUT

    leaf = _innermost(exc)
    if isinstance(leaf, httpx2.HTTPStatusError):
        status = leaf.response.status_code
    if status is not None:
        reason = _HTTP_REASONS.get(status, f"HTTP {status}")
    elif isinstance(leaf, (httpx2.TimeoutException, TimeoutError)) or (
        isinstance(leaf, MCPError) and leaf.code == REQUEST_TIMEOUT
    ):
        reason = "timed out waiting for the reply (the command may still run)"
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

    parts = urlsplit(url)  # resolve_machine let only is_http_url() urls through: this cannot raise
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}"


def _allow_large_replies() -> None:
    """Lift the SDK's 1 MiB per-event limit for this process (see ``MAX_REPLY_EVENT_BYTES``).

    ``mcp.client.streamable_http`` builds ``EventSource(response)`` with httpx2's default and has
    no option for it, so the CLI swaps in a subclass with a larger default. Idempotent. An SDK that
    no longer uses ``EventSource`` fails loudly here rather than truncating replies silently.
    """
    import httpx2
    from mcp.client import streamable_http

    current = getattr(streamable_http, "EventSource", None)
    if current is None or not (isinstance(current, type) and issubclass(current, httpx2.EventSource)):
        raise RuntimeError("mcp.client.streamable_http no longer uses httpx2.EventSource")
    if current is not httpx2.EventSource:
        return  # already swapped

    class LargeEventSource(httpx2.EventSource):
        def __init__(self, response, max_event_size: int | None = MAX_REPLY_EVENT_BYTES) -> None:
            super().__init__(response, max_event_size=max_event_size)

    streamable_http.EventSource = LargeEventSource


def _quiet_library_logging() -> None:
    """Silence the libraries' own loggers (fastmcp configures its stderr handler at import)."""
    for name in _LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)


async def call_tool(machine: Machine, tool: str, arguments: dict, wait_s: float):
    """One ``tools/call`` on *machine*; returns fastmcp's ``CallToolResult`` (``is_error`` set, not raised)."""
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    _quiet_library_logging()
    _allow_large_replies()
    http_errors = _HttpErrors()

    def http_client(headers=None, timeout=None, auth=None, **_ignored) -> httpx2.AsyncClient:
        # fastmcp's default client (redirects off: the SDK follows same-origin ones itself), plus
        # the status hook, and trust_env off like the gateway's own clients: no HTTP(S)_PROXY may
        # carry the bearer elsewhere, no ~/.netrc may add credentials.
        client = httpx2.AsyncClient(
            headers=headers,
            auth=auth,
            timeout=timeout or httpx2.Timeout(CONNECT_TIMEOUT_S, read=wait_s),
            trust_env=False,
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


def _echo(text: str, err: bool = False) -> None:
    """Print remote text unchanged: ``color=True`` keeps ANSI escapes click strips off a pipe."""
    click.echo(text, err=err, color=True)


def run_shell(machine: Machine, command: str, timeout: int, cwd: str | None) -> int:
    arguments: dict = {"command": command, "timeout": timeout}
    if cwd:
        arguments["cwd"] = cwd
    # the client waits as long as the server's own stale-task sweep allows the call
    result = _run(machine, "Shell", arguments, declared_runtime(arguments))
    if result.is_error:
        raise _tool_failed(result)
    reply = parse_shell_reply(result_text(result))
    if reply.stdout:
        _echo(reply.stdout)
    if reply.stderr:
        _echo(reply.stderr, err=True)
    code = process_exit_code(reply.exit_code)
    if code != reply.exit_code:
        _echo(f"remoteos-call: remote exit code {reply.exit_code}, reported as {code}", err=True)
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
    write_atomically(local, data)
    return 0


def _new_file_mode(path: Path) -> int:
    """An existing file keeps its mode; a new one gets ``0o666 & ~umask``, as ``scp`` and ``cp`` do."""
    try:
        return path.stat().st_mode & 0o7777
    except OSError:
        umask = os.umask(0)  # read the umask: only setting it returns it (the CLI is single-threaded)
        os.umask(umask)
        return 0o666 & ~umask


def write_atomically(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a fsynced temp file in the same directory: never a half file."""
    directory = path.parent
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".part")
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot write into {directory}: {exc.strerror}") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, _new_file_mode(path))
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
        _echo(text)
    return 0


# -- the command ---------------------------------------------------------------------------


def _parse_json_arguments(raw: str) -> dict:
    try:
        arguments = json.loads(raw)
    except ValueError as exc:
        raise click.UsageError(f"--json is not valid JSON ({exc})") from None
    if not isinstance(arguments, dict):
        raise click.UsageError("--json must be a JSON object")
    return arguments


def plan(
    tool: str, args: tuple[str, ...], timeout: int | None, cwd: str | None, json_args: str | None
) -> Callable[[Machine], int]:
    """Check the command line's shape BEFORE the config is read; returns the call to make.

    A usage error is exit 2 whatever the config holds, and nothing is called.
    """
    wait = DEFAULT_WAIT_S if timeout is None else float(timeout)
    if json_args is not None:
        if args or cwd is not None:
            raise click.UsageError("--json carries all of the tool's arguments: no positional arguments, no --cwd")
        arguments = _parse_json_arguments(json_args)
        if timeout is None:  # a call that declares its runtime (Shell timeout, Wait seconds) gets it
            wait = max(wait, declared_runtime(arguments) or 0.0)
        return lambda machine: run_tool(machine, tool, arguments, wait)
    if tool == "Shell":
        if len(args) != 1:
            raise click.UsageError("Shell takes exactly one COMMAND (quote it; `--` before one starting with -)")
        shell_timeout = SHELL_DEFAULT_TIMEOUT_S if timeout is None else timeout
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


def _check_options(machine, tool, project, list_machines, timeout, cwd, json_args) -> None:
    if project is not None and not _PROJECT_RE.fullmatch(project):
        raise click.UsageError(f"invalid project name {project!r}")
    if list_machines:
        if machine or tool or timeout is not None or cwd is not None or json_args is not None:
            raise click.UsageError("--list takes no MACHINE, TOOL, --timeout, --cwd or --json")
    elif not machine or not tool:
        raise click.UsageError("MACHINE and TOOL are required (or --list)")


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

    Exit codes: Shell = the remote exit code; 0 = success; 125 = the tool failed; 126 = no result
    (config, unknown machine, connection, auth, timeout); 141 = broken pipe; 2 = usage.
    """
    if config is not None and project is not None:
        raise click.UsageError("--config and --project are exclusive")
    _check_options(machine, tool, project, list_machines, timeout, cwd, json_args)
    call = None if list_machines else plan(tool, args, timeout, cwd, json_args)
    try:
        path = config_path(config, project)
        if call is None:
            for name in fleet_machines(load_servers(path)):
                _echo(name)
            code = 0
        else:
            code = call(resolve_machine(load_servers(path), machine, path))
        sys.stdout.flush()  # a closed pipe surfaces here, inside the handler, not at exit
    except CallFailed as failure:
        _echo(f"remoteos-call: {failure.message}", err=True)
        code = failure.code
    except BrokenPipeError:
        # the reader went away: silence the interpreter's own flush at exit, report like a shell
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError) as exc:  # no real file descriptor behind stdout
            log.debug("stdout not redirected after the broken pipe: %s", type(exc).__name__)
        code = EXIT_BROKEN_PIPE
    except Exception as exc:  # never a traceback (it could carry a request), never exit 1
        log.debug("unexpected failure: %s", type(exc).__name__)
        _echo(f"remoteos-call: unexpected {type(exc).__name__}", err=True)
        code = EXIT_NO_CALL
    sys.exit(code)


if __name__ == "__main__":
    main()
