"""Real loopback stand-ins for fleet machines and the gateway (#51).

``FakeFleetMachine`` is a REAL HTTP server on 127.0.0.1 (a ``ThreadingHTTPServer`` in a
daemon thread), so every client under test (the gateway's production pool, ``machine
set-key``'s probe, ``machine import``, ``check``) talks to it over a real socket, from any
event loop or thread. It answers like a remoteos machine: ``POST /mcp`` with an accepted
``Authorization: Bearer <key>`` gets 200, anything else 401. ``GET /healthz`` answers like
the gateway's own health endpoint.

Every key and token in these tests is generated per run (``secrets.token_hex``) and every
file lives under ``tmp_path``: the real ``~/.config/remoteos-gateway`` and
``~/.claude/mcp-configs`` are never touched.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from starlette.responses import JSONResponse

SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")  # this checkout's src/, not whatever is installed


def free_port() -> int:
    """A loopback port that was free a moment ago (for a real server started in a subprocess)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hermetic_child_env(home: Path) -> dict[str, str]:
    """The environment of a real server started as a child: no ``REMOTEOS_*`` / ``FASTMCP_*`` from
    the runner, *home* as HOME and XDG_DATA_HOME's parent, FastMCP's PyPI update check and banner
    off, and this checkout's ``src`` first on PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMOTEOS_", "FASTMCP_"))}
    env.update(
        PYTHONUNBUFFERED="1",
        PYTHONPATH=os.pathsep.join(p for p in (SRC_DIR, env.get("PYTHONPATH", "")) if p),
        HOME=str(home),
        XDG_DATA_HOME=str(home / "xdg-data"),
        FASTMCP_CHECK_FOR_UPDATES="off",  # no call to pypi.org, no version cache written
        FASTMCP_SHOW_SERVER_BANNER="false",
    )
    return env


def fresh_secret(prefix: str) -> str:
    """A throwaway fixture key/token: letters and digits only, like the installers' keys."""
    return prefix + secrets.token_hex(16)


class FakeFleetMachine:
    """A remoteos machine (or, with ``healthz``, the gateway) on a real loopback port."""

    def __init__(
        self,
        accept_keys: tuple[str, ...] = (),
        *,
        status: int | None = None,
        healthz: bool = False,
        version: str | None = None,
        admin: bool | None = None,
        releases: list | None = None,
    ):
        self.accept_keys = list(accept_keys)
        self.status = status  # a fixed answer (e.g. 500) instead of the key check
        self.healthz = healthz
        self.version = version  # answers GET /health like a machine; None = 404
        self.admin = admin  # /health's admin key (#57); None = a server that has none
        self.releases = releases  # answers GET /releases like GitHub; None = 404
        self.seen_health_authorization: list[str | None] = []
        self.seen_authorization: list[str | None] = []
        self.seen_methods: list[str | None] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> FakeFleetMachine:
        machine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                return  # keep the test output clean

            def _answer(self, code: int, payload: dict | list) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - stdlib naming
                if self.path == "/healthz" and machine.healthz:
                    self._answer(200, {"status": "ok", "version": "0.0.0-test"})
                elif self.path == "/health" and machine.version is not None:
                    machine.seen_health_authorization.append(self.headers.get("authorization"))
                    extra = {} if machine.admin is None else {"admin": machine.admin}
                    self._answer(200, {"status": "ok", "version": machine.version, **extra})
                elif self.path == "/releases" and machine.releases is not None:
                    self._answer(200, machine.releases)
                else:
                    self._answer(404, {"error": "Not Found"})

            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                message = json.loads(raw) if raw else {}
                machine.seen_authorization.append(self.headers.get("authorization"))
                machine.seen_methods.append(message.get("method"))
                if self.path != "/mcp":
                    self._answer(404, {"error": "Not Found"})
                    return
                if machine.status is not None:
                    self._answer(machine.status, {"error": "fixed answer"})
                    return
                accepted = {f"Bearer {key}" for key in machine.accept_keys}
                if self.headers.get("authorization") not in accepted:
                    self._answer(401, {"error": "Unauthorized"})
                    return
                self._answer(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "remoteos-test"}},
                    },
                )

        class Server(ThreadingHTTPServer):
            request_queue_size = 256  # a whole fake fleet may probe one box at once

        self._server = Server(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()


def closed_port_url() -> str:
    """An http url on 127.0.0.1 where nothing listens: connect is refused at once."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


def write_private_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# -- fake ssh / sudo (the OS boundary of the other-host / other-account writers) -----------
#
# Each fake logs its argv as one JSON line to $FAKE_REMOTE_LOG and then runs the command
# LOCALLY with stdin passed through, as the real tool would on the far side. Set
# FAKE_SSH_DENY / FAKE_SUDO_DENY to make it fail the way a missing key login / sudoers rule does.

_FAKE_SSH = '''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_REMOTE_LOG"], "a") as log:
    log.write(json.dumps({{"tool": "ssh", "argv": args}}) + "\\n")
if os.environ.get("FAKE_SSH_DENY"):
    sys.stderr.write("newlevel@dev2: Permission denied (publickey).\\n")
    sys.exit(255)
i = 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-p", "-i", "-l") else 1
# ssh hands everything after the destination to the remote shell as ONE command line
sys.exit(subprocess.run(["/bin/sh", "-c", " ".join(args[i + 1:])]).returncode)
'''

_FAKE_SUDO = '''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_REMOTE_LOG"], "a") as log:
    log.write(json.dumps({{"tool": "sudo", "argv": args}}) + "\\n")
if os.environ.get("FAKE_SUDO_DENY"):
    sys.stderr.write("sudo: a password is required\\n")
    sys.exit(1)
assert args[:2] == ["-n", "-u"] and args[3] == "--", args
sys.exit(subprocess.run(args[4:]).returncode)
'''


def install_fake_remote_tools(bin_dir: Path, monkeypatch) -> Path:
    """Write fake ``ssh`` and ``sudo`` into *bin_dir* and point the gateway at them.

    Returns the JSON-lines log path (one ``{"tool", "argv"}`` record per call).
    """
    import sys

    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "remote-calls.jsonl"
    for name, template in (("ssh", _FAKE_SSH), ("sudo", _FAKE_SUDO)):
        path = bin_dir / name
        path.write_text(template.format(python=sys.executable), encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("REMOTEOS_GATEWAY_SSH", str(bin_dir / "ssh"))
    monkeypatch.setenv("REMOTEOS_GATEWAY_SUDO", str(bin_dir / "sudo"))
    monkeypatch.setenv("FAKE_REMOTE_LOG", str(log))
    return log


_FAKE_AIRULESET = '''#!{python}
import json, os, sys
with open(os.environ["FAKE_NOTIFY_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
# like the real `notify --body`: it PRINTS send()'s status and exits 0 even when the post failed
print(os.environ.get("FAKE_NOTIFY_STATUS", "sent"))
sys.exit(int(os.environ.get("FAKE_NOTIFY_EXIT", "0")))
'''


def install_fake_airuleset(directory: Path, monkeypatch) -> tuple[Path, Path]:
    """A stand-in ``airuleset.py`` whose ``notify`` logs its argv (JSON line) to the returned log.

    Like the real one it prints the send status (``FAKE_NOTIFY_STATUS``, default ``sent``; the
    real ``send()`` returns sent / dedup / dry-run / no-config / error) and exits 0 regardless;
    ``FAKE_NOTIFY_EXIT`` makes the script itself fail (it did not run).
    """
    import sys

    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "airuleset.py"
    script.write_text(_FAKE_AIRULESET.format(python=sys.executable), encoding="utf-8")
    log = directory / "notify-calls.jsonl"
    monkeypatch.setenv("FAKE_NOTIFY_LOG", str(log))
    return script, log


def notify_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def remote_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- real servers in this process: a machine and the gateway, both on real sockets -----------


class LoopbackServer:
    """A uvicorn server for *app* on a free 127.0.0.1 port, in a daemon thread. In this process, so
    caplog sees its log lines, and ``stop()`` can drop it mid-call like a box that dies."""

    def __init__(self, app) -> None:
        self.port = free_port()
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("the loopback server did not start")
            time.sleep(0.01)

    def stop(self, *, force: bool = False) -> None:
        self._server.force_exit = force
        self._server.should_exit = True
        self._thread.join(timeout=15)

    def __enter__(self) -> LoopbackServer:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop(force=True)


PIXEL_PNG = base64.b64decode(  # a real 1x1 PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class RealMachine(LoopbackServer):
    """A real FastMCP server behind the machines' bearer check, with the shapes a box returns:
    text, image + text, a tool failure, a box that dies mid-answer. ``/health`` answers like remoteos."""

    def __init__(self, key: str, *, os_name: str = "Linux test", version: str = "9.9.9", health: bool = True) -> None:
        server = FastMCP("fake-box")
        self.received: list[dict] = []
        self.key = key
        self.die_at_tool_answer = False  # drop the connection after the headers of a tools/call

        @server.tool(name="Echo")
        def echo(text: str) -> str:
            """Echo the text."""
            self.received.append({"text": text})
            return f"echo: {text}"

        @server.tool(name="Snap")
        def snap() -> list:
            """Text and an image."""
            return ["a caption", Image(data=PIXEL_PNG, format="png")]

        @server.tool(name="Boom")
        def boom() -> str:
            """Always fails."""
            raise ToolError("box says no")

        if health:

            @server.custom_route("/health", methods=["GET"])
            async def health_route(request):
                return JSONResponse({"status": "ok", "version": version, "os": os_name, "hostname": "fakebox"})

        app = server.http_app(path="/mcp", stateless_http=True)
        machine = self

        async def checked(scope, receive, send):
            headers = dict(scope.get("headers") or [])
            if (
                scope["type"] == "http"
                and scope["path"] == "/mcp"
                and headers.get(b"authorization") != f"Bearer {machine.key}".encode()
            ):
                await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                return

            async def dying_send(message):
                # uvicorn drops the connection when the app raises after the headers went out
                if message["type"] == "http.response.body" and b'"result"' in message.get("body", b""):
                    raise ConnectionAbortedError("the box died")
                await send(message)

            await app(scope, receive, dying_send if machine.die_at_tool_answer else send)

        super().__init__(checked)

    @property
    def url(self) -> str:
        return f"{self.base_url}/mcp"
