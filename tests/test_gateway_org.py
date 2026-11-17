"""The company a machine belongs to (#105, owner 2026-11-17): Slovnormal boxes kept apart from nlmedia.

Every key-store machine carries ``org`` (``nlmedia`` / ``slovnormal``). A machine without one still
serves (the gateway must not go dark over a metadata field), but ``sync-configs`` refuses the whole
run and ``machine list`` warns until ``machine set-org`` set it. A project may hold machines of ONE
org only; the fleet-ops project ``remoteos-mcp`` is the only exception. ``enroll`` takes the org
and refuses a project of another org before any box is touched. The ``fleet-down`` owner notice is
one text grouped by org. Real CLI, real files under tmp_path, real loopback machines; no key may
appear in any output.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, closed_port_url, fresh_secret, write_private_json
from test_gateway_check import Check
from test_gateway_enroll import Fleet as EnrollFleet
from test_gateway_sync import Fleet as SyncFleet

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway import fleet_notice
from remoteos.gateway.registry import MachineSourceError, parse_keystore
from remoteos.gateway.targets import current_account


def _cli(tmp_path, *args, input=None):
    cfg = tmp_path / "cfg"
    paths = ["--registry", str(cfg / "registry.json"), "--machines", str(cfg / "machines.json")]
    return CliRunner().invoke(gateway_cli.main, [*paths, *args], input=input)


def _store(tmp_path, entries: dict):
    return write_private_json(tmp_path / "cfg" / "machines.json", entries)


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "cfg" / "machines.json").read_text(encoding="utf-8"))


# -- the key-store field ------------------------------------------------------------------------


def test_an_org_outside_the_known_set_is_refused_without_echoing_the_value():
    pasted = fresh_secret("keyinorg")
    with pytest.raises(MachineSourceError) as caught:
        parse_keystore({"linux-a": {"url": "http://a.lan:8092/mcp", "key": "k1", "org": pasted}})
    assert "nlmedia, slovnormal" in str(caught.value)
    assert pasted not in str(caught.value)


def test_an_entry_without_org_still_loads_so_the_gateway_keeps_serving():
    entries = parse_keystore({"linux-a": {"url": "http://a.lan:8092/mcp", "key": "k1"}})
    assert entries["linux-a"].org is None
    assert entries["linux-a"].to_json() == {"url": "http://a.lan:8092/mcp", "key": "k1"}


# -- machine set-org / list / set-key -----------------------------------------------------------


def test_set_org_sets_only_the_org_and_keeps_url_key_and_repair(tmp_path):
    keys = {name: fresh_secret("k") for name in ("win-pz-server", "win-spisska-pc", "linux-cam1")}
    recipe = {"ssh": "root@pz.lan", "key_file": "/etc/remoteos-mcp/config.json", "sudo": True}
    _store(
        tmp_path,
        {
            "win-pz-server": {"url": "http://pz.lan:8092/mcp", "key": keys["win-pz-server"], "repair": recipe},
            "win-spisska-pc": {"url": "http://sp.lan:8092/mcp", "key": keys["win-spisska-pc"]},
            "linux-cam1": {"url": "http://cam1.lan:8092/mcp", "key": keys["linux-cam1"]},
        },
    )

    first = _cli(tmp_path, "machine", "set-org", "slovnormal", "win-pz-server", "win-spisska-pc")
    again = _cli(tmp_path, "machine", "set-org", "slovnormal", "win-pz-server")

    assert first.exit_code == 0, first.output
    assert "org slovnormal: win-pz-server, win-spisska-pc" in first.output
    assert again.exit_code == 0 and "already slovnormal: win-pz-server" in again.output
    stored = _stored(tmp_path)
    assert stored["win-pz-server"] == {
        "url": "http://pz.lan:8092/mcp", "key": keys["win-pz-server"], "org": "slovnormal", "repair": recipe
    }
    assert stored["win-spisska-pc"]["org"] == "slovnormal"
    assert "org" not in stored["linux-cam1"]
    for key in keys.values():
        assert key not in first.output and key not in again.output


def test_set_org_with_one_unknown_name_changes_nothing(tmp_path):
    _store(tmp_path, {"linux-cam1": {"url": "http://cam1.lan:8092/mcp", "key": "k1"}})
    before = (tmp_path / "cfg" / "machines.json").read_bytes()

    result = _cli(tmp_path, "machine", "set-org", "nlmedia", "linux-cam1", "linux-nope")
    typo = _cli(tmp_path, "machine", "set-org", "slovnormall", "linux-cam1")

    assert result.exit_code == 1 and "linux-nope" in result.output and "nothing changed" in result.output
    assert typo.exit_code == 2 and "slovnormall" in typo.output
    assert (tmp_path / "cfg" / "machines.json").read_bytes() == before


def test_machine_list_shows_the_org_counts_and_warns_about_a_missing_one(tmp_path):
    key = fresh_secret("listkey")
    _store(
        tmp_path,
        {
            "win-pz-server": {"url": "http://pz.lan:8092/mcp", "key": key, "org": "slovnormal"},
            "linux-cam1": {"url": "http://cam1.lan:8092/mcp", "key": key, "org": "nlmedia"},
            "linux-cam2": {"url": "http://cam2.lan:8092/mcp", "key": key},
        },
    )

    listing = _cli(tmp_path, "machine", "list")

    assert listing.exit_code == 0
    assert "win-pz-server  org slovnormal  sleep off  http://pz.lan:8092/mcp" in listing.output
    assert "linux-cam2  org MISSING  sleep off  http://cam2.lan:8092/mcp" in listing.output
    assert "orgs: nlmedia 1, slovnormal 1" in listing.output
    assert "WARNING: 1 machine(s) have NO org: linux-cam2" in listing.output
    assert "machine set-org" in listing.output
    assert key not in listing.output


def test_set_key_adds_a_machine_only_with_its_org_and_keeps_the_org_on_a_key_change(tmp_path):
    key, newer = fresh_secret("newkey"), fresh_secret("newerkey")
    with FakeFleetMachine(accept_keys=(key, newer)) as box:
        no_org = _cli(tmp_path, "machine", "set-key", "linux-new", "--url", box.url, input=key + "\n")
        added = _cli(
            tmp_path, "machine", "set-key", "linux-new", "--url", box.url, "--org", "slovnormal", input=key + "\n"
        )
        rotated = _cli(tmp_path, "machine", "set-key", "linux-new", input=newer + "\n")

    assert no_org.exit_code == 1 and "--org nlmedia|slovnormal" in no_org.output
    assert added.exit_code == 0, added.output
    assert rotated.exit_code == 0, rotated.output
    assert _stored(tmp_path) == {"linux-new": {"url": box.url, "key": newer, "org": "slovnormal"}}


def test_an_import_keeps_the_org_of_a_known_machine_and_a_new_one_gets_none(tmp_path):
    old, new, other = fresh_secret("old"), fresh_secret("new"), fresh_secret("other")
    with FakeFleetMachine(accept_keys=(new,)) as box, FakeFleetMachine(accept_keys=(other,)) as second:
        _store(tmp_path, {"linux-cam1": {"url": box.url, "key": old, "org": "nlmedia"}})
        config = tmp_path / "camera-box.mcp.json"
        config.write_text(json.dumps({"mcpServers": {
            "linux-cam1": {"type": "http", "url": box.url, "headers": {"Authorization": f"Bearer {new}"}},
            "linux-cam9": {"type": "http", "url": second.url, "headers": {"Authorization": f"Bearer {other}"}},
        }}), encoding="utf-8")
        result = _cli(tmp_path, "machine", "import", "--from", str(config))

    assert result.exit_code == 0, result.output
    stored = _stored(tmp_path)
    assert stored["linux-cam1"] == {"url": box.url, "key": new, "org": "nlmedia"}
    assert "org" not in stored["linux-cam9"]  # sync-configs refuses until set-org names it


# -- sync-configs: one org per project ----------------------------------------------------------


@pytest.fixture
def fleet(tmp_path) -> SyncFleet:
    return SyncFleet(tmp_path)


def _set_orgs(fleet: SyncFleet, orgs: dict[str, str | None]) -> None:
    store = json.loads(fleet.keystore.read_text(encoding="utf-8"))
    for name, org in orgs.items():
        if org is None:
            store[name].pop("org", None)
        else:
            store[name]["org"] = org
    write_private_json(fleet.keystore, store)


def test_a_project_mixing_two_orgs_refuses_the_whole_run(fleet):
    _set_orgs(fleet, {"linux-cam2": "slovnormal"})
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    before_config, before_registry = path.read_bytes(), fleet.registry.read_bytes()

    result = fleet.run()

    assert result.exit_code != 0
    assert "[projects.camera-box] mixes nlmedia (linux-cam1, win-stream-snv), slovnormal (linux-cam2)" in result.output
    assert "Nothing was changed" in result.output
    assert path.read_bytes() == before_config
    assert fleet.registry.read_bytes() == before_registry


def test_a_manifest_machine_without_org_refuses_the_whole_run_and_names_the_fix(fleet):
    _set_orgs(fleet, {"linux-cam1": None})
    path = fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    before_config, before_registry = path.read_bytes(), fleet.registry.read_bytes()

    result = fleet.run()

    assert result.exit_code != 0
    assert "manifest machines with no org in the key store: linux-cam1" in result.output
    assert "machine set-org" in result.output
    assert path.read_bytes() == before_config
    assert fleet.registry.read_bytes() == before_registry


def test_remoteos_mcp_is_the_one_project_that_may_reach_both_orgs(fleet):
    _set_orgs(fleet, {"linux-cam2": "slovnormal"})
    fleet.write_config("remoteos-mcp", '{\n  "mcpServers": {}\n}\n')
    project = fleet.camera_box_project()
    project["config"] = str(fleet.configs / "remoteos-mcp.mcp.json")
    fleet.write_manifest({"remoteos-mcp": project})

    result = fleet.run()

    assert result.exit_code == 0, result.output
    servers = fleet.servers(fleet.configs / "remoteos-mcp.mcp.json")
    assert {"win-stream-snv", "linux-cam1", "linux-cam2"} <= set(servers)


def test_an_unlisted_machine_without_org_does_not_block_the_run(fleet):
    _set_orgs(fleet, {"linux-presenter": None})  # in the key store, in no project
    fleet.write_config("camera-box", fleet.camera_box_text())
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})

    assert fleet.run().exit_code == 0


# -- enroll -------------------------------------------------------------------------------------


@pytest.fixture
def enrolling(tmp_path, monkeypatch):
    world = EnrollFleet(tmp_path, monkeypatch)
    yield world
    world.close()


def test_enroll_needs_an_org(enrolling):
    result = enrolling.enroll("--projects", "camera-box", org=None)
    assert result.exit_code == 2 and "--org" in result.output
    assert enrolling.calls() == []


def test_enroll_into_a_project_of_another_org_is_refused_before_the_box_is_touched(enrolling):
    result = enrolling.enroll("--projects", "camera-box", org="slovnormal")

    assert result.exit_code == 1
    assert "camera-box holds machines of another org than slovnormal: linux-cam2 (nlmedia)" in result.output
    assert enrolling.calls() == []
    assert "win-new-pc" not in json.loads(enrolling.machines.read_text())


def test_an_enrolled_machine_is_stored_with_its_org(enrolling):
    with FakeFleetMachine(accept_keys=(enrolling.key,), version="0.7.0.dev50") as machine:
        enrolling.post(machine.url)
        result = enrolling.enroll("--projects", "camera-box", "--url", machine.url, org="nlmedia")
        assert result.exit_code == 0, result.output
    assert json.loads(enrolling.machines.read_text())["win-new-pc"]["org"] == "nlmedia"


# -- check + the fleet-down notice ----------------------------------------------------------------


def test_check_records_each_machines_org_in_the_status(tmp_path, monkeypatch):
    check = Check(tmp_path, monkeypatch)
    check.store(
        {
            "win-pz-server": {"url": closed_port_url(), "key": fresh_secret("k"), "org": "slovnormal"},
            "linux-cam4": {"url": closed_port_url(), "key": fresh_secret("k")},
        }
    )
    with FakeFleetMachine(healthz=True) as gateway:
        assert check.run(gateway.base_url).exit_code == 0

    machines = check.state()["machines"]
    assert machines["win-pz-server"]["org"] == "slovnormal"
    assert "org" not in machines["linux-cam4"]


def _record(state: str, org: str | None) -> dict:
    record = {"state": state, "since": "2026-11-17T08:00:00.000+00:00", "detail": "x", "notified": False}
    if org is not None:
        record["org"] = org
    return record


def test_fleet_down_is_one_text_grouped_slovnormal_then_nlmedia_then_none():
    machines = {
        "linux-cam1": _record("unreachable", "nlmedia"),
        "win-pz-server": _record("unreachable", "slovnormal"),
        "win-holla-pc": _record("unreachable", "slovnormal"),
        "linux-odd": _record("unreachable", None),
        "linux-ok": _record("ok", "nlmedia"),
    }

    notice = fleet_notice.build(machines, "dev1")[fleet_notice.DOWN]

    lines = notice.text.splitlines()
    assert lines[0] == "MCP brána (dev1): nefunkčné stroje (4)"
    order = [line.split(":")[0].lstrip("• ") for line in lines[1:]]
    assert order == ["Slovnormal", "win-holla-pc", "win-pz-server", "nlmedia", "linux-cam1", "bez organizácie",
                     "linux-odd"]
    assert notice.fingerprint[0] == ["win-holla-pc", "unreachable", "slovnormal"]


def test_a_machine_changing_org_changes_the_fleet_down_fingerprint():
    before = fleet_notice.build({"linux-a": _record("unreachable", None)}, "dev1")[fleet_notice.DOWN]
    after = fleet_notice.build({"linux-a": _record("unreachable", "nlmedia")}, "dev1")[fleet_notice.DOWN]
    assert before.fingerprint != after.fingerprint


def test_a_cut_grouped_fleet_down_counts_only_hidden_boxes_and_never_ends_on_a_header():
    machines = {}
    for index in range(30):
        machines[f"win-sn-{index:02d}-" + "x" * 40] = {**_record("401", "slovnormal"), "detail": "y" * 200}
        machines[f"linux-nl-{index:02d}-" + "x" * 40] = {**_record("401", "nlmedia"), "detail": "y" * 200}
    for index in range(5):
        machines[f"linux-no-{index}"] = _record("401", None)

    text = fleet_notice.build(machines, "dev1")[fleet_notice.DOWN].text

    lines = text.splitlines()
    shown = sum(1 for line in lines if line.startswith("• "))
    hidden = 65 - shown
    assert 0 < shown < 65
    more = next(line for line in lines if line.startswith("… a ďalších"))
    assert more.startswith(f"… a ďalších {hidden} (")
    assert "bez organizácie 5" in more
    before_more = lines[lines.index(more) - 1]
    assert before_more.startswith("• ")  # the last kept line is a box, never a bare header


def test_set_key_never_changes_the_org_of_a_known_machine(tmp_path):
    key = fresh_secret("k")
    with FakeFleetMachine(accept_keys=(key,)) as box:
        _store(tmp_path, {"linux-a": {"url": box.url, "key": key, "org": "nlmedia"}})
        result = _cli(tmp_path, "machine", "set-key", "linux-a", "--org", "slovnormal", input=key + "\n")
    assert result.exit_code == 1 and "machine set-org" in result.output
    assert _stored(tmp_path)["linux-a"]["org"] == "nlmedia"


def test_set_org_warns_about_a_project_it_makes_mixed(fleet):
    fleet.write_manifest({"camera-box": fleet.camera_box_project()})
    result = CliRunner().invoke(gateway_cli.main, [
        "--registry", str(fleet.registry), "--machines", str(fleet.keystore), "--manifest", str(fleet.manifest),
        "machine", "set-org", "slovnormal", "linux-cam2",
    ])
    assert result.exit_code == 0, result.output
    assert "WARNING: [projects.camera-box] now mixes orgs" in result.output


def test_enroll_replace_with_another_org_is_refused_before_the_box_is_touched(enrolling):
    result = CliRunner().invoke(gateway_cli.main, [
        *enrolling.base(), "enroll", "linux-cam2", "--org", "slovnormal", "--projects", "song.player", "--replace",
        "--ssh-key", str(enrolling.ssh_pub),
    ])
    assert result.exit_code == 1 and "machine set-org" in result.output
    assert enrolling.calls() == []


def test_enroll_into_remoteos_mcp_may_mix_orgs(enrolling, tmp_path):
    manifest = enrolling.manifest.read_text()
    enrolling.manifest.write_text(manifest + (
        '\n[projects.remoteos-mcp]\nhost = "dev1"\naccount = "' + current_account() + '"\n'
        f'config = "{tmp_path / "remoteos-mcp.mcp.json"}"\nmachines = ["linux-cam2"]\n'
    ))
    with FakeFleetMachine(accept_keys=(enrolling.key,), version="0.7.0.dev50") as machine:
        enrolling.post(machine.url)
        result = enrolling.enroll("--projects", "remoteos-mcp", "--url", machine.url, org="slovnormal")
        assert result.exit_code == 0, result.output
    assert json.loads(enrolling.machines.read_text())["win-new-pc"]["org"] == "slovnormal"
