"""Boxes that sleep after hours (#118, owner 2026-11-17): reported only when they did not come up.

The pokladne close at 17-18:30 and are switched off overnight; they must not put every box into the
``fleet-down`` owner item each evening. A key-store machine with ``may_sleep`` (set by
``machine set-sleep NAME on|off``, shown by ``machine list``) that is not ok is left out of the
owner notice, unless it has not been ok at ANY moment since local midnight (Europe/Bratislava) and
the local time is 10:00 or later; then it is reported like any other box. A box without the flag is
reported at once, as before. ``check``'s output and status file still show every box's real state.

Real CLI, real files under tmp_path, real loopback machines, the fake airuleset with the
``--owner-notice`` flag; the clock is injected into ``run_check``.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, closed_port_url, fresh_secret, write_private_json
from test_gateway_owner_notice import NoticeCheck, _option

from remoteos.gateway import check as check_module
from remoteos.gateway import cli as gateway_cli
from remoteos.gateway import sleep_rule
from remoteos.gateway.check import run_check
from remoteos.gateway.owner_notice import airuleset_notifier
from remoteos.gateway.registry import MachineSourceError, parse_keystore

UTC = dt.timezone.utc


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "devel" / "remoteos-mcp"
    (directory / ".git").mkdir(parents=True)
    return directory


@pytest.fixture
def with_flag(monkeypatch):
    monkeypatch.setenv("FAKE_NOTIFY_OWNER_NOTICE", "1")
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")


def _cli(tmp_path, *args):
    cfg = tmp_path / "cfg"
    paths = ["--registry", str(cfg / "registry.json"), "--machines", str(cfg / "machines.json")]
    return CliRunner().invoke(gateway_cli.main, [*paths, *args])


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "cfg" / "machines.json").read_text(encoding="utf-8"))


def _at(text: str) -> dt.datetime:
    """A moment given in UTC (Bratislava is UTC+1 in winter, UTC+2 in summer)."""
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


class SleepCheck(NoticeCheck):
    def run_at(self, gateway_url: str, moment: dt.datetime):
        notifier = airuleset_notifier(self.airuleset, "zbynek", project_dir=self.project, notices=self.notices)
        return run_check(
            self.keystore, self.status, gateway_url=gateway_url, notifier=notifier, now=lambda: moment
        )

    def down_item(self) -> str | None:
        return self.u().get("fleet-down")


# -- the key-store flag ------------------------------------------------------------------------


def test_may_sleep_is_a_bool_in_the_key_store_written_only_when_on():
    entries = parse_keystore(
        {
            "win-pjzav-pokladna": {"url": "http://p.lan:8092/mcp", "key": "k1", "may_sleep": True},
            "linux-cam1": {"url": "http://c.lan:8092/mcp", "key": "k2", "may_sleep": False},
            "linux-cam2": {"url": "http://d.lan:8092/mcp", "key": "k3"},
        }
    )
    assert entries["win-pjzav-pokladna"].may_sleep is True
    assert entries["linux-cam1"].may_sleep is False and entries["linux-cam2"].may_sleep is False
    assert entries["win-pjzav-pokladna"].to_json()["may_sleep"] is True
    assert "may_sleep" not in entries["linux-cam1"].to_json()  # off = the shape every older gateway reads


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_a_may_sleep_that_is_not_a_bool_is_refused(bad):
    with pytest.raises(MachineSourceError, match="may_sleep"):
        parse_keystore({"linux-a": {"url": "http://a.lan:8092/mcp", "key": "k1", "may_sleep": bad}})


def test_set_sleep_turns_the_flag_on_and_off_and_keeps_url_key_org_and_repair(tmp_path):
    key = fresh_secret("k")
    recipe = {"ssh": "root@p.lan", "key_file": "/etc/remoteos-mcp/config.json", "sudo": True}
    write_private_json(
        tmp_path / "cfg" / "machines.json",
        {"win-pjzav-pokladna": {"url": "http://p.lan:8092/mcp", "key": key, "org": "slovnormal", "repair": recipe}},
    )

    on = _cli(tmp_path, "machine", "set-sleep", "win-pjzav-pokladna", "on")
    assert on.exit_code == 0, on.output
    assert "win-pjzav-pokladna: may sleep after hours" in on.output
    assert _stored(tmp_path)["win-pjzav-pokladna"] == {
        "url": "http://p.lan:8092/mcp", "key": key, "org": "slovnormal", "may_sleep": True, "repair": recipe,
    }
    again = _cli(tmp_path, "machine", "set-sleep", "win-pjzav-pokladna", "on")
    assert again.exit_code == 0 and "already" in again.output

    off = _cli(tmp_path, "machine", "set-sleep", "win-pjzav-pokladna", "off")
    assert off.exit_code == 0, off.output
    assert "may_sleep" not in _stored(tmp_path)["win-pjzav-pokladna"]
    for result in (on, again, off):
        assert key not in result.output


def test_set_sleep_refuses_an_unknown_machine_and_a_value_other_than_on_or_off(tmp_path):
    write_private_json(tmp_path / "cfg" / "machines.json", {"linux-a": {"url": "http://a.lan:8092/mcp", "key": "k1"}})
    before = _stored(tmp_path)

    unknown = _cli(tmp_path, "machine", "set-sleep", "linux-b", "on")
    bad = _cli(tmp_path, "machine", "set-sleep", "linux-a", "yes")

    assert unknown.exit_code != 0 and "linux-b" in unknown.output
    assert bad.exit_code != 0
    assert _stored(tmp_path) == before


def test_machine_list_shows_the_sleep_column(tmp_path):
    write_private_json(
        tmp_path / "cfg" / "machines.json",
        {
            "win-pjzav-pokladna": {"url": "http://p.lan:8092/mcp", "key": "k1", "org": "slovnormal", "may_sleep": True},
            "linux-cam1": {"url": "http://c.lan:8092/mcp", "key": "k2", "org": "nlmedia"},
        },
    )
    listing = _cli(tmp_path, "machine", "list", "--status-file", str(tmp_path / "status.json"))
    assert listing.exit_code == 0, listing.output
    lines = {line.split()[0]: line for line in listing.output.splitlines() if line.startswith("  ")}
    assert "sleep on" in lines["win-pjzav-pokladna"]
    assert "sleep off" in lines["linux-cam1"]


# -- the rule (pure) -----------------------------------------------------------------------------


def _record(state: str, last_ok: str | None = None) -> dict:
    record = {"state": state}
    if last_ok is not None:
        record["last_ok"] = last_ok
    return record


def test_an_ok_box_is_never_asleep():
    assert not sleep_rule.excused(_record("ok"), _at("2026-11-17T20:00:00"))


def test_before_ten_local_a_box_that_has_not_come_up_yet_is_excused():
    # 08:59 local (UTC+1) on a day the box has not been ok yet, and it never was
    assert sleep_rule.excused(_record("unreachable"), _at("2026-11-17T07:59:00"))
    assert sleep_rule.excused(_record("unreachable", "2026-11-16T15:00:00+00:00"), _at("2026-11-17T08:59:00"))


def test_from_ten_local_a_box_not_ok_since_midnight_is_reported():
    assert not sleep_rule.excused(_record("unreachable", "2026-11-16T15:00:00+00:00"), _at("2026-11-17T09:00:00"))
    assert not sleep_rule.excused(_record("unreachable"), _at("2026-11-17T12:00:00"))


def test_a_box_ok_earlier_today_sleeps_without_a_report_all_evening():
    # ok at 07:30 local, closed after 18:00 local, still asleep at 23:50 local
    assert sleep_rule.excused(_record("unreachable", "2026-11-17T06:30:00+00:00"), _at("2026-11-17T22:50:00"))


def test_local_midnight_is_bratislava_time_across_both_dst_changes():
    # 2026-10-25: CEST -> CET at 03:00 local. Local midnight = 2026-10-24T22:00Z (still CEST).
    moment = _at("2026-10-25T10:30:00")  # 11:30 CET
    assert sleep_rule.local_midnight(moment) == _at("2026-10-24T22:00:00")
    assert sleep_rule.excused(_record("unreachable", "2026-10-24T22:30:00+00:00"), moment)  # 00:30 local, today
    assert not sleep_rule.excused(_record("unreachable", "2026-10-24T21:30:00+00:00"), moment)  # 23:30 yesterday
    # 2026-03-29: CET -> CEST at 02:00 local. Local midnight = 2026-03-28T23:00Z (still CET).
    spring = _at("2026-03-29T09:30:00")  # 11:30 CEST
    assert sleep_rule.local_midnight(spring) == _at("2026-03-28T23:00:00")
    assert sleep_rule.excused(_record("unreachable", "2026-03-28T23:10:00+00:00"), spring)
    assert not sleep_rule.excused(_record("unreachable", "2026-03-28T22:50:00+00:00"), spring)
    # 09:30 local in summer is 07:30Z: before ten, excused whatever last_ok says
    assert sleep_rule.excused(_record("unreachable"), _at("2026-06-01T07:30:00"))
    assert not sleep_rule.excused(_record("unreachable"), _at("2026-06-01T08:00:00"))


@pytest.mark.parametrize("bad", ["yesterday", 12, "2026-11-17T06:30:00"])  # no offset = not what check writes
def test_a_hand_edited_last_ok_counts_as_never_ok(bad):
    record = {"state": "unreachable", "last_ok": bad}
    assert not sleep_rule.excused(record, _at("2026-11-17T12:00:00"))
    assert sleep_rule.excused(record, _at("2026-11-17T07:00:00"))


# -- check, end to end ---------------------------------------------------------------------------


def test_asleep_tonight_after_an_ok_day_is_silent_and_an_unflagged_box_is_reported_at_once(
    tmp_path, monkeypatch, project, with_flag
):
    check = SleepCheck(tmp_path, monkeypatch, project)
    pokladna_key, cam_key = fresh_secret("k"), fresh_secret("k")
    with FakeFleetMachine(healthz=True) as gateway:
        with FakeFleetMachine(accept_keys=(pokladna_key,)) as pokladna, FakeFleetMachine(accept_keys=(cam_key,)) as cam:
            check.store(
                {
                    "win-pjzav-pokladna": {"url": pokladna.url, "key": pokladna_key, "may_sleep": True},
                    "linux-cam1": {"url": cam.url, "key": cam_key},
                }
            )
            check.run_at(gateway.base_url, _at("2026-11-17T13:00:00"))  # 14:00 local: both ok
            assert check.state()["machines"]["win-pjzav-pokladna"]["last_ok"].startswith("2026-11-17T13:00")
        # both switched off after 18:00 local
        report = check.run_at(gateway.base_url, _at("2026-11-17T18:30:00"))

    machines = report.status["machines"]
    assert machines["win-pjzav-pokladna"]["state"] == "unreachable"  # its real state stays visible
    assert machines["win-pjzav-pokladna"]["asleep"] is True
    assert "asleep" not in machines["linux-cam1"]
    body = check.down_item()
    assert body is not None and "linux-cam1: nedostupný" in body  # unflagged: at once
    assert "win-pjzav-pokladna" not in body and "(1)" in body


def test_a_sleeping_box_that_never_came_up_today_is_reported_from_ten_and_its_wake_up_is_news(
    tmp_path, monkeypatch, project, with_flag
):
    check = SleepCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    with FakeFleetMachine(healthz=True) as gateway:
        with FakeFleetMachine(accept_keys=(key,)) as pokladna:
            check.store({"win-pjzav-pokladna": {"url": pokladna.url, "key": key, "may_sleep": True}})
            check.run_at(gateway.base_url, _at("2026-11-16T15:00:00"))  # ok yesterday afternoon
        check.run_at(gateway.base_url, _at("2026-11-16T19:00:00"))  # closed: asleep, silent
        assert check.down_item() is None
        check.run_at(gateway.base_url, _at("2026-11-17T08:50:00"))  # 09:50 local: not yet
        assert check.down_item() is None and check.calls() == []

        report = check.run_at(gateway.base_url, _at("2026-11-17T09:00:00"))  # 10:00 local, never up today
        record = report.status["machines"]["win-pjzav-pokladna"]
        assert "asleep" not in record and record["notified"] is True
        assert "win-pjzav-pokladna: nedostupný" in check.down_item()

        with FakeFleetMachine(accept_keys=(key,)) as late:  # switched on late
            check.store({"win-pjzav-pokladna": {"url": late.url, "key": key, "may_sleep": True}})
            check.run_at(gateway.base_url, _at("2026-11-17T09:30:00"))
    assert check.down_item() is None  # the item is cleared
    plain = [argv for argv in check.calls() if "--owner-notice" not in argv and "--clear-owner-notice" not in argv]
    assert len(plain) == 1 and "win-pjzav-pokladna" in _option(plain[0], "--body")  # reported, so back is news


def test_waking_up_after_a_silent_night_is_not_news(tmp_path, monkeypatch, project, with_flag):
    check = SleepCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    with FakeFleetMachine(healthz=True) as gateway:
        with FakeFleetMachine(accept_keys=(key,)) as pokladna:
            check.store({"win-pjzav-pokladna": {"url": pokladna.url, "key": key, "may_sleep": True}})
            check.run_at(gateway.base_url, _at("2026-11-16T15:00:00"))
        check.run_at(gateway.base_url, _at("2026-11-16T19:00:00"))  # asleep
        with FakeFleetMachine(accept_keys=(key,)) as morning:
            check.store({"win-pjzav-pokladna": {"url": morning.url, "key": key, "may_sleep": True}})
            report = check.run_at(gateway.base_url, _at("2026-11-17T06:30:00"))  # 07:30 local: opened

    assert report.status["machines"]["win-pjzav-pokladna"]["state"] == "ok"
    assert report.status["machines"]["win-pjzav-pokladna"]["notified"] is True
    assert check.calls() == []  # neither the evening nor the morning sent anything


def test_without_the_flag_a_down_box_is_reported_even_before_ten(tmp_path, monkeypatch, project, with_flag):
    check = SleepCheck(tmp_path, monkeypatch, project)
    check.store({"linux-cam4": {"url": closed_port_url(), "key": fresh_secret("k")}})
    with FakeFleetMachine(healthz=True) as gateway:
        check.run_at(gateway.base_url, _at("2026-11-17T02:00:00"))  # 03:00 local
    assert "linux-cam4: nedostupný" in check.down_item()


def test_plain_notices_skip_a_sleeping_box_too(tmp_path, monkeypatch):
    """An airuleset without --owner-notice: the per-box message is held back the same way."""
    check = SleepCheck(tmp_path, monkeypatch, tmp_path / "nowhere")
    check.store(
        {
            "win-pjzav-pokladna": {"url": closed_port_url(), "key": fresh_secret("k"), "may_sleep": True},
            "linux-cam4": {"url": closed_port_url(), "key": fresh_secret("k")},
        }
    )
    with FakeFleetMachine(healthz=True) as gateway:
        check.run_at(gateway.base_url, _at("2026-11-17T07:00:00"))  # 08:00 local
        [body] = check.notifications()
        assert "linux-cam4" in body and "win-pjzav-pokladna" not in body
        check.run_at(gateway.base_url, _at("2026-11-17T09:10:00"))  # 10:10 local: now it is reported
    later = check.notifications()[1:]
    assert len(later) == 1 and "win-pjzav-pokladna" in later[0] and "linux-cam4" not in later[0]


def test_check_output_shows_the_real_state_and_marks_the_sleeping_box(tmp_path, monkeypatch, project, with_flag):
    check = SleepCheck(tmp_path, monkeypatch, project)
    check.store({"win-pjzav-pokladna": {"url": closed_port_url(), "key": fresh_secret("k"), "may_sleep": True}})
    outputs = {}
    with FakeFleetMachine(healthz=True) as gateway:
        for label, moment in (("night", "2026-11-17T02:00:00"), ("noon", "2026-11-17T11:00:00")):
            monkeypatch.setattr(check_module, "utc_now", lambda moment=moment: _at(moment))
            result = check.run(gateway.base_url)
            assert result.exit_code == 0, result.output
            outputs[label] = next(line for line in result.output.splitlines() if line.startswith("  win-pjzav"))
    assert "unreachable" in outputs["night"] and "asleep (may_sleep)" in outputs["night"]
    assert "unreachable" in outputs["noon"] and "asleep" not in outputs["noon"]
    assert check.state()["machines"]["win-pjzav-pokladna"]["may_sleep"] is True
