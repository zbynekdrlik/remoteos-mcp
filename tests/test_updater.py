"""The per-box updater (#65): the decision table, the guards, and that a target reaches SelfUpgrade."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from remoteos import selfupgrade, updater
from remoteos.release_channel import Release, ReleaseFetchError

NOW = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
INSTALLED = "0.7.0.dev42"


def release(version: str, days_ago: float, held: bool = False) -> Release:
    return Release(f"v{version}", NOW - dt.timedelta(days=days_ago), held)


def ring_file(tmp_path: Path, ring: object = None) -> Path:
    path = tmp_path / "updater.json"
    if ring is not None:
        path.write_text(json.dumps({"ring": ring}))
    return path


class Run:
    def __init__(self, tmp_path: Path, releases, ring: object = None, installed: str = INSTALLED):
        self.started: list[str] = []
        self.path = ring_file(tmp_path, ring)
        self.cgroup = tmp_path / "cgroup"
        self.cgroup.write_text("0::/system.slice/remoteos-mcp-updater.service\n")
        self.releases, self.installed = releases, installed

    def __call__(self, start=None) -> int:
        def fetch():
            if isinstance(self.releases, Exception):
                raise self.releases
            return self.releases

        def record(tag: str) -> str:
            self.started.append(tag)
            return f"Upgrade to {tag} started."

        return updater.run(self.path, installed=self.installed, now=lambda: NOW, fetch=fetch,
                           start=start or record, cgroup_file=str(self.cgroup))


CASES = {
    "up to date": (None, [release(INSTALLED, 20)], []),
    "newer stable": (None, [release("0.7.0.dev50", 20), release(INSTALLED, 40)], ["v0.7.0.dev50"]),
    "newest is still soaking": (None, [release("0.7.0.dev51", 2), release("0.7.0.dev50", 20)], ["v0.7.0.dev50"]),
    "only a soaking release": ("stable", [release("0.7.0.dev51", 2)], []),
    "held on stable": ("stable", [release("0.7.0.dev50", 20, held=True), release(INSTALLED, 40)], []),
    "canary takes the newest": ("canary", [release("0.7.0.dev51", 0), release("0.7.0.dev50", 20)], ["v0.7.0.dev51"]),
    "held on canary": ("canary", [release("0.7.0.dev51", 0, held=True)], []),
    "ahead of the ring never goes back": ("stable", [release("0.7.0.dev30", 20)], []),
    "no releases": ("stable", [], []),
}


@pytest.mark.parametrize("name", CASES)
def test_the_decision_table(tmp_path, name):
    ring, releases, expected = CASES[name]
    run = Run(tmp_path, releases, ring)
    assert run() == 0
    assert run.started == expected


def test_the_releases_unreachable_skips_the_run_and_says_so(tmp_path, caplog):
    run = Run(tmp_path, ReleaseFetchError("HTTP 503"))
    with caplog.at_level(logging.WARNING, logger="remoteos.updater"):
        assert run() == 0
    assert run.started == []
    assert "HTTP 503" in caplog.text


def test_a_version_that_is_not_comparable_is_left_alone(tmp_path, caplog):
    run = Run(tmp_path, [release("0.7.0.dev50", 20)], installed="weird")
    with caplog.at_level(logging.INFO, logger="remoteos.updater"):
        assert run() == 0
    assert run.started == []
    assert "not comparable" in caplog.text


def test_every_decision_is_logged(tmp_path, caplog):
    run = Run(tmp_path, [release(INSTALLED, 20)])
    with caplog.at_level(logging.INFO, logger="remoteos.updater"):
        run()
    assert f"up to date (installed {INSTALLED}, stable v{INSTALLED})" in caplog.text


def test_it_never_runs_inside_the_server_s_service(tmp_path, caplog):
    run = Run(tmp_path, [release("0.7.0.dev50", 20)])
    run.cgroup.write_text("0::/system.slice/remoteos-mcp.service\n")
    assert run() == 1
    assert run.started == []
    assert "inside remoteos-mcp.service" in caplog.text


def test_the_server_does_not_load_the_updater():
    """Only the OS scheduler starts it: the server's module must not reach it."""
    text = (Path(__file__).parent.parent / "src/remoteos/__main__.py").read_text(encoding="utf-8")
    assert "updater" not in text


def test_an_upgrade_that_does_not_start_fails_the_run(tmp_path, caplog):
    def refused(tag: str) -> str:
        raise RuntimeError("no sudoers drop-in")

    run = Run(tmp_path, [release("0.7.0.dev50", 20)])
    assert run(start=refused) == 1
    assert "did not start" in caplog.text and "no sudoers drop-in" in caplog.text


@pytest.mark.parametrize("text", ["{not json", "[1]", '{"ring": "nightly"}', '{"ring": 3}', '{"ring": ["canary"]}'])
def test_a_broken_ring_file_means_stable_and_is_logged(tmp_path, caplog, text):
    path = ring_file(tmp_path)
    path.write_text(text)
    assert updater.read_ring(path) == "stable"
    assert "using ring stable" in caplog.text


def test_no_ring_file_means_stable_quietly(tmp_path, caplog):
    assert updater.read_ring(tmp_path / "updater.json") == "stable"
    assert caplog.text == ""


@pytest.mark.parametrize("kind", ["linux", "macos", "windows"])
def test_the_ring_file_is_where_each_installer_writes_it(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(updater, "is_windows", lambda: kind == "windows")
    monkeypatch.setattr(updater, "is_macos", lambda: kind == "macos")
    monkeypatch.setattr(updater.Path, "home", classmethod(lambda cls: tmp_path))
    expected = Path("/etc/remoteos-mcp/updater.json") if kind == "linux" else tmp_path / ".remoteos-mcp/updater.json"
    assert updater.config_path() == expected


class TestAttempts:
    """A box-specific failure must not stop the server and roll back every day, forever."""

    def test_a_release_is_tried_twice_then_left_alone_and_said_so(self, tmp_path, caplog):
        run = Run(tmp_path, [release("0.7.0.dev50", 20)])
        assert [run(), run(), run()] == [0, 0, 0]
        assert run.started == ["v0.7.0.dev50"] * 2
        assert "failed here 2 times" in caplog.text

    def test_a_newer_release_gets_its_own_tries(self, tmp_path):
        run = Run(tmp_path, [release("0.7.0.dev50", 20)])
        run(), run(), run()
        run.releases = [release("0.7.0.dev51", 20), release("0.7.0.dev50", 40)]
        run()
        assert run.started[-1] == "v0.7.0.dev51"

    def test_up_to_date_never_counts(self, tmp_path):
        run = Run(tmp_path, [release(INSTALLED, 20)])
        run()
        assert not (tmp_path / updater.STATE_NAME).exists()

    def test_the_attempt_is_recorded_before_the_start(self, tmp_path):
        run = Run(tmp_path, [release("0.7.0.dev50", 20)])

        def killed(tag: str) -> str:
            assert json.loads((tmp_path / updater.STATE_NAME).read_text()) == {"tag": tag, "attempts": 1}
            raise SystemExit(9)

        with pytest.raises(SystemExit):
            run(start=killed)

    def test_a_runner_that_did_not_start_costs_no_try(self, tmp_path):
        run = Run(tmp_path, [release("0.7.0.dev50", 20)])

        def refused(tag: str) -> str:
            raise RuntimeError("no upgrade task")

        assert run(start=refused) == 1
        assert updater.attempts_at(tmp_path / updater.STATE_NAME, "v0.7.0.dev50") == 0

    def test_no_record_no_upgrade(self, tmp_path, caplog):
        run = Run(tmp_path, [release("0.7.0.dev50", 20)])
        (tmp_path / updater.STATE_NAME).mkdir()  # a write fails, on any account
        assert run() == 1
        assert run.started == []
        assert "cannot record" in caplog.text

    @pytest.mark.parametrize("text", ["{bad", "[]", '{"tag": "v0.7.0.dev50", "attempts": "9"}',
                                      '{"tag": "v0.7.0.dev50", "attempts": true}'])
    def test_a_broken_state_file_costs_one_try_at_most(self, tmp_path, text):
        path = tmp_path / updater.STATE_NAME
        path.write_text(text)
        assert updater.attempts_at(path, "v0.7.0.dev50") == 0


def test_an_unexpected_failure_is_logged_for_a_run_nobody_watches(tmp_path, monkeypatch):
    def broken(path):
        raise KeyError("boom")

    monkeypatch.setattr(updater, "_set_up_logging", lambda log_file: None)
    monkeypatch.setattr(updater, "run", broken)
    assert updater.main(["--config", str(tmp_path / "u.json")]) == 1


def test_a_target_reaches_selfupgrade_as_the_release_tag(monkeypatch):
    seen = {}
    monkeypatch.setattr(selfupgrade, "start", lambda plan: seen.setdefault("plan", plan).ref)
    assert updater._start_upgrade("v0.7.0.dev50") == "v0.7.0.dev50"
    assert seen["plan"].ref == "v0.7.0.dev50"


def test_main_logs_to_the_file_and_works_with_no_stderr(tmp_path, monkeypatch):
    """pythonw.exe: sys.stderr is None, the file is the only log."""
    monkeypatch.setattr(updater.sys, "stderr", None)
    monkeypatch.setattr(updater.release_channel, "fetch_releases", lambda *a, **k: [])
    monkeypatch.setattr(updater, "run", lambda path, **k: updater.log.info("ran %s", path.name) or 0)
    root = logging.getLogger()
    before = root.handlers[:]
    root.handlers[:] = []
    level = root.level
    try:
        log = tmp_path / "updater.log"
        assert updater.main(["--config", str(tmp_path / "u.json"), "--log-file", str(log)]) == 0
        for handler in root.handlers:
            handler.close()
    finally:
        root.handlers[:] = before
        root.setLevel(level)
    assert "ran u.json" in log.read_text(encoding="utf-8")
