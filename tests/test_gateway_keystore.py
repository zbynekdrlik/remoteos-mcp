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
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, fresh_secret, write_private_json

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.accesslog import AccessLog, read_access_log
from remoteos.gateway.app import create_app
from remoteos.gateway.registry import LiveConfig, token_sha256

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
