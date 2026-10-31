"""install.sh's enrollment (#26), whole on the fake mac. The `shell` CI job runs this under bash 3.2."""

from __future__ import annotations

import json
from pathlib import Path

from fake_linux_tools import records
from installer_harness import _make_shim
from test_installer_macos_key import K_CONFIG, Mac

URL = "https://rough-sea.trycloudflare.com/EnrollTok_en-1234567890/"
TOKEN = "EnrollTok_en-1234567890"
SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake dev1"


def _mac(tmp_path: Path, *, reply: str = "ok", sudo_ok: bool = True) -> Mac:
    mac = Mac(tmp_path)
    mac.home = tmp_path / "mac-home"
    mac.home.mkdir()
    mac.bodies = tmp_path / "curl-bodies.log"
    real = mac.shims / ".real" / "curl"
    real.write_text(real.read_text().replace("#!/bin/bash\n", f"""#!/bin/bash
for a in "$@"; do
    if [[ "$a" == "-K" ]]; then
        {{ cat /dev/fd/5; cat; echo; }} >> "{mac.bodies}"
        printf '%s' '{reply}'
        exit 0
    fi
done
""", 1))
    _make_shim(mac.shims / "sudo", f"exit {0 if sudo_ok else 1}\n")
    mac.script.write_text(mac.script.read_text().replace("then sleep 5", "then sleep 0"))
    return mac


def _run(mac: Mac, **env: str):
    return mac.run(HOME=str(mac.home), **env)


def test_macos_enrolls_after_the_proof(tmp_path):
    mac = _mac(tmp_path)
    mac.config.parent.mkdir(exist_ok=True)
    mac.config.write_text(json.dumps({"auth_key": K_CONFIG}))
    result = _run(mac, REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = mac.bodies.read_text().splitlines()
    assert lines[0] == f'url = "{URL}"'
    body = json.loads(lines[1])
    assert body["auth_key"] == K_CONFIG and body["os"] == "macos" and body["port"] == 8092 and body["account"]
    assert (mac.home / ".ssh" / "authorized_keys").read_text() == SSH_KEY + "\n"
    assert "Remote Login: on" in result.stdout
    for argv in records(mac.records):
        assert not any(K_CONFIG in arg or TOKEN in arg for arg in argv), argv
    assert TOKEN not in Path(f"{mac.records}.env").read_text()
    assert K_CONFIG not in result.stdout + result.stderr and TOKEN not in result.stdout + result.stderr


def test_macos_no_sudo_warns_and_the_box_is_enrolled(tmp_path):
    mac = _mac(tmp_path, sudo_ok=False)
    result = _run(mac, REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[!] Remote Login could not be turned on" in result.stdout
    assert "Enrolled" in result.stdout


def test_macos_a_reply_other_than_ok_fails_the_run(tmp_path):
    mac = _mac(tmp_path, reply="<html>302 Found</html>")
    result = _run(mac, REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 1 and "got no answer from dev1" in result.stdout
    assert mac.bodies.read_text().count("url = ") == 3
    assert not (mac.home / ".ssh").exists()


def test_macos_a_plain_install_does_none_of_it(tmp_path):
    mac = _mac(tmp_path)
    result = _run(mac)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not mac.bodies.exists()
    assert not (mac.home / ".ssh").exists()


def test_macos_a_bad_url_changes_nothing(tmp_path):
    mac = _mac(tmp_path)
    result = _run(mac, REMOTEOS_ENROLL="http://x/y/", REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 1 and "REMOTEOS_ENROLL is not" in result.stdout
    assert not mac.config.exists()
