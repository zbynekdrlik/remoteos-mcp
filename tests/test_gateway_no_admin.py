"""``remoteos-gateway check`` reports a box without admin once, by name (#57).

The message and status logic is driven directly; ``no_admin.update`` runs inside a real check in
``test_a_real_check_reports_a_box_without_admin_once``, against a real loopback machine.
"""

from __future__ import annotations

import pytest
from fleet_harness import FakeFleetMachine, fresh_secret
from test_gateway_check import Check

from remoteos.gateway import no_admin

STAMP = "2026-10-30T10:00:00.000+00:00"


class Notifier:
    def __init__(self, delivered: bool = True):
        self.texts: list[str] = []
        self.delivered = delivered

    def __call__(self, text: str) -> bool:
        self.texts.append(text)
        return self.delivered


def run(machines, previous, admins, notifier):
    return no_admin.update(machines, previous, admins, STAMP, "dev1", notifier)


def test_a_box_without_admin_is_announced_once_by_name():
    notifier = Notifier()
    first = {"win-foh": {"state": "ok"}, "win-ok": {"state": "ok"}}
    assert run(first, {}, {"win-foh": False, "win-ok": True}, notifier) is True
    assert first["win-foh"]["no_admin"] == {"since": STAMP, "notified": True}
    assert "no_admin" not in first["win-ok"]
    assert len(notifier.texts) == 1
    assert "win-foh" in notifier.texts[0] and "win-ok" not in notifier.texts[0]

    second = {"win-foh": {"state": "ok"}, "win-ok": {"state": "ok"}}
    assert run(second, first, {"win-foh": False, "win-ok": True}, notifier) is None
    assert second["win-foh"]["no_admin"] == first["win-foh"]["no_admin"]
    assert len(notifier.texts) == 1


def test_an_undelivered_announcement_is_retried():
    failed = {"m": {}}
    assert run(failed, {}, {"m": False}, Notifier(delivered=False)) is False
    assert failed["m"]["no_admin"]["notified"] is False
    retry = Notifier()
    again = {"m": {}}
    assert run(again, failed, {"m": False}, retry) is True
    assert again["m"]["no_admin"]["notified"] is True
    assert len(retry.texts) == 1


def test_admin_back_ends_the_episode_and_a_new_one_is_announced_again():
    notifier = Notifier()
    first = {"m": {}}
    run(first, {}, {"m": False}, notifier)
    back = {"m": {}}
    run(back, first, {"m": True}, notifier)
    assert "no_admin" not in back["m"]
    third = {"m": {}}
    run(third, back, {"m": False}, notifier)
    assert len(notifier.texts) == 2


@pytest.mark.parametrize("admins", [{}, {"m": None}])
def test_no_answer_or_an_older_server_keeps_what_the_box_had(admins):
    notifier = Notifier()
    before = {"m": {"no_admin": {"since": "earlier", "notified": True}}}
    after = {"m": {}}
    assert run(after, before, admins, notifier) is None
    assert after["m"]["no_admin"] == {"since": "earlier", "notified": True}
    fresh = {"m": {}}
    run(fresh, {}, admins, notifier)
    assert "no_admin" not in fresh["m"]
    assert notifier.texts == []


def test_a_failing_notifier_costs_nothing_but_the_announcement():
    def broken(text: str) -> bool:
        raise OSError("gone")

    machines = {"m": {}}
    assert run(machines, {}, {"m": False}, broken) is None
    assert machines["m"]["no_admin"]["notified"] is False


def test_a_hand_edited_record_is_not_trusted():
    after = {"m": {}}
    run(after, {"m": {"no_admin": "yes"}}, {"m": False}, Notifier())
    assert after["m"]["no_admin"] == {"since": STAMP, "notified": True}


def test_a_big_fleet_goes_out_in_messages_that_fit():
    names = [f"win-box-with-a-rather-long-name-{i:03}" for i in range(200)]
    notifier = Notifier()
    machines = {n: {} for n in names}
    assert run(machines, {}, dict.fromkeys(names, False), notifier) is True
    assert len(notifier.texts) > 1
    assert all(len(text) <= 1700 for text in notifier.texts)
    assert all(name in "".join(notifier.texts) for name in names)


def test_a_real_check_reports_a_box_without_admin_once(tmp_path, monkeypatch):
    check = Check(tmp_path, monkeypatch)
    key = fresh_secret("machinekey")
    with FakeFleetMachine(accept_keys=(key,), version="0.7.0.dev42", admin=False) as box:
        with FakeFleetMachine(healthz=True) as gateway:
            check.store({"win-foh": {"url": box.url, "key": key}})
            assert check.run(gateway.base_url).exit_code == 0
            assert check.run(gateway.base_url).exit_code == 0
            box.admin = True
            assert check.run(gateway.base_url).exit_code == 0
            box.admin = None  # an older server: says nothing
            assert check.run(gateway.base_url).exit_code == 0
    assert "no_admin" not in check.state()["machines"]["win-foh"]
    bodies = [body for body in check.notifications() if "bez admin" in body]
    assert len(bodies) == 1 and "win-foh" in bodies[0]
