"""The file plumbing under #51: atomic private writes + backups (``fileops``), the manifest
(``projects.toml``) validation, and ``machine import --from-manifest`` reading configs where
they live. Every file lives under ``tmp_path``."""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import sys

import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, fresh_secret, install_fake_remote_tools, remote_calls, write_private_json

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway import fileops
from remoteos.gateway.manifest import ManifestError, parse_manifest

ME = pwd.getpwuid(os.getuid()).pw_name


# -- fileops ------------------------------------------------------------------------------------


def test_write_with_backup_keeps_every_earlier_copy_of_the_day_all_private(tmp_path):
    path = tmp_path / "configs" / "camera-box.mcp.json"
    path.parent.mkdir()
    path.write_text("v1", encoding="utf-8")

    first = fileops.write_with_backup(str(path), b"v2", "2026-10-21")
    second = fileops.write_with_backup(str(path), b"v3", "2026-10-21")

    backup_dir = path.parent / ".backup-2026-10-21"
    assert first["backup"] == str(backup_dir / "camera-box.mcp.json")
    assert second["backup"] == str(backup_dir / "camera-box.mcp.json.1")  # never overwritten
    assert (backup_dir / "camera-box.mcp.json").read_text() == "v1"
    assert (backup_dir / "camera-box.mcp.json.1").read_text() == "v2"
    assert path.read_text() == "v3"
    for p in (path, backup_dir / "camera-box.mcp.json", backup_dir / "camera-box.mcp.json.1"):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(backup_dir).st_mode) == 0o700
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_a_symlinked_config_is_written_through_and_stays_a_symlink(tmp_path):
    master = tmp_path / "mcp-configs" / "camera-box.mcp.json"
    master.parent.mkdir()
    master.write_text("old", encoding="utf-8")
    link = tmp_path / "project" / ".mcp.json"
    link.parent.mkdir()
    link.symlink_to(master)

    fileops.write_with_backup(fileops.expand(str(link)), b"new", "2026-10-21")

    assert link.is_symlink() and link.resolve() == master
    assert master.read_text() == "new"
    assert (master.parent / ".backup-2026-10-21" / "camera-box.mcp.json").read_text() == "old"


def test_the_helper_runs_standalone_from_its_source_with_contents_on_stdin(tmp_path):
    source = open(fileops.__file__, encoding="utf-8").read()
    path = tmp_path / "x.json"

    missing = subprocess.run([sys.executable, "-c", source, "read", str(path)], capture_output=True)
    wrote = subprocess.run(
        [sys.executable, "-c", source, "write", str(path), "2026-10-21"], input=b'{"a": 1}\n', capture_output=True
    )
    read = subprocess.run([sys.executable, "-c", source, "read", str(path)], capture_output=True)
    bad_tag = subprocess.run(
        [sys.executable, "-c", source, "write", str(path), "../../etc"], input=b"x", capture_output=True
    )

    assert missing.returncode == fileops.EXIT_MISSING and missing.stdout == b""
    assert wrote.returncode == 0 and json.loads(wrote.stdout) == {"path": str(path), "backup": None}
    assert read.returncode == 0 and read.stdout == b'{"a": 1}\n'
    assert bad_tag.returncode != 0 and path.read_bytes() == b'{"a": 1}\n'


def test_a_bare_relative_file_name_is_written_in_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    fileops.atomic_write("registry.json", b"{}\n")
    with fileops.exclusive_lock("registry.json.lock"):
        pass

    assert (tmp_path / "registry.json").read_bytes() == b"{}\n"
    assert stat.S_IMODE(os.stat(tmp_path / "registry.json").st_mode) == 0o600


def test_expand_refuses_a_relative_path_and_uses_the_account_home():
    with pytest.raises(ValueError):
        fileops.expand("relative/path.json")
    assert fileops.expand("~/x.json") == os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "x.json"))


# -- the manifest -------------------------------------------------------------------------------


def _project(**overrides) -> dict:
    project = {"host": "dev1", "account": "newlevel", "config": "~/.claude/mcp-configs/p.mcp.json", "machines": ["a-1"]}
    project.update(overrides)
    return project


def test_a_valid_manifest_parses_with_defaults():
    manifest = parse_manifest(
        {
            "projects": {
                "camera-box": _project(),
                "remoteos-mcp": _project(
                    config="/home/newlevel/.claude/mcp-configs/remoteos-mcp.mcp.json", mode="direct"
                ),
            }
        }
    )

    assert manifest.gateway_url == "http://100.104.8.125:8796"
    assert manifest.projects["camera-box"].consumer == "camera-box@dev1"
    assert manifest.projects["camera-box"].mode == "gateway"
    assert manifest.projects["remoteos-mcp"].mode == "direct"
    assert manifest.machine_url("linux-cam1") == "http://100.104.8.125:8796/linux-cam1/mcp"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"projekts": {}}, "unknown top-level"),
        ({"gateway": {"url": "ftp://x"}}, "url must be"),
        ({"gateway": {"url": "http://x/path"}}, "url must be"),
        ({"projects": {"p": _project(extra=1)}}, "unknown keys"),
        ({"projects": {"p": {"host": "dev1"}}}, "is missing"),
        ({"projects": {"p": _project(account="Root;reboot")}}, "account must be"),
        ({"projects": {"p": _project(host="dev1 -oProxyCommand=x")}}, "host must be"),
        ({"projects": {"p": _project(config="relative.json")}}, "config must be"),
        ({"projects": {"p": _project(machines=["a-1", "a-1"])}}, "twice"),
        ({"projects": {"p": _project(machines=["bad name"])}}, "machines must be"),
        ({"projects": {"p": _project(machines=[])}}, "no machines"),
        ({"projects": {"p": _project(mode="proxy")}}, "mode must be"),
        ({"projects": {"p": _project(), "q": _project()}}, "same config file"),
        (
            {"projects": {"p": _project(), "q": _project(config="/home/newlevel/.claude/mcp-configs/p.mcp.json")}},
            "same config file",
        ),
        ({"projects": {"-bad": _project()}}, "invalid project name"),
    ],
)
def test_an_invalid_manifest_is_refused_with_a_clear_message(data, message):
    with pytest.raises(ManifestError, match=message):
        parse_manifest(data)


# -- machine import --from-manifest -------------------------------------------------------------


def test_import_from_manifest_reads_each_project_config_where_it_lives(tmp_path, monkeypatch):
    log = install_fake_remote_tools(tmp_path / "bin", monkeypatch)
    key = fresh_secret("dev2key")
    with FakeFleetMachine(accept_keys=(key,)) as box:
        dev2_config = write_private_json(
            tmp_path / "dev2" / "presenter.mcp.json",
            {
                "mcpServers": {
                    "linux-presenter": {"type": "http", "url": box.url, "headers": {"Authorization": f"Bearer {key}"}}
                }
            },
        )
        manifest = tmp_path / "cfg" / "projects.toml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            "[projects.presenter]\n"
            'host = "dev2"\naccount = "newlevel"\n'
            f'config = "{dev2_config}"\nmachines = ["linux-presenter"]\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            gateway_cli.main,
            [
                "--registry",
                str(tmp_path / "cfg" / "registry.json"),
                "--machines",
                str(tmp_path / "cfg" / "machines.json"),
                "--manifest",
                str(manifest),
                "machine",
                "import",
                "--from-manifest",
                "--local-host",
                "dev1",
            ],
        )

    assert result.exit_code == 0, result.output
    store = json.loads((tmp_path / "cfg" / "machines.json").read_text(encoding="utf-8"))
    assert store == {"linux-presenter": {"url": box.url, "key": key}}
    assert "presenter (ssh newlevel@dev2)" in result.output
    assert [c["tool"] for c in remote_calls(log)] == ["ssh"]
    assert key not in result.output and key not in json.dumps(remote_calls(log))


# -- review round 6 -------------------------------------------------------------------------------


def test_a_write_whose_content_arrived_short_is_refused_and_the_file_kept(tmp_path):
    source = open(fileops.__file__, encoding="utf-8").read()
    path = tmp_path / "configs" / "camera-box.mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    full = b'{"mcpServers": {"linux-cam1": {"type": "http"}}}\n'

    short = subprocess.run(
        [sys.executable, "-c", source, "write", str(path), "2026-10-21", str(len(full))],
        input=full[: len(full) // 2],  # an ssh that died mid-transfer: EOF too early
        capture_output=True,
    )
    whole = subprocess.run(
        [sys.executable, "-c", source, "write", str(path), "2026-10-21", str(len(full))],
        input=full,
        capture_output=True,
    )

    assert short.returncode != 0 and b"bytes" in short.stderr
    assert whole.returncode == 0 and path.read_bytes() == full
    backups = sorted(p.name for p in (path.parent / ".backup-2026-10-21").iterdir())
    assert backups == ["camera-box.mcp.json"]  # the refused write did not even make a backup


def test_a_config_inside_a_git_work_tree_is_never_written(tmp_path):
    repo = tmp_path / "some-project"
    (repo / ".git").mkdir(parents=True)  # a git work tree: a token here could be committed
    target = repo / "nested" / ".mcp.json"

    with pytest.raises(ValueError, match="git work tree"):
        fileops.write_with_backup(fileops.expand(str(target)), b"{}\n", "2026-10-21")

    assert not target.exists()
