"""The Windows side of #65 in real PowerShell: the ring, the updater's task action, the rollback trap
(run for real against a venv, with a dead installer pid), and the uninstaller's desktop profile.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_install_ps1_key import _ps, _q
from test_install_ps1_wheelhouse import (  # noqa: F401
    _PRELUDE,
    INSTALLER,
    SHA_OLD,
    _installed,
    _run,
    _wheelhouse,
    shared_venv,
    venv,
)

ROOT = Path(__file__).parent.parent


def _functions_prelude() -> str:
    """install.ps1's own functions, as test_install_ps1_wheelhouse's probe loads them."""
    return _PRELUDE.format(installer=str(INSTALLER).replace("'", "''"))


@pytest.mark.parametrize(("ring", "ok"), [("", True), ("stable", True), ("canary", True), ("Canary", False),
                                          ("nightly", False), ("canary\n", False)])
def test_the_ring_check(tmp_path, ring, ok):
    assert _ps(tmp_path, f"@{{ ok = (Test-RemoteOSRing {_q(ring)}) }} | ConvertTo-Json -Compress")["ok"] is ok


def _set_ring(tmp_path: Path, config: Path, ring: str) -> str:
    _ps(tmp_path, f"Set-RemoteOSRing -ConfigDir {_q(config)} -Ring {_q(ring)}; @{{ a = 1 }} | ConvertTo-Json -Compress")
    return (config / "updater.json").read_bytes().decode("ascii")


def test_the_ring_file_is_what_the_updater_reads_and_a_rerun_keeps_it(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    assert json.loads(_set_ring(tmp_path, config, "")) == {"ring": "stable"}  # no BOM: ascii, and json.loads took it
    assert json.loads(_set_ring(tmp_path, config, "canary")) == {"ring": "canary"}
    assert json.loads(_set_ring(tmp_path, config, "")) == {"ring": "canary"}


def _action(tmp_path: Path, python: Path, config: Path) -> dict:
    body = f"Get-RemoteOSUpdaterAction -PythonPath {_q(python)} -ConfigDir {_q(config)} | ConvertTo-Json -Compress"
    return _ps(tmp_path, body)


def test_the_task_runs_pythonw_so_no_window_opens_on_a_live_desktop(tmp_path):
    folder = tmp_path / "py dir"
    folder.mkdir()
    python, pythonw = folder / "python.exe", folder / "pythonw.exe"
    python.touch()
    assert _action(tmp_path, python, tmp_path)["Execute"] == str(python)  # none there: python.exe still works
    pythonw.touch()
    action = _action(tmp_path, python, tmp_path / "it's config")
    assert action["Execute"] == str(pythonw)
    assert action["Argument"].startswith('-m remoteos.updater --log-file "')
    assert "updater.log" in action["Argument"]


class TestRollbackTrap:
    def _point(self, tmp_path: Path, version: str) -> str:
        folder = tmp_path / "wheelhouse" / SHA_OLD
        _wheelhouse(folder, version)
        return f"[pscustomobject]@{{ Ref = '{SHA_OLD}'; Path = {_q(folder)}; Version = '{version}' }}"

    def _script(self, tmp_path: Path, config: Path, python: Path, installer_pid: int) -> Path:
        point = self._point(tmp_path, "1.0")
        body = f"""
$text = Get-RemoteOSRollbackTrapScript -Point ({point}) -Python {_q(python)} -ConfigDir {_q(config)} `
    -Port 8092 -InstallerPid {installer_pid}
@{{ text = $text }} | ConvertTo-Json -Compress
"""
        text = _ps(tmp_path, body)["text"]
        # The box's real server functions stop tasks and read ports: faked at their names, after the installer's.
        fake = """
function Stop-RemoteOSServer { param($Port) }
function Start-RemoteOSServer { param($ConfigDir, $StartDirect); return $null }
function Test-RemoteOSServerHealth { param($Port, $Version, $StartDirect)
    return [pscustomobject]@{ Ok = ($Version -eq '1.0'); Message = 'fake' } }
"""
        script = tmp_path / "trap.ps1"
        marker_line = text.index("while ((Test-Path")
        script.write_text(text[:marker_line] + fake + text[marker_line:], encoding="utf-8-sig")
        return script

    def _run(self, script: Path) -> subprocess.CompletedProcess:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(script)], capture_output=True, text=True, timeout=240,
                              stdin=subprocess.DEVNULL)

    def _dead_pid(self) -> int:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def test_a_dead_installer_while_armed_rolls_back_offline(self, tmp_path, venv):  # noqa: F811
        config = tmp_path / "config"
        config.mkdir()
        (config / "rollback-pending.txt").write_text("1")
        result = self._run(self._script(tmp_path, config, venv, self._dead_pid()))
        assert result.returncode == 0, result.stdout + result.stderr
        assert (config / "rollback-watching.txt").exists()
        assert _installed(tmp_path, venv) == "1.0"
        assert not (config / "rollback-pending.txt").exists()
        assert "Rolled back to v1.0" in (config / "rollback.log").read_text(encoding="utf-8", errors="replace")

    def test_a_disarmed_run_does_nothing(self, tmp_path, venv):  # noqa: F811
        config = tmp_path / "config"
        config.mkdir()
        result = self._run(self._script(tmp_path, config, venv, os.getpid()))
        assert result.returncode == 0, result.stdout + result.stderr
        assert _installed(tmp_path, venv) is None
        assert not (config / "rollback.log").exists()

    def test_a_failed_rollback_is_logged_and_fails_the_task(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "rollback-pending.txt").write_text("1")
        script = self._script(tmp_path, config, tmp_path / "no-python", self._dead_pid())
        assert self._run(script).returncode == 1
        assert "The rollback failed" in (config / "rollback.log").read_text(encoding="utf-8", errors="replace")

    def test_a_quote_in_a_path_cannot_break_the_script(self, tmp_path, venv):  # noqa: F811
        config = tmp_path / "it's config"
        config.mkdir()
        result = self._run(self._script(tmp_path, config, venv, os.getpid()))
        assert result.returncode == 0, result.stdout + result.stderr


class TestRollbackWheelhouse:
    def _call(self, tmp_path: Path, config: Path) -> dict:
        body = f"""
function New-RemoteOSWheelhouse {{ param($Python, $Root, $Sha, $SourceUrl, $ConstraintsUrl)
    return [pscustomobject]@{{ Sha = $Sha; Source = $SourceUrl; Constraints = $ConstraintsUrl; Root = $Root }} }}
@{{ r = (New-RemoteOSRollbackWheelhouse -ConfigDir {_q(config)} -Root 'R' -Python 'p') }} | ConvertTo-Json -Compress
"""
        return _ps(tmp_path, body)["r"]

    def test_the_installed_commit_is_built_again_never_taken_from_the_profile(self, tmp_path):
        (tmp_path / "installed-ref.txt").write_text(SHA_OLD + "\n")
        got = self._call(tmp_path, tmp_path)
        assert got["Sha"] == SHA_OLD and got["Root"] == "R"
        assert got["Source"] == f"https://github.com/zbynekdrlik/remoteos-mcp/archive/{SHA_OLD}.zip"
        assert got["Constraints"].endswith(f"/{SHA_OLD}/constraints.txt")

    @pytest.mark.parametrize("text", [None, "not-a-sha"])
    def test_without_a_proven_commit_there_is_nothing_to_build(self, tmp_path, text):
        if text is not None:
            (tmp_path / "installed-ref.txt").write_text(text)
        assert self._call(tmp_path, tmp_path) is None


def test_the_heartbeat_decides_whether_the_trap_runs(tmp_path):
    body = f"""
$before = Wait-RemoteOSRollbackTrap -ConfigDir {_q(tmp_path)} -Seconds 1
Set-Content -LiteralPath (Join-Path {_q(tmp_path)} 'rollback-watching.txt') -Value 1
$after = Wait-RemoteOSRollbackTrap -ConfigDir {_q(tmp_path)} -Seconds 1
@{{ before = $before; after = $after }} | ConvertTo-Json -Compress
"""
    assert _ps(tmp_path, body) == {"before": False, "after": True}


def test_the_trap_gives_up_on_an_installer_that_never_finishes(tmp_path):
    """A piped run stopped with Ctrl+C keeps its powershell: the deadline must be in the watcher."""
    body = """$point = [pscustomobject]@{ Ref = 'r'; Path = 'p'; Version = '1' }
@{ s = (Get-RemoteOSRollbackTrapScript -Point $point -Python 'p' -ConfigDir 'c' -Port 1 -InstallerPid 1) } |
    ConvertTo-Json -Compress"""
    script = _ps(tmp_path, body)["s"]
    assert "(Get-Date) -lt $deadline" in script and "AddMinutes(30)" in script


class TestInstallLock:
    """Two real processes: the lock is the machine's, and it dies with its holder."""

    def _holder(self, tmp_path: Path, ready: Path) -> subprocess.Popen:
        script = tmp_path / "holder.ps1"
        script.write_text(f"""
{_functions_prelude()}
$lock = Enter-RemoteOSInstallLock
Set-Content -LiteralPath {_q(ready)} -Value 1
Start-Sleep -Seconds 60
""", encoding="utf-8-sig")
        process = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File", str(script)], stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert time.monotonic() < deadline and process.poll() is None
            time.sleep(0.1)
        return process

    def _taken(self, tmp_path: Path) -> bool:
        body = "$l = Enter-RemoteOSInstallLock; @{ got = [bool]$l } | ConvertTo-Json -Compress"
        return _ps(tmp_path, body)["got"]

    def test_a_held_lock_refuses_a_second_run_and_a_killed_holder_frees_it(self, tmp_path):
        process = self._holder(tmp_path, tmp_path / "ready")
        try:
            assert self._taken(tmp_path) is False
        finally:
            process.kill()
            process.wait()
        assert self._taken(tmp_path) is True


def test_a_new_run_holding_the_lock_keeps_the_trap_out(tmp_path):
    """A dead installer's watcher must not roll back under the run that took over."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "rollback-pending.txt").write_text("1")
    trap = TestRollbackTrap()
    script = trap._script(tmp_path, config, tmp_path / "no-python", trap._dead_pid())
    text = script.read_text(encoding="utf-8-sig").replace("$lock = Enter-RemoteOSInstallLock", "$lock = $null")
    script.write_text(text, encoding="utf-8-sig")
    assert trap._run(script).returncode == 0
    assert (config / "rollback-pending.txt").exists()
    assert "a new run holds the lock" in (config / "rollback.log").read_text(encoding="utf-8", errors="replace")


def test_a_console_stuck_past_the_deadline_is_rolled_back_though_it_holds_the_lock(tmp_path, venv):  # noqa: F811
    """Ctrl+C in a piped run: the installer's powershell lives on and holds the lock, the box has no server."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "rollback-pending.txt").write_text("1")
    trap = TestRollbackTrap()
    script = trap._script(tmp_path, config, venv, os.getpid())  # alive
    text = script.read_text(encoding="utf-8-sig")
    assert "AddMinutes(30)" in text
    text = text.replace("AddMinutes(30)", "AddMinutes(0)").replace("$lock = Enter-RemoteOSInstallLock", "$lock = $null")
    script.write_text(text, encoding="utf-8-sig")
    result = trap._run(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installed(tmp_path, venv) == "1.0"


def test_the_failure_exit_disarms_only_after_the_rollback(tmp_path):
    body = """
function Exit-RemoteOSInstaller { param($Code, $Pause); exit $Code }
function Invoke-RemoteOSRollback { param($Point, $Python, $StopServer, $StartServer, $ProveHealth)
    Write-Host "ROLLBACK"; return [pscustomobject]@{ Ok = $true } }
Exit-RemoteOSAfterFailure -Reason 'x' -Output '' -Point ([pscustomobject]@{ Ref = 'r'; Version = '1' }) -Python 'p' `
    -StopServer { } -StartServer { } -ProveHealth { } -Disarm { Write-Host "DISARM" } -Pause $false
"""
    result = _run(tmp_path, body)
    assert result.returncode == 1
    assert result.stdout.index("ROLLBACK") < result.stdout.index("DISARM")


def _uninstall_functions(tmp_path: Path) -> Path:
    text = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    functions = re.findall(r"^function .*?^}\n", text, re.S | re.M)
    path = tmp_path / "uninstall-functions.ps1"
    path.write_text("".join(functions), encoding="utf-8-sig")
    return path


@pytest.mark.parametrize(("sessions", "expected"), [
    ([" USERNAME  SESSIONNAME  ID  STATE", ">foh  console  1  Active"], "foh"),
    ([" admin  rdp-tcp#1  2  Active", " foh  console  1  Active"], "foh"),
    (["No User exists for *"], "runner"),
])
def test_the_uninstaller_finds_the_console_user_s_profile_not_its_own(tmp_path, sessions, expected):
    root = tmp_path / "Users"
    for name in ("foh", "runner"):
        (root / name).mkdir(parents=True)
    script = tmp_path / "t.ps1"
    listed = ", ".join(_q(line) for line in sessions)
    script.write_text(f"""
. {_q(_uninstall_functions(tmp_path))}
Write-Host (Get-RemoteOSDesktopProfile -Sessions @({listed}) -UserName 'runner' -FallbackProfile 'FALLBACK' `
    -ProfilesRoot {_q(root)})
""", encoding="utf-8-sig")
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert result.stdout.strip() == str(root / expected), result.stderr


def test_a_console_user_with_no_profile_falls_back(tmp_path):
    script = tmp_path / "t.ps1"
    script.write_text(f"""
. {_q(_uninstall_functions(tmp_path))}
Write-Host (Get-RemoteOSDesktopProfile -Sessions @('x console 1 Active') -UserName 'r' -FallbackProfile 'FALLBACK' `
    -ProfilesRoot {_q(tmp_path)})
""", encoding="utf-8-sig")
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert result.stdout.strip() == "FALLBACK", result.stderr


def test_registering_is_idempotent_and_leaves_a_daily_catching_up_highest_task(tmp_path):
    python = Path(sys.executable)
    body = f"""
try {{
    $task = @{{ ConfigDir = {_q(tmp_path)}; PythonPath = {_q(python)}; User = $env:USERNAME }}
    $first = Register-RemoteOSUpdaterTask @task
    $second = Register-RemoteOSUpdaterTask @task
    $task = Get-ScheduledTask -TaskName 'RemoteOSMCP-Updater'
    @{{ first = $first; second = $second; level = [string]$task.Principal.RunLevel
        catchUp = [bool]$task.Settings.StartWhenAvailable
        triggers = @($task.Triggers | Where-Object {{ $_ }}).Count }} | ConvertTo-Json -Compress
}} finally {{
    Unregister-ScheduledTask -TaskName 'RemoteOSMCP-Updater' -Confirm:$false -ErrorAction SilentlyContinue
}}
"""
    got = _ps(tmp_path, body)
    assert (got["first"], got["second"], got["level"]) == (True, False, "Highest")
    assert got["catchUp"] is True and got["triggers"] == 1


def test_the_ring_crosses_the_elevation_inside_the_command(tmp_path):
    command = "Get-RemoteOSRelaunchCommand -ScriptPath 'x.ps1' -InstallerUrl 'u' -SourceRef 'r' -Ring 'canary'"
    body = f"@{{ c = ({command}) }} | ConvertTo-Json -Compress"
    assert "$env:REMOTEOS_MCP_RING = 'canary'" in _ps(tmp_path, body)["c"]
