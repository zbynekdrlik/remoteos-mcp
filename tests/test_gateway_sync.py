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


def test_a_project_leaving_the_manifest_loses_its_managed_consumer_but_a_hand_made_one_is_refused(fleet):
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    other = fleet.write_config("obs-studio", '{\n  "mcpServers": {}\n}\n')
    obs = {"host": "dev1", "account": ME, "config": str(other), "machines": ["win-stream-snv"]}
    fleet.write_manifest({"camera-box": fleet.camera_box_project(), "obs-studio": obs})
    assert fleet.run().exit_code == 0
    assert "obs-studio@dev1" in load_registry(fleet.registry).consumers

    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    dropped = fleet.run()

    assert dropped.exit_code == 0, dropped.output
    assert "obs-studio@dev1" not in load_registry(fleet.registry).consumers
    assert "obs-studio@dev1" in dropped.output

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
