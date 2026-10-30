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
import platform
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fleet_harness import free_port, hermetic_child_env

from remoteos import __version__

_START_TIMEOUT = 60.0  # a cold interpreter imports fastmcp + uvicorn + PIL before it binds


def _runner_os() -> str:
    """The OS name the server must announce on this Linux runner (#59), read from the OS, never via identity."""
    return platform.freedesktop_os_release()["PRETTY_NAME"]


def _runner_admin() -> bool:
    """Read the way ``identity`` must: root, or sudo that asks no password."""
    if os.geteuid() == 0:
        return True
    return subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL, capture_output=True).returncode == 0


def _health() -> dict:
    """``/health`` on this runner: status + version (the installers' contract), #59's os + hostname, #57's admin."""
    return {
        "status": "ok",
        "version": __version__,
        "os": _runner_os(),
        "hostname": socket.gethostname(),
        "admin": _runner_admin(),
    }


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
    def __init__(self, tmp_path, args: list[str], env: dict[str, str] | None = None):
        self.port = free_port()
        self.log_path = tmp_path / "server.log"
        self._log = open(self.log_path, "wb")
        config = _empty_config(tmp_path)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "remoteos", "--port", str(self.port), "--config", str(config), *args],
            cwd=tmp_path,
            env={**hermetic_child_env(tmp_path), **(env or {})},
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

    def call_tool(self, name: str, arguments: dict, key: str | None = None) -> str:
        """One stateless MCP ``tools/call`` over streamable HTTP; returns the raw (SSE) response body."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        ).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def initialize(self) -> dict:
        """One MCP ``initialize`` over streamable HTTP; returns its JSON-RPC ``result``."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        ).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = resp.read().decode("utf-8", "replace")
        # an SSE stream ("event: message\ndata: {...}") or a plain JSON body
        payloads = [line[len("data:") :].strip() for line in reply.splitlines() if line.startswith("data:")]
        return json.loads(payloads[-1] if payloads else reply)["result"]

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

    def _start(*args: str, env: dict[str, str] | None = None) -> _Server:
        srv = _Server(tmp_path, list(args), env)
        servers.append(srv)
        return srv

    yield _start
    for srv in servers:
        srv.stop()


def test_ssl_flags_start_the_server_on_https(tmp_path, start_server):
    certfile, keyfile = _self_signed_cert(tmp_path)
    srv = start_server("--ssl-certfile", str(certfile), "--ssl-keyfile", str(keyfile))
    trust_our_cert = ssl.create_default_context(cafile=str(certfile))
    assert srv.health("https", trust_our_cert) == _health()
    assert "TypeError" not in srv.output()


def test_debug_flag_starts_the_server_with_debug_logging(start_server):
    srv = start_server("--debug")
    assert srv.health() == _health()
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


def test_the_auth_key_from_the_environment_never_reaches_a_tool_s_child(start_server):
    # The installers hand the key over in REMOTEOS_AUTH_KEY (#12), never an argv. The server
    # must not pass it on: every Shell/App child would inherit it.
    key = secrets.token_hex(16)
    srv = start_server("--enable-all", env={"REMOTEOS_AUTH_KEY": key})
    srv.health()
    reply = srv.call_tool("Shell", {"command": "echo marker-$((40+2)); printenv REMOTEOS_AUTH_KEY; true"}, key=key)
    assert "marker-42" in reply, reply  # authenticated, and the call really ran
    assert key not in reply, "a Shell child inherited the auth key"
    with pytest.raises(urllib.error.HTTPError) as refused:
        srv.call_tool("Shell", {"command": "true"})
    assert refused.value.code == 401  # the key is still enforced


def test_initialize_and_health_name_this_machine_its_os_and_its_shell(start_server):
    # #59: a box's MCP name went stale (win-stream-snv runs Ubuntu now) and clients sent it
    # PowerShell. The real server tells every client at initialize, before any tool call.
    srv = start_server()
    health = srv.health()
    assert health == _health()  # status + version unchanged: the installers read version
    instructions = srv.initialize()["instructions"]
    assert instructions.startswith(
        f"Remote OS MCP Server on {socket.gethostname()} — {_runner_os()}, {platform.machine()}. "
        "The Shell tool runs /bin/bash -c. "
    ), instructions
    assert "for Windows, macOS, and Linux machines" not in instructions


def test_reload_flag_warns_instead_of_crashing(start_server):
    srv = start_server("--reload")
    assert srv.health() == _health()
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
        env=hermetic_child_env(tmp_path),
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


def _exits_without_serving(srv) -> None:
    """The server process ends before ``/health`` ever answers (a refused start, never a served one)."""
    deadline = time.monotonic() + _START_TIMEOUT
    while srv.proc.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/health", timeout=2):
                pytest.fail(f"the server started:\n{srv.output()[-2000:]}")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.2)


@pytest.mark.parametrize("tier3", [["--enable-all"], ["--enable-tier3"], ["--tools", "Shell"]])
def test_an_unauthenticated_lan_bind_with_tier3_refuses_to_start(start_server, tier3):
    # #39 (upstream PR #76's rule): an open full shell on the LAN is never started
    srv = start_server("--host", "0.0.0.0", *tier3)
    _exits_without_serving(srv)
    assert srv.proc.returncode == 2, srv.output()[-2000:]
    assert "refusing to bind 0.0.0.0 without an auth key while Tier 3" in srv.output()


def test_a_keyed_lan_bind_with_tier3_starts(start_server):
    # every installer starts `--enable-all` with a key on a LAN address
    key = secrets.token_hex(16)
    srv = start_server("--host", "0.0.0.0", "--enable-all", env={"REMOTEOS_AUTH_KEY": key})
    assert srv.health() == _health()


# Runs the real CLI start-up and, in place of serving (FastMCP's mcp.run, the transport), prints the
# Scrape policy the start-up configured. Scrape itself is excluded on Linux (tiers), so the policy
# is read where the tool reads it.
_SCRAPE_POLICY_PROBE = """
import sys
import remoteos.__main__ as server
from remoteos import scrape
scrape.BLOCK_PRIVATE = True  # not the default: the False cases then prove the start-up configured it
server.mcp.run = lambda **kw: print("BLOCK_PRIVATE=" + str(scrape.BLOCK_PRIVATE), flush=True)
sys.argv = ["remoteos", *sys.argv[1:]]
server.cli()
"""


@pytest.mark.parametrize(
    ("how", "expected"), [("default", "False"), ("flag", "True"), ("toml", "True"), ("toml-false", "False")]
)
def test_scrape_block_private_is_configured_at_start_up(tmp_path, how, expected):
    # review (#39): the flag and the remoteos.toml key reach the Scrape policy; the default allows the LAN
    config = tmp_path / "remoteos.toml"
    config.write_text(
        {
            "toml": "[security]\nscrape_block_private = true\n",
            "toml-false": '[security]\nscrape_block_private = "no"\n',
        }.get(how, "")
    )
    args = ["--config", str(config), *(["--scrape-block-private"] if how == "flag" else [])]
    done = subprocess.run(
        [sys.executable, "-c", _SCRAPE_POLICY_PROBE, *args],
        cwd=tmp_path,
        env=hermetic_child_env(tmp_path),
        capture_output=True,
        timeout=_START_TIMEOUT,
    )
    out = done.stdout.decode("utf-8", "replace")
    assert f"BLOCK_PRIVATE={expected}" in out, (out + done.stderr.decode("utf-8", "replace"))[-2000:]


def test_oauth_without_a_secret_warns_at_start_up(start_server):
    # review round 2 (#39): authorize approves every client, so a token is free for anyone
    srv = start_server("--oauth-client-id", "c1")
    srv.health()
    assert "WARNING: OAuth is on without --oauth-client-secret" in srv.output()


def test_oauth_with_a_secret_starts_without_that_warning(start_server):
    srv = start_server("--oauth-client-id", "c1", env={"REMOTEOS_OAUTH_CLIENT_SECRET": "s-" + secrets.token_hex(8)})
    srv.health()
    assert "OAuth is on without" not in srv.output()


def test_an_unauthenticated_lan_bind_with_tier_1_2_only_starts_with_the_warning(start_server):
    srv = start_server("--host", "0.0.0.0")
    assert srv.health() == _health()
    assert "open to the network without auth" in srv.output()
