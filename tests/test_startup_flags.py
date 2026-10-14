"""Server start flags (#31): --ssl-certfile/--ssl-keyfile, --debug and --reload.

FastMCP 4.0.3's ``run_http_async`` takes ``uvicorn_config``; the CLI passed
``uvicorn_args``, so every TLS or reload start died with a TypeError before the
port was bound. These tests start the REAL server — ``python -m remoteos`` in a
subprocess, FastMCP + uvicorn included — on a free loopback port and talk to it
over HTTP or stdio. The child is hermetic: an empty config file, no ``REMOTEOS_*``
/ ``FASTMCP_*`` variables from the runner, a temp HOME / XDG_DATA_HOME / working
directory, FastMCP's PyPI update check and banner off, and this checkout's
``src`` first on PYTHONPATH. The TLS certificate is a throwaway self-signed one
made with ``cryptography`` (a ``test`` extra).
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import remoteos
from remoteos import __version__

_START_TIMEOUT = 60.0  # a cold interpreter imports fastmcp + uvicorn + PIL before it binds
_SRC = str(Path(remoteos.__file__).resolve().parents[1])  # this checkout's src/, not whatever is installed


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _child_env(tmp_path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMOTEOS_", "FASTMCP_"))}
    env.update(
        PYTHONUNBUFFERED="1",
        PYTHONPATH=os.pathsep.join(p for p in (_SRC, env.get("PYTHONPATH", "")) if p),
        HOME=str(tmp_path),
        XDG_DATA_HOME=str(tmp_path / "xdg-data"),
        FASTMCP_CHECK_FOR_UPDATES="off",  # no call to pypi.org, no version cache written
        FASTMCP_SHOW_SERVER_BANNER="false",
    )
    return env


def _empty_config(tmp_path) -> Path:
    config = tmp_path / "remoteos.toml"
    config.write_text("")
    return config


def _self_signed_cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return certfile, keyfile


class _Server:
    def __init__(self, tmp_path, args: list[str]):
        self.port = _free_port()
        self.log_path = tmp_path / "server.log"
        self._log = open(self.log_path, "wb")
        config = _empty_config(tmp_path)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "remoteos", "--port", str(self.port), "--config", str(config), *args],
            cwd=tmp_path,
            env=_child_env(tmp_path),
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def output(self) -> str:
        if not self._log.closed:
            self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def health(self, scheme: str = "http", context: ssl.SSLContext | None = None) -> dict:
        """Poll ``/health`` until it answers; fail with the server log if the process dies first."""
        url = f"{scheme}://127.0.0.1:{self.port}/health"
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"server exited with {self.proc.returncode} before answering:\n{self.output()[-3000:]}")
            try:
                with urllib.request.urlopen(url, context=context, timeout=2) as resp:
                    return json.loads(resp.read())
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        pytest.fail(f"server did not answer {url} within {_START_TIMEOUT}s:\n{self.output()[-3000:]}")

    def call_tool(self, name: str, arguments: dict) -> str:
        """One stateless MCP ``tools/call`` over streamable HTTP; returns the raw (SSE) response body."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/mcp",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        self._log.close()


@pytest.fixture
def start_server(tmp_path):
    servers: list[_Server] = []

    def _start(*args: str) -> _Server:
        srv = _Server(tmp_path, list(args))
        servers.append(srv)
        return srv

    yield _start
    for srv in servers:
        srv.stop()


def test_ssl_flags_start_the_server_on_https(tmp_path, start_server):
    certfile, keyfile = _self_signed_cert(tmp_path)
    srv = start_server("--ssl-certfile", str(certfile), "--ssl-keyfile", str(keyfile))
    trust_our_cert = ssl.create_default_context(cafile=str(certfile))
    assert srv.health("https", trust_our_cert) == {"status": "ok", "version": __version__}
    assert "TypeError" not in srv.output()


def test_debug_flag_starts_the_server_with_debug_logging(start_server):
    srv = start_server("--debug")
    assert srv.health() == {"status": "ok", "version": __version__}
    out = srv.output()
    assert "DEBUG remoteos.mcp: debug logging on" in out
    assert "TypeError" not in out


def test_debug_log_never_carries_tool_arguments_or_replies(start_server):
    # Tool arguments and replies are typed text, clipboard, Shell commands, FileUpload
    # data: --debug must not put them in the log (#31 review).
    marker = "secret-marker-7f3a9c"
    srv = start_server("--debug")
    srv.health()
    reply = srv.call_tool("FileList", {"path": f"/nonexistent-{marker}"})
    assert f"Not a directory: /nonexistent-{marker}" in reply  # the call really ran
    srv.stop()
    out = srv.output()
    assert "DEBUG remoteos.mcp: debug logging on" in out
    assert marker not in out


def test_reload_flag_warns_instead_of_crashing(start_server):
    srv = start_server("--reload")
    assert srv.health() == {"status": "ok", "version": __version__}
    out = srv.output()
    assert "--reload has no effect" in out
    assert "TypeError" not in out


@pytest.mark.parametrize("given", ["--ssl-certfile", "--ssl-keyfile"])
def test_one_ssl_flag_alone_is_a_usage_error_never_a_plain_http_start(tmp_path, start_server, given):
    certfile, keyfile = _self_signed_cert(tmp_path)
    srv = start_server(given, str(certfile if given == "--ssl-certfile" else keyfile))
    deadline = time.monotonic() + _START_TIMEOUT
    while srv.proc.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/health", timeout=2):
                pytest.fail(f"{given} alone silently started a plain-HTTP server:\n{srv.output()[-2000:]}")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.2)
    assert srv.proc.returncode == 2, srv.output()[-2000:]
    assert "--ssl-certfile and --ssl-keyfile must be given together" in srv.output()


def test_stdio_stdout_carries_only_json_rpc(tmp_path):
    # In stdio mode stdout IS the JSON-RPC stream: a status print there corrupts it (#31 review).
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    done = subprocess.run(
        [sys.executable, "-m", "remoteos", "--transport", "stdio", "--config", str(_empty_config(tmp_path))],
        input=json.dumps(initialize).encode() + b"\n",
        cwd=tmp_path,
        env=_child_env(tmp_path),
        capture_output=True,
        timeout=_START_TIMEOUT,
    )
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    assert lines, done.stderr.decode("utf-8", "replace")[-2000:]
    for line in lines:
        try:
            json.loads(line)
        except ValueError:
            pytest.fail(f"non-JSON line on the stdio stdout: {line!r}")
    assert json.loads(lines[-1])["id"] == 1
