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

import json
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def fresh_secret(prefix: str) -> str:
    """A throwaway fixture key/token: letters and digits only, like the installers' keys."""
    return prefix + secrets.token_hex(16)


class FakeFleetMachine:
    """A remoteos machine (or, with ``healthz``, the gateway) on a real loopback port."""

    def __init__(self, accept_keys: tuple[str, ...] = (), *, status: int | None = None, healthz: bool = False):
        self.accept_keys = list(accept_keys)
        self.status = status  # a fixed answer (e.g. 500) instead of the key check
        self.healthz = healthz
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

            def _answer(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - stdlib naming
                if self.path == "/healthz" and machine.healthz:
                    self._answer(200, {"status": "ok", "version": "0.0.0-test"})
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

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
