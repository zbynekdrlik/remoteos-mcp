"""install.sh keeps the LaunchAgent account's admin (#71). It runs as the user, so the drop-in goes
through sudo, and only a refused visudo stops the run: a mac with no sudo keeps its install.

Runs under bash 3.2 too (the `shell` CI job: macOS's /bin/bash).
"""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest
from fake_linux_tools import records
from installer_harness import (
    FAKE_SUDO,
    NEW_VERSION,
    ROOT,
    _create_macos_shims,
    _make_shim,
    _prepare_macos_installer,
    _run_installer,
    health_shim,
    install_old_remoteos,
    log_every_command,
    setup_fake_pip,
)


class Mac:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.script = _prepare_macos_installer(tmp_path)
        self.shims = tmp_path / "shims"
        self.shims.mkdir()
        _create_macos_shims(self.shims, tmp_path / "invocations.log")
        self.pip = setup_fake_pip(tmp_path, self.shims)
        health_shim(self.shims, NEW_VERSION)
        install_old_remoteos(self.pip)
        self.records = tmp_path / "argv.records"
        self.sudoers = tmp_path / "sudoers.d" / "90-remoteos-mcp"
        self.plist = tmp_path / "LaunchAgents" / "com.remoteos-mcp.plist"

    def log(self) -> None:
        log_every_command(self.shims, self.records)

    def run(self):
        return _run_installer(self.script, self.shims, self.tmp)

    def commands(self, tool: str) -> list[list[str]]:
        return [r[1:] for r in records(self.records) if r and r[0] == tool]


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    return Mac(tmp_path)


def line() -> str:
    return f"{pwd.getpwuid(os.getuid()).pw_name} ALL=(ALL) NOPASSWD:ALL\n"


def test_the_account_gets_the_drop_in_through_sudo(mac):
    mac.log()
    result = mac.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert mac.sudoers.read_text() == line()
    assert stat.S_IMODE(mac.sudoers.stat().st_mode) == 0o440
    assert not (mac.sudoers.parent / "90-remoteos-mcp.new").exists()
    installs = [a for a in mac.commands("sudo") if "install" in a]
    assert installs and installs[0][-1].endswith("90-remoteos-mcp.new")
    assert "-o" in installs[0] and "wheel" in installs[0]


def test_visudo_judges_the_file_before_pip_runs(mac):
    _make_shim(mac.shims / "visudo", 'echo "syntax error" >&2\nexit 1')
    result = mac.run()
    assert result.returncode == 1
    assert "nothing was changed" in result.stdout and "syntax error" in result.stdout
    assert mac.pip.calls("install") == [] and mac.pip.calls("uninstall") == []
    assert not mac.plist.exists() and not mac.sudoers.exists()


def test_a_hand_made_identical_file_is_adopted_untouched(mac):
    mac.sudoers.parent.mkdir()
    mac.sudoers.write_text(line())
    mac.sudoers.chmod(0o440)
    before = mac.sudoers.stat().st_mtime_ns
    result = mac.run()
    assert result.returncode == 0
    assert "sudoers drop-in: unchanged" in result.stdout
    assert mac.sudoers.stat().st_mtime_ns == before


def test_no_sudo_without_a_terminal_warns_and_the_install_stands(mac):
    _make_shim(mac.shims / "sudo", "exit 1")
    result = mac.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[!] No sudo here" in result.stdout
    assert not mac.sudoers.exists() and mac.plist.exists()


def test_a_root_run_gets_no_drop_in(mac):
    _make_shim(
        mac.shims / "id",
        'if [[ "$1" == "-u" ]]; then echo 0; else echo root; fi',
    )
    result = mac.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not mac.sudoers.exists()


def test_a_failed_health_check_writes_no_drop_in(mac):
    health_shim(mac.shims, "9.9.9")
    result = mac.run()
    assert result.returncode == 1
    assert not mac.sudoers.exists()


def _uninstall(tmp_path: Path, shims: Path, sudoers: Path) -> subprocess.CompletedProcess:
    text = (ROOT / "uninstall.sh").read_text()
    assert 'SUDOERS_FILE="/etc/sudoers.d/90-remoteos-mcp"' in text
    copy = tmp_path / "uninstall-macos.sh"
    copy.write_text(text.replace('SUDOERS_FILE="/etc/sudoers.d/90-remoteos-mcp"', f'SUDOERS_FILE="{sudoers}"'))
    env = {"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path), "TERM": "dumb"}
    return subprocess.run(
        [os.environ.get("INSTALLER_TEST_BASH", "bash"), str(copy)],
        input="\n", capture_output=True, text=True, env=env, timeout=30, start_new_session=True,
    )


def test_the_uninstaller_removes_the_drop_in(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    _make_shim(shims / "sudo", FAKE_SUDO)
    _make_shim(shims / "launchctl", "exit 0")
    sudoers = tmp_path / "90-remoteos-mcp"
    sudoers.write_text("x\n")
    result = _uninstall(tmp_path, shims, sudoers)
    assert not sudoers.exists(), result.stdout + result.stderr
    assert f"Removed {sudoers}" in result.stdout


def test_the_uninstaller_without_sudo_says_so(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    _make_shim(shims / "sudo", "exit 1")
    _make_shim(shims / "launchctl", "exit 0")
    sudoers = tmp_path / "90-remoteos-mcp"
    sudoers.write_text("x\n")
    result = _uninstall(tmp_path, shims, sudoers)
    assert sudoers.exists()
    assert "remove" in result.stdout and "by hand" in result.stdout
