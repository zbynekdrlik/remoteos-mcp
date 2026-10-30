"""The stable/canary resolver and the CI release step (#63).

``choose`` is a pure table. ``fetch_releases`` runs against a real loopback server. The release
script runs for real with a fake ``gh`` on PATH; the workflow is read as YAML.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fleet_harness import FakeFleetMachine, closed_port_url

from remoteos.release_channel import (
    Release,
    ReleaseFetchError,
    behind_since,
    choose,
    fetch_releases,
    parse_releases,
    soak_days,
    version_key,
)

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)


def days_ago(days: float) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


def rel(tag: str, age: float, *, held: bool = False) -> Release:
    return Release(tag=tag, published_at=days_ago(age), held=held)


@pytest.mark.parametrize(
    ("releases", "canary", "stable"),
    [
        ([], None, None),
        ([rel("v1", 3)], "v1", None),  # none old enough
        ([rel("v1", 14)], "v1", "v1"),  # exactly the soak
        ([rel("v1", 13.9)], "v1", None),
        ([rel("v1", 30), rel("v2", 20), rel("v3", 2)], "v3", "v2"),
        ([rel("v2", 20), rel("v3", 2), rel("v1", 30)], "v3", "v2"),  # the input order does not matter
        ([rel("v1", 30), rel("v2", 20, held=True), rel("v3", 2)], "v3", "v1"),  # a held one is skipped
        ([rel("v1", 30), rel("v3", 2, held=True)], "v1", "v1"),  # canary skips it too
        ([rel("v1", 30, held=True)], None, None),
        # order is by PUBLISH date, never by version: v9 came first
        ([rel("v9", 30), rel("v2", 20)], "v2", "v2"),
    ],
)
def test_choose(releases, canary, stable):
    chosen = choose(releases, NOW, 14)
    assert (chosen.canary.tag if chosen.canary else None) == canary
    assert (chosen.stable.tag if chosen.stable else None) == stable


def test_choose_with_a_shorter_soak():
    assert choose([rel("v1", 8), rel("v2", 2)], NOW, 7).stable.tag == "v1"


@pytest.mark.parametrize(
    ("older", "newer"),
    [("0.7.0.dev39", "0.7.0.dev40"), ("0.7.0.dev40", "0.7.0"), ("0.7.0.dev9", "0.7.0.dev10"), ("0.7.1", "0.10.0")],
)
def test_version_key_order(older, newer):
    assert version_key(older) < version_key(newer)


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3rc1", "1.2+local", "v1.2", "1.2.dev"])
def test_version_key_refuses_what_it_cannot_order(bad):
    assert version_key(bad) is None


def test_behind_since_is_when_the_oldest_newer_release_finished_its_soak():
    releases = [rel("v0.7.0.dev40", 40), rel("v0.7.0.dev41", 30), rel("v0.7.0.dev42", 20), rel("v0.7.0.dev43", 2)]
    # a new stable (dev42) while the box stays behind does not restart the count
    assert behind_since("0.7.0.dev40", releases, NOW, 14) == days_ago(16)
    assert behind_since("0.7.0.dev20", releases, NOW, 14) == days_ago(26)  # older than every release


def test_behind_since_is_none_when_not_older_than_stable():
    releases = [rel("v0.7.0.dev40", 30), rel("v0.7.0.dev41", 2)]
    assert behind_since("0.7.0.dev40", releases, NOW, 14) is None  # on stable
    assert behind_since("0.7.0.dev41", releases, NOW, 14) is None  # on canary
    assert behind_since("0.7.0.dev39", [rel("v0.7.0.dev40", 2)], NOW, 14) is None  # no stable yet
    assert behind_since("weird", releases, NOW, 14) is None


def test_a_held_release_never_makes_a_box_behind():
    assert behind_since("0.7.0.dev40", [rel("v0.7.0.dev40", 40), rel("v0.7.0.dev41", 30, held=True)], NOW, 14) is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [({}, 14), ({"REMOTEOS_SOAK_DAYS": "3"}, 3), ({"REMOTEOS_SOAK_DAYS": "0"}, 0),
     ({"REMOTEOS_SOAK_DAYS": "x"}, 14), ({"REMOTEOS_SOAK_DAYS": "-1"}, 14), ({"REMOTEOS_SOAK_DAYS": ""}, 14)],
)
def test_soak_days(env, expected):
    assert soak_days(env) == expected


def github_release(tag: str, published: str = "2026-10-01T10:00:00Z", **extra) -> dict:
    return {"tag_name": tag, "published_at": published, "prerelease": False, "draft": False, **extra}


def test_parse_reads_the_hold_and_leaves_drafts_out():
    releases = parse_releases(
        [
            github_release("v0.7.0.dev40"),
            github_release("v0.7.0.dev41", prerelease=True),
            {"tag_name": "v0.7.0.dev42", "published_at": None, "prerelease": False, "draft": True},
        ]
    )
    assert [(r.tag, r.version, r.held) for r in releases] == [
        ("v0.7.0.dev40", "0.7.0.dev40", False),
        ("v0.7.0.dev41", "0.7.0.dev41", True),
    ]
    assert releases[0].published_at == dt.datetime(2026, 10, 1, 10, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "rate limit"},
        [1],
        [{"tag_name": "v1"}],
        [github_release("v1", published="not a date")],
        [github_release("v1", published="2026-10-01T10:00:00")],  # no time zone
        [github_release("v1", prerelease="yes")],
    ],
)
def test_parse_refuses_another_shape(payload):
    with pytest.raises(ValueError):
        parse_releases(payload)


def test_fetch_reads_a_real_http_answer():
    with FakeFleetMachine(releases=[github_release("v1")]) as github:
        assert [r.tag for r in fetch_releases(f"{github.base_url}/releases", timeout=5)] == ["v1"]


def test_every_fetch_failure_is_one_exception():
    with pytest.raises(ReleaseFetchError):
        fetch_releases(closed_port_url(), timeout=5)
    with FakeFleetMachine() as server:
        with pytest.raises(ReleaseFetchError, match="404"):
            fetch_releases(f"{server.base_url}/releases", timeout=5)
    with FakeFleetMachine(releases={"not": "a list"}) as server:  # type: ignore[arg-type]
        with pytest.raises(ReleaseFetchError):
            fetch_releases(f"{server.base_url}/releases", timeout=5)


# -- scripts/create-release.sh, for real, with a fake gh ---------------------------------------


FAKE_GH = """#!{python}
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["release", "view"]:
    sys.exit(0 if os.environ.get("FAKE_GH_EXISTS") else 1)
sys.exit(int(os.environ.get("FAKE_GH_CREATE_EXIT", "0")))
"""


def run_release_script(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "gh.jsonl"
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_SHA": "abc123",
        "FAKE_GH_LOG": str(log),
        **env,
    }
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "create-release.sh")],
        cwd=REPO,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return proc, calls


def project_version() -> str:
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError("no version line")


def test_release_is_created_on_the_merge_commit_and_never_as_a_prerelease(tmp_path):
    proc, calls = run_release_script(tmp_path)
    tag = f"v{project_version()}"
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        ["release", "view", tag],
        ["release", "create", tag, "--target", "abc123", "--title", tag, "--generate-notes"],
    ]


def test_an_existing_release_is_left_alone(tmp_path):
    proc, calls = run_release_script(tmp_path, FAKE_GH_EXISTS="1")
    assert proc.returncode == 0, proc.stderr
    assert [call[:2] for call in calls] == [["release", "view"]]  # a held release stays held


def test_a_failed_create_fails_the_job(tmp_path):
    proc, _ = run_release_script(tmp_path, FAKE_GH_CREATE_EXIT="1")
    assert proc.returncode == 1


# -- the workflow ---------------------------------------------------------------------------


def workflow() -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_the_release_job_runs_only_on_a_push_to_main_after_every_test_job():
    jobs = workflow()["jobs"]
    release = jobs["release"]
    assert release["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    # version-check runs on pull requests only: on a push it would be skipped and block the job
    assert set(release["needs"]) == set(jobs) - {"release", "version-check"}
    assert release["permissions"] == {"contents": "write"}
    assert "continue-on-error" not in release
    assert not any("continue-on-error" in step for step in release["steps"])
    assert any(step.get("run") == "scripts/create-release.sh" for step in release["steps"])


def test_the_release_script_is_executable_in_git():
    mode = subprocess.run(
        ["git", "ls-files", "-s", "scripts/create-release.sh"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()[0]
    assert mode == "100755"
