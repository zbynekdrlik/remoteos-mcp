"""Server start flags (#31): --ssl-certfile/--ssl-keyfile, --debug and --reload.

FastMCP 4.0.3's ``run_http_async`` takes ``uvicorn_config``; the CLI passed
``uvicorn_args``, so every TLS or reload start died with a TypeError before the
port was bound. These tests start the REAL server — ``python -m remoteos`` in a
subprocess, FastMCP + uvicorn included — on a free loopback port, with an empty
config file, no ``REMOTEOS_*`` environment and a temp working directory, and
then read ``/health`` from it. The TLS certificate is a throwaway self-signed one
made with ``cryptography`` (already installed: authlib and pyjwt depend on it).
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

import pytest

from remoteos import __version__

_START_TIMEOUT = 60.0  # a cold interpreter imports fastmcp + uvicorn + PIL before it binds


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return certfile, keyfile


class _Server:
    def __init__(self, tmp_path, args: list[str]):
        self.port = _free_port()
        config = tmp_path / "remoteos.toml"
        config.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("REMOTEOS_")}
        env["PYTHONUNBUFFERED"] = "1"
        self.log_path = tmp_path / "server.log"
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "remoteos", "--port", str(self.port), "--config", str(config), *args],
            cwd=tmp_path,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def output(self) -> str:
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
