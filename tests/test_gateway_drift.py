"""``remoteos-gateway check`` records versions and reports fleet drift (#63).

Real loopback machines answer ``/health``, a real loopback server answers like the GitHub release
list, and ``airuleset.py`` is faked at the OS boundary (``Check`` from the watchdog's tests).
"""

from __future__ import annotations

import datetime as dt
import logging

from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, closed_port_url, fresh_secret
from test_gateway_check import Check

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway import drift as drift_module

OLD_VERSION = "0.7.0.dev20"
STABLE_VERSION = "0.7.0.dev40"


def stamp(days_ago: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def github_release(version: str, days_ago: float, prerelease: bool = False) -> dict:
    return {"tag_name": f"v{version}", "published_at": stamp(days_ago), "prerelease": prerelease, "draft": False}


def soaked(extra_days: float = 10) -> list[dict]:
    """A release past the 14 day soak by *extra_days*."""
    return [github_release(STABLE_VERSION, 14 + extra_days)]


class Fleet:
    """One machine, the gateway and a fake GitHub, for a whole test."""

    def __init__(self, tmp_path, monkeypatch, *, version=OLD_VERSION, releases=None):
        self.check = Check(tmp_path, monkeypatch)
        self.key = fresh_secret("machinekey")
        self.box = FakeFleetMachine(accept_keys=(self.key,), version=version)
        self.gateway = FakeFleetMachine(healthz=True)
        self.github = FakeFleetMachine(releases=soaked() if releases is None else releases)

    def __enter__(self):
        for server in (self.box, self.gateway, self.github):
            server.__enter__()
        self.check.releases_url = f"{self.github.base_url}/releases"
        self.check.store({"linux-cam2": {"url": self.box.url, "key": self.key}})
        return self

    def __exit__(self, *exc):
        for server in (self.box, self.gateway, self.github):
            server.__exit__(*exc)

    def run(self):
        result = self.check.run(self.gateway.base_url)
        assert result.exit_code == 0, result.output
        return result

    @property
    def record(self) -> dict:
        return self.check.state()["machines"]["linux-cam2"]


def test_a_box_older_than_stable_is_reported_once_with_its_version_and_days(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        fleet.run()

        assert len(fleet.check.notifications()) == 1
        [body] = fleet.check.notifications()
        assert f"v{STABLE_VERSION}" in body
        assert f"linux-cam2: verzia {OLD_VERSION}, za stable už 10 dní" in body
        assert fleet.key not in body
        assert fleet.record["version"] == OLD_VERSION
        assert fleet.record["drift"]["notified"] is True
        assert fleet.box.seen_health_authorization[0] is None  # /health is public: the key stays home


def test_no_report_while_the_grace_lasts(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch, releases=soaked(extra_days=2.9)) as fleet:
        fleet.run()
        assert fleet.check.notifications() == []
        assert "drift" not in fleet.record


def test_a_box_on_stable_or_newer_is_not_reported(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch, version=STABLE_VERSION) as fleet:
        fleet.run()
        fleet.box.version = "0.7.0.dev41"
        fleet.run()
        assert fleet.check.notifications() == []
        assert fleet.record["version"] == "0.7.0.dev41"


def test_catching_up_ends_the_episode_so_a_later_fall_is_reported_again(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        fleet.box.version = STABLE_VERSION
        fleet.run()
        assert "drift" not in fleet.record
        fleet.box.version = OLD_VERSION  # rolled back
        fleet.run()
        assert len(fleet.check.notifications()) == 2


def test_a_failed_release_fetch_only_skips_drift_and_is_logged(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="remoteos.gateway.drift")
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.check.releases_url = closed_port_url()
        result = fleet.run()

        assert fleet.check.notifications() == []
        assert fleet.record["state"] == "ok"
        assert fleet.record["version"] == OLD_VERSION  # the version is still recorded
        assert "drift" not in fleet.record
        assert "did NOT go out" not in result.output
        assert any("release list could not be read" in r.getMessage() for r in caplog.records)


def test_a_fetch_failure_keeps_a_known_drift_and_never_re_reports_it(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        reported = fleet.record["drift"]
        fleet.check.releases_url = closed_port_url()
        fleet.run()
        assert fleet.record["drift"] == reported
        assert len(fleet.check.notifications()) == 1


def test_an_undelivered_report_is_retried_on_the_next_run(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        monkeypatch.setenv("FAKE_NOTIFY_STATUS", "error")
        first = fleet.run()
        assert fleet.record["drift"]["notified"] is False
        assert "did NOT go out" in first.output

        monkeypatch.setenv("FAKE_NOTIFY_STATUS", "sent")
        fleet.run()
        assert fleet.record["drift"]["notified"] is True
        assert len(fleet.check.notifications()) == 2


def test_an_owner_policy_suppression_counts_as_delivered_and_never_loops(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")
        fleet.run()
        fleet.run()
        assert fleet.record["drift"]["notified"] is True
        assert len(fleet.check.notifications()) == 1


def test_a_box_that_stops_answering_keeps_its_last_version_and_is_not_judged(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.box.version = STABLE_VERSION
        fleet.run()
        seen = fleet.record["version_seen"]
        fleet.box.version = None  # /health answers 404 now, initialize still works
        fleet.run()
        assert fleet.record["version"] == STABLE_VERSION
        assert fleet.record["version_seen"] == seen
        assert fleet.check.notifications() == []


def test_an_unexpected_drift_failure_never_costs_the_status_write(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        reported = fleet.record["drift"]
        fleet.box.accept_keys = []  # a state change, announced in the run that drift fails
        monkeypatch.setattr(drift_module, "judge", lambda *a, **k: 1 / 0)
        result = fleet.check.run(fleet.gateway.base_url)
        assert result.exit_code == 0, result.output
        assert fleet.record["state"] == "401" and fleet.record["notified"] is True
        assert fleet.record["drift"] == reported
        assert "error" not in fleet.check.state()


def test_a_huge_soak_value_only_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTEOS_SOAK_DAYS", "99999999999")
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        assert len(fleet.check.notifications()) == 1


def test_a_stable_that_cannot_be_compared_keeps_a_known_drift(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        reported = fleet.record["drift"]
        fleet.github.releases = [github_release("0.8.0-rc1", 20), *soaked()]
        fleet.run()
        assert fleet.record["drift"] == reported


def test_a_trailing_slash_on_the_url_still_reads_the_version(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.check.store({"linux-cam2": {"url": fleet.box.url + "/", "key": fleet.key}})
        fleet.run()
        assert fleet.record.get("version") == OLD_VERSION


def test_a_junk_or_uncomparable_version_never_reaches_the_owner(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch, version="evil @everyone text") as fleet:
        fleet.run()
        assert "version" not in fleet.record
        fleet.box.version = "1.2.3rc1"  # fine as text, not comparable
        fleet.run()
        assert fleet.record["version"] == "1.2.3rc1"
        assert fleet.check.notifications() == []


def test_a_hand_edited_drift_record_is_treated_as_unknown(tmp_path, monkeypatch):
    import json

    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        status = fleet.check.state()
        status["machines"]["linux-cam2"]["drift"] = "broken"
        status["machines"]["linux-cam2"]["version"] = 7
        fleet.check.status.write_text(json.dumps(status), encoding="utf-8")
        fleet.run()
        assert fleet.record["drift"]["notified"] is True
        assert len(fleet.check.notifications()) == 2


def test_a_fleet_wide_drift_goes_out_in_chunks_and_each_is_delivered(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        entry = {"url": fleet.box.url, "key": fleet.key}
        names = [f"win-pokladna-with-a-long-name-{i:03}" for i in range(60)]
        fleet.check.store({name: entry for name in names})
        fleet.run()

        bodies = fleet.check.notifications()
        assert len(bodies) > 1
        assert all(len(body) <= 1700 for body in bodies)
        assert sum(body.count("verzia") for body in bodies) == 60
        assert all(r["drift"]["notified"] for r in fleet.check.state()["machines"].values())


def test_machine_list_shows_the_last_seen_version(tmp_path, monkeypatch):
    with Fleet(tmp_path, monkeypatch) as fleet:
        fleet.run()
        listing = CliRunner().invoke(
            gateway_cli.main,
            [
                "--registry",
                str(tmp_path / "cfg" / "registry.json"),
                "--machines",
                str(fleet.check.keystore),
                "machine",
                "list",
                "--status-file",
                str(fleet.check.status),
            ],
        )
    assert listing.exit_code == 0, listing.output
    assert f"version {OLD_VERSION}" in listing.output
    assert fleet.key not in listing.output
