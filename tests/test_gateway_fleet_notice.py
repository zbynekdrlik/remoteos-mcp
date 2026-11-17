"""The fleet kind notice text (#102): ONE U item per kind, so its text is never split."""

from __future__ import annotations

from remoteos.gateway import fleet_notice
from remoteos.gateway.messages import CHUNK_LIMIT


def _down(count: int, name_length: int) -> dict[str, dict]:
    return {
        f"win-{index:03d}-" + "x" * name_length: {
            "state": "unreachable",
            "since": "2026-11-16T08:00:00.000+00:00",
            "detail": "ConnectError: " + "y" * 200,
            "notified": False,
        }
        for index in range(count)
    }


def test_a_down_fleet_too_long_for_one_message_is_still_one_text_that_says_how_many_more():
    machines = _down(60, 50)
    notice = fleet_notice.build(machines, "dev1")[fleet_notice.DOWN]
    assert len(notice.text) <= CHUNK_LIMIT
    assert notice.text.startswith("MCP brána (dev1): nefunkčné stroje (60)")
    shown = notice.text.count("\n• ")
    assert 0 < shown < 60 and f"… a ďalších {60 - shown}" in notice.text
    assert len(notice.names) == 60 and len(notice.fingerprint) == 60  # all boxes count, all marked


def test_a_short_down_fleet_lists_every_box_with_state_and_since():
    machines = _down(3, 1)
    machines["linux-b"] = {"state": "401", "since": "2026-11-15T07:30:00.000+00:00", "detail": "HTTP 401"}
    notice = fleet_notice.build(machines, "dev1")[fleet_notice.DOWN]
    assert "… a ďalších" not in notice.text
    assert "• linux-b: odmieta kľúč (401) (od 2026-11-15 07:30) — HTTP 401" in notice.text
    assert fleet_notice.KEY_HINT in notice.text
    assert ["linux-b", "401", ""] in notice.fingerprint


def test_nothing_open_builds_no_notice_and_no_open_slug():
    machines = {"linux-a": {"state": "ok", "since": "x", "detail": "ok"}}
    assert fleet_notice.build(machines, "dev1") == {}
    assert fleet_notice.open_slugs(machines) == set()


# -- only a changed BOX SET is news (#109, owner 2026-11-17) ----------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    def __call__(self, body: str, notice: str | None = None, record: dict | None = None) -> bool:
        self.sent.append((body, notice))
        return True


def _status(machines: dict[str, dict], known: dict) -> dict:
    return {"machines": machines, fleet_notice.STATUS_KEY: known}


def _stale_box(version: str) -> dict:
    return {"state": "ok", "since": "x", "detail": "ok", "version": version,
            "drift": {"stable": "0.7.0.dev50", "since": "2026-11-01", "notified": True}}


def test_the_same_down_boxes_are_never_re_sent_whatever_the_fingerprint_format_or_state():
    machines = _down(2, 1)
    names = sorted(machines)
    old_format = [[name, "unreachable"] for name in names]  # a fingerprint written by an older version
    machines[names[0]]["state"] = "401"  # same box, another state
    machines[names[0]]["org"] = "nlmedia"  # and an org the old fingerprint never had
    status = _status(machines, {fleet_notice.DOWN: old_format})
    send = _Recorder()

    assert fleet_notice.reconcile(status, send, "dev1") is None
    assert send.sent == []
    assert sorted(item[0] for item in status[fleet_notice.STATUS_KEY][fleet_notice.DOWN]) == names
    assert all(r["notified"] for r in machines.values())


def test_a_changed_down_set_is_sent_again():
    machines = _down(2, 1)
    names = sorted(machines)
    status = _status(machines, {fleet_notice.DOWN: [[names[0], "unreachable", ""]]})
    send = _Recorder()
    assert fleet_notice.reconcile(status, send, "dev1") is True
    [(body, _)] = send.sent
    assert body.startswith("MCP brána (dev1): nefunkčné stroje (2)")


def test_the_same_stale_boxes_on_a_newer_version_are_not_re_sent_but_a_new_box_is():
    known = {fleet_notice.STALE: [["linux-a", "0.7.0.dev40", "0.7.0.dev50"]]}
    status = _status({"linux-a": _stale_box("0.7.0.dev45")}, known)  # upgraded, still behind
    send = _Recorder()
    assert fleet_notice.reconcile(status, send, "dev1") is None and send.sent == []

    status["machines"]["linux-b"] = _stale_box("0.7.0.dev41")
    assert fleet_notice.reconcile(status, send, "dev1") is True
    [(body, _)] = send.sent
    assert "linux-a" in body and "linux-b" in body


def test_an_owed_clear_or_a_hand_edited_fingerprint_is_sent_again():
    for known in ([], [["linux-a"], "x"], "junk"):
        status = _status({"linux-a": _stale_box("0.7.0.dev45")}, {fleet_notice.STALE: known})
        send = _Recorder()
        assert fleet_notice.reconcile(status, send, "dev1") is True, known
