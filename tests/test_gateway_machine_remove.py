"""Taking a machine out of the fleet (#109): the sync's direct-entry record, ``machine remove`` and
``machine rename``.

Owner 2026-11-17: sold boxes (mac-propresenter, win-iem-snv) had to be removed by hand, and each
time the DIRECT remoteos-mcp config kept the dead entry ("unchanged"): in direct mode an entry was
sync's only while its url was in the key store. ``sync-direct.json`` (next to the registry) now
records what sync wrote into each direct config, so a recorded entry whose machine left the key
store is removed; a hand-made entry (never recorded) is kept, as gateway.md requires.

Everything runs the real CLI against files under ``tmp_path`` (``Fleet`` from the sync tests); the
end-to-end check test probes real loopback HTTP servers (``FakeFleetMachine``).
"""

from __future__ import annotations

import json

import pytest
from test_gateway_sync import ME, Fleet

from remoteos.gateway import targets

DIRECT = {"host": "dev1", "mode": "direct"}


@pytest.fixture
def fleet(tmp_path) -> Fleet:
    return Fleet(tmp_path)


def direct_project(fleet: Fleet, path, machines) -> dict:
    return {**DIRECT, "account": ME, "config": str(path), "machines": list(machines)}


def ledger(fleet: Fleet) -> dict:
    path = fleet.registry.with_name("sync-direct.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def drop_from_keystore(fleet: Fleet, *names: str) -> None:
    store = json.loads(fleet.keystore.read_text(encoding="utf-8"))
    for name in names:
        del store[name]
    fleet.keystore.write_text(json.dumps(store), encoding="utf-8")


# -- the direct-entry record ----------------------------------------------------------------------


def test_a_direct_entry_sync_wrote_is_removed_once_its_machine_left_the_key_store(fleet):
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {\n    "n8n": {"command": "npx"}\n  }\n}\n')
    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam1", "linux-cam2"])})
    assert fleet.run().exit_code == 0
    assert ledger(fleet) == {"remoteos-mcp": {m: fleet.urls[m] for m in ("linux-cam1", "linux-cam2")}}

    # the hand removal of 2026-11-17: out of the manifest AND the key store
    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam1"])})
    drop_from_keystore(fleet, "linux-cam2")
    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert list(fleet.servers(path)) == ["n8n", "linux-cam1"]
    assert fleet.keys["linux-cam2"] not in path.read_text(encoding="utf-8")
    assert "- linux-cam2" in result.output
    assert ledger(fleet) == {"remoteos-mcp": {"linux-cam1": fleet.urls["linux-cam1"]}}
    for secret in fleet.secrets():
        assert secret not in result.output
        assert secret not in json.dumps(ledger(fleet))  # urls only, never a key


def test_a_hand_made_direct_entry_is_never_recorded_and_always_kept(fleet):
    hand_made = '"linux-dev2-only": {"type": "http", "url": "http://10.9.9.9:8092/mcp"}'
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {\n    ' + hand_made + "\n  }\n}\n")
    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam1"])})
    for _ in range(2):
        result = fleet.run()
        assert result.exit_code == 0, result.output
        assert hand_made in path.read_text(encoding="utf-8")
    assert ledger(fleet) == {"remoteos-mcp": {"linux-cam1": fleet.urls["linux-cam1"]}}


def test_the_first_run_seeds_the_record_from_entries_already_at_the_key_store_urls(fleet):
    """A config written before the record existed (today's dev1): an UNCHANGED run seeds it, so the
    next removal works; nothing extra is removed on that first run."""
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam1", "linux-cam2"])})
    assert fleet.run().exit_code == 0
    fleet.registry.with_name("sync-direct.json").unlink()  # as before the upgrade
    before = path.read_bytes()

    dry = fleet.run("--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "sync-direct.json" in dry.output and not fleet.registry.with_name("sync-direct.json").exists()
    result = fleet.run()

    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before
    assert ledger(fleet) == {"remoteos-mcp": {m: fleet.urls[m] for m in ("linux-cam1", "linux-cam2")}}


def test_a_failed_write_keeps_the_union_so_the_old_entry_stays_owned(fleet, monkeypatch):
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam1"])})
    assert fleet.run().exit_code == 0

    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam2"])})

    def fails(self, config, text, tag):
        raise targets.TargetError("permission denied")

    monkeypatch.setattr(targets.Target, "write", fails)
    assert fleet.run().exit_code == 1
    assert ledger(fleet) == {"remoteos-mcp": {m: fleet.urls[m] for m in ("linux-cam1", "linux-cam2")}}

    monkeypatch.undo()
    drop_from_keystore(fleet, "linux-cam1")
    result = fleet.run()
    assert result.exit_code == 0, result.output
    assert list(fleet.servers(path)) == ["linux-cam2"]
    assert ledger(fleet) == {"remoteos-mcp": {"linux-cam2": fleet.urls["linux-cam2"]}}
