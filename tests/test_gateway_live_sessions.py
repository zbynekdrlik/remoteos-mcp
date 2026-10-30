"""``sync-configs`` tells the owner which live Claude sessions need a restart (#66).

The scanner reads a fake ``/proc`` tree and a fake ``tmux``; the end-to-end tests start a REAL
process named ``claude`` in a project directory and run the real sync through the real local,
``sudo`` and ``ssh`` target paths (the latter two faked at the OS boundary). No test touches a
real session: a scan only ever matches a config under ``tmp_path``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time

import pytest
from fleet_harness import install_fake_airuleset, install_fake_remote_tools, notify_calls, remote_calls
from test_gateway_sync import ME, Fleet

from remoteos.gateway import live_sessions, session_scan
from remoteos.gateway import sync as sync_module
from remoteos.gateway.live_sessions import Session
from remoteos.gateway.manifest import Project

TMUX_LISTING = "%1 other:0\n%7 main:2\n%8 main:3\n"
FAKE_TMUX = """#!{python}
import json, os, sys
with open(os.environ["FAKE_TMUX_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write({listing!r})
sys.exit(int(os.environ.get("FAKE_TMUX_EXIT", "0")))
"""


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    bin_dir = tmp_path / "tmux-bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(FAKE_TMUX.format(python=sys.executable, listing=TMUX_LISTING), encoding="utf-8")
    tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TMUX_LOG", str(tmp_path / "tmux.jsonl"))
    return tmux


def project_dir(tmp_path, name: str, config) -> object:
    directory = tmp_path / "work" / name
    directory.mkdir(parents=True)
    (directory / ".mcp.json").symlink_to(config)
    return directory


class FakeProc:
    def __init__(self, root) -> None:
        self.root = root
        self.root.mkdir()
        (root / "self").mkdir()  # not a pid

    def add(self, pid: int, comm: str, cwd, environ: dict | None = None) -> None:
        directory = self.root / str(pid)
        directory.mkdir()
        (directory / "comm").write_text(comm + "\n", encoding="utf-8")
        (directory / "cwd").symlink_to(cwd)
        body = "\0".join(f"{k}={v}" for k, v in (environ or {}).items())
        (directory / "environ").write_text(body, encoding="utf-8")


def test_the_scan_finds_the_right_panes(tmp_path, fake_tmux):
    config = tmp_path / "configs" / "camera-box.mcp.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    other_config = config.with_name("other.mcp.json")
    other_config.write_text("{}", encoding="utf-8")
    project = project_dir(tmp_path, "camera-box", config)
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    other = project_dir(tmp_path, "other", other_config)
    tmux_env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%7", "SECRET_TOKEN": "hunter2"}
    proc = FakeProc(tmp_path / "proc")
    proc.add(100, "claude", project, tmux_env)
    proc.add(101, "claude", nested, {**tmux_env, "TMUX_PANE": "%8"})  # in a subdirectory of the project
    proc.add(102, "claude", project)  # outside tmux
    proc.add(103, "claude", other, {**tmux_env, "TMUX_PANE": "%1"})  # another project
    proc.add(104, "bash", project, tmux_env)  # not a session
    proc.add(105, "claude", tmp_path, tmux_env)  # no .mcp.json

    sessions = session_scan.scan(str(config), proc_root=str(proc.root), tmux=str(fake_tmux))

    assert sessions == [
        {"pid": 100, "tmux": True, "pane": "main:2"},
        {"pid": 101, "tmux": True, "pane": "main:3"},
        {"pid": 102, "tmux": False, "pane": None},
    ]
    assert "hunter2" not in json.dumps(sessions)
    [call] = [json.loads(line) for line in (tmp_path / "tmux.jsonl").read_text(encoding="utf-8").splitlines()]
    assert call[:2] == ["-S", "/tmp/tmux-1/default"]  # one query per tmux server


def test_a_dead_tmux_leaves_the_session_with_no_pane(tmp_path, fake_tmux, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_EXIT", "1")
    config = tmp_path / "x.mcp.json"
    config.write_text("{}", encoding="utf-8")
    proc = FakeProc(tmp_path / "proc")
    proc.add(100, "claude", project_dir(tmp_path, "x", config), {"TMUX": "/s,1,0", "TMUX_PANE": "%7"})

    assert session_scan.scan(str(config), str(proc.root), str(fake_tmux)) == [{"pid": 100, "tmux": True, "pane": None}]


def test_the_scanner_is_python_3_8_with_no_package_import():
    source = open(session_scan.__file__, encoding="utf-8").read()
    tree = ast.parse(source, feature_version=(3, 8))
    modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    modules += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
    assert set(modules) == {"__future__", "json", "os", "subprocess", "sys", "pwd"}


PROJECT = Project(name="camera-box", host="dev1", account="newlevel", config="/c", machines=())


def test_a_long_list_stays_under_the_limit():
    sessions = [Session(pid=i, pane=f"long-session-name-{i}:1") for i in range(100)]
    texts = live_sessions.compose_messages([(PROJECT, sessions)])
    assert len(texts) > 1 and all(len(text) <= 1700 for text in texts)
    assert all(text.endswith(live_sessions.ACTION) for text in texts)
    assert sum(text.count("•") for text in texts) == 100


def test_a_tmux_session_with_no_known_pane_is_not_called_outside_tmux():
    [text] = live_sessions.compose_messages([(PROJECT, [Session(pid=5, pane=None, in_tmux=True)])])
    assert "proces 5 v tmux" in text and "mimo tmux" not in text


def test_nothing_live_composes_nothing():
    assert live_sessions.compose_messages([]) == []


# -- end to end, with a real process named claude ----------------------------------------------


class LiveSession:
    """A real process whose comm is ``claude`` (a shebang script keeps its file name as comm)."""

    def __init__(self, tmp_path, cwd, pane: str | None) -> None:
        script = tmp_path / "session-bin" / "claude"
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/sh\nread -r _\n", encoding="utf-8")
        script.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
        if pane:
            env.update(TMUX="/tmp/fake-tmux,1,0", TMUX_PANE=pane)
        self.process = subprocess.Popen([str(script)], cwd=cwd, env=env, stdin=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            comm = open(f"/proc/{self.process.pid}/comm", encoding="utf-8").read().strip()
            if comm == "claude":
                return
            time.sleep(0.01)
        raise AssertionError(f"the process is named {comm!r}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.process.kill()
        self.process.wait()


class Run:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.fleet = Fleet(tmp_path)
        self.tmp = tmp_path
        self.airuleset, self.log = install_fake_airuleset(tmp_path / "airuleset", monkeypatch)
        self.config = self.fleet.write_config("camera-box", self.fleet.camera_box_text())
        self.project = project_dir(tmp_path, "camera-box", self.config)

    def manifest(self, **overrides) -> None:
        self.fleet.write_manifest({"camera-box": {**self.fleet.camera_box_project(), **overrides}})

    def sync(self, *args: str):
        return self.fleet.run("--airuleset", str(self.airuleset), *args)

    def bodies(self) -> list[str]:
        return [argv[2] for argv in notify_calls(self.log)]


@pytest.fixture
def run(tmp_path, monkeypatch, fake_tmux) -> Run:
    return Run(tmp_path, monkeypatch)


def test_a_written_config_with_a_live_session_sends_one_message_with_the_pane(run):
    run.manifest()
    with LiveSession(run.tmp, run.project, "%7"), LiveSession(run.tmp, run.project, None) as outside:
        result = run.sync()

    assert result.exit_code == 0, result.output
    [body] = run.bodies()
    assert f"• camera-box ({ME}@dev1): main:2" in body
    assert f"proces {outside.process.pid} mimo tmux" in body
    assert "`/exit`" in body and "`claude -c`" in body
    token = run.fleet.token_of(run.config, "linux-cam1")
    for secret in (*run.fleet.secrets(), token):
        assert secret not in body
        assert secret not in json.dumps(notify_calls(run.log))
    assert body in result.output  # it may be suppressed by the owner's policy: the text stays visible


def test_the_message_is_pinned_to_the_owner_and_an_explicit_one_wins(run, monkeypatch):
    run.manifest()
    with LiveSession(run.tmp, run.project, "%7"):
        assert run.sync().exit_code == 0
    [argv] = notify_calls(run.log)
    assert argv[argv.index("--owner-name") + 1] == "zbynek"  # a run with no tmux pane cannot be routed

    run.fleet.write_config("camera-box", run.fleet.camera_box_text())
    with LiveSession(run.tmp, run.project, "%7"):
        assert run.sync("--notify-owner", "david").exit_code == 0
    argv = notify_calls(run.log)[-1]
    assert argv[argv.index("--owner-name") + 1] == "david"


def test_a_failed_closing_registry_save_still_gives_the_notice(run, monkeypatch):
    run.manifest()
    real_commit = sync_module._Saver.commit
    calls = []

    def fails_at_the_end(saver):
        calls.append(1)
        if len(calls) >= 3:  # register, prune, then the closing save of the run
            raise OSError("disk full")
        return real_commit(saver)

    monkeypatch.setattr(sync_module._Saver, "commit", fails_at_the_end)
    with LiveSession(run.tmp, run.project, "%7"):
        run.sync()
    assert any("main:2" in body for body in run.bodies())


def test_no_live_session_sends_nothing(run):
    run.manifest()
    result = run.sync()
    assert result.exit_code == 0, result.output
    assert run.bodies() == []


def test_an_unchanged_config_sends_nothing_even_with_a_live_session(run):
    run.manifest()
    assert run.sync().exit_code == 0
    with LiveSession(run.tmp, run.project, "%7"):
        result = run.sync()
    assert result.exit_code == 0, result.output
    assert run.bodies() == []


def test_a_dry_run_sends_nothing_and_prints_what_it_would_send(run):
    run.manifest()
    with LiveSession(run.tmp, run.project, "%7"):
        result = run.sync("--dry-run")
    assert result.exit_code == 0, result.output
    assert run.bodies() == []
    assert "the owner would get" in result.output and "main:2" in result.output


def test_an_undelivered_notice_is_reported_and_fails_nothing(run, monkeypatch):
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "error")
    run.manifest()
    with LiveSession(run.tmp, run.project, "%7"):
        result = run.sync()
    assert result.exit_code == 0, result.output
    assert "restart notice NOT delivered" in result.output and "main:2" in result.output
    assert run.fleet.token_of(run.config, "linux-cam1")


def test_no_airuleset_prints_the_text_instead(run):
    run.manifest()
    with LiveSession(run.tmp, run.project, "%7"):
        result = run.fleet.run("--airuleset", str(run.tmp / "missing.py"))
    assert result.exit_code == 0, result.output
    assert "no airuleset.py here" in result.output and "main:2" in result.output


def test_a_failed_scan_is_reported_and_fails_nothing(run, monkeypatch):
    log = install_fake_remote_tools(run.tmp / "bin", monkeypatch)
    run.manifest(account="another-account")
    real_run = live_sessions.find_sessions

    def denied(target, config):  # the write went through; the scan's sudo is refused
        monkeypatch.setenv("FAKE_SUDO_DENY", "1")
        return real_run(target, config)

    monkeypatch.setattr(live_sessions, "find_sessions", denied)
    result = run.sync()

    assert result.exit_code == 0, result.output
    assert "cannot list the live sessions of camera-box" in result.output
    assert run.bodies() == []
    assert any(call["tool"] == "sudo" for call in remote_calls(log))


@pytest.mark.parametrize(("overrides", "tool"), [({"account": "another-account"}, "sudo"), ({"host": "dev2"}, "ssh")])
def test_the_other_writers_scan_through_the_same_target_with_no_secret_in_an_argv(
    run, monkeypatch, overrides, tool
):
    log = install_fake_remote_tools(run.tmp / "bin", monkeypatch)
    run.manifest(**overrides)
    with LiveSession(run.tmp, run.project, "%7"):
        result = run.sync()

    assert result.exit_code == 0, result.output
    [body] = run.bodies()
    assert "main:2" in body
    calls = remote_calls(log)
    assert sum(1 for call in calls if call["tool"] == tool) == 3  # read, write, scan
    assert "list-panes" in json.dumps(calls[-1])  # the scanner's source
    token = run.fleet.token_of(run.config, "linux-cam1")
    assert token not in json.dumps(calls)
