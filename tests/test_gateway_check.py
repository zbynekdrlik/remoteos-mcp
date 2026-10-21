"""``remoteos-gateway check`` (#51): the key-drift watchdog the 10-minute timer runs.

It probes every key-store machine DIRECTLY with its stored key (``initialize``) plus the
gateway's own ``/healthz``, keeps ``machines-status.json`` (state + since), and notifies ONCE
per state change. A 401 is auto-repaired only for a machine with an explicit ``repair`` recipe.
Real loopback machines; ``airuleset.py`` and ``ssh`` are faked at the OS boundary; no key may
appear in the output, the logs, the status file or a notification.
"""

from __future__ import annotations

import json
import os
import stat

from click.testing import CliRunner
from fleet_harness import (
    FakeFleetMachine,
    closed_port_url,
    fresh_secret,
    install_fake_airuleset,
    install_fake_remote_tools,
    notify_calls,
    remote_calls,
    write_private_json,
)

from remoteos.gateway import cli as gateway_cli


class Check:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.keystore = tmp_path / "cfg" / "machines.json"
        self.status = tmp_path / "state" / "machines-status.json"
        self.airuleset, self.notify_log = install_fake_airuleset(tmp_path / "airuleset", monkeypatch)

    def store(self, entries: dict) -> None:
        write_private_json(self.keystore, entries)

    def run(self, gateway_url: str, *extra: str):
        return CliRunner().invoke(
            gateway_cli.main,
            [
                "--registry",
                str(self.tmp / "cfg" / "registry.json"),
                "--machines",
                str(self.keystore),
                "check",
                "--status-file",
                str(self.status),
                "--gateway-url",
                gateway_url,
                "--airuleset",
                str(self.airuleset),
                *extra,
            ],
        )

    def state(self) -> dict:
        return json.loads(self.status.read_text(encoding="utf-8"))

    def notifications(self) -> list[str]:
        bodies = []
        for argv in notify_calls(self.notify_log):
            assert argv[:2] == ["notify", "--body"], argv
            bodies.append(argv[2])
        return bodies


def _assert_no_secret(texts: list[str], secrets: list[str]) -> None:
    for text in texts:
        for secret in secrets:
            assert secret not in text


def test_an_ok_to_401_change_notifies_once_and_a_repeat_does_not(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="remoteos")
    check = Check(tmp_path, monkeypatch)
    key, rotated = fresh_secret("machinekey"), fresh_secret("rotatedkey")
    with FakeFleetMachine(accept_keys=(key,)) as box, FakeFleetMachine(healthz=True) as gateway:
        check.store({"linux-cam2": {"url": box.url, "key": key}})

        first = check.run(gateway.base_url)
        assert first.exit_code == 0, first.output
        assert check.state()["machines"]["linux-cam2"]["state"] == "ok"
        assert check.notifications() == []  # a machine first seen OK is not news

        box.accept_keys = [rotated]  # the box was re-keyed outside the key store
        second = check.run(gateway.base_url)
        third = check.run(gateway.base_url)

    assert second.exit_code == 0 and third.exit_code == 0, second.output + third.output
    [body] = check.notifications()  # exactly once
    assert "linux-cam2" in body and "401" in body and "ok" in body
    status = check.state()
    entry = status["machines"]["linux-cam2"]
    assert entry["state"] == "401"
    assert entry["since"] == status["checked_at"] or entry["since"] < status["checked_at"]
    assert stat.S_IMODE(os.stat(check.status).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(check.status.parent).st_mode) == 0o700
    texts = [first.output, second.output, third.output, caplog.text, check.status.read_text(), body]
    _assert_no_secret(texts, [key, rotated])


def test_an_unreachable_machine_is_recorded_with_its_since_time_and_announced_once(tmp_path, monkeypatch):
    check = Check(tmp_path, monkeypatch)
    key = fresh_secret("machinekey")
    check.store({"linux-cam4": {"url": closed_port_url(), "key": key}})
    with FakeFleetMachine(healthz=True) as gateway:
        first = check.run(gateway.base_url)
        since = check.state()["machines"]["linux-cam4"]["since"]
        second = check.run(gateway.base_url)

    assert first.exit_code == 0 and second.exit_code == 0
    status = check.state()
    assert status["machines"]["linux-cam4"]["state"] == "unreachable"
    assert status["machines"]["linux-cam4"]["since"] == since  # kept while the state holds
    assert since <= status["checked_at"]
    [body] = check.notifications()
    assert "linux-cam4" in body and "unreachable" in body
    assert "linux-cam4" in second.output and "unreachable" in second.output


def test_a_notification_that_failed_is_sent_on_the_next_run(tmp_path, monkeypatch):
    check = Check(tmp_path, monkeypatch)
    check.store({"linux-cam4": {"url": closed_port_url(), "key": fresh_secret("machinekey")}})
    with FakeFleetMachine(healthz=True) as gateway:
        monkeypatch.setenv("FAKE_NOTIFY_EXIT", "1")
        failed = check.run(gateway.base_url)
        assert check.state()["machines"]["linux-cam4"]["notified"] is False
        monkeypatch.setenv("FAKE_NOTIFY_EXIT", "0")
        retried = check.run(gateway.base_url)
        quiet = check.run(gateway.base_url)

    assert failed.exit_code == retried.exit_code == quiet.exit_code == 0
    assert len(check.notifications()) == 2  # the failed attempt + the retry; then silence
    assert check.state()["machines"]["linux-cam4"]["notified"] is True


def test_the_gateway_healthz_is_probed_and_its_outage_is_announced(tmp_path, monkeypatch):
    check = Check(tmp_path, monkeypatch)
    key = fresh_secret("machinekey")
    with FakeFleetMachine(accept_keys=(key,)) as box:
        check.store({"linux-cam1": {"url": box.url, "key": key}})
        down = closed_port_url().rsplit("/mcp", 1)[0]
        result = check.run(down)

    assert result.exit_code == 0, result.output
    status = check.state()
    assert status["gateway"]["state"] == "unreachable"
    assert status["machines"]["linux-cam1"]["state"] == "ok"
    [body] = check.notifications()
    assert "gateway" in body.lower() and "unreachable" in body


def test_a_repair_recipe_is_used_only_where_configured(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="remoteos")
    check = Check(tmp_path, monkeypatch)
    ssh_log = install_fake_remote_tools(tmp_path / "bin", monkeypatch)
    stale_b, real_b = fresh_secret("stalekey"), fresh_secret("realkey")
    stale_c, real_c = fresh_secret("stalekey"), fresh_secret("realkey")
    key_file = tmp_path / "box-b" / "config.json"  # what `cat` prints on the box (fake ssh runs it here)
    write_private_json(key_file, {"port": 8092, "auth_key": real_b, "host": "0.0.0.0"})
    with (
        FakeFleetMachine(accept_keys=(real_b,)) as box_b,
        FakeFleetMachine(accept_keys=(real_c,)) as box_c,
        FakeFleetMachine(healthz=True) as gateway,
    ):
        recipe = {"ssh": "root@cam2.lan", "key_file": str(key_file)}
        check.store(
            {
                "linux-cam2": {"url": box_b.url, "key": stale_b, "repair": recipe},
                "linux-cam3": {"url": box_c.url, "key": stale_c},  # no recipe: a 401 is only reported
            }
        )
        result = check.run(gateway.base_url)

    assert result.exit_code == 0, result.output
    store = json.loads(check.keystore.read_text(encoding="utf-8"))
    assert store["linux-cam2"]["key"] == real_b  # repaired in the key store
    assert store["linux-cam2"]["repair"]["ssh"] == "root@cam2.lan"  # the recipe is kept
    assert store["linux-cam3"]["key"] == stale_c  # never guessed
    status = check.state()["machines"]
    assert status["linux-cam2"]["state"] == "ok" and "repaired" in status["linux-cam2"]["detail"]
    assert status["linux-cam3"]["state"] == "401"
    ssh_calls = [c["argv"] for c in remote_calls(ssh_log) if c["tool"] == "ssh"]
    assert len(ssh_calls) == 1 and "root@cam2.lan" in ssh_calls[0] and "BatchMode=yes" in ssh_calls[0]
    [body] = check.notifications()
    assert "linux-cam2" in body and "repaired" in body and "linux-cam3" in body
    texts = [result.output, caplog.text, check.status.read_text(), body, json.dumps(ssh_calls)]
    _assert_no_secret(texts, [stale_b, real_b, stale_c, real_c])


def test_without_airuleset_a_change_is_a_log_line(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="remoteos")
    check = Check(tmp_path, monkeypatch)
    check.airuleset = tmp_path / "nowhere" / "airuleset.py"
    key = fresh_secret("machinekey")
    check.store({"linux-cam4": {"url": closed_port_url(), "key": key}})
    with FakeFleetMachine(healthz=True) as gateway:
        result = check.run(gateway.base_url)

    assert result.exit_code == 0, result.output
    assert any("linux-cam4" in r.getMessage() and "unreachable" in r.getMessage() for r in caplog.records)
    assert check.state()["machines"]["linux-cam4"]["notified"] is True  # the log line WAS the notification
    assert key not in caplog.text
