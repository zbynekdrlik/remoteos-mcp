"""The RemoteOSMCP-Upgrade task's command in real Windows PowerShell 5.1 (#71).

The task runs hidden, elevated and unattended, so its command must take the ref from the file
next to config.json, refuse a bad one before any download, and log everything. Invoke-RestMethod
is replaced by a function (it beats the cmdlet): the "installer" is the text it returns.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest
from test_install_ps1_key import _ps, _q

_FAKE_DOWNLOAD = """\
function Invoke-RestMethod {{ param($Uri, $TimeoutSec)
    Set-Content -LiteralPath {seen} -Value $Uri
    return {installer}
}}
"""
_REPORT = 'Write-Host "REF=$env:REMOTEOS_MCP_REF RELAUNCHED=$env:REMOTEOS_MCP_RELAUNCHED"\n'


def _command(tmp_path: Path, config: Path) -> str:
    body = f"@{{ command = (Get-RemoteOSUpgradeCommand -ConfigDir {_q(config)}) }} | ConvertTo-Json -Compress"
    return _ps(tmp_path, body)["command"]


def _run(tmp_path: Path, config: Path, installer: str = _REPORT) -> tuple[subprocess.CompletedProcess, Path]:
    """The task's command with the fake download; -> its result, the file naming the URL it fetched."""
    command = _command(tmp_path, config)
    seen = tmp_path / "fetched.txt"
    script = tmp_path / "task.ps1"
    script.write_text(_FAKE_DOWNLOAD.format(seen=_q(seen), installer=_q(installer)) + command, encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    return result, seen


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "it's the config"  # the quote: the path goes into the command as a literal
    path.mkdir()
    return path


def test_the_ref_from_the_file_reaches_the_installer_and_the_log(tmp_path, config):
    (config / "upgrade-ref.txt").write_text("release/0.7\n")
    result, seen = _run(tmp_path, config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert seen.read_text().strip() == (
        "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/release/0.7/install.ps1"
    )
    log = (config / "upgrade.log").read_text(encoding="utf-8-sig", errors="replace")
    assert "REF=release/0.7 RELAUNCHED=1" in log
    assert not (config / "upgrade-ref.txt").exists()


def test_no_file_means_main(tmp_path, config):
    result, seen = _run(tmp_path, config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/remoteos-mcp/main/install.ps1" in seen.read_text()


def test_the_installers_exit_code_is_the_tasks(tmp_path, config):
    assert _run(tmp_path, config, "exit 7\n")[0].returncode == 7


@pytest.mark.parametrize("ref", ["a;calc", "-x", "a..b", "$(calc)", "a b", "a" * 101])
def test_a_bad_ref_is_refused_before_any_download(tmp_path, config, ref):
    (config / "upgrade-ref.txt").write_text(ref + "\n")
    result, seen = _run(tmp_path, config)
    assert result.returncode == 1
    assert not seen.exists()
    assert "nothing was changed" in (config / "upgrade.log").read_text(encoding="utf-8-sig", errors="replace")


def test_the_task_arguments_run_that_command_with_no_prompt(tmp_path, config):
    """The real argument string, as Task Scheduler passes it; a bad ref stops it before the network."""
    (config / "upgrade-ref.txt").write_text("a;b\n")
    arguments = _ps(
        tmp_path, f"@{{ a = (Get-RemoteOSUpgradeTaskArguments -ConfigDir {_q(config)}) }} | ConvertTo-Json -Compress"
    )["a"]
    words = arguments.split(" ")
    assert words[:5] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle"]
    assert base64.b64decode(words[-1]).decode("utf-16-le") == _command(tmp_path, config)
    result = subprocess.run(
        ["powershell", *words], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 1


def test_registering_is_idempotent_and_leaves_an_on_demand_highest_task(tmp_path, config):
    body = f"""
try {{
    $first = Register-RemoteOSUpgradeTask -ConfigDir {_q(config)} -User $env:USERNAME
    $second = Register-RemoteOSUpgradeTask -ConfigDir {_q(config)} -User $env:USERNAME
    $other = Register-RemoteOSUpgradeTask -ConfigDir {_q(tmp_path)} -User $env:USERNAME
    $task = Get-ScheduledTask -TaskName 'RemoteOSMCP-Upgrade'
    @{{ first = $first; second = $second; other = $other; level = [string]$task.Principal.RunLevel
        triggers = @($task.Triggers).Count; run = [string]@($task.Actions)[0].Execute }} | ConvertTo-Json -Compress
}} finally {{
    Unregister-ScheduledTask -TaskName 'RemoteOSMCP-Upgrade' -Confirm:$false -ErrorAction SilentlyContinue
}}
"""
    got = _ps(tmp_path, body)
    assert (got["first"], got["second"], got["other"]) == (True, False, True)
    assert (got["level"], got["triggers"], got["run"]) == ("Highest", 0, "powershell.exe")
