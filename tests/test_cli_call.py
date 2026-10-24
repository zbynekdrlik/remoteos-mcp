"""``remoteos-call`` (#70): a script's MCP client, run against a REAL server and a REAL gateway.

The stack is real end to end, nothing faked: ``python -m remoteos --enable-all`` (FastMCP +
uvicorn, keyed through ``REMOTEOS_AUTH_KEY`` like the installers) on a free loopback port, and
``remoteos-gateway serve`` in front of it, with a registry that lets one consumer token reach the
machine. The CLI reads a project ``.mcp.json`` like the one ``sync-configs`` writes and talks to
the gateway with fastmcp's streamable-HTTP Client. Every key and token is generated per run and
every file lives under a pytest temp dir: no real config, gateway or fleet machine is touched.

Most calls go through click's ``CliRunner`` (in-process: the log records are visible to the
token check); the console script itself is also run as a real process.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from fleet_harness import closed_port_url, fresh_secret

import remoteos
from remoteos import cli_call
from remoteos.cli_call import main
from remoteos.gateway.registry import token_sha256

_SRC = str(Path(remoteos.__file__).resolve().parents[1])
_START_TIMEOUT = 60.0
MACHINE = "linux-cli-test"
OTHER_MACHINE = "linux-cli-other"  # exposed, but the consumer may not reach it
CONSUMER = "cli-test@dev1"


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _child_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMOTEOS_", "FASTMCP_"))}
    env.update(
        PYTHONUNBUFFERED="1",
        PYTHONPATH=os.pathsep.join(p for p in (_SRC, env.get("PYTHONPATH", "")) if p),
        HOME=str(home),
        XDG_DATA_HOME=str(home / "xdg-data"),
        FASTMCP_CHECK_FOR_UPDATES="off",
        FASTMCP_SHOW_SERVER_BANNER="false",
    )
    return env


class _Process:
    """A child process whose combined output goes to a log file; stopped at teardown."""

    def __init__(self, argv: list[str], home: Path, log_name: str, env: dict[str, str] | None = None):
        self.log_path = home / log_name
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            argv, cwd=home, env={**_child_env(home), **(env or {})}, stdout=self._log, stderr=subprocess.STDOUT
        )

    def output(self) -> str:
        if not self._log.closed:
            self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def wait_for(self, url: str) -> None:
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"process exited with {self.proc.returncode}:\n{self.output()[-3000:]}")
            try:
                with urllib.request.urlopen(url, timeout=2):
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        pytest.fail(f"{url} did not answer within {_START_TIMEOUT}s:\n{self.output()[-3000:]}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        self._log.close()


@dataclass
class Stack:
    home: Path
    machine_key: str
    token: str  # the project's consumer token, in the generated config only
    server_url: str
    gateway_base: str
    access_log: Path
    gateway: _Process
    server: _Process

    def gateway_url(self, machine: str = MACHINE) -> str:
        return f"{self.gateway_base}/{machine}/mcp"

    def config(self, directory: Path, **overrides) -> Path:
        """A project ``.mcp.json`` like the one ``sync-configs`` writes (plus non-fleet entries)."""
        servers = {
            MACHINE: {"type": "http", "url": self.gateway_url(), "headers": {"Authorization": f"Bearer {self.token}"}},
            OTHER_MACHINE: {
                "type": "http",
                "url": self.gateway_url(OTHER_MACHINE),
                "headers": {"Authorization": f"Bearer {self.token}"},
            },
            "linux-direct": {
                "type": "http",
                "url": self.server_url,
                "headers": {"Authorization": f"Bearer {self.machine_key}"},
            },
            "playwright": {"command": "npx", "args": ["@playwright/mcp"]},
            "n8n-mcp": {"type": "http", "url": "http://127.0.0.1:9/mcp"},
        }
        servers.update(overrides)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
        return path


@pytest.fixture(scope="module")
def stack(tmp_path_factory) -> Stack:
    home = tmp_path_factory.mktemp("cli-call-stack")
    (home / "remoteos.toml").write_text("")
    machine_key = fresh_secret("machinekey")
    token = fresh_secret("consumer")
    server_port, gateway_port = _free_port(), _free_port()
    server = _Process(
        [sys.executable, "-m", "remoteos", "--host", "127.0.0.1", "--port", str(server_port)]
        + ["--config", str(home / "remoteos.toml"), "--enable-all"],
        home,
        "server.log",
        env={"REMOTEOS_AUTH_KEY": machine_key},
    )
    server_url = f"http://127.0.0.1:{server_port}/mcp"
    machines = home / "machines.json"
    machines.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {"type": "http", "url": server_url, "headers": {"Authorization": f"Bearer {machine_key}"}}
                    for name in (MACHINE, OTHER_MACHINE)
                }
            }
        ),
        encoding="utf-8",
    )
    registry = home / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "exposed": [MACHINE, OTHER_MACHINE],
                "consumers": {
                    CONSUMER: {"sha256": [token_sha256(token)], "machines": [MACHINE], "added": "2026-10-24"}
                },
            }
        ),
        encoding="utf-8",
    )
    access_log = home / "access.jsonl"
    access_log.touch()  # the gateway creates it at the first request; a test may read it before
    gateway = _Process(
        [sys.executable, "-c", "from remoteos.gateway.cli import main; main()"]
        + ["--registry", str(registry), "--machines", str(machines), "--access-log", str(access_log)]
        + ["--manifest", str(home / "projects.toml"), "serve", "--host", "127.0.0.1", "--port", str(gateway_port)],
        home,
        "gateway.log",
    )
    try:
        server.wait_for(f"http://127.0.0.1:{server_port}/health")
        gateway.wait_for(f"http://127.0.0.1:{gateway_port}/healthz")
        yield Stack(
            home=home,
            machine_key=machine_key,
            token=token,
            server_url=server_url,
            gateway_base=f"http://127.0.0.1:{gateway_port}",
            access_log=access_log,
            gateway=gateway,
            server=server,
        )
    finally:
        gateway.stop()
        server.stop()
        for secret in (machine_key, token):  # neither process ever logged a secret
            assert secret not in server.output() and secret not in gateway.output()


@pytest.fixture(autouse=True)
def _restore_process_wide_cli_settings():
    """The CLI sets two process-wide things for its own (short) process: the library loggers'
    levels and the SDK's EventSource. In-process runs must not leak them into other test modules
    (test_tool_errors reads fastmcp's log)."""
    from mcp.client import streamable_http

    levels = {name: logging.getLogger(name).level for name in cli_call._LIBRARY_LOGGERS}
    event_source = streamable_http.EventSource
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    streamable_http.EventSource = event_source


@dataclass
class Ran:
    code: int
    stdout: str
    stderr: str


@pytest.fixture
def run(stack, tmp_path, caplog, monkeypatch):
    """Run ``remoteos-call`` in-process with the project's config in the cwd; assert no secret leaked."""
    project = tmp_path / "project"
    stack.config(project)
    monkeypatch.chdir(project)
    caplog.set_level(logging.DEBUG)
    runs: list[Ran] = []

    def _run(*args: str) -> Ran:
        assert stack.token not in " ".join(args)  # the token never goes into an argv
        result = CliRunner().invoke(main, list(args), catch_exceptions=False)
        ran = Ran(result.exit_code, result.stdout, result.stderr)
        runs.append(ran)
        return ran

    yield _run
    logged = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    for ran in runs:
        for secret in (stack.token, stack.machine_key):
            assert secret not in ran.stdout and secret not in ran.stderr, "a secret reached the output"
    for secret in (stack.token, stack.machine_key):
        assert secret not in logged, "a secret reached a log record"
        assert secret not in stack.access_log.read_text(encoding="utf-8")


# -- Shell: stdout, stderr and the exit code ---------------------------------------------------


def test_shell_through_the_gateway_splits_stdout_stderr_and_exits_with_the_remote_code(run):
    ran = run(MACHINE, "Shell", "echo out-line; echo err-line >&2; exit 3")
    assert (ran.code, ran.stdout, ran.stderr) == (3, "out-line\n", "err-line\n")


def test_shell_success_exits_0_and_keeps_every_stdout_line(run):
    ran = run(MACHINE, "Shell", "printf 'one\\ntwo\\n\\nfour\\n'")
    assert (ran.code, ran.stdout, ran.stderr) == (0, "one\ntwo\n\nfour\n", "")


def test_shell_with_no_output_prints_nothing(run):
    ran = run(MACHINE, "Shell", "true")
    assert (ran.code, ran.stdout, ran.stderr) == (0, "", "")


def test_shell_failing_with_only_stderr_keeps_stdout_empty(run):
    ran = run(MACHINE, "Shell", "echo only-err >&2; exit 1")
    assert (ran.code, ran.stdout, ran.stderr) == (1, "", "only-err\n")


def test_shell_exit_code_without_any_output(run):
    ran = run(MACHINE, "Shell", "exit 42")
    assert (ran.code, ran.stdout, ran.stderr) == (42, "", "")


def test_a_remote_command_killed_by_a_signal_exits_128_plus_the_signal(run):
    ran = run(MACHINE, "Shell", "kill -9 $$")
    assert ran.code == 137
    assert ran.stderr == "remoteos-call: remote exit code -9, reported as 137\n"


def test_shell_cwd_runs_the_command_in_that_directory(run, tmp_path):
    work = tmp_path / "dir with space"
    work.mkdir()
    ran = run(MACHINE, "Shell", "pwd -P", "--cwd", str(work))
    assert (ran.code, ran.stdout) == (0, f"{work.resolve()}\n")


def test_a_missing_cwd_is_a_tool_error_125(run, tmp_path):
    ran = run(MACHINE, "Shell", "true", "--cwd", str(tmp_path / "nope"))
    assert ran.code == 125
    assert ran.stdout == ""
    assert ran.stderr == f"remoteos-call: Shell error: working directory not found: {tmp_path / 'nope'}\n"


def test_shell_timeout_goes_to_the_remote_command_and_a_timeout_is_a_tool_error_125(run):
    started = time.monotonic()
    ran = run(MACHINE, "Shell", "sleep 20", "--timeout", "1")
    assert time.monotonic() - started < 15
    assert ran.code == 125
    assert ran.stderr == "remoteos-call: Command timed out after 1s\n"


def test_a_direct_machine_entry_works_without_the_gateway(run):
    ran = run("linux-direct", "Shell", "echo direct")
    assert (ran.code, ran.stdout) == (0, "direct\n")


def test_shell_output_over_one_mebibyte_arrives_whole(run):
    # one reply is one server-sent event; httpx2's default refuses an event over 1 MiB
    ran = run(MACHINE, "Shell", "head -c 2500000 /dev/zero | tr '\\0' x; echo; echo tail-line")
    assert ran.code == 0
    assert ran.stdout == "x" * 2_500_000 + "\ntail-line\n"


def test_ansi_escapes_in_remote_output_are_kept_when_piped(run):
    ran = run(MACHINE, "Shell", "printf 'a\\033[31mRED\\033[0mb\\n'; printf 'e\\033[1mB\\033[0m\\n' >&2")
    assert (ran.code, ran.stdout, ran.stderr) == (0, "a\x1b[31mRED\x1b[0mb\n", "e\x1b[1mB\x1b[0m\n")


def test_a_proxy_in_the_environment_is_never_used(run, monkeypatch):
    dead = closed_port_url().removesuffix("/mcp")  # a proxy there would refuse every request
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    ran = run(MACHINE, "Shell", "echo not-proxied")
    assert (ran.code, ran.stdout) == (0, "not-proxied\n")


def test_a_lowercase_authorization_header_is_read_like_the_gateway_reads_it(stack, run, tmp_path, monkeypatch):
    project = tmp_path / "lowercase"
    entry = {"url": stack.gateway_url(), "headers": {"authorization": f"Bearer {stack.token}"}}
    stack.config(project, **{MACHINE: entry})
    monkeypatch.chdir(project)
    ran = run(MACHINE, "Shell", "echo lower")
    assert (ran.code, ran.stdout) == (0, "lower\n")


# -- put / get ---------------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_put_then_get_round_trips_a_binary_file_byte_for_byte(run, tmp_path):
    payload = secrets.token_bytes(300_000) + b"\x00\r\n\xff" * 64
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)
    remote = tmp_path / "remote side" / "copy.bin"

    put = run(MACHINE, "put", str(local), str(remote))
    assert (put.code, put.stdout, put.stderr) == (0, "", "")
    assert _sha256(remote.read_bytes()) == _sha256(payload)  # the remote file itself
    remote_sum = run(MACHINE, "Shell", f"sha256sum '{remote}' | cut -d' ' -f1")
    assert remote_sum.stdout == _sha256(payload) + "\n"

    back = tmp_path / "back.bin"
    got = run(MACHINE, "get", str(remote), str(back))
    assert (got.code, got.stdout, got.stderr) == (0, "", "")
    assert _sha256(back.read_bytes()) == _sha256(payload)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")] == []  # no temp file left


def test_put_of_an_empty_file_and_get_it_back(run, tmp_path):
    local = tmp_path / "empty"
    local.write_bytes(b"")
    assert run(MACHINE, "put", str(local), str(tmp_path / "remote-empty")).code == 0
    assert run(MACHINE, "get", str(tmp_path / "remote-empty"), str(tmp_path / "back-empty")).code == 0
    assert (tmp_path / "back-empty").read_bytes() == b""


def test_get_of_a_missing_remote_file_is_a_tool_error_and_writes_nothing(run, tmp_path):
    local = tmp_path / "never.bin"
    ran = run(MACHINE, "get", str(tmp_path / "missing.bin"), str(local))
    assert ran.code == 125
    assert ran.stderr == f"remoteos-call: File not found: {tmp_path / 'missing.bin'}\n"
    assert not local.exists()


def test_put_of_a_missing_local_file_makes_no_call(run, stack, tmp_path):
    before = stack.access_log.read_text(encoding="utf-8")
    ran = run(MACHINE, "put", str(tmp_path / "nope.bin"), str(tmp_path / "remote.bin"))
    assert ran.code == 126
    assert ran.stderr == f"remoteos-call: cannot read {tmp_path / 'nope.bin'}: No such file or directory\n"
    assert stack.access_log.read_text(encoding="utf-8") == before


def test_get_of_a_multi_megabyte_file_through_the_gateway_matches_its_sha256(run, tmp_path):
    # the machine writes it (no put limit involved); its base64 reply is ~6.7 MB, one SSE event
    remote = tmp_path / "big-remote.bin"
    payload = secrets.token_bytes(5_000_000)
    remote.write_bytes(payload)
    back = tmp_path / "big-back.bin"
    got = run(MACHINE, "get", str(remote), str(back))
    assert (got.code, got.stdout, got.stderr) == (0, "", "")
    assert _sha256(back.read_bytes()) == _sha256(payload)


def test_put_of_almost_3_mb_through_the_gateway_works(run, tmp_path):
    payload = secrets.token_bytes(2_900_000)  # ~3.9 MB of base64 + JSON: just inside the machine's 4 MiB
    local = tmp_path / "almost.bin"
    local.write_bytes(payload)
    remote = tmp_path / "almost-remote.bin"
    assert run(MACHINE, "put", str(local), str(remote)).code == 0
    assert _sha256(remote.read_bytes()) == _sha256(payload)


@pytest.mark.parametrize("machine", [MACHINE, "linux-direct"])
def test_a_put_over_the_machine_s_request_limit_is_refused_in_one_line(run, tmp_path, machine):
    local = tmp_path / "big.bin"
    local.write_bytes(b"\0" * 3_500_000)  # ~4.7 MB of base64: over the machine's 4 MiB, under the gateway's 16
    ran = run(machine, "put", str(local), str(tmp_path / "big-remote.bin"))
    assert ran.code == 126
    reason = "request too large (413): a machine takes at most 4 MiB per request, so a put carries about 3 MB"
    assert ran.stderr.startswith(f"remoteos-call: {machine}: {reason} (")
    assert ran.stderr.count("\n") == 1
    assert not (tmp_path / "big-remote.bin").exists()


# -- any tool, --list ----------------------------------------------------------------------------


def test_any_tool_with_json_arguments_prints_its_text(run, tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("Žltý kôň\nline two\n", encoding="utf-8")
    ran = run(MACHINE, "FileRead", "--json", json.dumps({"path": str(note)}))
    assert (ran.code, ran.stdout) == (0, "Žltý kôň\nline two\n\n")


def test_shell_through_json_returns_the_raw_reply(run):
    ran = run(MACHINE, "Shell", "--json", '{"command": "echo raw; exit 4"}')
    assert (ran.code, ran.stdout) == (0, "raw\n\n[Exit code: 4]\n")


def test_an_unknown_tool_is_a_tool_error_125(run):
    ran = run(MACHINE, "NoSuchTool", "--json", "{}")
    assert ran.code == 125
    assert ran.stderr == "remoteos-call: Unknown tool: 'NoSuchTool'\n"


def test_invalid_tool_arguments_are_a_tool_error_125(run):
    ran = run(MACHINE, "FileRead", "--json", "{}")
    assert ran.code == 125
    assert ran.stderr.startswith("remoteos-call: 1 validation error for call[FileRead]\npath\n")


def test_no_reply_within_the_wait_is_a_timeout_126(run, stack):
    started = time.monotonic()
    ran = run(MACHINE, "Shell", "--json", '{"command": "sleep 8", "timeout": 30}', "--timeout", "1")
    assert time.monotonic() - started < 7
    assert ran.code == 126
    reason = "timed out waiting for the reply (the command may still run)"
    assert ran.stderr == f"remoteos-call: {MACHINE}: {reason} ({stack.gateway_url()})\n"


def test_list_shows_only_the_config_s_fleet_machines(run):
    ran = run("--list")
    assert (ran.code, ran.stdout) == (0, f"{OTHER_MACHINE}\n{MACHINE}\nlinux-direct\n")


# -- 126: no call possible -----------------------------------------------------------------------


def test_an_unknown_machine_exits_126_in_one_line(run):
    ran = run("linux-nope", "Shell", "true")
    assert ran.code == 126
    assert ran.stderr.startswith("remoteos-call: unknown machine 'linux-nope' in ")
    assert ran.stderr.count("\n") == 1


def test_a_non_fleet_entry_is_not_a_machine(run):
    ran = run("n8n-mcp", "Shell", "true")
    assert ran.code == 126
    assert "unknown machine 'n8n-mcp'" in ran.stderr


def test_a_wrong_token_is_an_auth_failure_126(stack, run, tmp_path, monkeypatch):
    wrong = fresh_secret("wrong")
    project = tmp_path / "wrong-token"
    stack.config(project, **{MACHINE: {"url": stack.gateway_url(), "headers": {"Authorization": f"Bearer {wrong}"}}})
    monkeypatch.chdir(project)
    ran = run(MACHINE, "Shell", "echo never")
    assert ran.code == 126
    reason = "authentication refused (401): the token in the config is not accepted"
    assert ran.stderr == f"remoteos-call: {MACHINE}: {reason} ({stack.gateway_url()})\n"
    assert wrong not in ran.stderr


def test_a_machine_the_token_may_not_reach_is_refused_126(stack, run):
    ran = run(OTHER_MACHINE, "Shell", "echo never")
    assert ran.code == 126
    assert "not allowed (403)" in ran.stderr


def test_an_unreachable_gateway_is_a_transport_failure_126(stack, run, tmp_path, monkeypatch):
    project = tmp_path / "dead"
    dead = closed_port_url()
    stack.config(project, **{MACHINE: {"url": dead, "headers": {"Authorization": f"Bearer {stack.token}"}}})
    monkeypatch.chdir(project)
    ran = run(MACHINE, "Shell", "echo never")
    assert ran.code == 126
    assert ran.stderr == f"remoteos-call: {MACHINE}: cannot connect (connection refused or host unreachable) ({dead})\n"


def test_no_config_in_the_cwd_is_126(run, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ran = run(MACHINE, "Shell", "true")
    assert ran.code == 126
    assert (
        ran.stderr
        == f"remoteos-call: no MCP config at {tmp_path / '.mcp.json'} (use --config PATH or --project NAME)\n"
    )


def test_project_reads_the_generated_config_from_claude_mcp_configs(stack, run, tmp_path, monkeypatch):
    home = tmp_path / "home"
    generated = stack.config(home / ".claude" / "mcp-configs")
    generated.rename(generated.with_name("myproj.mcp.json"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # no .mcp.json here
    ran = run("--project", "myproj", MACHINE, "Shell", "echo via-project")
    assert (ran.code, ran.stdout) == (0, "via-project\n")


def test_config_flag_overrides_the_cwd(stack, run, tmp_path, monkeypatch):
    other = stack.config(tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    ran = run("--config", str(other), MACHINE, "Shell", "echo via-config")
    assert (ran.code, ran.stdout) == (0, "via-config\n")


def test_a_malformed_url_is_refused_in_one_line_never_a_traceback(stack, run, tmp_path, monkeypatch):
    project = tmp_path / "badurl"
    entry = {"url": "http://[bad/mcp", "headers": {"Authorization": f"Bearer {stack.token}"}}
    stack.config(project, **{MACHINE: entry})
    monkeypatch.chdir(project)
    ran = run(MACHINE, "Shell", "true")
    assert ran.code == 126
    assert ran.stderr == f"remoteos-call: machine '{MACHINE}' in {project / '.mcp.json'} has no valid http(s) url\n"
    assert MACHINE not in run("--list").stdout.split()


def test_a_broken_config_is_126_without_echoing_its_content(stack, run, tmp_path, monkeypatch):
    project = tmp_path / "broken"
    project.mkdir()
    (project / ".mcp.json").write_text('{"mcpServers": {"x": "Bearer ' + stack.token + '"', encoding="utf-8")
    monkeypatch.chdir(project)
    ran = run(MACHINE, "Shell", "true")
    assert ran.code == 126
    assert "is not valid JSON" in ran.stderr


@pytest.mark.parametrize(
    "args",
    [
        (MACHINE, "Shell"),
        (MACHINE, "Shell", "a", "b"),
        (MACHINE, "put", "only-one"),
        (MACHINE, "get", "a", "b", "c"),
        (MACHINE, "FileRead", "positional"),
        (MACHINE, "FileRead", "--json", "[1]"),
        (MACHINE, "FileRead", "--json", "{nope"),
        (MACHINE, "put", "a", "b", "--cwd", "/tmp"),
        (MACHINE,),
        ("--list", MACHINE),
        ("--list", "--timeout", "5"),
        ("--list", "--json", "{}"),
        ("--project", "../escape", MACHINE, "Shell", "true"),
        ("--config", "x.json", "--project", "p", MACHINE, "Shell", "true"),
    ],
)
def test_usage_errors_exit_2_before_any_call(run, stack, args):
    before = stack.access_log.read_text(encoding="utf-8")
    assert run(*args).code == 2
    assert stack.access_log.read_text(encoding="utf-8") == before


def test_a_usage_error_is_2_even_without_a_config(run, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .mcp.json: the shape is checked before the config is read
    ran = run("linux-anything", "Shell")
    assert ran.code == 2
    assert "Shell takes exactly one COMMAND" in ran.stderr


# -- the console script as a real process ------------------------------------------------------


def _script_process(stack, tmp_path, command: str) -> tuple[list[str], dict[str, str], Path]:
    script = Path(sys.executable).with_name("remoteos-call")
    assert script.exists(), "the package must install the remoteos-call console script"
    project = tmp_path / "proc"
    stack.config(project)
    argv = [str(script), MACHINE, "Shell", command]
    assert stack.token not in " ".join(argv)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REMOTEOS_", "FASTMCP_"))}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (_SRC, env.get("PYTHONPATH", "")) if p)
    env["FASTMCP_LOG_LEVEL"] = "DEBUG"  # fastmcp's own stderr handler must still stay silent
    assert all(stack.token not in value for value in env.values())  # nor in the environment
    return argv, env, project


def test_the_console_script_runs_as_a_process_with_the_token_only_in_the_config(stack, tmp_path):
    argv, env, project = _script_process(stack, tmp_path, "echo proc-out; echo proc-err >&2; exit 7")
    done = subprocess.run(argv, cwd=project, env=env, capture_output=True, text=True, timeout=120)
    assert (done.returncode, done.stdout, done.stderr) == (7, "proc-out\n", "proc-err\n")


def test_a_closed_stdout_pipe_exits_141_without_a_traceback(stack, tmp_path):
    # `remoteos-call … | head -1`: the reader goes away while the CLI still writes
    argv, env, project = _script_process(stack, tmp_path, "seq 1 400000")
    proc = subprocess.Popen(argv, cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout.readline() == b"1\n"
    proc.stdout.close()
    stderr = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=120) == 141
    assert stderr == b""


# -- the reply parser (pure) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("[task:ab12] hello", ("hello", "", 0)),
        ("[task:ab12] (no output)", ("", "", 0)),
        ("[task:ab12] [Exit code: 2]", ("", "", 2)),
        ("[task:ab12] [STDERR] boom\n\n[Exit code: 1]", ("", "boom", 1)),
        ("[task:ab12] out\n\n[STDERR] err\n\n[Exit code: 5]", ("out", "err", 5)),
        ("out\n[STDERR] e1\n[STDERR] e2", ("out", "e1\n[STDERR] e2", 0)),  # split at the first marker
        ("a\n[Exit code: 3] not the end", ("a\n[Exit code: 3] not the end", "", 0)),  # only a final line counts
        ("[task:ab12] [Exit code: -15]", ("", "", -15)),
        ("hi\n[Exit code: 9]", ("hi", "", 9)),  # a line the command printed itself: documented limit
    ],
)
def test_parse_shell_reply(reply, expected):
    parsed = cli_call.parse_shell_reply(reply)
    assert (parsed.stdout, parsed.stderr, parsed.exit_code) == expected


@pytest.mark.parametrize(
    ("remote", "code"),
    [(0, 0), (1, 1), (255, 255), (-9, 137), (-15, 143), (256, 255), (4294967295, 255), (-200, 255)],
)
def test_process_exit_code(remote, code):
    assert cli_call.process_exit_code(remote) == code


def test_the_machine_repr_never_shows_the_authorization():
    machine = cli_call.Machine("linux-x", "http://h/mcp", "Bearer " + secrets.token_hex(8))
    assert machine.authorization not in repr(machine)


def test_transport_failure_text_is_scrubbed_of_the_token():
    token = secrets.token_hex(12)
    machine = cli_call.Machine("linux-x", "http://user:pw@h:81/x/mcp?q=1", f"Bearer {token}")
    failure = cli_call._transport_failure(RuntimeError(f"echoed Bearer {token} and {token}"), machine)
    assert failure.code == 126
    assert token not in failure.message
    assert failure.message == "linux-x: RuntimeError: echoed <authorization> and <token> (http://h:81/x/mcp)"


def test_help_lists_the_exit_codes():
    text = " ".join(CliRunner().invoke(main, ["--help"]).output.split())
    assert "125 = the tool failed" in text and "126 = no result" in text and "141 = broken pipe" in text


# -- failure classification (pure) -------------------------------------------------------------

_M = cli_call.Machine("linux-x", "http://h:81/mcp", "Bearer " + secrets.token_hex(8))


def _mcp_error(code: int, message: str):
    from mcp.shared.exceptions import MCPError

    return MCPError(code, message)


def test_a_json_rpc_error_the_server_answered_is_a_tool_error_125():
    failure = cli_call._protocol_failure(RuntimeError("wrapped"), _M)
    assert failure is None  # not an MCPError: no server answer
    answered = cli_call._protocol_failure(ExceptionGroup("g", [_mcp_error(-32602, "Invalid params")]), _M)
    assert (answered.code, answered.message) == (125, "linux-x: Invalid params")


@pytest.mark.parametrize("code", [-32001, -32000])  # REQUEST_TIMEOUT, CONNECTION_CLOSED
def test_a_timeout_or_a_closed_connection_is_no_server_answer(code):
    assert cli_call._protocol_failure(_mcp_error(code, "x"), _M) is None


def test_a_request_timeout_reads_as_a_timeout_126():
    failure = cli_call._transport_failure(_mcp_error(-32001, "Request 'tools/call' timed out"), _M)
    assert failure.message == "linux-x: timed out waiting for the reply (the command may still run) (http://h:81/mcp)"


def test_a_handshake_timeout_reads_as_a_timeout_not_a_bare_type():
    # fastmcp raises RuntimeError("Failed to initialize server session") from a builtin TimeoutError
    try:
        try:
            raise TimeoutError()
        except TimeoutError as inner:
            raise RuntimeError("Failed to initialize server session") from inner
    except RuntimeError as exc:
        failure = cli_call._transport_failure(exc, _M)
    assert failure.code == 126
    assert "timed out waiting for the reply" in failure.message


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "authentication refused (401)"),
        (403, "not allowed (403)"),
        (404, "not found (404)"),
        (413, "request too large (413)"),
        (502, "HTTP 502"),
    ],
)
def test_an_http_status_names_its_reason(status, reason):
    import httpx2

    request = httpx2.Request("POST", "http://h:81/mcp")
    error = httpx2.HTTPStatusError("x", request=request, response=httpx2.Response(status, request=request))
    assert cli_call._transport_failure(error, _M).message.startswith(f"linux-x: {reason}")
    recorded = cli_call._transport_failure(_mcp_error(-32603, "Server returned an error response"), _M, status)
    assert recorded.message.startswith(f"linux-x: {reason}")


def test_the_status_hook_records_only_the_first_post_error():
    import httpx2

    hook = cli_call._HttpErrors()

    def answer(method: str, status: int):
        return httpx2.Response(status, request=httpx2.Request(method, "http://h/mcp"))

    asyncio.run(hook(answer("GET", 405)))  # the SDK's optional GET stream: not a request's answer
    asyncio.run(hook(answer("POST", 200)))
    assert hook.status is None
    asyncio.run(hook(answer("POST", 413)))
    asyncio.run(hook(answer("POST", 401)))
    assert hook.status == 413


# -- put / get reply checks: the remote reply faked at the network boundary (cli_call._run) -------


def _reply(text: str, is_error: bool = False):
    from fastmcp.client.client import CallToolResult
    from mcp.types import TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=None, meta=None, is_error=is_error
    )


@pytest.fixture
def remote_replies(monkeypatch):
    """Answer every call with the next queued reply; record what was asked."""
    queued: list = []
    asked: list[tuple[str, dict, float]] = []

    def fake_run(machine, tool, arguments, wait_s):
        asked.append((tool, arguments, wait_s))
        return queued.pop(0)

    monkeypatch.setattr(cli_call, "_run", fake_run)
    return queued, asked


def _expect_failure(call) -> cli_call.CallFailed:
    with pytest.raises(cli_call.CallFailed) as caught:
        call()
    return caught.value


def test_put_refuses_a_size_mismatch_and_an_unexpected_reply(remote_replies, tmp_path):
    queued, asked = remote_replies
    local = tmp_path / "f.bin"
    local.write_bytes(b"12345")
    queued.append(_reply("[task:a1] Written 4 bytes to /r"))
    failure = _expect_failure(lambda: cli_call.run_put(_M, local, "/r", 9.0))
    assert (failure.code, failure.message) == (125, "put: size mismatch, sent 5 bytes, remote wrote 4")
    assert asked == [("FileUpload", {"path": "/r", "data_base64": "MTIzNDU="}, 9.0)]
    queued.append(_reply("[task:a2] something else"))
    assert _expect_failure(lambda: cli_call.run_put(_M, local, "/r", 9.0)).message == (
        "put: unexpected FileUpload reply: something else"
    )
    queued.append(_reply("[task:a3] FileUpload error: Permission denied", is_error=True))
    failure = _expect_failure(lambda: cli_call.run_put(_M, local, "/r", 9.0))
    assert (failure.code, failure.message) == (125, "FileUpload error: Permission denied")
    queued.append(_reply("[task:a4] Written 5 bytes to /r"))
    assert cli_call.run_put(_M, local, "/r", 9.0) == 0


def test_get_refuses_a_size_mismatch_bad_base64_and_an_unexpected_reply_and_writes_nothing(remote_replies, tmp_path):
    queued, _ = remote_replies
    local = tmp_path / "out.bin"
    for text, message in (
        ("[task:b1] base64:9bytes:MTIzNDU=", "get: size mismatch, remote sent 9 bytes, received 5"),
        ("[task:b2] base64:5bytes:!!!!", "get: the FileDownload reply is not valid base64"),
        ("[task:b3] what?", "get: unexpected FileDownload reply: what?"),
    ):
        queued.append(_reply(text))
        failure = _expect_failure(lambda: cli_call.run_get(_M, "/r", local, 9.0))
        assert failure.code == 125
        assert failure.message.startswith(message)
    assert list(tmp_path.iterdir()) == []
    queued.append(_reply("[task:b4] base64:5bytes:MTIzNDU="))
    assert cli_call.run_get(_M, "/r", local, 9.0) == 0
    assert local.read_bytes() == b"12345"


def test_a_non_text_block_is_named_never_dumped(remote_replies, capsys):
    from fastmcp.client.client import CallToolResult
    from mcp.types import ImageContent

    queued, _ = remote_replies
    image = ImageContent(type="image", data=base64.b64encode(b"png-bytes").decode(), mimeType="image/png")
    queued.append(CallToolResult(content=[image], structured_content=None, meta=None, is_error=False))
    assert cli_call.run_tool(_M, "Snapshot", {}, 9.0) == 0
    assert capsys.readouterr().out == "[image block: image/png]\n"


# -- the local write of get ------------------------------------------------------------------------


def test_write_atomically_keeps_an_existing_file_s_mode_and_gives_a_new_one_the_umask(tmp_path):
    existing = tmp_path / "existing"
    existing.write_bytes(b"old")
    existing.chmod(0o640)
    cli_call.write_atomically(existing, b"new")
    assert (existing.read_bytes(), existing.stat().st_mode & 0o777) == (b"new", 0o640)
    old_umask = os.umask(0o027)
    try:
        cli_call.write_atomically(tmp_path / "fresh", b"x")
    finally:
        os.umask(old_umask)
    assert (tmp_path / "fresh").stat().st_mode & 0o777 == 0o640  # 0o666 & ~0o027
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing", "fresh"]  # no temp file left


def test_write_atomically_into_a_missing_directory_is_126(tmp_path):
    failure = _expect_failure(lambda: cli_call.write_atomically(tmp_path / "no" / "f", b"x"))
    assert (failure.code, failure.message) == (126, f"cannot write into {tmp_path / 'no'}: No such file or directory")
