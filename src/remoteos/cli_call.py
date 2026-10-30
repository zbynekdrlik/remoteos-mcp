"""``remoteos-call``: run one MCP tool on a fleet machine from a script (#70, part b of #67).

    remoteos-call --list
    remoteos-call linux-stream-snv Shell 'sudo -n true; echo ok' [--timeout 60] [--cwd /tmp]
    remoteos-call linux-stream-snv put ./big.deb /tmp/big.deb   # any size, checked, atomic
    remoteos-call linux-stream-snv get /var/log/syslog ./syslog
    remoteos-call win-resolume-pp GetSystemInfo --json '{}'

The machine's ``url`` and ``Authorization`` header come from an MCP config: ``--config PATH``,
else ``--project NAME`` (``~/.claude/mcp-configs/NAME.mcp.json``, what ``remoteos-gateway
sync-configs`` writes), else ``./.mcp.json``. The token is read from that file only. It never
goes into an argv, an output line or a log record.

Exit codes:

- ``Shell``: the remote command's exit code (Shell's structured ``exit_code``; from an older
  server ``[Exit code: N]`` in its text, none = 0).
  A code outside 0..255 is reported as 255 (a negative POSIX signal code ``-S`` as 128+S),
  with one ``remoteos-call:`` line on stderr that names the real code.
- 0: put/get/any other tool succeeded.
- 125: the tool itself failed (the MCP result has ``isError``, or the server answered the call
  with a JSON-RPC error), or a put/get size or sha256 check failed.
- 126: the CLI got no result: a missing/broken config, an unknown machine, a refused connection,
  401/403/404/413, no reply in time, a dropped connection, a local file it cannot read/write.
  After a timeout or a dropped connection the remote command MAY have run (or still run).
- 130: interrupted (Ctrl-C); the remote command may still run.
- 141: stdout was closed (a broken pipe, e.g. ``| head``).
- 2: a usage error (click).

The CLI's own failures are always ONE stderr line starting with ``remoteos-call:``; a remote
command that itself exits 125 or 126 prints no such line.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
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
EXIT_INTERRUPTED = 130  # 128 + SIGINT, what a shell reports for Ctrl-C
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
_HASH_RE = re.compile(r"\Asha256:([0-9a-f]{64}) (\d+) bytes\Z", re.ASCII)

SHELL_DEFAULT_TIMEOUT_S = 30  # Shell's own default
DEFAULT_WAIT_S = 300.0  # put/get/--json: how long the client waits for the reply
CONNECT_TIMEOUT_S = 30.0
# One FileUpload call of a put: 2.5 MB is 3.34 MB of base64, under a machine's 4 MiB per request.
PUT_CHUNK_BYTES = 2_500_000
PUT_SUFFIX = ".part"
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
    exact: bool = False  # the streams as the command wrote them (structured), not parsed from text


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


def shell_reply(result) -> ShellReply:
    """Shell's structured fields; a server without them (older than 0.7.0.dev43) gets the parser."""
    fields = getattr(result, "structured_content", None)
    if isinstance(fields, dict):
        stdout, stderr, code = fields.get("stdout"), fields.get("stderr"), fields.get("exit_code")
        if isinstance(stdout, str) and isinstance(stderr, str) and type(code) is int:
            return ShellReply(stdout, stderr, code, exact=True)
    return parse_shell_reply(result_text(result))


def process_exit_code(remote: int) -> int:
    """The remote exit code as a process exit code (0..255)."""
    if 0 <= remote <= 255:
        return remote
    if -127 <= remote < 0:  # POSIX Popen: killed by signal -remote, the shell's 128+S convention
        return 128 - remote
    return 255  # a Windows 32-bit code (4294967295 = exit -1) or anything else out of range


def result_text(result) -> str:
    """The text blocks of a ``CallToolResult``, joined; other blocks are named, never dumped.

    The task manager puts ``[task:<id>] `` in front of the FIRST text block, which is not always
    the first block (Snapshot: the image comes first), and adds a text block holding only the
    prefix when there is none (taskmanager.wrap_sync_tool). That prefix is removed here, once,
    and a block left empty by it is dropped.
    """
    parts = []
    prefix_seen = False
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            if not prefix_seen:
                prefix_seen = True
                text = strip_task_prefix(text)
                if not text:
                    continue
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
    413: "request too large (413): a machine takes at most 4 MiB per request",
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
    from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

    leaf = _innermost(exc)
    if isinstance(leaf, httpx2.HTTPStatusError):
        status = leaf.response.status_code
    if status is not None:
        reason = _HTTP_REASONS.get(status, f"HTTP {status}")
    elif isinstance(leaf, (httpx2.TimeoutException, TimeoutError)) or (
        isinstance(leaf, MCPError) and leaf.code == REQUEST_TIMEOUT
    ):
        reason = "timed out waiting for the reply (the command may still run)"
    elif isinstance(leaf, MCPError) and leaf.code == CONNECTION_CLOSED:
        # a dropped stream, the gateway's upstream read timeout, a reply over MAX_REPLY_EVENT_BYTES
        reason = f"connection closed before the reply (the command may have run): {leaf.message}"
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

    ``mcp.client.streamable_http`` reads a POST reply with its module-level name ``EventSource``:
    mcp 2.2 as ``EventSource(response)`` (httpx2's 1 MiB default, no option), mcp 2.3 as
    ``EventSource(response, max_event_size=self.max_sse_event_size)`` (1 MiB again: fastmcp 4.1.2
    never passes ``max_sse_event_size`` to ``streamable_http_client``). The CLI swaps that name for
    a subclass that always uses ``MAX_REPLY_EVENT_BYTES``, which covers both. Idempotent. An SDK
    without that name fails loudly here; one that stops using it fails a big reply with the SDK's
    own "exceeded the 1048576 byte limit" line (and ``test_cli_call``'s large-reply tests).
    """
    import httpx2
    from mcp.client import streamable_http

    current = getattr(streamable_http, "EventSource", None)
    if current is None or not (isinstance(current, type) and issubclass(current, httpx2.EventSource)):
        raise CallFailed(
            EXIT_NO_CALL,
            "this mcp SDK is incompatible: mcp.client.streamable_http no longer uses httpx2.EventSource "
            "(replies over 1 MiB could not be received)",
        )
    if current is not httpx2.EventSource:
        return  # already swapped

    class LargeEventSource(httpx2.EventSource):
        def __init__(self, response, max_event_size: int | None = None) -> None:  # the SDK's value is ignored
            super().__init__(response, max_event_size=MAX_REPLY_EVENT_BYTES)

    streamable_http.EventSource = LargeEventSource


def _quiet_library_logging() -> None:
    """Silence the libraries' own loggers (fastmcp configures its stderr handler at import)."""
    import fastmcp  # noqa: F401 - imported first: its import sets the fastmcp logger's level

    for name in _LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)


def prepare_process() -> None:
    """The CLI's two process-wide settings, made once by ``main`` before a call (never by a
    library function, so an in-process caller of ``call_tool`` keeps its own logging and SDK)."""
    _quiet_library_logging()
    _allow_large_replies()


async def call_tool(machine: Machine, tool: str, arguments: dict, wait_s: float):
    """One ``tools/call`` on *machine*; returns fastmcp's ``CallToolResult`` (``is_error`` set, not raised)."""
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

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
    return CallFailed(EXIT_TOOL_ERROR, result_text(result))


def _echo(text: str, err: bool = False, nl: bool = True) -> None:
    """Print remote text unchanged: ``color=True`` keeps ANSI escapes click strips off a pipe."""
    click.echo(text, err=err, color=True, nl=nl)


def run_shell(machine: Machine, command: str, timeout: int, cwd: str | None) -> int:
    arguments: dict = {"command": command, "timeout": timeout}
    if cwd:
        arguments["cwd"] = cwd
    # the client waits as long as the server's own stale-task sweep allows the call
    result = _run(machine, "Shell", arguments, declared_runtime(arguments))
    if result.is_error:
        raise _tool_failed(result)
    reply = shell_reply(result)
    if reply.stdout:
        _echo(reply.stdout, nl=not reply.exact)
    if reply.stderr:
        _echo(reply.stderr, err=True, nl=not reply.exact)
    code = process_exit_code(reply.exit_code)
    if code != reply.exit_code:
        _echo(f"remoteos-call: remote exit code {reply.exit_code}, reported as {code}", err=True)
    return code


def _upload_chunk(machine: Machine, path: str, chunk: bytes, append: bool, wait_s: float) -> None:
    arguments = {"path": path, "data_base64": base64.b64encode(chunk).decode(), "append": append}
    result = _run(machine, "FileUpload", arguments, wait_s)
    if result.is_error:
        if "unexpected_keyword_argument" in result_text(result):  # refused before anything was written
            raise CallFailed(EXIT_TOOL_ERROR, f"put: {machine.name} runs a server older than 0.7.0.dev43: upgrade it")
        raise _tool_failed(result)
    reply = result_text(result)
    match = _UPLOAD_RE.match(reply)
    if not match:
        raise CallFailed(EXIT_TOOL_ERROR, f"put: unexpected FileUpload reply: {reply[:200]}")
    written = int(match.group(1))
    if written != len(chunk):
        raise CallFailed(EXIT_TOOL_ERROR, f"put: size mismatch, sent {len(chunk)} bytes, remote wrote {written}")


def _upload(machine: Machine, local: Path, part: str, wait_s: float) -> tuple[str, int]:
    """Send *local* to *part* in chunks; returns the sha256 and size of what was sent."""
    digest = hashlib.sha256()
    size = 0
    append = False  # the first chunk replaces a leftover .part, and an empty file still gets one
    try:
        with local.open("rb") as source:
            while (chunk := source.read(PUT_CHUNK_BYTES)) or not append:
                _upload_chunk(machine, part, chunk, append, wait_s)
                digest.update(chunk)
                size += len(chunk)
                append = True
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot read {local}: {exc.strerror}") from None
    return digest.hexdigest(), size


def run_put(machine: Machine, local: Path, remote: str, wait_s: float) -> int:
    """Upload to ``<remote>.part``, check its size and sha256, then rename it over *remote*.

    Any failure leaves *remote* as it was. A leftover ``.part`` is replaced by the next put.
    """
    part = remote + PUT_SUFFIX
    sent_sha256, sent_size = _upload(machine, local, part, wait_s)
    result = _run(machine, "FileHash", {"path": part}, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    reply = result_text(result)
    match = _HASH_RE.match(reply)
    if not match:
        raise CallFailed(EXIT_TOOL_ERROR, f"put: unexpected FileHash reply: {reply[:200]}")
    if int(match.group(2)) != sent_size:
        raise CallFailed(EXIT_TOOL_ERROR, f"put: size mismatch, sent {sent_size} bytes, {part} has {match.group(2)}")
    if match.group(1) != sent_sha256:
        raise CallFailed(EXIT_TOOL_ERROR, f"put: sha256 mismatch on {part}, {remote} is untouched")
    result = _run(machine, "FileRename", {"path": part, "new_path": remote}, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    return 0


def run_get(machine: Machine, remote: str, local: Path, wait_s: float) -> int:
    result = _run(machine, "FileDownload", {"path": remote}, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    reply = result_text(result)
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


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable. A directory that cannot be opened or synced is logged, not fatal:
    the file itself is complete and in place."""
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        log.debug("directory fsync skipped: %s", type(exc).__name__)


def write_atomically(path: Path, data: bytes) -> None:
    """Write *data* to *path* like ``cp`` would, but never as a half-written file.

    A symlink is written through (its target is replaced), but only after the KERNEL followed it
    (``os.stat``), so ``fs.protected_symlinks`` still refuses a link another user planted in a
    sticky directory, and a dangling link is refused like GNU ``cp`` does. An existing file keeps
    its mode; a new one is created ``0o666`` minus the umask (the kernel applies it). The data goes
    to a fsynced temp file in the same directory, renamed over the target, and the directory is
    fsynced.
    """
    try:
        mode = os.stat(path).st_mode & 0o7777  # follows a symlink in the kernel, with its checks
    except FileNotFoundError:
        if path.is_symlink():
            raise CallFailed(EXIT_NO_CALL, f"not writing through dangling symlink {path}") from None
        mode = None
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot write {path}: {exc.strerror}") from None
    target = Path(os.path.realpath(path))
    # a long name stays under NAME_MAX (255) with the temp file's 18 extra characters
    tmp = target.parent / f".{target.name[:200]}.{secrets.token_hex(6)}.part"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    except OSError as exc:
        raise CallFailed(EXIT_NO_CALL, f"cannot write into {target.parent}: {exc.strerror}") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise CallFailed(EXIT_NO_CALL, f"cannot write {path}: {exc.strerror}") from None
    _fsync_directory(target.parent)


def run_tool(machine: Machine, tool: str, arguments: dict, wait_s: float) -> int:
    result = _run(machine, tool, arguments, wait_s)
    if result.is_error:
        raise _tool_failed(result)
    text = result_text(result)
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
    if cwd == "":
        raise click.UsageError("--cwd must not be empty")
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
    (config, unknown machine, connection, auth, timeout); 130 = interrupted; 141 = broken pipe;
    2 = usage.
    """
    if config is not None and project is not None:
        raise click.UsageError("--config and --project are exclusive")
    _check_options(machine, tool, project, list_machines, timeout, cwd, json_args)
    call = None if list_machines else plan(tool, args, timeout, cwd, json_args)
    target = None  # the resolved machine: its token is scrubbed from any unexpected message
    try:
        path = config_path(config, project)
        if call is None:
            for name in fleet_machines(load_servers(path)):
                _echo(name)
            code = 0
        else:
            target = resolve_machine(load_servers(path), machine, path)
            prepare_process()
            code = call(target)
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
    except KeyboardInterrupt:  # click would print "Aborted!" and exit 1, a remote exit 1's code
        _echo("remoteos-call: interrupted (the remote command may still run)", err=True)
        code = EXIT_INTERRUPTED
    except Exception as exc:  # never a traceback (it could carry a request), never exit 1
        log.debug("unexpected failure: %s", type(exc).__name__)
        _echo(f"remoteos-call: unexpected {_unexpected_text(exc, target)}", err=True)
        code = EXIT_NO_CALL
    sys.exit(code)


def _unexpected_text(exc: BaseException, machine: Machine | None) -> str:
    """``<Type>: <first line of the message>``, the token scrubbed: a bug report a user can act on."""
    lines = str(exc).splitlines()
    text = f"{type(exc).__name__}: {lines[0]}" if lines and lines[0] else type(exc).__name__
    return _scrub(text, machine) if machine is not None else text


if __name__ == "__main__":
    main()
