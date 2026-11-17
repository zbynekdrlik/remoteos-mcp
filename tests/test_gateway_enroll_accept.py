"""``enroll-accept`` (#101): a queued registration becomes a machine, only after the box proves it is
the host that posted, and only on a private address. Plus the ONE owner notice while one waits.

The machine is a real loopback server (``FakeFleetMachine``); the CLI is the real one, against the
same fake world as ``enroll`` (#26). Loopback is never a private address, so a box on 127.0.0.1 goes
through ``--url``, exactly the operator's override.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, fresh_secret
from test_gateway_enroll import Fleet

from remoteos.gateway import check, fleet_notice
from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.enroll import parse_registration
from remoteos.gateway.enroll_queue import EnrollQueue
from remoteos.gateway.manifest import load_manifest


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    world = Fleet(tmp_path, monkeypatch)
    world.queue = EnrollQueue(tmp_path / "enroll")
    yield world
    world.close()


def _queue(fleet, *, hostname: str = "PREDAJ3", ips: list[str] | None = None, key: str | None = None) -> str:
    raw = {"hostname": hostname, "ips": ips if ips is not None else ["10.77.8.31"], "port": 8092,
           "version": "0.7.0.dev61", "os": "windows", "account": "pokladna", "auth_key": key or fleet.key}
    entry, _ = fleet.queue.add(parse_registration(json.dumps(raw).encode()), "203.0.113.7")
    return entry.id


def _accept(fleet, ref: str, *args: str):
    return CliRunner().invoke(gateway_cli.main, [
        *fleet.base(), "enroll-accept", ref, "--name", "win-predaj3", "--org", "nlmedia",
        "--enroll-dir", str(fleet.queue.root), *args])


def test_accept_by_hostname_enrolls_and_removes_the_registration(fleet):
    entry_id = _queue(fleet)
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev61", hostname="predaj3") as machine:
        result = _accept(fleet, "predaj3", "--projects", "camera-box", "--url", machine.url)
        assert result.exit_code == 0, result.output
    store = json.loads(fleet.machines.read_text())
    assert store["win-predaj3"] == {"url": machine.url, "key": fleet.key, "org": "nlmedia"}
    assert "win-predaj3" in load_manifest(fleet.manifest).projects["camera-box"].machines
    assert fleet.queue.pending() == []
    assert f"registration {entry_id} done" in result.output
    assert fleet.key not in result.output


def test_a_box_that_is_not_the_posted_host_never_gets_the_key(fleet):
    entry_id = _queue(fleet)
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev61", hostname="other-pc") as machine:
        result = _accept(fleet, entry_id, "--projects", "camera-box", "--url", machine.url)
        assert machine.seen_authorization == []  # the key was never sent there
    assert result.exit_code == 1
    assert "does NOT match" in result.output and "stays queued" in result.output
    assert "win-predaj3" not in fleet.machines.read_text()
    assert [entry.id for entry in fleet.queue.pending()] == [entry_id]


def test_a_box_that_reports_no_hostname_is_not_accepted(fleet):
    entry_id = _queue(fleet)
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev61") as machine:
        result = _accept(fleet, entry_id, "--projects", "camera-box", "--url", machine.url)
        assert machine.seen_authorization == []
    assert result.exit_code == 1 and fleet.queue.pending()


def test_the_key_goes_to_private_addresses_only(fleet):
    entry_id = _queue(fleet, ips=["8.8.8.8", "127.0.0.1", "169.254.1.1"])
    result = _accept(fleet, entry_id, "--projects", "camera-box")
    assert result.exit_code == 1
    assert "no private address: pass --url" in result.output
    assert "8.8.8.8" not in result.output


def test_two_registrations_with_one_hostname_need_the_id(fleet):
    first = _queue(fleet)
    second = _queue(fleet, key=fresh_secret("Other"))
    result = _accept(fleet, "predaj3", "--projects", "camera-box")
    assert result.exit_code == 1 and first in result.output and second in result.output


def test_an_unknown_project_is_refused_before_the_queue_is_read(fleet):
    _queue(fleet)
    result = _accept(fleet, "predaj3", "--projects", "nope")
    assert result.exit_code == 1 and "nope" in result.output
    assert len(fleet.queue.pending()) == 1


# -- the owner notice: ONE U item while a registration waits --------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    def __call__(self, body: str, notice: str | None = None, record: dict | None = None) -> bool:
        self.sent.append((body, notice))
        return True


def test_a_waiting_registration_is_one_notice_sent_once_per_set():
    status = {"machines": {}, fleet_notice.PENDING_KEY: [["0a1b2c3d", "predaj3"], ["11223344", "dsg"]]}
    send = _Recorder()
    assert fleet_notice.reconcile(status, send, "dev1") is True
    [(text, _)] = send.sent
    assert list(status[fleet_notice.STATUS_KEY]) == [fleet_notice.ENROLL]
    assert "Nový počítač čaká na prijatie" in text and "predaj3 (id 0a1b2c3d)" in text and "dsg" in text
    assert "enroll-accept" in text
    assert fleet_notice.reconcile(status, send, "dev1") is None  # the same set: not news
    assert check.open_notices({"gateway": {"state": "ok"}, **status}) == {fleet_notice.ENROLL}


def test_no_waiting_registration_means_the_notice_is_not_open():
    status = {"gateway": {"state": "ok"}, "machines": {}}
    assert fleet_notice.ENROLL not in check.open_notices(status)
    assert fleet_notice.pending_of({fleet_notice.PENDING_KEY: [["only-one"], 5, "x"]}) == []


def test_check_records_the_queue_and_keeps_the_last_list_when_it_cannot_be_read(tmp_path):
    keystore = tmp_path / "machines.json"
    keystore.write_text("{}")
    keystore.chmod(0o600)
    status_path = tmp_path / "status.json"
    report = check.run_check(keystore, status_path, gateway_url="http://127.0.0.1:9/", notifier=None,
                             enroll_pending=[["0a1b2c3d", "predaj3"]])
    assert report.status[fleet_notice.PENDING_KEY] == [["0a1b2c3d", "predaj3"]]
    again = check.run_check(keystore, status_path, gateway_url="http://127.0.0.1:9/", notifier=None,
                            enroll_pending=None)
    assert again.status[fleet_notice.PENDING_KEY] == [["0a1b2c3d", "predaj3"]]
    empty = check.run_check(keystore, status_path, gateway_url="http://127.0.0.1:9/", notifier=None,
                            enroll_pending=[])
    assert fleet_notice.PENDING_KEY not in empty.status
