"""The installers' enrollment (#26): REMOTEOS_ENROLL set = SSH + the registration, after the proof.

Linux runs whole on its fake box. macOS: test_installer_enroll_macos.py (it also runs under bash 3.2).
install.ps1's parts run in real PowerShell in tests_windows/test_install_ps1_enroll.py; its order is
pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from linux_installer_harness import K_EXPLICIT, assert_never_in_an_argv, linux_box, output

URL = "https://rough-sea.trycloudflare.com/EnrollTok_en-1234567890/"
TOKEN = "EnrollTok_en-1234567890"
SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake dev1"
ROOT = Path(__file__).resolve().parent.parent


def _enroll_posts(box) -> list[dict]:
    log = Path(box.cfg()["curl_log"])
    lines = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [line for line in lines if "enroll_url" in line]


def _authorized(box) -> Path:
    return box.root / "home" / "testuser" / ".ssh" / "authorized_keys"


@pytest.fixture
def box(tmp_path):
    return linux_box(tmp_path)


def test_linux_enrolls_after_the_proof(box):
    result = box.run(REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY, REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)
    assert result.returncode == 0, output(result)
    post = _enroll_posts(box)[0]
    assert post["enroll_url"] == URL and post["agent"] == "remoteos-enroll" and post["proto"] == "=https"
    body = json.loads(post["body"])
    assert body == {"hostname": "testhost", "ips": ["192.168.1.100"], "port": 8092, "version": "0.7.0.dev32",
                    "os": "linux", "account": "testuser", "auth_key": K_EXPLICIT}
    assert ["enable", "--now", "ssh"] in box.calls("systemctl")
    assert _authorized(box).read_text() == SSH_KEY + "\n"
    assert oct(_authorized(box).stat().st_mode)[-3:] == "600"
    assert oct(_authorized(box).parent.stat().st_mode)[-3:] == "700"


def test_linux_the_key_and_the_token_reach_no_argv_output_or_child_environment(box):
    result = box.run(REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY, REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)
    assert result.returncode == 0, output(result)
    assert_never_in_an_argv(box, K_EXPLICIT)
    assert_never_in_an_argv(box, TOKEN)
    assert K_EXPLICIT not in output(result) and TOKEN not in output(result)
    environments = box.child_environments()
    assert TOKEN not in environments and "REMOTEOS_ENROLL" not in environments


def test_linux_a_plain_install_does_none_of_it(box):
    result = box.run(REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)
    assert result.returncode == 0, output(result)
    assert _enroll_posts(box) == []
    assert not (box.root / "home").exists()
    assert not [a for a in box.calls("systemctl") if a[:1] == ["enable"] and a[-1] in ("ssh", "sshd")]
    assert "Enrolling" not in output(result)


def test_linux_a_second_run_adds_the_key_once_and_keeps_the_others(box):
    keys = _authorized(box)
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-rsa AAAAother me")  # no final newline
    env = {"REMOTEOS_ENROLL": URL, "REMOTEOS_ENROLL_SSH_KEY": SSH_KEY}
    assert box.run(**env).returncode == 0
    assert box.run(**env).returncode == 0
    assert keys.read_text() == f"ssh-rsa AAAAother me\n{SSH_KEY}\n"


@pytest.mark.parametrize("config", [{"enroll_fail": True}, {"enroll_reply": "<html>302 Found</html>"}])
def test_linux_a_failed_post_fails_the_run_and_the_install_stands(box, config):
    box.configure(**config)
    box.script.write_text(box.script.read_text().replace("|| sleep 5", "|| sleep 0"))
    result = box.run(REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 1
    assert "got no answer from dev1" in output(result)
    assert len(_enroll_posts(box)) == 3
    assert box.state()["active"], "the proven install stays up"
    assert TOKEN not in output(result)
    assert not _authorized(box).exists()


def test_linux_an_ssh_failure_comes_after_the_registration(box):
    box.script.write_text(box.script.read_text().replace("type -P sshd", "type -P no-such-sshd"))
    result = box.run(REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 1 and len(_enroll_posts(box)) == 1


@pytest.mark.parametrize(("url", "ssh_key", "message"), [
    ("http://x.trycloudflare.com/tok/", SSH_KEY, "REMOTEOS_ENROLL is not"),
    ("https://x.com/a b/", SSH_KEY, "REMOTEOS_ENROLL is not"),
    ('https://x.com/tok/"\nurl = "http://evil/', SSH_KEY, "REMOTEOS_ENROLL is not"),
    (URL, "", "REMOTEOS_ENROLL_SSH_KEY must"),
    (URL, 'command="x" ssh-ed25519 AAAA', "REMOTEOS_ENROLL_SSH_KEY must"),
    (URL, f"{SSH_KEY}\nssh-rsa BBBB", "REMOTEOS_ENROLL_SSH_KEY must"),
])
def test_linux_bad_inputs_change_nothing(box, url, ssh_key, message):
    before = box.snapshot()
    result = box.run(REMOTEOS_ENROLL=url, REMOTEOS_ENROLL_SSH_KEY=ssh_key)
    assert result.returncode == 1 and message in output(result)
    assert box.snapshot() == before


def test_linux_enable_only_cannot_enroll(box):
    result = box.run(REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY, REMOTEOS_MCP_POLICY="enable-only")
    assert result.returncode == 1 and "needs the restart policy" in output(result)


def test_linux_a_missing_ssh_server_is_installed_or_the_run_fails(box):
    text = box.script.read_text().replace("type -P sshd", "type -P no-such-sshd")
    box.script.write_text(text)
    result = box.run(REMOTEOS_ENROLL=URL, REMOTEOS_ENROLL_SSH_KEY=SSH_KEY)
    assert result.returncode == 1 and "OpenSSH server could not be installed" in output(result)
    assert ["install", "-y", "openssh-server"] in box.calls("apt-get")


def _ps1() -> str:
    return (ROOT / "install.ps1").read_text(encoding="ascii")


def test_ps1_the_enrollment_values_leave_the_environment_before_the_first_child():
    text = _ps1()
    remove = text.index("Remove-Item Env:REMOTEOS_ENROLL_SSH_KEY")
    assert remove < text.index("# --- The elevation gate (#57): before anything")
    assert remove < text.index("Invoke-RestMethod -Uri \"https://api.github.com")


def test_ps1_an_enrollment_never_crosses_the_uac_relaunch():
    assert "-ExplicitKey ([bool]$explicitKey -or [bool]$enrollUrl) `" in _ps1()


def test_ps1_the_inputs_are_judged_before_the_commit_is_resolved_and_python_before_the_reach_check():
    text = _ps1()
    assert text.index("Test-RemoteOSEnrollUrl $enrollUrl") < text.index("$sourceSha = $null")
    install = text.index("$python = Install-RemoteOSMachinePython")
    assert install < text.index("Test-RemoteOSPythonReachable -PythonPath $foundPythonPath")
    assert text.index('if ($python) { $foundPythonPath') > install


def test_ps1_a_plain_run_reaches_none_of_it():
    """Every enrollment step in the flow sits behind a test of $enrollUrl."""
    text = _ps1()
    flow = text[text.index('Write-Host "  RemoteOS MCP Installer"'):]
    assert "if ($enrollUrl -and -not $python) {" in flow
    block = flow[flow.index("# --- Enrollment (#26): after the proof") : flow.index("# --- Summary ---")]
    assert block.split("\n")[2] == "if ($enrollUrl) {"
    for call in ("Install-RemoteOSMachinePython ", "Enable-RemoteOSSsh ", "Send-RemoteOSEnrollment "):
        assert flow.count(call) == 1, call
    assert flow.index("if ($enrollUrl -and -not $python) {") < flow.index("Install-RemoteOSMachinePython ")
    assert flow.index("if ($enrollUrl) {") < flow.index("Enable-RemoteOSSsh ")


def test_ps1_enrollment_runs_after_the_proof_and_never_rolls_back():
    text = _ps1()
    disarm = text.rindex("Stop-RemoteOSRollbackTrap -ConfigDir $ConfigDir\n")
    start = text.index("# --- Enrollment (#26): after the proof")
    assert disarm < start < text.index("# --- Summary ---")
    block = text[start : text.index("# --- Summary ---")]
    assert "@rollback" not in block
    assert block.index("Send-RemoteOSEnrollment") < block.index("Enable-RemoteOSSsh"), "SSH is break-glass"
