"""The fleet kind notice text (#102): ONE U item per kind, so its text is never split."""

from __future__ import annotations

import datetime as dt

from remoteos.gateway import fleet_notice
from remoteos.gateway.messages import CHUNK_LIMIT

MOMENT = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)


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
    notice = fleet_notice.build(machines, "dev1", MOMENT)[fleet_notice.DOWN]
    assert len(notice.text) <= CHUNK_LIMIT
    assert notice.text.startswith("MCP brána (dev1): nefunkčné stroje (60)")
    shown = notice.text.count("\n• ")
    assert 0 < shown < 60 and f"… a ďalších {60 - shown}" in notice.text
    assert len(notice.names) == 60 and len(notice.fingerprint) == 60  # all boxes count, all marked


def test_a_short_down_fleet_lists_every_box_with_state_and_since():
    machines = _down(3, 1)
    machines["linux-b"] = {"state": "401", "since": "2026-11-15T07:30:00.000+00:00", "detail": "HTTP 401"}
    notice = fleet_notice.build(machines, "dev1", MOMENT)[fleet_notice.DOWN]
    assert "… a ďalších" not in notice.text
    assert "• linux-b: odmieta kľúč (401) (od 2026-11-15 07:30) — HTTP 401" in notice.text
    assert fleet_notice.KEY_HINT in notice.text
    assert ["linux-b", "401"] in notice.fingerprint


def test_nothing_open_builds_no_notice_and_no_open_slug():
    machines = {"linux-a": {"state": "ok", "since": "x", "detail": "ok"}}
    assert fleet_notice.build(machines, "dev1", MOMENT) == {}
    assert fleet_notice.open_slugs(machines) == set()
