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

import contextlib
import json

import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, closed_port_url, footer_u, install_fake_airuleset
from test_gateway_sync import ME, Fleet

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.registry import load_registry

DIRECT = {"host": "dev1", "mode": "direct"}


@pytest.fixture
def fleet(tmp_path) -> Fleet:
    return Fleet(tmp_path)


def direct_project(fleet: Fleet, path, machines) -> dict:
    return {**DIRECT, "account": ME, "config": str(path), "machines": list(machines)}


def ledger(fleet: Fleet) -> dict:
    path = fleet.registry.with_name("sync-direct.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@contextlib.contextmanager
def read_only(directory):
    directory.chmod(0o500)
    try:
        yield
    finally:
        directory.chmod(0o700)


@contextlib.contextmanager
def locked_out(lock):
    """A lock file nobody may open: the step that takes it fails for real (PermissionError)."""
    lock.touch()
    lock.chmod(0o000)
    try:
        yield
    finally:
        lock.chmod(0o600)


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


def test_a_failed_write_keeps_the_union_so_the_old_entry_stays_owned(fleet):
    path = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam1"])})
    assert fleet.run().exit_code == 0

    fleet.write_manifest({"remoteos-mcp": direct_project(fleet, path, ["linux-cam2"])})
    with read_only(path.parent):  # a real failed write: no backup dir, no temp file
        assert fleet.run().exit_code == 1
    assert ledger(fleet) == {"remoteos-mcp": {m: fleet.urls[m] for m in ("linux-cam1", "linux-cam2")}}

    drop_from_keystore(fleet, "linux-cam1")
    result = fleet.run()
    assert result.exit_code == 0, result.output
    assert list(fleet.servers(path)) == ["linux-cam2"]
    assert ledger(fleet) == {"remoteos-mcp": {"linux-cam2": fleet.urls["linux-cam2"]}}


# -- machine remove / rename ----------------------------------------------------------------------

MANIFEST_COMMENT = "# the owner's note: keep this line"


def two_projects(fleet: Fleet):
    """camera-box (gateway) + remoteos-mcp (direct), synced once; the manifest carries a comment."""
    camera = fleet.write_config("camera-box", fleet.camera_box_text())
    direct = fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {}\n}\n')
    fleet.write_manifest(
        {
            "camera-box": fleet.camera_box_project(),
            "remoteos-mcp": direct_project(fleet, direct, ["linux-cam1", "linux-cam2", "win-stream-snv"]),
        }
    )
    fleet.manifest.write_text(MANIFEST_COMMENT + "\n" + fleet.manifest.read_text(encoding="utf-8"), encoding="utf-8")
    result = fleet.run()
    assert result.exit_code == 0, result.output
    return camera, direct


def machine(fleet: Fleet, *args: str):
    return CliRunner().invoke(
        gateway_cli.main,
        ["--registry", str(fleet.registry), "--machines", str(fleet.keystore), "--manifest", str(fleet.manifest),
         "machine", *args, "--local-host", "dev1"],
    )


def files(fleet: Fleet, *paths) -> dict:
    return {str(p): p.read_bytes() for p in (fleet.keystore, fleet.manifest, fleet.registry, *paths)}


def test_remove_takes_the_machines_out_of_everything_and_keeps_the_rest(fleet):
    camera, direct = two_projects(fleet)
    token = fleet.token_of(camera, "linux-cam1")
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["consumers"]["david3@subdev"]["machines"] = ["win-stream-snv", "linux-cam1"]
    fleet.registry.write_text(json.dumps(registry), encoding="utf-8")

    result = machine(fleet, "remove", "win-stream-snv", "linux-cam2")

    assert result.exit_code == 0, result.output
    keystore = json.loads(fleet.keystore.read_text(encoding="utf-8"))
    assert "win-stream-snv" not in keystore and "linux-cam2" not in keystore and "linux-cam1" in keystore
    manifest = fleet.manifest.read_text(encoding="utf-8")
    assert MANIFEST_COMMENT in manifest and "linux-cam2" not in manifest and "win-stream-snv" not in manifest
    registry = load_registry(fleet.registry)
    assert set(registry.exposed) == {"linux-cam1", "linux-presenter"}
    assert registry.consumers["camera-box@dev1"].machines == ["linux-cam1"]
    assert registry.consumers["david3@subdev"].machines == ["linux-cam1"]  # hand-made: narrowed too
    servers = fleet.servers(camera)
    assert "win-stream-snv" not in servers and "linux-cam2" not in servers
    assert {"linux-cam1", "fleet", "reaperiem", "n8n-mcp", "linux-presenter"} <= set(servers)
    assert fleet.token_of(camera, "linux-cam1") == token  # no new token, no restart beyond the entry
    assert list(fleet.servers(direct)) == ["linux-cam1"]
    assert ledger(fleet)["remoteos-mcp"] == {"linux-cam1": fleet.urls["linux-cam1"]}
    assert "win-stream-snv: removed" in result.output and fleet.urls["linux-cam2"] in result.output
    for secret in (*fleet.secrets(), token):
        assert secret not in result.output


def test_remove_works_before_the_direct_record_exists(fleet):
    _, direct = two_projects(fleet)
    fleet.registry.with_name("sync-direct.json").unlink()  # removed right after the upgrade

    result = machine(fleet, "remove", "linux-cam2")

    assert result.exit_code == 0, result.output
    assert list(fleet.servers(direct)) == ["linux-cam1", "win-stream-snv"]


def test_remove_refuses_to_empty_a_project_and_changes_nothing(fleet):
    camera, direct = two_projects(fleet)
    fleet.write_manifest(
        {
            "camera-box": fleet.camera_box_project(),
            "remoteos-mcp": direct_project(fleet, direct, ["linux-cam1", "linux-cam2"]),
        }
    )
    before = files(fleet, camera, direct)

    result = machine(fleet, "remove", "linux-cam1", "linux-cam2")

    assert result.exit_code == 1
    assert "remoteos-mcp would be left with no machines" in result.output
    assert files(fleet, camera, direct) == before


def test_remove_of_an_unknown_machine_changes_nothing(fleet):
    camera, direct = two_projects(fleet)
    before = files(fleet, camera, direct)
    result = machine(fleet, "remove", "linux-cam2", "linux-nope")
    assert result.exit_code == 1 and "linux-nope" in result.output
    assert files(fleet, camera, direct) == before


def test_a_remove_dry_run_writes_nothing_and_says_what_would_go(fleet):
    camera, direct = two_projects(fleet)
    before = files(fleet, camera, direct, fleet.registry.with_name("sync-direct.json"))
    result = machine(fleet, "remove", "linux-cam2", "--dry-run")
    assert result.exit_code == 0, result.output
    assert not (fleet.cfg / ".projects.lock").exists()  # not even a lock file
    assert "linux-cam2: would remove" in result.output and "- linux-cam2" in result.output
    assert files(fleet, camera, direct, fleet.registry.with_name("sync-direct.json")) == before
    for secret in fleet.secrets():
        assert secret not in result.output


def test_rename_moves_the_entry_access_and_both_configs(fleet):
    camera, direct = two_projects(fleet)
    token = fleet.token_of(camera, "linux-cam1")
    entry = json.loads(fleet.keystore.read_text(encoding="utf-8"))["linux-cam2"]
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["consumers"]["david3@subdev"]["machines"] = ["linux-cam2"]
    fleet.registry.write_text(json.dumps(registry), encoding="utf-8")

    result = machine(fleet, "rename", "linux-cam2", "linux-cam9")

    assert result.exit_code == 0, result.output
    keystore = json.loads(fleet.keystore.read_text(encoding="utf-8"))
    assert "linux-cam2" not in keystore and keystore["linux-cam9"] == entry  # url, key, org kept
    manifest = fleet.manifest.read_text(encoding="utf-8")
    assert '"win-stream-snv", "linux-cam1", "linux-cam9"' in manifest and MANIFEST_COMMENT in manifest
    registry = load_registry(fleet.registry)
    assert "linux-cam9" in registry.exposed and "linux-cam2" not in registry.exposed
    assert registry.consumers["camera-box@dev1"].machines == ["win-stream-snv", "linux-cam1", "linux-cam9"]
    assert registry.consumers["david3@subdev"].machines == ["linux-cam9"]
    servers = fleet.servers(camera)
    assert "linux-cam2" not in servers
    assert servers["linux-cam9"]["url"].endswith("/linux-cam9/mcp") and fleet.token_of(camera, "linux-cam9") == token
    direct_servers = fleet.servers(direct)
    assert "linux-cam2" not in direct_servers
    assert direct_servers["linux-cam9"]["url"] == fleet.urls["linux-cam2"]
    for secret in (*fleet.secrets(), token):
        assert secret not in result.output


def test_a_kill_switched_machine_stays_unexposed_under_its_new_name(fleet):
    two_projects(fleet)
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["exposed"].remove("linux-cam2")
    fleet.registry.write_text(json.dumps(registry), encoding="utf-8")

    result = machine(fleet, "rename", "linux-cam2", "linux-cam9")

    assert result.exit_code == 0, result.output
    exposed = load_registry(fleet.registry).exposed
    assert "linux-cam9" not in exposed and "linux-cam2" not in exposed
    assert "linux-cam9 is not exposed" in result.output


def test_rename_refuses_a_taken_or_invalid_name_and_changes_nothing(fleet):
    camera, direct = two_projects(fleet)
    before = files(fleet, camera, direct)
    for new in ("linux-cam1", "fleet", "bad name"):
        result = machine(fleet, "rename", "linux-cam2", new)
        assert result.exit_code != 0, (new, result.output)
    assert files(fleet, camera, direct) == before


def test_a_rename_dry_run_writes_nothing(fleet):
    camera, direct = two_projects(fleet)
    before = files(fleet, camera, direct)
    result = machine(fleet, "rename", "linux-cam2", "linux-cam9", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "would rename" in result.output and "+ linux-cam9" in result.output
    assert files(fleet, camera, direct) == before


# -- end to end: remove, then check ---------------------------------------------------------------


def test_after_remove_check_forgets_the_box_in_the_status_and_the_fleet_down_item(fleet, monkeypatch):
    project_dir = fleet.tmp / "devel" / "remoteos-mcp"
    (project_dir / ".git").mkdir(parents=True)
    script, _ = install_fake_airuleset(fleet.tmp / "airuleset", monkeypatch)
    monkeypatch.setenv("FAKE_NOTIFY_OWNER_NOTICE", "1")
    monkeypatch.setenv("FAKE_NOTIFY_STATUS", "suppressed")
    store = json.loads(fleet.keystore.read_text(encoding="utf-8"))
    for name in store:  # every box down, fast: a closed loopback port
        store[name]["url"] = closed_port_url()
    fleet.keystore.write_text(json.dumps(store), encoding="utf-8")
    two_projects(fleet)
    status = fleet.tmp / "state" / "machines-status.json"

    def check(gateway):
        return CliRunner().invoke(
            gateway_cli.main,
            ["--registry", str(fleet.registry), "--machines", str(fleet.keystore), "check",
             "--status-file", str(status), "--gateway-url", gateway.base_url, "--airuleset", str(script),
             "--releases-url", closed_port_url(), "--notify-project-dir", str(project_dir),
             "--notices-file", str(fleet.tmp / "state" / "owner-notices.json")],
        )

    with FakeFleetMachine(healthz=True) as gateway:
        assert check(gateway).exit_code == 0
        [down] = [body for key, body in footer_u(script.parent).items() if key.endswith("|fleet-down")]
        assert "linux-cam2" in down

        assert machine(fleet, "remove", "linux-cam2").exit_code == 0
        result = check(gateway)

    assert result.exit_code == 0, result.output
    machines = json.loads(status.read_text(encoding="utf-8"))["machines"]
    assert "linux-cam2" not in machines and "linux-cam1" in machines
    [down] = [body for key, body in footer_u(script.parent).items() if key.endswith("|fleet-down")]
    assert "linux-cam2" not in down and "linux-cam1" in down


# -- the manifest text edit -------------------------------------------------------------------------

LAYOUTS = '''[gateway]
url = "http://100.104.8.125:8796"

[projects.one]   # inline
host = "dev1"
account = "a"
config = "~/one.json"
machines = ["linux-a", "linux-gone", 'linux-b']

[projects.two]
host = "dev1"
account = "a"
config = "~/two.json"
machines = [
    "linux-gone",  # sold 2026-11-17
    "linux-a",
    # a comment line stays
    "linux-gone2"
]

[projects.three]
host = "dev1"
account = "a"
config = "~/three.json"
machines = ["linux-a", "linux-gone"]
'''


def test_remove_edits_inline_and_multi_line_arrays_and_keeps_comments(tmp_path):
    from remoteos.gateway.manifest import load_manifest, remove_machines

    path = tmp_path / "projects.toml"
    path.write_text(LAYOUTS, encoding="utf-8")
    path.chmod(0o600)

    after = remove_machines(path, ["linux-gone", "linux-gone2"])

    assert {n: p.machines for n, p in after.projects.items()} == {
        "one": ("linux-a", "linux-b"), "two": ("linux-a",), "three": ("linux-a",)}
    assert load_manifest(path) == after
    text = path.read_text(encoding="utf-8")
    assert "# inline" in text and "# a comment line stays" in text and "sold 2026" not in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_rename_keeps_the_order_and_a_dry_run_writes_nothing(tmp_path):
    from remoteos.gateway.manifest import rename_machine

    path = tmp_path / "projects.toml"
    path.write_text(LAYOUTS, encoding="utf-8")

    dry = rename_machine(path, "linux-gone", "linux-new", dry_run=True)
    assert path.read_text(encoding="utf-8") == LAYOUTS
    after = rename_machine(path, "linux-gone", "linux-new")

    assert dry == after
    assert after.projects["one"].machines == ("linux-a", "linux-new", "linux-b")
    assert after.projects["two"].machines == ("linux-new", "linux-a", "linux-gone2")
    assert "# sold 2026-11-17" in path.read_text(encoding="utf-8")


# -- review round 1: leftovers, failures half way, layouts --------------------------------------------


def test_rename_refuses_a_new_name_left_in_the_registry_and_changes_nothing(fleet):
    """A hand removal can leave the NEW name exposed or in an allow-list: carrying OLD over would
    hand the box to that consumer."""
    camera, direct = two_projects(fleet)
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["consumers"]["david3@subdev"]["machines"] = ["linux-cam9"]
    fleet.registry.write_text(json.dumps(registry), encoding="utf-8")
    before = files(fleet, camera, direct)

    result = machine(fleet, "rename", "linux-cam2", "linux-cam9")

    assert result.exit_code == 1
    assert "'linux-cam9' is still in the registry (allowed for david3@subdev)" in result.output
    assert files(fleet, camera, direct) == before


def test_a_remove_that_fails_half_way_says_what_was_done_and_a_rerun_finishes_it(fleet):
    camera, direct = two_projects(fleet)
    fleet.registry.with_name("sync-direct.json").unlink()  # the first removal after the upgrade
    with locked_out(fleet.keystore.with_name("machines.json.lock")):
        result = machine(fleet, "remove", "linux-cam2")
    assert result.exit_code == 1
    assert "done: direct record, manifest — rerun `remoteos-gateway machine remove linux-cam2`" in result.output
    assert "Traceback" not in result.output

    result = machine(fleet, "remove", "linux-cam2")

    assert result.exit_code == 0, result.output
    assert "linux-cam2" not in json.loads(fleet.keystore.read_text(encoding="utf-8"))
    assert "linux-cam2" not in load_registry(fleet.registry).exposed
    assert "linux-cam2" not in fleet.servers(direct) and "linux-cam2" not in fleet.servers(camera)


def test_a_rename_that_fails_half_way_is_finished_by_a_rerun(fleet):
    camera, direct = two_projects(fleet)
    with locked_out(fleet.keystore.with_name("machines.json.lock")):
        result = machine(fleet, "rename", "linux-cam2", "linux-cam9")
    assert result.exit_code == 1 and "done: direct record, manifest" in result.output

    result = machine(fleet, "rename", "linux-cam2", "linux-cam9")

    assert result.exit_code == 0, result.output
    assert "linux-cam9" in json.loads(fleet.keystore.read_text(encoding="utf-8"))
    assert "linux-cam9" in fleet.servers(direct) and "linux-cam2" not in fleet.servers(direct)
    assert "linux-cam9" in fleet.servers(camera) and "linux-cam2" not in fleet.servers(camera)


def test_a_remove_whose_sync_write_fails_without_a_record_is_finished_by_a_plain_sync(fleet):
    _, direct = two_projects(fleet)
    fleet.registry.with_name("sync-direct.json").unlink()
    with read_only(direct.parent):
        assert machine(fleet, "remove", "linux-cam2").exit_code == 1
    assert "linux-cam2" in fleet.servers(direct)  # the write failed

    result = fleet.run()  # no retired urls passed: the record kept them

    assert result.exit_code == 0, result.output
    assert list(fleet.servers(direct)) == ["linux-cam1", "win-stream-snv"]


def test_remove_handles_crlf_and_a_trailing_comma_without_leaving_blanks(tmp_path):
    from remoteos.gateway.manifest import remove_machines

    path = tmp_path / "projects.toml"
    text = (
        LAYOUTS.replace("[projects.three]", '[projects.four]\nhost = "dev1"\naccount = "a"\nconfig = "~/four.json"\n'
                        'machines = ["linux-a", "linux-gone",\n]\n\n[projects.three]')
    ).replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))

    after = remove_machines(path, ["linux-gone", "linux-gone2"])

    assert after.projects["four"].machines == ("linux-a",) and after.projects["two"].machines == ("linux-a",)
    written = path.read_bytes().decode("utf-8")
    assert "linux-gone" not in written and ", \r\n" not in written and " \r\n" not in written


# -- review round 2 -------------------------------------------------------------------------------


def test_rename_of_an_unexposed_unallowed_box_still_refuses_a_new_name_left_in_the_registry(fleet):
    """The stale check must run even when nothing carries OLD (a kill-switched box only a direct
    project uses): otherwise the leftover consumer gets the renamed box."""
    camera, direct = two_projects(fleet)
    registry = json.loads(fleet.registry.read_text(encoding="utf-8"))
    registry["exposed"] = [m for m in registry["exposed"] if m != "linux-cam2"] + ["linux-cam9"]
    for consumer in registry["consumers"].values():
        consumer["machines"] = [m for m in consumer["machines"] if m != "linux-cam2"]
    registry["consumers"]["david3@subdev"]["machines"] = ["linux-cam9"]
    fleet.registry.write_text(json.dumps(registry), encoding="utf-8")
    before = files(fleet, camera, direct)

    result = machine(fleet, "rename", "linux-cam2", "linux-cam9")

    assert result.exit_code == 1 and "'linux-cam9' is still in the registry" in result.output
    assert files(fleet, camera, direct) == before


def test_a_remove_whose_sync_cannot_run_points_to_sync_configs_which_finishes_it(fleet):
    _, direct = two_projects(fleet)
    other = fleet.write_config("other", '{\n  "mcpServers": {}\n}\n')
    text = fleet.manifest.read_text(encoding="utf-8")
    # sync refuses the whole run (direct mode for another account), only after every edit step
    bad = '\n[projects.other]\nhost = "dev1"\naccount = "someone-else"\nconfig = "%s"\nmode = "direct"\n' % other
    fleet.manifest.write_text(text + bad + 'machines = ["linux-cam1"]\n', encoding="utf-8")

    result = machine(fleet, "remove", "linux-cam2")

    assert result.exit_code == 1
    assert "done: direct record, manifest, key store, registry — rerun `remoteos-gateway sync-configs`" in result.output
    fleet.manifest.write_text(fleet.manifest.read_text(encoding="utf-8").split("\n[projects.other]")[0] + "\n",
                              encoding="utf-8")
    assert fleet.run().exit_code == 0
    assert "linux-cam2" not in fleet.servers(direct)


def test_a_typo_for_old_next_to_an_existing_new_is_refused_not_reported_as_a_rename(fleet):
    camera, direct = two_projects(fleet)
    before = files(fleet, camera, direct, fleet.registry.with_name("sync-direct.json"))
    result = machine(fleet, "rename", "linux-typo", "linux-cam1")
    assert result.exit_code == 1 and "no earlier rename of it is in progress" in result.output
    assert files(fleet, camera, direct, fleet.registry.with_name("sync-direct.json")) == before
