"""The gateway's key store (#51): one machine key per machine, read live by the running gateway.

The point of #51: when a machine's key changes, ONE edit of the key store
(``remoteos-gateway machine set-key``) must reach every consumer, with no change on the
consumer side and no reconnect. These tests drive the gateway's PRODUCTION upstream client
against a real loopback machine (``fleet_harness.FakeFleetMachine``).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import httpx2
import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, fresh_secret, write_private_json

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.accesslog import AccessLog, read_access_log
from remoteos.gateway.app import create_app
from remoteos.gateway.registry import LiveConfig, load_machines, load_registry, token_sha256

MACHINE = "linux-test-cam"
CONSUMER = "camera-box@dev1"


def _tools_list(request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}


def _registry(path, token: str) -> None:
    write_private_json(
        path,
        {
            "exposed": [MACHINE],
            "consumers": {CONSUMER: {"sha256": [token_sha256(token)], "machines": [MACHINE], "added": "2026-10-21"}},
        },
    )


def test_a_key_changed_in_the_key_store_reaches_the_running_gateway_with_no_consumer_change(tmp_path):
    old_key, new_key = fresh_secret("oldkey"), fresh_secret("newkey")
    token = fresh_secret("projecttoken")
    registry_path = tmp_path / "cfg" / "registry.json"
    keystore_path = tmp_path / "cfg" / "machines.json"
    access_log_path = tmp_path / "state" / "access.jsonl"
    _registry(registry_path, token)

    with FakeFleetMachine(accept_keys=(new_key,)) as box:  # the box was re-keyed; the store is stale
        write_private_json(keystore_path, {MACHINE: {"url": box.url, "key": old_key}})

        async def go():
            # ONE running gateway, built exactly like `serve` builds it (production clients).
            app = create_app(LiveConfig(registry_path, keystore_path), AccessLog(access_log_path))
            auth = {"Authorization": f"Bearer {token}"}  # the consumer side never changes
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw.test") as c:
                before = await c.post(f"/{MACHINE}/mcp", json=_tools_list(1), headers=auth)
                result = await asyncio.to_thread(
                    CliRunner().invoke,
                    gateway_cli.main,
                    ["--registry", str(registry_path), "--machines", str(keystore_path), "machine", "set-key", MACHINE],
                    input=new_key + "\n",
                )
                after = await c.post(f"/{MACHINE}/mcp", json=_tools_list(2), headers=auth)
            await app.state.gateway.aclose()
            return before, result, after

        before, result, after = asyncio.run(go())
        seen = list(box.seen_authorization)

    assert before.status_code == 401  # the stale key, relayed unchanged
    assert result.exit_code == 0, result.output
    assert after.status_code == 200
    assert after.json()["id"] == 2
    # what the machine saw: the stale key, set-key's own probe of the new key, then the gateway with it
    assert seen == [f"Bearer {old_key}", f"Bearer {new_key}", f"Bearer {new_key}"]
    assert [r["outcome"] for r in read_access_log(access_log_path)] == ["upstream_denied", "ok"]
    stored = json.loads(keystore_path.read_text(encoding="utf-8"))
    assert stored[MACHINE] == {"url": box.url, "key": new_key}
    assert stat.S_IMODE(os.stat(keystore_path).st_mode) == 0o600
    for secret in (old_key, new_key, token):
        assert secret not in result.output


# -- set-key / list / repair-recipe: the CLI around the key store --------------------------


def _cli(tmp_path, *args, input=None):
    cfg = tmp_path / "cfg"
    paths = ["--registry", str(cfg / "registry.json"), "--machines", str(cfg / "machines.json")]
    return CliRunner().invoke(gateway_cli.main, [*paths, *args], input=input)


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "cfg" / "machines.json").read_text(encoding="utf-8"))


def test_set_key_refuses_a_key_the_machine_rejects_unless_forced(tmp_path):
    good, typo = fresh_secret("goodkey"), fresh_secret("typokey")
    with FakeFleetMachine(accept_keys=(good,)) as box:
        write_private_json(tmp_path / "cfg" / "machines.json", {MACHINE: {"url": box.url, "key": good}})
        before = (tmp_path / "cfg" / "machines.json").read_bytes()

        refused = _cli(tmp_path, "machine", "set-key", MACHINE, input=typo + "\n")
        unchanged = (tmp_path / "cfg" / "machines.json").read_bytes()
        forced = _cli(tmp_path, "machine", "set-key", MACHINE, "--force", input=typo + "\n")

    assert refused.exit_code == 1
    assert "REJECTS this key" in refused.output and "nothing changed" in refused.output
    assert unchanged == before
    assert forced.exit_code == 0, forced.output
    assert _stored(tmp_path)[MACHINE]["key"] == typo
    assert "WARNING: not verified" in forced.output
    for secret in (good, typo):
        assert secret not in refused.output + forced.output


@pytest.mark.parametrize("bad", ["", "Bearer abcdef", "has space", "a" * 257, "tab\there"])
def test_set_key_refuses_a_malformed_key_without_echoing_it(tmp_path, bad):
    write_private_json(tmp_path / "cfg" / "machines.json", {MACHINE: {"url": "http://cam.lan:8092/mcp", "key": "k1"}})

    result = _cli(tmp_path, "machine", "set-key", MACHINE, input=bad + "\n")

    assert result.exit_code == 1
    assert "must be 1-256 characters" in result.output
    if bad.strip():
        assert bad.strip() not in result.output
    assert _stored(tmp_path)[MACHINE]["key"] == "k1"


def test_set_key_adds_a_new_machine_only_with_url_and_exposes_it(tmp_path):
    key = fresh_secret("newkey")
    with FakeFleetMachine(accept_keys=(key,)) as box:
        unknown = _cli(tmp_path, "machine", "set-key", "linux-new", input=key + "\n")
        added = _cli(tmp_path, "machine", "set-key", "linux-new", "--url", box.url, input=key + "\n")

    assert unknown.exit_code == 1 and "--url" in unknown.output
    assert added.exit_code == 0, added.output
    assert "linux-new: added" in added.output and "probe with the new key: ok" in added.output
    assert _stored(tmp_path) == {"linux-new": {"url": box.url, "key": key}}
    assert "linux-new" in load_registry(tmp_path / "cfg" / "registry.json").exposed
    assert key not in added.output


def test_set_key_never_edits_a_legacy_mcp_config(tmp_path):
    legacy = {"mcpServers": {MACHINE: {"type": "http", "url": "http://cam.lan:8092/mcp", "headers": {}}}}
    write_private_json(tmp_path / "cfg" / "machines.json", legacy)

    result = _cli(tmp_path, "machine", "set-key", MACHINE, "--force", input=fresh_secret("k") + "\n")

    assert result.exit_code == 1
    assert "machine import" in result.output
    assert _stored(tmp_path) == legacy


def test_machine_list_masks_every_key_and_a_repair_recipe_is_validated_set_and_cleared(tmp_path):
    key = fresh_secret("listkey")
    write_private_json(tmp_path / "cfg" / "machines.json", {MACHINE: {"url": "http://cam.lan:8092/mcp", "key": key}})

    recipe = _cli(tmp_path, "machine", "repair-recipe", MACHINE, "--ssh", "root@cam.lan", "--sudo")
    listing = _cli(tmp_path, "machine", "list")
    injected = _cli(tmp_path, "machine", "repair-recipe", MACHINE, "--ssh", "root@cam.lan;reboot")
    cleared = _cli(tmp_path, "machine", "repair-recipe", MACHINE, "--clear")

    assert recipe.exit_code == 0, recipe.output
    assert listing.exit_code == 0
    assert f"{MACHINE}  http://cam.lan:8092/mcp  key <{len(key)} chars, sha256:" in listing.output
    assert "repair: ssh root@cam.lan sudo -n cat /etc/remoteos-mcp/config.json" in listing.output
    assert key not in listing.output
    assert injected.exit_code == 1 and "user@host" in injected.output
    assert cleared.exit_code == 0
    assert _stored(tmp_path) == {MACHINE: {"url": "http://cam.lan:8092/mcp", "key": key}}


# -- the running gateway and a broken key store (fail closed) --------------------------------


def test_an_invalid_key_store_at_reload_keeps_the_last_good_copy(tmp_path):
    key, token = fresh_secret("key"), fresh_secret("token")
    registry_path, keystore_path = tmp_path / "cfg" / "registry.json", tmp_path / "cfg" / "machines.json"
    _registry(registry_path, token)
    with FakeFleetMachine(accept_keys=(key,)) as box:
        write_private_json(keystore_path, {MACHINE: {"url": box.url, "key": key}})
        broken_stores = [
            {MACHINE: {"url": box.url, "key": key, "extra": 1}},  # an unknown field
            {MACHINE: {"url": "ftp://x/mcp", "key": key}},  # not http(s)
            {MACHINE: {"url": box.url, "key": "has space"}},  # not a header-safe key
            {MACHINE: "not-an-object"},
        ]

        async def go():
            app = create_app(LiveConfig(registry_path, keystore_path), AccessLog(tmp_path / "access.jsonl"))
            auth = {"Authorization": f"Bearer {token}"}
            codes = []
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw.test") as c:
                for broken in broken_stores:
                    write_private_json(keystore_path, broken)
                    codes.append((await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)).status_code)
                # proof the reload path is live: a VALID store with a key the box rejects is applied
                write_private_json(keystore_path, {MACHINE: {"url": box.url, "key": fresh_secret("wrong")}})
                codes.append((await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)).status_code)
            await app.state.gateway.aclose()
            return codes

        assert asyncio.run(go()) == [200, 200, 200, 200, 401]


def test_the_legacy_mcp_config_shape_still_loads_as_the_machine_source(tmp_path):
    key = fresh_secret("legacykey")
    entry = {"type": "http", "url": "http://cam.lan:8092/mcp", "headers": {"Authorization": f"Bearer {key}"}}
    path = write_private_json(tmp_path / "remoteos-mcp.mcp.json", {"mcpServers": {MACHINE: entry}})

    machines = load_machines(path)

    assert machines[MACHINE].url == "http://cam.lan:8092/mcp"
    assert machines[MACHINE].authorization == f"Bearer {key}"
    assert key not in repr(machines)
