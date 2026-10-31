"""`remoteos-gateway enroll` (#26): dev1's half of one-command onboarding.

The real CLI runs against a fake `airuleset.py` (a vault of plain files that keeps the real
one's `secret` contract) and a fake `cloudflared`; the machine is a real loopback server.
The real endpoint, the real tunnel and a real box are the supervisor's live run.
"""

from __future__ import annotations

import json
import stat
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner
from fleet_harness import FakeFleetMachine, closed_port_url, fresh_secret, write_private_json

from remoteos.gateway import cli as gateway_cli
from remoteos.gateway import enroll
from remoteos.gateway.enroll import EnrollError, Registration
from remoteos.gateway.manifest import ManifestError, add_machine, load_manifest

SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests dev1"
TOKEN = "EnrollTok_en-123456789012345678901234"

MANIFEST = """\
[gateway]
url = "{gateway}"

# the camera project
[projects.camera-box]  # kept
host = "dev1"
account = "newlevel"
config = "{config}"
machines = ["linux-cam2"]  # the first cam

[projects."song.player"]
host = "dev1"
account = "newlevel"
config = "{config2}"
machines = [
    "linux-cam2",  # shared
    "linux-cam3",
]
"""

FAKE_AIRULESET = """\
#!{python}
import json, os, subprocess, sys
vault = os.environ["FAKE_VAULT"]
os.makedirs(vault, exist_ok=True)
with open(os.path.join(vault, "calls.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
assert sys.argv[1] == "secret"
action, name = sys.argv[2], sys.argv[3]
value, pending = os.path.join(vault, name), os.path.join(vault, name + ".pending")
if action == "request":
    if os.environ.get("FAKE_REQUEST_FAILS"):
        print("no tailscale", file=sys.stderr)
        sys.exit(1)
    open(pending, "w").close()
    print("http://100.104.8.125:8830/{token}/   [tailscale]")
    print("http://10.66.69.23:8830/{token}/   [ztsjslsf7z]")
    posted = os.environ.get("FAKE_POSTED")
    if posted:
        with open(value, "w") as f:
            f.write(posted)
elif action == "status":
    state = "ready expires=2026-10-31T08:48:00+0100" if os.path.exists(value) else None
    print(name, state or ("pending" if os.path.exists(pending) else "absent"))
elif action == "forget":
    for path in (value, pending):
        if os.path.exists(path):
            os.unlink(path)
elif action == "exec":
    cmd = sys.argv[sys.argv.index("--") + 1:]
    with open(value, "rb") as f:
        r = subprocess.run(cmd, stdin=f, capture_output=True)
    sys.stdout.buffer.write(r.stdout)
    sys.stderr.buffer.write(r.stderr)
    sys.exit(r.returncode)
"""

FAKE_CLOUDFLARED = """\
#!{python}
import json, os, sys, time
with open(os.environ["FAKE_CF_LOG"], "w") as f:
    json.dump(sys.argv[1:], f)
if os.environ.get("FAKE_CF_DIES"):
    sys.exit(3)
print("INF |  https://rough-sea-word-word.trycloudflare.com  |", file=sys.stderr, flush=True)
time.sleep(60)
"""


class Fleet:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.key = fresh_secret("Enrolled")
        self.machines = write_private_json(tmp_path / "machines.json", {
            "linux-cam2": {"url": closed_port_url(), "key": "cam2"},
            "linux-cam3": {"url": closed_port_url(), "key": "cam3"},
        })
        self.registry = tmp_path / "registry.json"
        self.config = tmp_path / "camera-box.mcp.json"
        self.config2 = tmp_path / "song.mcp.json"
        self.status = tmp_path / "status.json"
        self.vault = tmp_path / "vault"
        self.cf_log = tmp_path / "cf.json"
        self.manifest = tmp_path / "projects.toml"
        self.gateway = FakeFleetMachine(healthz=True)
        self.gateway.__enter__()
        self.manifest.write_text(MANIFEST.format(gateway=self.gateway.base_url, config=self.config,
                                                 config2=self.config2))
        self.airuleset = tmp_path / "airuleset.py"
        self.airuleset.write_text(FAKE_AIRULESET.format(python=sys.executable, token=TOKEN))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        cloudflared = bin_dir / "cloudflared"
        cloudflared.write_text(FAKE_CLOUDFLARED.format(python=sys.executable))
        for path in (cloudflared, self.airuleset):
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin")
        monkeypatch.setenv("FAKE_VAULT", str(self.vault))
        monkeypatch.setenv("FAKE_CF_LOG", str(self.cf_log))
        monkeypatch.setenv("REMOTEOS_GATEWAY_STATUS_FILE", str(self.status))
        monkeypatch.setenv("REMOTEOS_GATEWAY_AIRULESET", str(self.airuleset))
        monkeypatch.setenv("REMOTEOS_GATEWAY_LOCAL_HOST", "dev1")
        monkeypatch.setenv("REMOTEOS_RELEASES_URL", closed_port_url())
        monkeypatch.setattr(enroll, "DNS_SETTLE_S", 0)
        monkeypatch.setattr(enroll, "POLL_S", 0.01)
        monkeypatch.setattr(enroll, "public_health", lambda url: 204)
        self.ssh_pub = tmp_path / "id.pub"
        self.ssh_pub.write_text(SSH_KEY + "\n")

    def close(self) -> None:
        self.gateway.__exit__(None, None, None)

    def post(self, url: str, **overrides: object) -> None:
        host, port = url.removeprefix("http://").removesuffix("/mcp").split(":")
        body = {"hostname": "new-pc", "ips": [host], "port": int(port), "version": "0.7.0.dev50",
                "os": "windows", "auth_key": self.key, **overrides}
        self.monkeypatch.setenv("FAKE_POSTED", json.dumps(body))

    def base(self) -> list[str]:
        return ["--registry", str(self.registry), "--machines", str(self.machines),
                "--access-log", str(self.tmp / "access.jsonl"), "--manifest", str(self.manifest)]

    def enroll(self, *args: str):
        return CliRunner().invoke(gateway_cli.main, [
            *self.base(), "enroll", "win-new-pc", "--ssh-key", str(self.ssh_pub), "--timeout", "30", *args])

    def calls(self) -> list[list[str]]:
        log = self.vault / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    world = Fleet(tmp_path, monkeypatch)
    yield world
    world.close()


def test_enroll_stores_the_key_adds_the_projects_and_syncs(fleet):
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev50") as machine:
        fleet.post(machine.url)
        result = fleet.enroll("--projects", "camera-box,song.player", "--url", machine.url)
        assert result.exit_code == 0, result.output
        store = json.loads(fleet.machines.read_text())
    assert store["win-new-pc"]["url"] == machine.url
    assert store["win-new-pc"]["key"] == fleet.key
    projects = load_manifest(fleet.manifest).projects
    assert projects["camera-box"].machines == ("linux-cam2", "win-new-pc")
    assert projects["song.player"].machines == ("linux-cam2", "linux-cam3", "win-new-pc")
    config = json.loads(fleet.config.read_text())
    assert config["mcpServers"]["win-new-pc"]["url"] == f"{fleet.gateway.base_url}/win-new-pc/mcp"
    assert json.loads(fleet.status.read_text())["machines"]["win-new-pc"]["state"] == "ok"
    assert fleet.key not in result.output
    assert any(argv[1] == "forget" for argv in fleet.calls()[1:])
    assert not (fleet.vault / enroll.secret_name("win-new-pc")).exists()


def test_the_printed_commands_carry_the_public_url_and_never_the_private_one(fleet):
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev50") as machine:
        fleet.post(machine.url)
        result = fleet.enroll("--projects", "camera-box", "--ref", "abc1234", "--url", machine.url)
    public = f"https://rough-sea-word-word.trycloudflare.com/{TOKEN}/"
    assert f'$env:REMOTEOS_ENROLL="{public}"' in result.output
    assert f"REMOTEOS_ENROLL='{public}' REMOTEOS_ENROLL_SSH_KEY='{SSH_KEY.rsplit(' ', 1)[0]}'" in result.output
    assert "/abc1234/install.ps1 | iex" in result.output
    assert "REMOTEOS_MCP_REF" in result.output
    assert "100.104.8.125" not in result.output


def test_the_endpoint_is_private_and_the_tunnel_reads_no_config_of_the_box(fleet):
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev50") as machine:
        fleet.post(machine.url)
        assert fleet.enroll("--projects", "camera-box", "--url", machine.url).exit_code == 0
    request = next(argv for argv in fleet.calls() if argv[1] == "request")
    assert "--private" in request
    argv = json.loads(fleet.cf_log.read_text())
    assert argv[:2] == ["tunnel", "--config"] and Path(argv[2]).name == "empty.yml"
    assert argv[argv.index("--url") + 1] == "http://100.104.8.125:8830"  # the tailscale line


def test_a_key_the_machine_rejects_is_stored_nowhere(fleet):
    with FakeFleetMachine(accept_keys=("another",), version="0.7.0.dev50") as machine:
        fleet.post(machine.url)
        before = fleet.manifest.read_text()
        result = fleet.enroll("--projects", "camera-box", "--url", machine.url)
    assert result.exit_code == 1
    assert "accepted the key at no address" in result.output
    assert "stays in the vault" in result.output
    assert "win-new-pc" not in fleet.machines.read_text()
    assert "enroll-finish win-new-pc --projects camera-box" in result.output and "--replace" in result.output
    assert fleet.manifest.read_text() == before
    assert fleet.key not in result.output
    assert (fleet.vault / enroll.secret_name("win-new-pc")).exists(), "kept for the retry"


def test_an_unknown_project_is_refused_before_any_endpoint(fleet):
    result = fleet.enroll("--projects", "nope")
    assert result.exit_code == 1 and "nope" in result.output
    assert fleet.calls() == []


def test_a_known_machine_needs_replace(fleet):
    write_private_json(fleet.machines, {"win-new-pc": {"url": "http://10.0.0.1:8092/mcp", "key": "abc"}})
    result = fleet.enroll("--projects", "camera-box")
    assert result.exit_code == 1 and "--replace" in result.output
    assert fleet.calls() == []


def test_a_dead_tunnel_prints_no_url(fleet, monkeypatch):
    monkeypatch.setenv("FAKE_CF_DIES", "1")
    result = fleet.enroll("--projects", "camera-box")
    assert result.exit_code == 1 and "cloudflared exited" in result.output
    assert "REMOTEOS_ENROLL=" not in result.output
    assert TOKEN not in result.output
    assert any(argv[1] == "forget" for argv in fleet.calls())


def test_a_failed_request_prints_no_url(fleet, monkeypatch):
    monkeypatch.setenv("FAKE_REQUEST_FAILS", "1")
    result = fleet.enroll("--projects", "camera-box")
    assert result.exit_code == 1 and "secret request failed" in result.output
    assert "REMOTEOS_ENROLL=" not in result.output


def test_no_box_within_the_time_stops_and_forgets(fleet, monkeypatch):
    monkeypatch.setattr(enroll.time, "monotonic", iter(range(0, 100000, 40)).__next__)
    result = fleet.enroll("--projects", "camera-box")
    assert result.exit_code == 1 and "no box enrolled" in result.output
    assert not list(fleet.vault.glob("*.pending"))


def _registration(**overrides: object) -> bytes:
    body = {"hostname": "pc", "ips": ["10.0.0.5"], "port": 8092, "version": "1.0", "os": "linux",
            "auth_key": "abc123", **overrides}
    return json.dumps(body).encode()


@pytest.mark.parametrize("overrides", [
    {"hostname": "a b"}, {"hostname": None}, {"version": "x;y"}, {"port": 0}, {"port": "8092"}, {"port": True},
    {"os": "beos"}, {"auth_key": ""}, {"auth_key": None}, {"auth_key": "a\nb"}, {"ips": "10.0.0.5"},
    {"ips": ["10.0.0.5"] * 17},
])
def test_a_bad_registration_is_refused_without_repeating_a_value(overrides):
    with pytest.raises(EnrollError) as caught:
        enroll.parse_registration(_registration(**overrides))
    value = next(iter(overrides.values()))
    assert not isinstance(value, str) or not value or value not in str(caught.value)


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"\xff\xfe", b"[" * 100000])
def test_a_body_that_is_no_object_is_refused(raw):
    with pytest.raises(EnrollError):
        enroll.parse_registration(raw)


def test_a_registration_with_no_private_address_needs_url(fleet, monkeypatch):
    with FakeFleetMachine(accept_keys=(fleet.key,), version="0.7.0.dev50") as machine:
        fleet.post(machine.url)  # 127.0.0.1: never a candidate
        result = fleet.enroll("--projects", "camera-box")
    assert result.exit_code == 1 and "names no private address" in result.output
    assert machine.seen_authorization == [], "the key went to an address the box only claimed"


def test_the_key_goes_only_to_private_addresses_tailscale_first():
    registration = enroll.parse_registration(_registration(ips=[
        "8.8.8.8", "127.0.0.1", "169.254.1.1", "0.0.0.0", "224.0.0.1", "10.0.0.5", "not-ip", 5,
        "192.168.1.9", "100.100.1.2", "100.128.0.1", "10.0.0.5",
    ]))
    assert enroll.candidate_urls(registration) == [
        "http://100.100.1.2:8092/mcp", "http://10.0.0.5:8092/mcp", "http://192.168.1.9:8092/mcp"]


def test_the_key_stays_out_of_the_registrations_repr():
    registration = Registration("pc", (), 8092, "1.0", "linux", "TopSecretKey")
    assert "TopSecretKey" not in repr(registration)


def test_secret_name_fits_the_vaults_grammar():
    name = enroll.secret_name("win-a.b_c" + "x" * 80)
    assert name.startswith("REMOTEOS_ENROLL_win_a_b_c") and len(name) <= 64
    assert name.replace("_", "").isalnum()


def test_endpoint_prefers_the_tailscale_line():
    text = "http://10.1.1.1:8830/tok/  [lan]\nhttp://100.104.8.125:8830/tok/  [tailscale x]\n"
    assert enroll.endpoint_from_request(text) == ("http://100.104.8.125:8830", "tok")
    with pytest.raises(EnrollError):
        enroll.endpoint_from_request("nothing here")


@pytest.mark.parametrize("text", ["command=\"x\" ssh-ed25519 AAAA", "ssh-ed25519 AAAA a\nssh-rsa BBBB", "hello"])
def test_only_one_public_key_line_is_sent(tmp_path, text):
    path = tmp_path / "k.pub"
    path.write_text(text)
    with pytest.raises(EnrollError):
        enroll.read_ssh_key(path)


def test_a_public_keys_comment_is_dropped_it_may_hold_spaces(tmp_path):
    path = tmp_path / "k.pub"
    path.write_text("ssh-ed25519 AAAAkey david grena mac\n")
    assert enroll.read_ssh_key(path) == "ssh-ed25519 AAAAkey"


def test_add_machine_keeps_comments_and_layout(fleet):
    add_machine(fleet.manifest, "camera-box", "win-x")
    add_machine(fleet.manifest, "song.player", "win-x")
    text = fleet.manifest.read_text()
    assert 'machines = ["linux-cam2", "win-x"]  # the first cam' in text
    assert '    "linux-cam3",\n    "win-x",\n]' in text
    assert "# the camera project" in text and "# kept" in text and '"linux-cam2",  # shared' in text
    tomllib.loads(text)


def test_add_machine_twice_changes_nothing(fleet):
    assert add_machine(fleet.manifest, "camera-box", "win-x") is True
    text = fleet.manifest.read_text()
    assert add_machine(fleet.manifest, "camera-box", "win-x") is False
    assert fleet.manifest.read_text() == text


def test_add_machine_keeps_the_files_mode(fleet):
    fleet.manifest.chmod(0o640)
    add_machine(fleet.manifest, "camera-box", "win-x")
    assert stat.S_IMODE(fleet.manifest.stat().st_mode) == 0o640
    assert [p.name for p in fleet.tmp.glob("projects.toml*")] == ["projects.toml"]


def test_add_machine_refuses_an_unknown_project(fleet):
    with pytest.raises(ManifestError):
        add_machine(fleet.manifest, "nope", "win-x")


def test_a_comment_with_a_bracket_or_a_quote_does_not_fool_the_edit(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(
        '[projects.a]\nhost = "dev1"\naccount = "n"\nconfig = "~/a.json"\n'
        'machines = [\n  "m1",  # not "this"]\n  "m2"  # last\n]\n'
    )
    add_machine(path, "a", "win-x")
    assert load_manifest(path).projects["a"].machines == ("m1", "m2", "win-x")
