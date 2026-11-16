"""Owner notices reach the footer U, one per open condition, and are cleared when it is gone (#102).

airuleset #1300 added ``notify --owner-notice <slug> --cwd <project>`` (a suppressed send becomes one
U item, the same slug replaces it, exit 1 = not recorded) and ``--clear-owner-notice``. The fake
``airuleset.py`` (``fleet_harness``) is a real script run as a real subprocess: with
``FAKE_NOTIFY_OWNER_NOTICE=1`` its ``notify --help`` lists the flags and it keeps a fake U; without
it the flags are an argparse error, like the airuleset installed before #1300.
"""

from __future__ import annotations

import json
import logging

import pytest
from fleet_harness import (
    FakeFleetMachine,
    closed_port_url,
    footer_u,
    fresh_secret,
    install_fake_airuleset,
    install_fake_remote_tools,
    notify_calls,
    remote_calls,
)
from test_gateway_check import Check
from test_gateway_live_sessions import LiveSession, Run, fake_tmux  # noqa: F401 (fixture)

from remoteos.gateway import check as check_module
from remoteos.gateway.owner_notice import SLUG_RE, airuleset_notifier, slug

OK_STATUS = {"gateway": {"state": "ok"}, "machines": {}}


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "devel" / "remoteos-mcp"
    (directory / ".git").mkdir(parents=True)
    return directory


@pytest.fixture
def with_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_NOTIFY_OWNER_NOTICE", "1")
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")  # owner zbynek: the policy posts nothing


class NoticeCheck(Check):
    def __init__(self, tmp_path, monkeypatch, project) -> None:
        super().__init__(tmp_path, monkeypatch)
        self.project = project
        self.notices = tmp_path / "state" / "owner-notices.json"

    def run(self, gateway_url: str, *extra: str):
        flags = ("--notify-project-dir", str(self.project), "--notices-file", str(self.notices))
        return super().run(gateway_url, *flags, *extra)

    def u(self) -> dict[str, str]:
        return {key.split("|", 1)[1]: body for key, body in footer_u(self.airuleset.parent).items()}

    def book(self) -> dict:
        return json.loads(self.notices.read_text(encoding="utf-8"))["notices"] if self.notices.exists() else {}

    def calls(self) -> list[list[str]]:
        return notify_calls(self.notify_log)


def _option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def test_a_401_box_is_one_u_item_and_its_recovery_clears_it(tmp_path, monkeypatch, project, with_flag):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    key, rotated = fresh_secret("machinekey"), fresh_secret("rotatedkey")
    with FakeFleetMachine(accept_keys=(key,)) as box, FakeFleetMachine(healthz=True) as gateway:
        check.store({"linux-cam2": {"url": box.url, "key": key}})
        assert check.run(gateway.base_url).exit_code == 0
        assert check.calls() == []  # first seen ok: not news, nothing in U

        box.accept_keys = [rotated]
        result = check.run(gateway.base_url)
        assert result.exit_code == 0, result.output
        [argv] = check.calls()
        assert _option(argv, "--owner-notice") == "state-linux-cam2"
        assert _option(argv, "--cwd") == str(project)  # explicit: the unit runs in $HOME
        assert set(check.u()) == {"state-linux-cam2"} and "401" in check.u()["state-linux-cam2"]
        assert set(check.book()) == {"state-linux-cam2"}
        assert check.state()["machines"]["linux-cam2"]["notified"] is True

        assert check.run(gateway.base_url).exit_code == 0  # still 401: nothing new, nothing cleared
        assert len(check.calls()) == 1 and set(check.u()) == {"state-linux-cam2"}

        box.accept_keys = [key]  # back
        assert check.run(gateway.base_url).exit_code == 0

    recovery, clear = check.calls()[1:]
    assert _option(recovery, "--owner-notice") is None  # "it works again" is news, not an open condition
    assert "linux-cam2" in _option(recovery, "--body")
    assert _option(clear, "--clear-owner-notice") == "state-linux-cam2" and _option(clear, "--cwd") == str(project)
    assert check.u() == {} and check.book() == {}
    for argv in check.calls():
        assert key not in json.dumps(argv) and rotated not in json.dumps(argv)


def test_each_box_is_its_own_item_and_the_flag_is_probed_once_per_run(tmp_path, monkeypatch, project, with_flag):
    help_log = tmp_path / "help.log"
    monkeypatch.setenv("FAKE_HELP_LOG", str(help_log))
    check = NoticeCheck(tmp_path, monkeypatch, project)
    check.store({name: {"url": closed_port_url(), "key": fresh_secret("k")} for name in ("linux-a", "linux-b")})
    with FakeFleetMachine(healthz=True) as gateway:
        result = check.run(gateway.base_url)

    assert result.exit_code == 0, result.output
    assert sorted(_option(argv, "--owner-notice") for argv in check.calls()) == ["state-linux-a", "state-linux-b"]
    assert "linux-b" not in check.u()["state-linux-a"] and "linux-a" not in check.u()["state-linux-b"]
    assert help_log.read_text(encoding="utf-8").count("help") == 1


def test_a_notice_airuleset_did_not_record_is_not_delivered_and_is_retried(tmp_path, monkeypatch, project, with_flag):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    check.store({"linux-a": {"url": closed_port_url(), "key": fresh_secret("k")}})
    monkeypatch.setenv("FAKE_RECORD_EXIT", "1")
    with FakeFleetMachine(healthz=True) as gateway:
        first = check.run(gateway.base_url)
        assert "did NOT go out" in first.output
        assert check.state()["machines"]["linux-a"]["notified"] is False
        assert check.u() == {} and check.book() == {}

        monkeypatch.delenv("FAKE_RECORD_EXIT")
        assert check.run(gateway.base_url).exit_code == 0

    assert check.state()["machines"]["linux-a"]["notified"] is True
    assert set(check.u()) == {"state-linux-a"} and set(check.book()) == {"state-linux-a"}


def test_a_clear_that_failed_stays_recorded_and_is_retried(tmp_path, monkeypatch, project, with_flag):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    with FakeFleetMachine(accept_keys=("other",)) as box, FakeFleetMachine(healthz=True) as gateway:
        check.store({"linux-a": {"url": box.url, "key": key}})
        assert check.run(gateway.base_url).exit_code == 0
        assert set(check.u()) == {"state-linux-a"}

        box.accept_keys = [key]
        monkeypatch.setenv("FAKE_CLEAR_EXIT", "1")
        assert check.run(gateway.base_url).exit_code == 0  # a failed clear never fails the run
        assert set(check.u()) == {"state-linux-a"} and set(check.book()) == {"state-linux-a"}

        monkeypatch.delenv("FAKE_CLEAR_EXIT")
        assert check.run(gateway.base_url).exit_code == 0

    clears = [argv for argv in check.calls() if "--clear-owner-notice" in argv]
    assert len(clears) == 2
    assert check.u() == {} and check.book() == {}


def test_without_the_flag_the_notices_are_today_s_grouped_ones(tmp_path, monkeypatch, project):
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")
    check = NoticeCheck(tmp_path, monkeypatch, project)
    check.store({name: {"url": closed_port_url(), "key": fresh_secret("k")} for name in ("linux-a", "linux-b")})
    with FakeFleetMachine(healthz=True) as gateway:
        result = check.run(gateway.base_url)

    assert result.exit_code == 0, result.output
    [argv] = check.calls()  # ONE message for both, as before #102
    assert "--owner-notice" not in argv and "--cwd" not in argv
    assert "linux-a" in argv[2] and "linux-b" in argv[2]
    assert check.state()["machines"]["linux-a"]["notified"] is True  # suppressed still counts
    assert not check.notices.exists()


@pytest.mark.parametrize("where", ["no-git", "home"])
def test_an_unusable_project_dir_falls_back_to_plain_notices(tmp_path, monkeypatch, with_flag, caplog, where):
    caplog.set_level(logging.ERROR, logger="remoteos.gateway.owner_notice")
    directory = tmp_path / "plain-dir"
    directory.mkdir()
    if where == "home":
        (directory / ".git").mkdir()
        monkeypatch.setenv("HOME", str(directory))
    check = NoticeCheck(tmp_path, monkeypatch, directory)
    check.store({"linux-a": {"url": closed_port_url(), "key": fresh_secret("k")}})
    with FakeFleetMachine(healthz=True) as gateway:
        assert check.run(gateway.base_url).exit_code == 0

    [argv] = check.calls()
    assert "--owner-notice" not in argv  # never an exit-1 send that could never be delivered
    assert "--notify-project-dir unusable" in caplog.text


def test_no_admin_and_a_down_gateway_have_their_own_items(tmp_path, monkeypatch, project, with_flag):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    with FakeFleetMachine(accept_keys=(key,), version="0.7.0.dev56", admin=False) as box:
        check.store({"win-foh": {"url": box.url, "key": key}})
        assert check.run(closed_port_url()).exit_code == 0  # the gateway does not answer
        assert set(check.u()) == {"no-admin-win-foh", "gateway-health"}

        box.admin = True
        with FakeFleetMachine(healthz=True) as gateway:
            assert check.run(gateway.base_url).exit_code == 0

    assert check.u() == {} and check.book() == {}


def test_a_check_that_cannot_run_is_an_item_until_it_runs_again(tmp_path, monkeypatch, project, with_flag):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    check.keystore.parent.mkdir(parents=True)
    check.keystore.write_text("{not json", encoding="utf-8")
    with FakeFleetMachine(healthz=True) as gateway:
        assert check.run(gateway.base_url).exit_code != 0
        assert set(check.u()) == {"check-failed"} and "NEBEŽÍ" in check.u()["check-failed"]

        check.store({})
        result = check.run(gateway.base_url)

    assert result.exit_code == 0, result.output
    recovery = [argv for argv in check.calls() if "opäť beží" in (_option(argv, "--body") or "")]
    assert recovery and _option(recovery[0], "--owner-notice") is None
    assert check.u() == {} and check.book() == {}


def test_a_recorded_notice_for_a_box_that_left_the_key_store_is_cleared(tmp_path, monkeypatch, project, with_flag):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    check.store({"linux-a": {"url": closed_port_url(), "key": fresh_secret("k")}})
    with FakeFleetMachine(healthz=True) as gateway:
        check.run(gateway.base_url)
        assert set(check.u()) == {"state-linux-a"}
        check.store({})
        assert check.run(gateway.base_url).exit_code == 0
    assert check.u() == {}


@pytest.mark.parametrize("name", ["linux-cam2", "Win-PZ", "linux-" + "x" * 80, "a_b"])
def test_every_slug_fits_airuleset_and_two_names_never_share_one(name):
    made = slug("state", name)
    assert SLUG_RE.fullmatch(made)
    assert made != slug("state", name.swapcase())
    if name == "linux-cam2":
        assert made == "state-linux-cam2"


# -- restart notices: recorded by sync-configs, cleared by check once the sessions are gone ---------


def _notifier(run: Run, project, notices):
    return airuleset_notifier(run.airuleset, "zbynek", project_dir=project, notices=notices)


@pytest.mark.parametrize("host", ["dev1", "dev2"])
def test_a_restart_notice_stays_until_its_sessions_are_gone(
    tmp_path,
    monkeypatch,
    fake_tmux,  # noqa: F811 (the imported fixture)
    project,
    with_flag,
    host,  # noqa: F811 (fixture)
):
    run = Run(tmp_path, monkeypatch)
    remote_log = install_fake_remote_tools(tmp_path / "bin", monkeypatch)
    run.manifest(host=host)
    notices = tmp_path / "state" / "owner-notices.json"
    flags = ("--notify-project-dir", str(project), "--notices-file", str(notices))
    session = LiveSession(run.tmp, run.project, "%7")
    try:
        result = run.sync(*flags)
        assert result.exit_code == 0, result.output
        [argv] = notify_calls(run.log)
        assert _option(argv, "--owner-notice") == "restart-camera-box"
        assert "main:2" in footer_u(run.airuleset.parent)[f"{project}|restart-camera-box"]
        [recorded] = json.loads(notices.read_text(encoding="utf-8"))["notices"].values()
        assert recorded["restart"]["host"] == host
        [noted] = recorded["restart"]["sessions"]
        assert noted["pid"] == session.process.pid and isinstance(noted["start"], int)

        check_module._settle(_notifier(run, project, notices), OK_STATUS, "dev1")
        assert "--clear-owner-notice" not in json.dumps(notify_calls(run.log))  # still running
    finally:
        session.__exit__()

    check_module._settle(_notifier(run, project, notices), OK_STATUS, "dev1")
    assert footer_u(run.airuleset.parent) == {}
    assert json.loads(notices.read_text(encoding="utf-8"))["notices"] == {}
    went_over_ssh = any("--alive" in json.dumps(call) for call in remote_calls(remote_log))
    assert went_over_ssh is (host == "dev2")


def test_a_restart_that_cannot_be_checked_keeps_its_notice(tmp_path, monkeypatch, project, with_flag):
    install_fake_remote_tools(tmp_path / "bin", monkeypatch)
    monkeypatch.setenv("FAKE_SSH_DENY", "1")
    script, _log = install_fake_airuleset(tmp_path / "airuleset", monkeypatch)
    notifier = airuleset_notifier(script, "zbynek", project_dir=project, notices=tmp_path / "owner-notices.json")
    entry = {"restart": {"host": "dev2", "account": "a", "sessions": [{"pid": 1, "start": 2}]}}
    with notifier.book.locked():
        notifier.book.add("restart-x", entry)
    check_module._settle(notifier, OK_STATUS, "dev1")
    assert set(notifier.book.read()) == {"restart-x"}  # unknown this run: kept, never cleared blind


def test_a_dry_run_sync_records_nothing(tmp_path, monkeypatch, fake_tmux, project, with_flag):  # noqa: F811
    run = Run(tmp_path, monkeypatch)
    run.manifest()
    notices = tmp_path / "state" / "owner-notices.json"
    with LiveSession(run.tmp, run.project, "%7"):
        result = run.sync("--dry-run", "--notify-project-dir", str(project), "--notices-file", str(notices))
    assert result.exit_code == 0, result.output
    assert notify_calls(run.log) == [] and not notices.exists()


def test_only_the_same_claude_process_counts_as_still_running(tmp_path):
    from test_gateway_live_sessions import FakeProc

    from remoteos.gateway import session_scan

    proc = FakeProc(tmp_path / "proc")
    proc.add(100, "claude", tmp_path)  # start 1000
    proc.add(101, "bash", tmp_path)  # the pid was reused by something else
    pairs = ["100:1000", "100:999", "101:1010", "102:1020", "100:None", "x:1"]
    assert session_scan.alive(pairs, proc_root=str(proc.root)) == [100, 100]
    assert session_scan.alive(["100:999"], proc_root=str(proc.root)) == []  # same pid, another start: restarted


def test_conditions_announced_before_the_flag_existed_are_put_into_u_once(tmp_path, monkeypatch, project):
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")
    check = NoticeCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    with FakeFleetMachine(accept_keys=("other",), version="0.7.0.dev56", admin=False) as box:
        check.store({"win-foh": {"url": box.url, "key": key}})
        with FakeFleetMachine(healthz=True) as gateway:
            assert check.run(gateway.base_url).exit_code == 0  # the old airuleset: plain, nothing in U
            assert len(check.calls()) == 2 and check.u() == {}

            monkeypatch.setenv("FAKE_NOTIFY_OWNER_NOTICE", "1")  # airuleset upgraded
            assert check.run(gateway.base_url).exit_code == 0
            assert set(check.u()) == {"state-win-foh", "no-admin-win-foh"}
            assert check.run(gateway.base_url).exit_code == 0

    assert len(check.calls()) == 4  # re-sent once, never again


def test_a_notices_file_that_cannot_be_locked_sends_plain_and_never_fails_the_run(
    tmp_path, monkeypatch, project, with_flag
):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    locked_out = tmp_path / "read-only"
    locked_out.mkdir(mode=0o500)
    check.notices = locked_out / "owner-notices.json"
    check.store({"linux-a": {"url": closed_port_url(), "key": fresh_secret("k")}})
    with FakeFleetMachine(healthz=True) as gateway:
        result = check.run(gateway.base_url)

    assert result.exit_code == 0, result.output
    [argv] = check.calls()
    assert "--owner-notice" not in argv  # in U it could never be cleared
    assert check.state()["machines"]["linux-a"]["notified"] is True


def test_a_book_that_cannot_be_written_never_makes_a_notice_go_out_every_run(
    tmp_path, monkeypatch, project, with_flag
):
    check = NoticeCheck(tmp_path, monkeypatch, project)
    full = tmp_path / "full-disk"
    full.mkdir()
    check.notices = full / "owner-notices.json"
    (full / "owner-notices.json.lock").write_text("", encoding="utf-8")  # the lock opens, the write cannot
    full.chmod(0o500)
    check.store({"linux-a": {"url": closed_port_url(), "key": fresh_secret("k")}})
    with FakeFleetMachine(healthz=True) as gateway:
        for _ in range(3):
            assert check.run(gateway.base_url).exit_code == 0

    [argv] = check.calls()  # recorded in U once, not re-sent by the one-time reopen
    assert _option(argv, "--owner-notice") == "state-linux-a"


def test_settle_never_clears_a_notice_re_sent_while_it_was_judged(tmp_path, monkeypatch, project, with_flag):
    script, log = install_fake_airuleset(tmp_path / "airuleset", monkeypatch)
    notifier = airuleset_notifier(script, "zbynek", project_dir=project, notices=tmp_path / "owner-notices.json")
    assert notifier("old text", "state-linux-a")

    def re_sent_meanwhile(name: str, entry: dict) -> bool:
        assert notifier("new text", name)  # sync/check re-recorded it after settle read the book
        return False

    notifier.settle(re_sent_meanwhile)
    assert "--clear-owner-notice" not in json.dumps(notify_calls(log))
    assert set(notifier.book.read()) == {"state-linux-a"}


def test_settle_waits_for_a_send_that_holds_the_book(tmp_path, monkeypatch, project, with_flag):
    import threading

    script, log = install_fake_airuleset(tmp_path / "airuleset", monkeypatch)
    notifier = airuleset_notifier(script, "zbynek", project_dir=project, notices=tmp_path / "owner-notices.json")
    assert notifier("text", "state-linux-a")
    holder = airuleset_notifier(script, "zbynek", project_dir=project, notices=tmp_path / "owner-notices.json")
    with holder.book.locked():  # another process mid-send
        settling = threading.Thread(target=notifier.settle, args=(lambda name, entry: False,))
        settling.start()
        settling.join(timeout=2)
        assert settling.is_alive()  # blocked on the lock
        assert "--clear-owner-notice" not in json.dumps(notify_calls(log))
    settling.join(timeout=30)
    assert not settling.is_alive()
    assert notifier.book.read() == {}


@pytest.mark.parametrize(("host", "account"), [("dev2", "-oProxyCommand=touch"), ("-oProxyCommand=x", "newlevel")])
def test_a_hand_edited_restart_record_never_becomes_an_ssh_option(tmp_path, monkeypatch, host, account):
    from remoteos.gateway import live_sessions

    log = install_fake_remote_tools(tmp_path / "bin", monkeypatch)
    entry = {"restart": {"host": host, "account": account, "sessions": [{"pid": 1, "start": 2}]}}
    assert live_sessions.still_running(entry, "dev1") is False  # done: cleared, never run
    assert remote_calls(log) == []
