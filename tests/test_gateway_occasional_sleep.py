"""Boxes switched on only occasionally (#130, owner 2026-11-19): never in the owner notice while down.

mac-david-nb, win-alex-nb and linux-imag-nb are notebooks that are off for days or weeks. The #118
after-hours rule (``may_sleep``) reports a box that has not been up since local midnight once it is
10:00, which fits a pokladna, not a notebook. ``machine set-sleep NAME occasional`` stores
``"occasional": true`` next to ``"may_sleep": true``; ``check`` then leaves the box out of
``fleet-down`` (and the plain per-box messages) however long it is down. Its real state stays in
``check``'s output and status file. A key store without the key reads exactly as before.

Real CLI, real files under tmp_path, real loopback machines, the fake airuleset with the
``--owner-notice`` flag; the clock is injected into ``run_check``.
"""

from __future__ import annotations

import pytest
from fleet_harness import FakeFleetMachine, closed_port_url, fresh_secret, write_private_json
from test_gateway_may_sleep import SleepCheck, _at, _cli, _stored
from test_gateway_owner_notice import _option

from remoteos.gateway import check as check_module
from remoteos.gateway import sleep_rule
from remoteos.gateway.keystore import KeystoreError, set_sleep
from remoteos.gateway.registry import MachineSourceError, parse_keystore

URL = "http://n.lan:8092/mcp"


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "devel" / "remoteos-mcp"
    (directory / ".git").mkdir(parents=True)
    return directory


@pytest.fixture
def with_flag(monkeypatch):
    monkeypatch.setenv("FAKE_NOTIFY_OWNER_NOTICE", "1")
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")


# -- the key-store shape -----------------------------------------------------------------------


def test_occasional_is_an_optional_bool_next_to_may_sleep_written_only_when_on():
    entries = parse_keystore(
        {
            "mac-david-nb": {"url": URL, "key": "k1", "may_sleep": True, "occasional": True},
            "win-pjzav-pokladna": {"url": URL, "key": "k2", "may_sleep": True, "occasional": False},
            "linux-cam2": {"url": URL, "key": "k3"},
        }
    )
    assert entries["mac-david-nb"].occasional is True and entries["mac-david-nb"].sleep == "occasional"
    assert entries["win-pjzav-pokladna"].occasional is False and entries["win-pjzav-pokladna"].sleep == "on"
    assert entries["linux-cam2"].occasional is False and entries["linux-cam2"].sleep == "off"
    assert entries["mac-david-nb"].to_json()["occasional"] is True
    assert entries["mac-david-nb"].to_json()["may_sleep"] is True
    # off = the key's absence: a key store that never used it is written exactly as before
    assert "occasional" not in entries["win-pjzav-pokladna"].to_json()
    assert "occasional" not in entries["linux-cam2"].to_json()


@pytest.mark.parametrize("bad", ["yes", 1, None, []])
def test_an_occasional_that_is_not_a_bool_is_refused(bad):
    with pytest.raises(MachineSourceError, match="occasional"):
        parse_keystore({"mac-david-nb": {"url": URL, "key": "k1", "may_sleep": True, "occasional": bad}})


@pytest.mark.parametrize("entry", [{"occasional": True}, {"may_sleep": False, "occasional": True}])
def test_occasional_without_may_sleep_is_refused_and_names_both_keys(entry):
    with pytest.raises(MachineSourceError, match="occasional.*may_sleep"):
        parse_keystore({"mac-david-nb": {"url": URL, "key": "k1", **entry}})


def test_set_sleep_occasional_and_back_keeps_url_key_org_and_repair(tmp_path):
    key = fresh_secret("k")
    recipe = {"ssh": "root@n.lan", "key_file": "/etc/remoteos-mcp/config.json", "sudo": True}
    write_private_json(
        tmp_path / "cfg" / "machines.json",
        {"mac-david-nb": {"url": URL, "key": key, "org": "nlmedia", "repair": recipe}},
    )

    occasional = _cli(tmp_path, "machine", "set-sleep", "mac-david-nb", "occasional")
    assert occasional.exit_code == 0, occasional.output
    assert "mac-david-nb: may be off for any length of time" in occasional.output
    assert _stored(tmp_path)["mac-david-nb"] == {
        "url": URL, "key": key, "org": "nlmedia", "may_sleep": True, "occasional": True, "repair": recipe,
    }
    again = _cli(tmp_path, "machine", "set-sleep", "mac-david-nb", "occasional")
    assert again.exit_code == 0 and "mac-david-nb: already occasional" in again.output

    on = _cli(tmp_path, "machine", "set-sleep", "mac-david-nb", "on")
    assert on.exit_code == 0, on.output
    assert "may sleep after hours" in on.output
    assert _stored(tmp_path)["mac-david-nb"]["may_sleep"] is True
    assert "occasional" not in _stored(tmp_path)["mac-david-nb"]

    _cli(tmp_path, "machine", "set-sleep", "mac-david-nb", "occasional")
    off = _cli(tmp_path, "machine", "set-sleep", "mac-david-nb", "off")
    assert off.exit_code == 0, off.output
    stored = _stored(tmp_path)["mac-david-nb"]
    assert "may_sleep" not in stored and "occasional" not in stored
    for result in (occasional, again, on, off):
        assert key not in result.output


def test_machine_list_shows_sleep_occasional(tmp_path):
    write_private_json(
        tmp_path / "cfg" / "machines.json",
        {
            "mac-david-nb": {"url": URL, "key": "k1", "org": "nlmedia", "may_sleep": True, "occasional": True},
            "win-pjzav-pokladna": {"url": URL, "key": "k2", "org": "slovnormal", "may_sleep": True},
        },
    )
    listing = _cli(tmp_path, "machine", "list", "--status-file", str(tmp_path / "status.json"))
    assert listing.exit_code == 0, listing.output
    lines = {line.split()[0]: line for line in listing.output.splitlines() if line.startswith("  ")}
    assert "sleep occasional" in lines["mac-david-nb"]
    assert "sleep on" in lines["win-pjzav-pokladna"]


# -- the rule (pure) -----------------------------------------------------------------------------


def test_an_occasional_box_down_for_weeks_is_excused_at_any_hour():
    weeks_ago = {"state": "unreachable", "last_ok": "2026-10-21T15:00:00+00:00"}
    never = {"state": "unreachable"}
    for moment in ("2026-11-19T02:00:00", "2026-11-19T09:00:00", "2026-11-19T12:00:00", "2026-11-19T22:50:00"):
        assert sleep_rule.excused(weeks_ago, _at(moment), occasional=True)
        assert sleep_rule.excused(never, _at(moment), occasional=True)
    # the after-hours rule itself is unchanged for the same record
    assert not sleep_rule.excused(weeks_ago, _at("2026-11-19T12:00:00"))
    assert not sleep_rule.excused({"state": "ok"}, _at("2026-11-19T12:00:00"), occasional=True)


# -- check, end to end ---------------------------------------------------------------------------


def test_an_occasional_notebook_down_for_weeks_stays_out_of_fleet_down_but_keeps_its_real_state(
    tmp_path, monkeypatch, project, with_flag
):
    check = SleepCheck(tmp_path, monkeypatch, project)
    check.store(
        {
            "mac-david-nb": {"url": closed_port_url(), "key": fresh_secret("k"), "may_sleep": True, "occasional": True},
            "linux-cam4": {"url": closed_port_url(), "key": fresh_secret("k")},
        }
    )
    with FakeFleetMachine(healthz=True) as gateway:
        for moment in ("2026-11-19T11:00:00", "2026-11-20T11:00:00", "2026-12-15T11:00:00"):  # noon, day after day
            report = check.run_at(gateway.base_url, _at(moment))
            record = report.status["machines"]["mac-david-nb"]
            assert record["state"] == "unreachable" and record["asleep"] is True and record["occasional"] is True
            body = check.down_item()
            assert body is not None and "linux-cam4: nedostupný" in body and "mac-david-nb" not in body


def test_switching_a_reported_box_to_occasional_takes_it_out_of_the_item(tmp_path, monkeypatch, project, with_flag):
    check = SleepCheck(tmp_path, monkeypatch, project)
    url, key = closed_port_url(), fresh_secret("k")
    check.store({"win-alex-nb": {"url": url, "key": key, "may_sleep": True}})
    with FakeFleetMachine(healthz=True) as gateway:
        check.run_at(gateway.base_url, _at("2026-11-19T11:00:00"))  # 12:00 local, not up today: reported
        assert "win-alex-nb: nedostupný" in check.down_item()
        check.store({"win-alex-nb": {"url": url, "key": key, "may_sleep": True, "occasional": True}})
        check.run_at(gateway.base_url, _at("2026-11-19T11:10:00"))
    assert check.down_item() is None
    clears = [_option(argv, "--clear-owner-notice") for argv in check.calls() if "--clear-owner-notice" in argv]
    assert clears == ["fleet-down"]


def test_an_occasional_notebook_switched_on_after_weeks_is_not_news(tmp_path, monkeypatch, project, with_flag):
    check = SleepCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    entry = {"key": key, "may_sleep": True, "occasional": True}
    with FakeFleetMachine(healthz=True) as gateway:
        check.store({"linux-imag-nb": {"url": closed_port_url(), **entry}})
        check.run_at(gateway.base_url, _at("2026-11-19T11:00:00"))
        check.run_at(gateway.base_url, _at("2026-12-01T11:00:00"))
        with FakeFleetMachine(accept_keys=(key,)) as notebook:
            check.store({"linux-imag-nb": {"url": notebook.url, **entry}})
            report = check.run_at(gateway.base_url, _at("2026-12-01T11:10:00"))
    record = report.status["machines"]["linux-imag-nb"]
    assert record["state"] == "ok" and record["notified"] is True and "asleep" not in record
    assert check.calls() == []  # nothing was sent while it was off, nor when it came back


def test_plain_notices_skip_an_occasional_box_too(tmp_path, monkeypatch):
    """An airuleset without --owner-notice: the per-box message is held back the same way."""
    check = SleepCheck(tmp_path, monkeypatch, tmp_path / "nowhere")
    check.store(
        {
            "mac-david-nb": {"url": closed_port_url(), "key": fresh_secret("k"), "may_sleep": True, "occasional": True},
            "linux-cam4": {"url": closed_port_url(), "key": fresh_secret("k")},
        }
    )
    with FakeFleetMachine(healthz=True) as gateway:
        check.run_at(gateway.base_url, _at("2026-11-19T11:00:00"))
        check.run_at(gateway.base_url, _at("2026-11-20T11:00:00"))
    [body] = check.notifications()
    assert "linux-cam4" in body and "mac-david-nb" not in body


def test_check_output_marks_an_occasional_box(tmp_path, monkeypatch, project, with_flag):
    check = SleepCheck(tmp_path, monkeypatch, project)
    check.store(
        {"mac-david-nb": {"url": closed_port_url(), "key": fresh_secret("k"), "may_sleep": True, "occasional": True}}
    )
    monkeypatch.setattr(check_module, "utc_now", lambda: _at("2026-11-19T11:00:00"))
    with FakeFleetMachine(healthz=True) as gateway:
        result = check.run(gateway.base_url)
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.startswith("  mac-david-nb"))
    assert "unreachable" in line and "asleep (occasional)" in line
    assert "asleep (not in the owner notice): mac-david-nb" in result.output


def test_a_box_reported_before_it_was_made_occasional_comes_back_without_news(
    tmp_path, monkeypatch, project, with_flag
):
    check = SleepCheck(tmp_path, monkeypatch, project)
    key = fresh_secret("k")
    with FakeFleetMachine(healthz=True) as gateway:
        check.store({"win-alex-nb": {"url": closed_port_url(), "key": key, "may_sleep": True}})
        check.run_at(gateway.base_url, _at("2026-11-19T11:00:00"))  # reported in fleet-down
        assert "win-alex-nb" in check.down_item()
        entry = {"key": key, "may_sleep": True, "occasional": True}
        check.store({"win-alex-nb": {"url": closed_port_url(), **entry}})
        check.run_at(gateway.base_url, _at("2026-11-19T11:10:00"))  # made occasional: item cleared
        with FakeFleetMachine(accept_keys=(key,)) as notebook:
            check.store({"win-alex-nb": {"url": notebook.url, **entry}})
            check.run_at(gateway.base_url, _at("2026-12-03T11:00:00"))  # switched on weeks later
    plain = [argv for argv in check.calls() if "--owner-notice" not in argv and "--clear-owner-notice" not in argv]
    assert plain == []  # no "running again" for a box the owner said may be off


def test_an_occasional_box_that_is_on_but_refuses_its_key_is_reported(tmp_path, monkeypatch, project, with_flag):
    """Only OFF (unreachable) is excused: a notebook that answers 401 is on and broken, which is news."""
    check = SleepCheck(tmp_path, monkeypatch, project)
    key, rotated = fresh_secret("k"), fresh_secret("k")
    with FakeFleetMachine(healthz=True) as gateway, FakeFleetMachine(accept_keys=(rotated,)) as notebook:
        check.store({"mac-david-nb": {"url": notebook.url, "key": key, "may_sleep": True, "occasional": True}})
        report = check.run_at(gateway.base_url, _at("2026-11-19T11:00:00"))
    record = report.status["machines"]["mac-david-nb"]
    assert record["state"] == "401" and "asleep" not in record and record["occasional"] is True
    assert "mac-david-nb: odmieta kľúč (401)" in check.down_item()


def test_an_occasional_box_in_error_follows_the_after_hours_rule():
    error = {"state": "error", "last_ok": "2026-10-21T15:00:00+00:00"}
    assert sleep_rule.excused(error, _at("2026-11-19T07:00:00"), occasional=True)  # 08:00 local: before ten
    assert not sleep_rule.excused(error, _at("2026-11-19T11:00:00"), occasional=True)


def test_set_sleep_refuses_a_mode_that_is_not_off_on_or_occasional(tmp_path):
    write_private_json(tmp_path / "cfg" / "machines.json", {"mac-david-nb": {"url": URL, "key": "k1"}})
    before = _stored(tmp_path)
    for bad in (True, "yes"):
        with pytest.raises(KeystoreError, match="sleep mode"):
            set_sleep(tmp_path / "cfg" / "machines.json", "mac-david-nb", bad)
    assert _stored(tmp_path) == before
