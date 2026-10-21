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
