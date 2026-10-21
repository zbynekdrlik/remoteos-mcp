"""``remoteos-gateway sync-configs`` (#51): generated project configs that route through the gateway.

For each project in ``projects.toml`` the sync ensures a consumer ``<project>@<host>`` with a
stable token and an allow-list EXACTLY equal to the manifest, and rewrites ONLY the fleet
entries of that project's config (same server names, so tool names do not change). Every other
entry stays byte-equal. Writes are atomic, 0600, after a backup; dev2 / other accounts go
through ``ssh`` / ``sudo -u`` (faked at the OS boundary here). No key or token may ever be
printed or logged. Every file lives under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import stat
from pathlib import Path

import httpx2
import pytest
from click.testing import CliRunner
from fleet_harness import (
    FakeFleetMachine,
    fresh_secret,
    install_fake_remote_tools,
    remote_calls,
    write_private_json,
)

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.accesslog import AccessLog
from remoteos.gateway.app import create_app
from remoteos.gateway.registry import LiveConfig, load_registry, token_sha256

GATEWAY = "http://100.104.8.125:8796"
ME = pwd.getpwuid(os.getuid()).pw_name
FLEET = ("win-stream-snv", "linux-cam1", "linux-cam2", "linux-presenter")


class Fleet:
    """Key store + registry + manifest + project configs under tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.cfg = tmp_path / "cfg"
        self.registry = self.cfg / "registry.json"
        self.keystore = self.cfg / "machines.json"
        self.manifest = self.cfg / "projects.toml"
        self.configs = tmp_path / "home" / ".claude" / "mcp-configs"
        self.keys = {name: fresh_secret("machinekey") for name in FLEET}
        self.urls = {name: f"http://{name}.lan:8092/mcp" for name in FLEET}
        self.david_token = fresh_secret("davidtoken")
        self.stale = {name: fresh_secret("stalekey") for name in FLEET}
        self.n8n_secret = fresh_secret("n8nsecret")
        write_private_json(self.keystore, {n: {"url": self.urls[n], "key": self.keys[n]} for n in FLEET})
        write_private_json(
            self.registry,
            {
                "exposed": list(FLEET),
                "consumers": {
                    "david3@subdev": {
                        "sha256": [token_sha256(self.david_token)],
                        "machines": ["win-stream-snv"],
                        "added": "2026-10-14",
                    }
                },
            },
        )

    # -- inputs -------------------------------------------------------------------------

    def camera_box_text(self) -> str:
        """A config as hand-edited today: legacy direct fleet entries + non-fleet entries in
        their own (not json.dumps) formatting."""
        s = self.stale
        return (
            "{\n"
            '  "mcpServers": {\n'
            '    "win-stream-snv": {\n'
            '      "type": "http",\n'
            '      "url": "http://stream.lan:8092/mcp",\n'
            f'      "headers": {{"Authorization": "Bearer {s["win-stream-snv"]}"}}\n'
            "    },\n"
            '    "reaperiem": {"command": "/usr/bin/python3", "args": ["-m", "reaperiem_mcp.server"], "cwd": "/srv"},\n'
            '    "linux-cam1": {\n'
            '      "type": "http",\n'
            '      "url": "http://cam1.lan:8092/mcp",\n'
            f'      "headers": {{"Authorization": "Bearer {s["linux-cam1"]}"}}\n'
            "    },\n"
            '    "n8n-mcp": {"type": "http", "url": "https://n8n.example.test/mcp",\n'
            f'                "headers": {{"Authorization": "Bearer {self.n8n_secret}"}}}},\n'
            '    "linux-presenter": {"type": "http", "url": "http://presenter.lan:8092/mcp", '
            f'"headers": {{"Authorization": "Bearer {s["linux-presenter"]}"}}}}\n'
            "  }\n"
            "}\n"
        )

    def non_fleet_fragments(self) -> list[str]:
        """The exact source text of every non-fleet entry of camera_box_text()."""
        s = self.stale
        return [
            '"reaperiem": {"command": "/usr/bin/python3", "args": ["-m", "reaperiem_mcp.server"], "cwd": "/srv"}',
            '"n8n-mcp": {"type": "http", "url": "https://n8n.example.test/mcp",\n'
            f'                "headers": {{"Authorization": "Bearer {self.n8n_secret}"}}}}',
            '"linux-presenter": {"type": "http", "url": "http://presenter.lan:8092/mcp", '
            f'"headers": {{"Authorization": "Bearer {s["linux-presenter"]}"}}}}',
        ]

    def write_config(self, project: str, text: str) -> Path:
        path = self.configs / f"{project}.mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def write_manifest(self, projects: dict[str, dict], gateway: str = GATEWAY) -> None:
        lines = ["[gateway]", f'url = "{gateway}"', ""]
        for name, project in projects.items():
            lines.append(f"[projects.{name}]")
            for key, value in project.items():
                lines.append(f"{key} = {json.dumps(value)}")
            lines.append("")
        self.manifest.write_text("\n".join(lines), encoding="utf-8")

    def camera_box_project(self, machines=("win-stream-snv", "linux-cam1", "linux-cam2")) -> dict:
        return {
            "host": "dev1",
            "account": ME,
            "config": str(self.configs / "camera-box.mcp.json"),
            "machines": list(machines),
        }

    # -- running ------------------------------------------------------------------------

    def run(self, *args: str):
        return CliRunner().invoke(
            gateway_cli.main,
            [
                "--registry",
                str(self.registry),
                "--machines",
                str(self.keystore),
                "--manifest",
                str(self.manifest),
                "sync-configs",
                "--local-host",
                "dev1",
                *args,
            ],
        )

    def secrets(self) -> list[str]:
        return [*self.keys.values(), *self.stale.values(), self.n8n_secret, self.david_token]

    @staticmethod
    def servers(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

    @staticmethod
    def token_of(path: Path, name: str) -> str:
        auth = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][name]["headers"]["Authorization"]
        assert auth.startswith("Bearer ")
        return auth[len("Bearer ") :]


@pytest.fixture
def fleet(tmp_path) -> Fleet:
    return Fleet(tmp_path)


def _gateway_entry(name: str, token: str) -> dict:
    return {"type": "http", "url": f"{GATEWAY}/{name}/mcp", "headers": {"Authorization": f"Bearer {token}"}}


def _assert_no_secret(text: str, secrets: list[str]) -> None:
    for secret in secrets:
        assert secret not in text


# -- what is rewritten, what is kept --------------------------------------------------------


def test_sync_rewrites_only_fleet_entries_and_every_other_entry_stays_byte_equal(fleet, caplog):
    caplog.set_level("DEBUG", logger="remoteos")
    original = fleet.camera_box_text()
    path = fleet.write_config("camera-box", original)
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})

    result = fleet.run()

    assert result.exit_code == 0, result.output
    text = path.read_text(encoding="utf-8")
    token = fleet.token_of(path, "linux-cam1")
    servers = fleet.servers(path)
    assert list(servers) == ["win-stream-snv", "reaperiem", "linux-cam1", "n8n-mcp", "linux-presenter", "linux-cam2"]
    for name in ("win-stream-snv", "linux-cam1", "linux-cam2"):
        assert servers[name] == _gateway_entry(name, token)  # same names, one project token
    for fragment in fleet.non_fleet_fragments():
        assert fragment in text  # byte-equal, formatting included
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    [backup_dir] = [p for p in path.parent.iterdir() if p.name.startswith(".backup-")]
    assert (backup_dir / "camera-box.mcp.json").read_text(encoding="utf-8") == original
    assert stat.S_IMODE(os.stat(backup_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(backup_dir / "camera-box.mcp.json").st_mode) == 0o600

    registry = load_registry(fleet.registry)
    consumer = registry.consumers["camera-box@dev1"]
    assert consumer.sha256 == [token_sha256(token)]
    assert consumer.machines == ["win-stream-snv", "linux-cam1", "linux-cam2"]  # exactly the manifest
    assert consumer.managed_by == "sync-configs"
    david = registry.consumers["david3@subdev"]  # a hand-made consumer is never touched
    assert david.sha256 == [token_sha256(fleet.david_token)]
    assert (david.machines, david.managed_by) == (["win-stream-snv"], "")

    assert "linux-presenter" in result.output and "not in the manifest" in result.output
    _assert_no_secret(result.output + caplog.text, [*fleet.secrets(), token])


def test_sync_is_idempotent_a_second_run_changes_nothing(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    first_text, first_mtime = path.read_bytes(), os.stat(path).st_mtime_ns
    first_registry = fleet.registry.read_bytes()

    again = fleet.run()

    assert again.exit_code == 0, again.output
    assert "camera-box: unchanged" in again.output
    assert path.read_bytes() == first_text
    assert os.stat(path).st_mtime_ns == first_mtime  # not even rewritten
    assert fleet.registry.read_bytes() == first_registry
    [backup_dir] = [p for p in path.parent.iterdir() if p.name.startswith(".backup-")]
    assert [p.name for p in backup_dir.iterdir()] == ["camera-box.mcp.json"]  # no second backup


def test_a_machine_removed_from_the_manifest_leaves_the_allow_list_and_the_config(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    token = fleet.token_of(path, "linux-cam1")

    fleet.write_manifest({"camera-box": fleet.camera_box_project(machines=("win-stream-snv", "linux-cam2"))})
    result = fleet.run()

    assert result.exit_code == 0, result.output
    servers = fleet.servers(path)
    assert "linux-cam1" not in servers
    assert list(servers) == ["win-stream-snv", "reaperiem", "n8n-mcp", "linux-presenter", "linux-cam2"]
    assert fleet.token_of(path, "linux-cam2") == token  # the token is stable
    consumer = load_registry(fleet.registry).consumers["camera-box@dev1"]
    assert consumer.machines == ["win-stream-snv", "linux-cam2"]
    assert consumer.sha256 == [token_sha256(token)]
    assert "- linux-cam1" in result.output


def test_dry_run_writes_nothing_and_masks_every_secret(fleet, caplog):
    caplog.set_level("DEBUG", logger="remoteos")
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    before_config, before_registry = path.read_bytes(), fleet.registry.read_bytes()

    result = fleet.run("--dry-run")

    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before_config
    assert fleet.registry.read_bytes() == before_registry
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".backup-")]
    out = result.output
    assert "dry run" in out.lower()
    assert "+ linux-cam2" in out and "~ win-stream-snv" in out and "~ linux-cam1" in out
    assert "camera-box@dev1" in out and "new token" in out
    _assert_no_secret(out + caplog.text, fleet.secrets())


# -- the generated token really works, and only for the manifest's machines ---------------


def test_the_generated_config_reaches_exactly_the_manifest_machines_through_the_gateway(tmp_path):
    fleet = Fleet(tmp_path)
    with FakeFleetMachine(accept_keys=(fleet.keys["linux-cam1"], fleet.keys["linux-cam2"])) as box:
        write_private_json(
            fleet.keystore,
            {name: {"url": box.url, "key": fleet.keys[name]} for name in ("linux-cam1", "linux-cam2")},
        )
        path = fleet.write_config("restreamer", '{\n  "mcpServers": {}\n}\n')
        fleet.write_manifest(
            {"restreamer": {"host": "dev1", "account": ME, "config": str(path), "machines": ["linux-cam1"]}}
        )
        assert fleet.run().exit_code == 0
        entry = fleet.servers(path)["linux-cam1"]
        token = fleet.token_of(path, "linux-cam1")

        async def go():
            app = create_app(LiveConfig(fleet.registry, fleet.keystore), AccessLog(tmp_path / "access.jsonl"))
            auth = {"Authorization": entry["headers"]["Authorization"]}
            call = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=GATEWAY) as c:
                allowed = await c.post("/linux-cam1/mcp", json=call, headers=auth)
                other = await c.post("/linux-cam2/mcp", json=call, headers=auth)
            await app.state.gateway.aclose()
            return allowed.status_code, other.status_code

        assert entry["url"] == f"{GATEWAY}/linux-cam1/mcp"
        assert asyncio.run(go()) == (200, 403)  # never broader than the manifest
        assert token not in box.seen_authorization[0]  # the machine got ITS key, never the token


# -- direct projects (remoteos-mcp), other hosts, other accounts --------------------------


def test_a_direct_project_gets_the_key_store_urls_and_keys_and_no_consumer(fleet):
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest(
        {
            "remoteos-mcp": {
                "host": "dev1",
                "account": ME,
                "config": str(path),
                "mode": "direct",
                "machines": ["linux-cam1", "win-stream-snv"],
            }
        }
    )

    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert fleet.servers(path) == {
        name: {"type": "http", "url": fleet.urls[name], "headers": {"Authorization": f"Bearer {fleet.keys[name]}"}}
        for name in ("linux-cam1", "win-stream-snv")
    }
    assert "remoteos-mcp@dev1" not in load_registry(fleet.registry).consumers
    _assert_no_secret(result.output, fleet.secrets())


def test_a_direct_project_on_another_host_is_refused_before_anything_is_written(fleet):
    fleet.write_manifest(
        {
            "presenter": {
                "host": "dev2",
                "account": "newlevel",
                "config": "~/.claude/mcp-configs/presenter.mcp.json",
                "mode": "direct",
                "machines": ["linux-cam1"],
            }
        }
    )
    before = fleet.registry.read_bytes()

    result = fleet.run()

    assert result.exit_code != 0
    assert "direct" in result.output and "dev1" in result.output
    assert fleet.registry.read_bytes() == before


def test_other_hosts_go_over_ssh_and_other_accounts_over_sudo_with_secrets_only_on_stdin(fleet, monkeypatch):
    log = install_fake_remote_tools(fleet.tmp / "bin", monkeypatch)
    presenter = fleet.write_config("presenter", fleet.camera_box_text())
    fohmixer = fleet.write_config("fohmixer", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest(
        {
            "presenter": {"host": "dev2", "account": "newlevel", "config": str(presenter), "machines": ["linux-cam1"]},
            "fohmixer": {"host": "dev1", "account": "fohmixer", "config": str(fohmixer), "machines": ["linux-cam2"]},
        }
    )

    result = fleet.run()

    assert result.exit_code == 0, result.output
    p_token, f_token = fleet.token_of(presenter, "linux-cam1"), fleet.token_of(fohmixer, "linux-cam2")
    assert p_token != f_token
    assert fleet.servers(fohmixer) == {"linux-cam2": _gateway_entry("linux-cam2", f_token)}
    assert any(p.name.startswith(".backup-") for p in presenter.parent.iterdir())
    calls = remote_calls(log)
    ssh = [c["argv"] for c in calls if c["tool"] == "ssh"]
    sudo = [c["argv"] for c in calls if c["tool"] == "sudo"]
    assert ssh and all("BatchMode=yes" in argv and "newlevel@dev2" in argv for argv in ssh)
    assert sudo and all(argv[:3] == ["-n", "-u", "fohmixer"] for argv in sudo)
    argv_text = json.dumps(calls)
    _assert_no_secret(argv_text, [*fleet.secrets(), p_token, f_token])  # contents travel on stdin only
    registry = load_registry(fleet.registry)
    assert registry.consumers["presenter@dev2"].machines == ["linux-cam1"]
    assert registry.consumers["fohmixer@dev1"].machines == ["linux-cam2"]


def test_missing_ssh_or_sudo_access_prints_the_one_time_step_and_the_rest_still_syncs(fleet, monkeypatch):
    install_fake_remote_tools(fleet.tmp / "bin", monkeypatch)
    monkeypatch.setenv("FAKE_SSH_DENY", "1")
    monkeypatch.setenv("FAKE_SUDO_DENY", "1")
    camera_box = fleet.write_config("camera-box", fleet.camera_box_text())
    presenter = fleet.write_config("presenter", '{\n  "mcpServers": {}\n}\n')
    fohmixer = fleet.write_config("fohmixer", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest(
        {
            "camera-box": fleet.camera_box_project(),
            "presenter": {"host": "dev2", "account": "newlevel", "config": str(presenter), "machines": ["linux-cam1"]},
            "fohmixer": {"host": "dev1", "account": "fohmixer", "config": str(fohmixer), "machines": ["linux-cam2"]},
        }
    )

    result = fleet.run()

    assert result.exit_code == 1
    assert "ssh-copy-id newlevel@dev2" in result.output
    assert "NOPASSWD" in result.output and "fohmixer" in result.output
    assert fleet.servers(camera_box)["linux-cam1"]["url"] == f"{GATEWAY}/linux-cam1/mcp"  # synced anyway
    consumers = load_registry(fleet.registry).consumers
    assert "presenter@dev2" not in consumers and "fohmixer@dev1" not in consumers  # no token without a config
    _assert_no_secret(result.output, fleet.secrets())


# -- fail closed ------------------------------------------------------------------------------


def test_a_manifest_machine_missing_from_the_key_store_refuses_the_whole_run(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project(machines=("linux-cam1", "linux-nope"))})
    before_config, before_registry = path.read_bytes(), fleet.registry.read_bytes()

    result = fleet.run()

    assert result.exit_code != 0
    assert "linux-nope" in result.output
    assert path.read_bytes() == before_config
    assert fleet.registry.read_bytes() == before_registry


def test_an_unparseable_config_is_never_overwritten(fleet):
    path = fleet.write_config("camera-box", '{"mcpServers": {"linux-cam1": ')
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})

    result = fleet.run()

    assert result.exit_code == 1
    assert path.read_text(encoding="utf-8") == '{"mcpServers": {"linux-cam1": '
    assert "camera-box@dev1" not in load_registry(fleet.registry).consumers


def test_a_project_leaving_the_manifest_loses_access_keeps_its_token_and_prune_removes_it(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    other = fleet.write_config("obs-studio", '{\n  "mcpServers": {}\n}\n')
    obs = {"host": "dev1", "account": ME, "config": str(other), "machines": ["win-stream-snv"]}
    fleet.write_manifest({"camera-box": fleet.camera_box_project(), "obs-studio": obs})
    assert fleet.run().exit_code == 0
    token = fleet.token_of(other, "win-stream-snv")

    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    dropped = fleet.run()

    assert dropped.exit_code == 0, dropped.output
    kept = load_registry(fleet.registry).consumers["obs-studio@dev1"]
    assert kept.machines == []  # access withdrawn: never broader than the manifest
    assert kept.sha256 == [token_sha256(token)]  # but the token survives a mistaken edit
    assert "obs-studio@dev1" in dropped.output and "--prune" in dropped.output

    fleet.write_manifest({"camera-box": fleet.camera_box_project(), "obs-studio": obs})
    restored = fleet.run()

    assert restored.exit_code == 0, restored.output
    assert fleet.token_of(other, "win-stream-snv") == token  # same token: no session restart needed
    assert load_registry(fleet.registry).consumers["obs-studio@dev1"].machines == ["win-stream-snv"]

    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    pruned = fleet.run("--prune")

    assert pruned.exit_code == 0, pruned.output
    assert "obs-studio@dev1" not in load_registry(fleet.registry).consumers
    assert "camera-box@dev1" in load_registry(fleet.registry).consumers

    # a consumer of the same name that sync did NOT create is never taken over
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["consumers"]["obs-studio@dev1"] = {"sha256": [token_sha256("x" * 43)], "machines": [], "added": "x"}
    write_private_json(fleet.registry, registry)
    fleet.write_manifest({"camera-box": fleet.camera_box_project(), "obs-studio": obs})
    refused = fleet.run()

    assert refused.exit_code == 1
    assert "not managed by sync-configs" in refused.output
    assert load_registry(fleet.registry).consumers["obs-studio@dev1"].machines == []
    assert path.exists()


def test_a_manifest_machine_that_is_not_exposed_is_written_but_warned_about(fleet):
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["exposed"] = ["win-stream-snv", "linux-cam1"]  # linux-cam2 behind the kill switch
    write_private_json(fleet.registry, registry)
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})

    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert "linux-cam2" in fleet.servers(path)
    assert "linux-cam2 is not exposed" in result.output
    assert load_registry(fleet.registry).exposed == ["win-stream-snv", "linux-cam1"]  # never re-exposed


# -- review round 1: the failure paths ---------------------------------------------------------


def test_a_write_that_landed_but_reported_failure_never_leaves_the_file_token_unknown(fleet, monkeypatch):
    from remoteos.gateway import targets

    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    original = targets.Target.write

    def landed_then_lost(self, config, text, tag):
        original(self, config, text, tag)  # the file IS replaced...
        raise targets.TargetError("ssh: connection lost after the write")  # ...but the reply is lost

    monkeypatch.setattr(targets.Target, "write", landed_then_lost)
    failed = fleet.run()

    assert failed.exit_code == 1
    token = fleet.token_of(path, "linux-cam1")
    consumer = load_registry(fleet.registry).consumers["camera-box@dev1"]
    assert token_sha256(token) in consumer.sha256  # the gateway knows the token the file holds
    assert consumer.machines == ["win-stream-snv", "linux-cam1", "linux-cam2"]  # the manifest, no more

    monkeypatch.setattr(targets.Target, "write", original)
    again = fleet.run()

    assert again.exit_code == 0, again.output
    assert load_registry(fleet.registry).consumers["camera-box@dev1"].sha256 == [token_sha256(token)]
    assert fleet.token_of(path, "linux-cam1") == token


def test_the_token_is_registered_before_the_file_carrying_it_is_written(fleet, monkeypatch):
    from remoteos.gateway import targets

    fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    original = targets.Target.write
    known_at_write: list[bool] = []

    def checking_write(self, config, text, tag):
        token = json.loads(text)["mcpServers"]["linux-cam1"]["headers"]["Authorization"].split(" ", 1)[1]
        consumer = load_registry(fleet.registry).consumers.get("camera-box@dev1")
        known_at_write.append(consumer is not None and token_sha256(token) in consumer.sha256)
        return original(self, config, text, tag)

    monkeypatch.setattr(targets.Target, "write", checking_write)

    assert fleet.run().exit_code == 0
    assert known_at_write == [True]


def test_a_failing_project_keeps_its_consumer_whose_allow_list_only_narrows(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    token = fleet.token_of(path, "linux-cam1")

    path.write_text('{"mcpServers": ', encoding="utf-8")  # the config broke meanwhile
    fleet.write_manifest(
        {"camera-box": fleet.camera_box_project(machines=("win-stream-snv", "linux-cam1", "linux-presenter"))}
    )
    result = fleet.run()

    assert result.exit_code == 1
    consumer = load_registry(fleet.registry).consumers["camera-box@dev1"]
    assert consumer.machines == ["win-stream-snv", "linux-cam1"]  # cam2 withdrawn, presenter NOT granted
    assert consumer.sha256 == [token_sha256(token)]
    assert path.read_text(encoding="utf-8") == '{"mcpServers": '


def test_an_empty_manifest_is_refused_and_withdraws_nothing(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    before = fleet.registry.read_bytes()

    fleet.write_manifest({})  # a truncated or emptied projects.toml
    result = fleet.run("--prune")

    assert result.exit_code != 0
    assert "no projects" in result.output
    assert fleet.registry.read_bytes() == before
    assert path.exists()


def test_a_config_whose_entries_do_not_change_is_left_byte_identical_whatever_its_layout(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    compact = json.dumps(json.loads(path.read_text(encoding="utf-8")), separators=(",", ":"))
    path.write_text(compact, encoding="utf-8")  # same entries, the owner's own compact layout
    mtime = os.stat(path).st_mtime_ns

    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert "camera-box: unchanged" in result.output
    assert path.read_text(encoding="utf-8") == compact
    assert os.stat(path).st_mtime_ns == mtime


def test_direct_mode_is_refused_for_another_account_even_on_the_gateway_host(fleet):
    fleet.write_manifest(
        {
            "fohmixer": {
                "host": "dev1",
                "account": "fohmixer" if ME != "fohmixer" else "someoneelse",
                "config": "~/.claude/mcp-configs/fohmixer.mcp.json",
                "mode": "direct",
                "machines": ["linux-cam1"],
            }
        }
    )
    before = fleet.registry.read_bytes()

    result = fleet.run()

    assert result.exit_code != 0
    assert f"only for the gateway's own account ({ME}@dev1)" in result.output
    assert "Nothing was changed" in result.output
    assert fleet.registry.read_bytes() == before


# -- review round 2: the token rules -------------------------------------------------------------


def _plant_token(path: Path, names: tuple[str, ...], token: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for name in names:
        data["mcpServers"][name]["headers"]["Authorization"] = f"Bearer {token}"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_a_token_in_the_file_that_is_not_this_consumers_is_never_adopted(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    machines = ("win-stream-snv", "linux-cam1", "linux-cam2")

    for planted in (fresh_secret("plantedtoken"), fleet.david_token):  # unregistered / another consumer's
        _plant_token(path, machines, planted)
        result = fleet.run()

        assert result.exit_code == 0, result.output
        minted = fleet.token_of(path, "linux-cam1")
        assert minted != planted
        registry = load_registry(fleet.registry)
        assert registry.consumers["camera-box@dev1"].sha256 == [token_sha256(minted)]
        assert registry.consumers["david3@subdev"].sha256 == [token_sha256(fleet.david_token)]  # untouched
        assert all(fleet.token_of(path, m) == minted for m in machines)


def test_a_token_change_on_an_existing_consumer_keeps_both_hashes_until_the_file_is_confirmed(fleet, monkeypatch):
    from remoteos.gateway import targets

    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    old = fleet.token_of(path, "linux-cam1")
    _plant_token(path, ("win-stream-snv", "linux-cam1", "linux-cam2"), fresh_secret("lostcopy"))  # forces a new one
    original = targets.Target.write

    def landed_then_lost(self, config, text, tag):
        original(self, config, text, tag)
        raise targets.TargetError("ssh: connection lost after the write")

    monkeypatch.setattr(targets.Target, "write", landed_then_lost)
    assert fleet.run().exit_code == 1
    new = fleet.token_of(path, "linux-cam1")
    assert new != old
    assert load_registry(fleet.registry).consumers["camera-box@dev1"].sha256 == [token_sha256(old), token_sha256(new)]

    monkeypatch.setattr(targets.Target, "write", original)
    assert fleet.run().exit_code == 0
    assert load_registry(fleet.registry).consumers["camera-box@dev1"].sha256 == [token_sha256(new)]  # pruned


def test_consumer_add_and_allow_refuse_to_widen_a_sync_managed_consumer(fleet):
    fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project(machines=("linux-cam1",))})
    assert fleet.run().exit_code == 0
    before = fleet.registry.read_bytes()
    base = ["--registry", str(fleet.registry), "--machines", str(fleet.keystore)]

    allow = CliRunner().invoke(gateway_cli.main, [*base, "allow", "camera-box@dev1", "linux-cam2"])
    add = CliRunner().invoke(
        gateway_cli.main, [*base, "consumer", "add", "camera-box@dev1", "--sha256", token_sha256("x" * 43)]
    )

    assert allow.exit_code == 1 and add.exit_code == 1
    assert "managed by sync-configs" in allow.output and "projects.toml" in allow.output
    assert "managed by sync-configs" in add.output
    assert fleet.registry.read_bytes() == before


# -- review round 3 -------------------------------------------------------------------------------


def test_a_dry_run_changes_no_consumer_on_any_path_not_even_with_prune(fleet):
    camera = fleet.write_config("camera-box", fleet.camera_box_text())
    obs = fleet.write_config("obs-studio", '{\n  "mcpServers": {}\n}\n')
    obs_project = {"host": "dev1", "account": ME, "config": str(obs), "machines": ["win-stream-snv"]}
    fleet.write_manifest({"camera-box": fleet.camera_box_project(), "obs-studio": obs_project})
    assert fleet.run().exit_code == 0

    camera.write_text('{"mcpServers": ', encoding="utf-8")  # camera-box will FAIL (narrowing path)
    narrowed = fleet.camera_box_project(machines=("win-stream-snv",))
    fleet.write_manifest({"camera-box": narrowed})  # obs-studio LEFT the manifest (withdraw path)
    before = fleet.registry.read_bytes()

    plain = fleet.run("--dry-run")
    pruned = fleet.run("--dry-run", "--prune")  # the delete path

    assert plain.exit_code == 1 and pruned.exit_code == 1  # camera-box still fails
    assert "obs-studio@dev1" in plain.output and "would be removed" in pruned.output
    assert fleet.registry.read_bytes() == before  # not one consumer touched


def test_rendering_into_an_empty_block_holding_a_newline_indents_like_json_dumps():
    from remoteos.gateway.sync import parse_config, render_config, render_entry

    entry = {"type": "http", "url": "http://gw.test/a/mcp", "headers": {"Authorization": "Bearer x"}}
    parsed = parse_config('{\n  "mcpServers": {\n  }\n}\n')

    text = render_config(parsed, [("linux-a", render_entry(entry, "    "))])

    assert text == json.dumps({"mcpServers": {"linux-a": entry}}, indent=2) + "\n"


def test_a_malformed_url_in_a_project_config_is_skipped_by_import_not_a_crash(fleet):
    entry = {"type": "http", "url": "http://[bad/mcp", "headers": {"Authorization": "Bearer abcdef"}}
    config = write_private_json(fleet.tmp / "bad.mcp.json", {"mcpServers": {"linux-cam9": entry}})
    result = CliRunner().invoke(
        gateway_cli.main,
        [
            "--registry",
            str(fleet.registry),
            "--machines",
            str(fleet.keystore),
            "--manifest",
            str(fleet.manifest),
            "machine",
            "import",
            "--from",
            str(config),
            "--dry-run",
        ],
    )

    assert result.exit_code == 1, result.output  # its only entry is skipped: no machine, nothing written
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "linux-cam9 url is not http(s)" in result.output


# -- review round 4 -------------------------------------------------------------------------------


def test_a_machine_removed_from_a_direct_project_takes_its_key_out_of_that_config(fleet):
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {\n    "n8n": {"command": "npx"}\n  }\n}\n')
    project = {"host": "dev1", "account": ME, "config": str(path), "mode": "direct"}
    fleet.write_manifest({"remoteos-mcp": {**project, "machines": ["linux-cam1", "linux-cam2"]}})
    assert fleet.run().exit_code == 0
    assert fleet.keys["linux-cam2"] in path.read_text(encoding="utf-8")

    fleet.write_manifest({"remoteos-mcp": {**project, "machines": ["linux-cam1"]}})
    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert list(fleet.servers(path)) == ["n8n", "linux-cam1"]
    assert fleet.keys["linux-cam2"] not in path.read_text(encoding="utf-8")  # never broader than the manifest
    assert "- linux-cam2" in result.output


# -- review round 5 -------------------------------------------------------------------------------


def test_a_new_project_gets_its_config_created_locally_and_over_ssh(fleet, monkeypatch):
    install_fake_remote_tools(fleet.tmp / "bin", monkeypatch)
    local = fleet.tmp / "home" / "new-local" / ".claude" / "mcp-configs" / "fresh.mcp.json"  # dirs missing too
    remote = fleet.tmp / "dev2-home" / ".claude" / "mcp-configs" / "fresh2.mcp.json"
    fleet.write_manifest(
        {
            "fresh": {"host": "dev1", "account": ME, "config": str(local), "machines": ["linux-cam1"]},
            "fresh2": {"host": "dev2", "account": "newlevel", "config": str(remote), "machines": ["linux-cam2"]},
        }
    )

    first = fleet.run()
    second = fleet.run()

    assert first.exit_code == 0, first.output
    assert first.output.count("backup: none — new file") == 2
    for path, name in ((local, "linux-cam1"), (remote, "linux-cam2")):
        assert list(fleet.servers(path)) == [name]
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert second.exit_code == 0 and "fresh: unchanged" in second.output and "fresh2: unchanged" in second.output


# -- review round 6 -------------------------------------------------------------------------------


def test_a_project_config_inside_a_git_work_tree_fails_that_project_locally_and_over_ssh(fleet, monkeypatch):
    install_fake_remote_tools(fleet.tmp / "bin", monkeypatch)
    repo = fleet.tmp / "repos" / "camera-box"
    (repo / ".git").mkdir(parents=True)
    local, remote = repo / ".mcp.json", repo / "sub" / "dev2.mcp.json"
    good = fleet.write_config("restreamer", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest(
        {
            "camera-box": {"host": "dev1", "account": ME, "config": str(local), "machines": ["linux-cam1"]},
            "presenter": {"host": "dev2", "account": "newlevel", "config": str(remote), "machines": ["linux-cam1"]},
            "restreamer": {"host": "dev1", "account": ME, "config": str(good), "machines": ["linux-cam2"]},
        }
    )

    result = fleet.run()

    assert result.exit_code == 1
    assert result.output.count("git work tree") == 2
    assert not local.exists() and not remote.exists()  # a token never lands where git could commit it
    consumers = load_registry(fleet.registry).consumers
    assert "restreamer@dev1" in consumers  # the others still sync
    # refused already when the config is READ: no token is minted or registered for a file
    # that can never be written (else every run would register one more unused hash)
    assert "camera-box@dev1" not in consumers and "presenter@dev2" not in consumers


def test_a_write_that_keeps_failing_never_piles_up_token_hashes(fleet, monkeypatch):
    from remoteos.gateway import targets

    def always_fails(self, config, text, tag):
        raise targets.TargetError("permission denied")

    fleet.write_manifest({"camera-box": fleet.camera_box_project()})  # a new project: no file yet
    monkeypatch.setattr(targets.Target, "write", always_fails)
    for _ in range(4):
        assert fleet.run().exit_code == 1

    # one previous hash is kept next to the new one (a failed write may have landed), no more
    assert len(load_registry(fleet.registry).consumers["camera-box@dev1"].sha256) <= 2


# -- review round 7 -------------------------------------------------------------------------------


def test_the_token_the_file_still_holds_stays_registered_through_any_number_of_failed_writes(fleet, monkeypatch):
    from remoteos.gateway import targets

    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    held = fleet.token_of(path, "linux-cam1")  # the token every running session of camera-box uses

    # the gateway moves to a new url: the file's entries no longer match, so a new token is minted...
    fleet.write_manifest({"camera-box": fleet.camera_box_project()}, gateway="http://dev1:8796")

    def never_lands(self, config, text, tag):
        raise targets.TargetError("permission denied")

    monkeypatch.setattr(targets.Target, "write", never_lands)
    for _ in range(3):  # ...and the write keeps failing
        assert fleet.run().exit_code == 1

    assert fleet.token_of(path, "linux-cam1") == held  # the file did not change
    consumer = load_registry(fleet.registry).consumers["camera-box@dev1"]
    assert token_sha256(held) in consumer.sha256  # so its token MUST still be known to the gateway
    assert len(consumer.sha256) <= 2


def test_a_landed_but_failed_write_then_a_url_change_still_keeps_the_files_token(fleet, monkeypatch):
    """The token kept next to a new one is the one the file HOLDS — not merely the oldest: a
    write can land and still report failure, so the file may hold the newest hash."""
    from remoteos.gateway import targets

    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    _plant_token(path, ("win-stream-snv", "linux-cam1", "linux-cam2"), fresh_secret("lostcopy"))
    original = targets.Target.write

    def landed_then_lost(self, config, text, tag):
        original(self, config, text, tag)
        raise targets.TargetError("ssh: connection lost after the write")

    monkeypatch.setattr(targets.Target, "write", landed_then_lost)
    assert fleet.run().exit_code == 1  # mints N, the file now holds N, the reply was lost
    held = fleet.token_of(path, "linux-cam1")

    def never_lands(self, config, text, tag):
        raise targets.TargetError("permission denied")

    fleet.write_manifest({"camera-box": fleet.camera_box_project()}, gateway="http://dev1:8796")
    monkeypatch.setattr(targets.Target, "write", never_lands)
    assert fleet.run().exit_code == 1  # the url change mints again; this write never lands

    assert fleet.token_of(path, "linux-cam1") == held
    assert token_sha256(held) in load_registry(fleet.registry).consumers["camera-box@dev1"].sha256


def test_a_kept_entry_keeps_its_whole_member_text_key_included(fleet):
    text = (
        "{\n"
        '  "mcpServers": {\n'
        '    "n8n\\u002dmcp" :  {"command": "npx"},\n'
        '    "linux-cam1": {"type": "http", "url": "http://cam1.lan:8092/mcp"}\n'
        "  }\n"
        "}\n"
    )
    path = fleet.write_config("camera-box", text)
    fleet.write_manifest({"camera-box": fleet.camera_box_project(machines=("linux-cam1",))})

    assert fleet.run().exit_code == 0

    assert '"n8n\\u002dmcp" :  {"command": "npx"}' in path.read_text(encoding="utf-8")  # key, spacing, value


# -- review round 8 -------------------------------------------------------------------------------


def test_a_machine_removed_together_with_a_gateway_url_change_still_leaves_the_config(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0

    # ONE edit: the gateway moves AND linux-cam2 leaves the project
    fleet.write_manifest(
        {"camera-box": fleet.camera_box_project(machines=("win-stream-snv", "linux-cam1"))}, gateway="http://dev1:8796"
    )
    result = fleet.run()

    assert result.exit_code == 0, result.output
    servers = fleet.servers(path)
    assert "linux-cam2" not in servers  # sync's own entry (its token says so), even at the old url
    assert "- linux-cam2" in result.output
    token = fleet.token_of(path, "linux-cam1")
    assert servers["linux-cam1"]["url"] == "http://dev1:8796/linux-cam1/mcp"
    assert load_registry(fleet.registry).consumers["camera-box@dev1"].sha256 == [token_sha256(token)]
    assert "linux-presenter" in servers  # a hand-made entry is still never touched


def test_a_config_that_cannot_be_written_as_utf8_fails_that_project_only(fleet):
    odd = fleet.write_config("odd", '{"note": "\\ud800"}\n')  # no mcpServers + a lone surrogate escape
    good = fleet.write_config("restreamer", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest(
        {
            "odd": {"host": "dev1", "account": ME, "config": str(odd), "machines": ["linux-cam1"]},
            "restreamer": {"host": "dev1", "account": ME, "config": str(good), "machines": ["linux-cam2"]},
        }
    )

    result = fleet.run()

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)  # no traceback
    assert odd.read_text(encoding="utf-8") == '{"note": "\\ud800"}\n'
    assert "linux-cam2" in fleet.servers(good)  # the other project still synced
    assert "odd@dev1" not in load_registry(fleet.registry).consumers  # no token for an unwritable file


# -- focused review of the round-8 fix ------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "server"),
    [
        ("gateway", '"hand": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer ab\\ud800"}}'),
        ("direct", '"hand": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer ab\\ud800"}}'),
        ("gateway", '"\\ud800x": {"command": "npx"}'),
    ],
    ids=["gateway-bearer", "direct-bearer", "entry-name"],
)
def test_a_lone_surrogate_anywhere_in_a_config_fails_that_project_only(fleet, mode, server):
    odd = fleet.write_config("odd", '{\n  "mcpServers": {\n    ' + server + "\n  }\n}\n")
    before = odd.read_bytes()
    good = fleet.write_config("restreamer", '{\n  "mcpServers": {}\n}\n')
    odd_project = {"host": "dev1", "account": ME, "config": str(odd), "machines": ["linux-cam1"], "mode": mode}
    fleet.write_manifest(
        {
            "odd": odd_project,
            "restreamer": {"host": "dev1", "account": ME, "config": str(good), "machines": ["linux-cam2"]},
        }
    )

    result = fleet.run()

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)  # no traceback
    assert odd.read_bytes() == before
    assert "linux-cam2" in fleet.servers(good)  # the other project still synced
    assert "odd@dev1" not in load_registry(fleet.registry).consumers


def test_a_hand_made_alias_carrying_this_projects_token_is_kept(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    assert fleet.run().exit_code == 0
    token = fleet.token_of(path, "linux-cam1")
    data = json.loads(path.read_text(encoding="utf-8"))
    alias = {"type": "http", "url": f"{GATEWAY}/linux-cam1/mcp", "headers": {"Authorization": f"Bearer {token}"}}
    data["mcpServers"]["cam1-alias"] = alias  # a person copied the entry under another name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert fleet.servers(path)["cam1-alias"] == alias  # not sync's shape (name != machine): never removed


def test_an_entry_carrying_another_consumers_token_is_kept(fleet):
    text = fleet.camera_box_text().replace(
        '    "reaperiem"',
        '    "win-old": {"type": "http", "url": "http://old:8796/win-old/mcp",'
        f' "headers": {{"Authorization": "Bearer {fleet.david_token}"}}}},\n    "reaperiem"',
    )
    path = fleet.write_config("camera-box", text)
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})

    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert "win-old" in fleet.servers(path)  # only THIS project's own token makes an entry sync's own
