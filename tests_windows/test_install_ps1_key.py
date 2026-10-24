"""install.ps1's key handling in real Windows PowerShell 5.1 (#12, design issuecomment-6293340521).

start-remoteos.bat used to start the server with `--auth-key "<key>"`, so the key was on the
server's command line (Win32_Process.CommandLine, Task Manager), and the key precedence read
only config.json and the old winremote config: a box whose key lived only in remoteos.toml
(stream-snv's config.json/toml split) got a NEW key.

These tests load install.ps1's own functions (parsed out of the real file, nothing re-typed)
into powershell.exe 5.1 and run them, plus one real cmd.exe run of the generated .bat.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "install.ps1"

_PRELUDE = """\
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{installer}', [ref]$null, [ref]$parseErrors)
if ($parseErrors) {{ throw "install.ps1 does not parse: $($parseErrors -join '; ')" }}
$isFunction = {{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }}
foreach ($definition in $ast.FindAll($isFunction, $false)) {{
    . ([ScriptBlock]::Create($definition.Extent.Text))
}}
"""


def _q(value: object) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps(tmp_path: Path, body: str) -> dict:
    """Run `body` after loading install.ps1's functions; its last output line is JSON."""
    script = tmp_path / "probe.ps1"
    script.write_text(_PRELUDE.format(installer=str(INSTALLER).replace("'", "''")) + body, encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _resolve(tmp_path: Path, *, explicit: str = "") -> dict:
    """Resolve-RemoteOSAuthKey over the key sources Get-RemoteOSExistingKeys finds in tmp_path:
    cfg/start-remoteos.bat, cfg/config.json, remoteos.toml, old/config.json."""
    return _ps(tmp_path, f"""
$found = Get-RemoteOSExistingKeys -ConfigDir {_q(tmp_path / 'cfg')} -TomlPath {_q(tmp_path / 'remoteos.toml')} `
    -OldConfigDir {_q(tmp_path / 'old')}
$r = Resolve-RemoteOSAuthKey -Explicit {_q(explicit)} -Found $found
@{{ key = $r.Key; source = $r.Source }} | ConvertTo-Json -Compress
""")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


class TestStartScript:
    def test_the_start_script_sets_the_key_in_the_environment_never_on_the_command_line(self, tmp_path: Path) -> None:
        out = _ps(tmp_path, """
$text = New-RemoteOSStartScript -PythonPath 'C:\\Python312\\python.exe' -Port 8092 -AuthKey 'StartKey0123456789'
@{ text = $text } | ConvertTo-Json -Compress
""")
        text = out["text"]
        lines = [line.strip() for line in text.splitlines()]
        assert "--auth-key" not in text, text
        server = [line for line in lines if "-m remoteos" in line]
        assert server == [
            '"C:\\Python312\\python.exe" -m remoteos --transport streamable-http --enable-all'
            " --host 0.0.0.0 --port 8092"
        ], server
        assert 'set "REMOTEOS_AUTH_KEY=StartKey0123456789"' in lines, text
        assert lines.index('set "REMOTEOS_AUTH_KEY=StartKey0123456789"') < lines.index(server[0])

    def test_cmd_hands_the_key_to_the_server_through_its_environment(self, tmp_path: Path) -> None:
        """A real cmd.exe run of the generated .bat. The 'server' is a .cmd that records the
        environment variable and its own arguments (started without `call`, so the .bat's
        restart loop ends with it)."""
        key = "CmdKey0123456789abcdef"
        server = tmp_path / "fake-server.cmd"
        server.write_text(
            '@echo off\r\n>"%~dp0seen-key.txt" echo %REMOTEOS_AUTH_KEY%\r\n>"%~dp0seen-args.txt" echo %*\r\n',
            encoding="ascii",
        )
        bat = tmp_path / "start-remoteos.bat"
        _ps(tmp_path, f"""
New-RemoteOSStartScript -PythonPath {_q(server)} -Port 18092 -AuthKey {_q(key)} | Set-Content -LiteralPath {_q(bat)}
@{{ ok = $true }} | ConvertTo-Json -Compress
""")

        subprocess.run(["cmd", "/c", str(bat)], capture_output=True, timeout=120, stdin=subprocess.DEVNULL)

        assert (tmp_path / "seen-key.txt").read_text().strip() == key
        args = (tmp_path / "seen-args.txt").read_text()
        assert "-m remoteos" in args and "--port 18092" in args, args
        assert key not in args and "--auth-key" not in args, args

    def test_the_start_script_s_port_check_never_gets_the_key(self, tmp_path: Path) -> None:
        """Review round 10: every program cmd.exe starts inherits its environment. A netstat.cmd
        in the .bat's working directory (cmd looks there before PATH) records what the loop's
        port check sees; the server still gets the key."""
        key = "LoopKey0123456789abcdef"
        server = tmp_path / "fake-server.cmd"
        server.write_text('@echo off\r\n>"%~dp0seen-key.txt" echo %REMOTEOS_AUTH_KEY%\r\n', encoding="ascii")
        (tmp_path / "netstat.cmd").write_text(
            '@echo off\r\n>"%~dp0netstat-saw.txt" echo [%REMOTEOS_AUTH_KEY%]\r\n', encoding="ascii"
        )
        bat = tmp_path / "start-remoteos.bat"
        _ps(tmp_path, f"""
New-RemoteOSStartScript -PythonPath {_q(server)} -Port 18093 -AuthKey {_q(key)} | Set-Content -LiteralPath {_q(bat)}
@{{ ok = $true }} | ConvertTo-Json -Compress
""")

        subprocess.run(["cmd", "/c", str(bat)], capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                       cwd=tmp_path)

        assert (tmp_path / "netstat-saw.txt").read_text().strip() == "[]", "the port check got the key"
        assert (tmp_path / "seen-key.txt").read_text().strip() == key


class TestKeyPrecedence:
    def test_a_key_only_in_remoteos_toml_is_reused(self, tmp_path: Path) -> None:
        _write(tmp_path / "remoteos.toml", '[server]\nport = 8092\nauth_key = "TomlOnlyKey0123456789"\n')

        out = _resolve(tmp_path)

        assert out["key"] == "TomlOnlyKey0123456789"
        assert "remoteos.toml" in out["source"]

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ({"cfg/start-remoteos.bat": '"C:\\py\\python.exe" -m remoteos --port 8092 --auth-key "BatLegacyKey01"\r\n',
              "cfg/config.json": '{"auth_key": "ConfigKey01"}', "remoteos.toml": '[server]\nauth_key = "TomlKey01"\n'},
             "BatLegacyKey01"),
            ({"cfg/start-remoteos.bat": '@echo off\r\nset "REMOTEOS_AUTH_KEY=BatEnvKey01"\r\n',
              "cfg/config.json": '{"auth_key": "ConfigKey01"}'},
             "BatEnvKey01"),
            ({"cfg/config.json": '{"auth_key": "ConfigKey01"}', "remoteos.toml": '[server]\nauth_key = "TomlKey01"\n'},
             "ConfigKey01"),
            ({"old/config.json": '{"auth_key": "WinremoteKey01"}'}, "WinremoteKey01"),
        ],
        ids=["legacy-bat-wins", "bat-env-wins", "config-before-toml", "old-winremote"],
    )
    def test_the_running_start_script_s_key_comes_first(self, tmp_path: Path, files: dict, expected: str) -> None:
        for rel, text in files.items():
            _write(tmp_path / rel, text)

        assert _resolve(tmp_path)["key"] == expected

    def test_an_auth_key_outside_the_server_table_is_not_a_key(self, tmp_path: Path) -> None:
        _write(tmp_path / "remoteos.toml", '[client]\nauth_key = "NotTheServerKey01"\n')

        out = _resolve(tmp_path)

        assert out["key"] != "NotTheServerKey01"
        assert not out["source"]

    def test_no_key_anywhere_gets_a_new_random_key(self, tmp_path: Path) -> None:
        keys = [_resolve(tmp_path)["key"] for _ in range(5)]

        assert all(re.fullmatch(r"[A-Za-z0-9]{32}", k) for k in keys), keys
        assert len(set(keys)) == 5
        # Get-Random -Count 32 drew 32 DISTINCT characters: no key could repeat one
        assert any(len(set(k)) < 32 for k in keys), "every key has 32 distinct characters: not uniform random"

    def test_an_existing_key_the_installer_cannot_carry_is_refused_never_replaced(self, tmp_path: Path) -> None:
        """Review round 1: a key with other characters was skipped and a NEW key minted, silently
        cutting the client off (#51). The first key the PC has decides."""
        _write(tmp_path / "cfg" / "config.json", '{"auth_key": "has-dash_and.dot"}')
        _write(tmp_path / "remoteos.toml", '[server]\nauth_key = "TomlKey01"\n')

        out = _ps(tmp_path, f"""
$found = Get-RemoteOSExistingKeys -ConfigDir {_q(tmp_path / 'cfg')} -TomlPath {_q(tmp_path / 'remoteos.toml')} `
    -OldConfigDir {_q(tmp_path / 'old')}
try {{ Resolve-RemoteOSAuthKey -Explicit '' -Found $found | Out-Null; $refused = $false }}
catch {{ $refused = "$_" -match 'REMOTEOS_MCP_AUTH_KEY' }}
@{{ refused = $refused }} | ConvertTo-Json -Compress
""")

        assert out["refused"] is True

    def test_an_explicit_key_wins_and_an_invalid_one_is_refused(self, tmp_path: Path) -> None:
        _write(tmp_path / "cfg" / "config.json", '{"auth_key": "ConfigKey01"}')

        assert _resolve(tmp_path, explicit="ExplicitKey01")["key"] == "ExplicitKey01"
        out = _ps(tmp_path, """
try { Resolve-RemoteOSAuthKey -Explicit 'not a valid key!' -Found ([ordered]@{}) | Out-Null; $refused = $false }
catch { $refused = "$_" -match 'REMOTEOS_MCP_AUTH_KEY' }
@{ refused = $refused } | ConvertTo-Json -Compress
""")
        assert out["refused"] is True


class TestOwnerOnlyAcl:
    def test_the_key_files_get_a_protected_acl_of_the_user_system_and_administrators(self, tmp_path: Path) -> None:
        secret = tmp_path / "config.json"
        secret.write_text("{}")

        out = _ps(tmp_path, f"""
Set-RemoteOSOwnerOnlyAcl -Path {_q(secret)} -User $env:USERNAME
$acl = Get-Acl -LiteralPath {_q(secret)}
$sid = [System.Security.Principal.SecurityIdentifier]
$user = ([System.Security.Principal.NTAccount]$env:USERNAME).Translate($sid).Value
$rules = @($acl.Access | ForEach-Object {{ @{{ sid = $_.IdentityReference.Translate($sid).Value;
    rights = $_.FileSystemRights.ToString(); type = $_.AccessControlType.ToString() }} }})
@{{ protected = $acl.AreAccessRulesProtected; user = $user; rules = $rules }} | ConvertTo-Json -Compress -Depth 4
""")

        assert out["protected"] is True
        sids = {rule["sid"] for rule in out["rules"]}
        assert sids == {out["user"], "S-1-5-18", "S-1-5-32-544"}, out["rules"]
        assert all(rule["type"] == "Allow" and "FullControl" in rule["rights"] for rule in out["rules"]), out["rules"]


class TestElevationGate:
    """#57: a non-admin run printed two warnings and carried on (no task, no firewall rule, a
    server without admin). Now: admin proceeds, a person at the desktop is relaunched elevated
    (one UAC click), automation and an explicit key fail before anything changes."""

    @pytest.mark.parametrize(
        ("is_admin", "interactive", "explicit_key", "relaunched", "expected"),
        [
            (True, True, True, False, "proceed"),
            (True, True, False, False, "proceed"),
            (True, False, True, False, "proceed"),
            (True, False, False, False, "proceed"),
            (False, True, False, False, "relaunch"),
            (False, True, True, False, "fail"),
            (False, False, False, False, "fail"),
            (False, False, True, False, "fail"),
            # the relaunched run: elevated proceeds, still not elevated never relaunches again
            (True, True, False, True, "proceed"),
            (False, True, False, True, "fail"),
        ],
    )
    def test_the_decision_table(self, tmp_path: Path, is_admin: bool, interactive: bool, explicit_key: bool,
                                relaunched: bool, expected: str) -> None:
        def ps_bool(value: bool) -> str:
            return "$true" if value else "$false"

        out = _ps(tmp_path, f"""
$a = Get-RemoteOSElevationAction -IsAdmin {ps_bool(is_admin)} -Interactive {ps_bool(interactive)} `
    -ExplicitKey {ps_bool(explicit_key)} -Relaunched {ps_bool(relaunched)}
@{{ action = $a }} | ConvertTo-Json -Compress
""")

        assert out["action"] == expected

    @pytest.mark.parametrize(
        ("ssh_connection", "ssh_client", "session_id", "redirected", "expected"),
        [
            ("", "", 1, False, True),
            ("", "", 2, False, True),
            ("", "", 0, False, False),
            ("10.0.0.5 50000 10.0.0.9 22", "", 1, False, False),
            ("", "10.0.0.5 50000 22", 1, False, False),
            ("10.0.0.5 50000 10.0.0.9 22", "10.0.0.5 50000 22", 0, False, False),
            ("", "", 1, True, False),
        ],
        ids=["desktop", "rdp-session", "session-0", "ssh-connection", "ssh-client", "ssh-in-session-0",
             "a-program-in-a-desktop-session"],
    )
    def test_ssh_session_0_or_no_console_input_is_not_interactive(
            self, tmp_path: Path, ssh_connection: str, ssh_client: str, session_id: int, redirected: bool,
            expected: bool) -> None:
        """Review round 1: the remoteos Shell tool (stdin NUL) runs in the desktop session with no
        SSH variable, and nobody would answer a UAC prompt there."""
        out = _ps(tmp_path, f"""
$i = Test-RemoteOSInteractiveSession -SshConnection {_q(ssh_connection)} -SshClient {_q(ssh_client)} `
    -SessionId {session_id} -InputRedirected {"$true" if redirected else "$false"}
@{{ interactive = $i }} | ConvertTo-Json -Compress
""")

        assert out["interactive"] is expected


class TestExitHelper:
    """Exit-RemoteOSInstaller: a piped run in a person's console waits for Enter before it exits
    (`exit` in `irm | iex` closes the console), every other run exits at once (review round 2)."""

    def _exit(self, tmp_path: Path, pause: bool, read_host: str) -> tuple[int, bool]:
        marker = tmp_path / "read-host-called.txt"
        script = tmp_path / "exit-probe.ps1"
        body = (f"function Read-Host {{ Set-Content -LiteralPath {_q(marker)} -Value 1; {read_host} }}\n"
                f"Exit-RemoteOSInstaller -Code 3 -Pause {'$true' if pause else '$false'}\n"
                "Write-Output 'not exited'\nexit 0\n")
        script.write_text(_PRELUDE.format(installer=str(INSTALLER).replace("'", "''")) + body, encoding="utf-8-sig")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        assert "not exited" not in result.stdout, result.stdout
        return result.returncode, marker.exists()

    def test_a_pause_reads_enter_then_exits_with_the_code(self, tmp_path: Path) -> None:
        assert self._exit(tmp_path, True, "'stub'") == (3, True)

    def test_no_pause_exits_at_once(self, tmp_path: Path) -> None:
        assert self._exit(tmp_path, False, "'stub'") == (3, False)

    def test_a_read_host_that_cannot_read_still_exits(self, tmp_path: Path) -> None:
        """-NonInteractive makes Read-Host throw; inside the health loop's catch-all that would be
        swallowed and the run would go on."""
        assert self._exit(tmp_path, True, "throw 'PowerShell is in NonInteractive mode'") == (3, True)


class TestElevatedRelaunch:
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_the_elevated_window_runs_the_file_with_the_commit_and_returns_its_exit_code(
            self, tmp_path: Path) -> None:
        """The relaunch command, run for real (not elevated; Read-Host stubbed): a path with a
        quote and a space survives, the child gets REMOTEOS_MCP_REF + REMOTEOS_MCP_RELAUNCHED,
        and the window's exit code is the installer's."""
        folder = tmp_path / "it's a dir"
        folder.mkdir()
        seen = tmp_path / "seen.txt"
        installer = folder / "fake install.ps1"
        installer.write_text(
            f"Set-Content -LiteralPath {_q(seen)} -Value \"$env:REMOTEOS_MCP_REF|$env:REMOTEOS_MCP_RELAUNCHED\"\r\n"
            "exit 7\r\n",
            encoding="ascii",
        )
        runner = tmp_path / "runner.ps1"
        out = _ps(tmp_path, f"""
$command = Get-RemoteOSRelaunchCommand -ScriptPath {_q(installer)} -InstallerUrl 'unused' -SourceRef '{self.SHA}'
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput($command, [ref]$null, [ref]$errors) | Out-Null
Set-Content -LiteralPath {_q(runner)} -Value ("function Read-Host {{ 'stub' }}`n" + $command) -Encoding UTF8
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File {_q(runner)} | Out-Null
$code = $LASTEXITCODE
@{{ parse = @($errors).Count; code = $code; seen = (Get-Content -LiteralPath {_q(seen)} -Raw).Trim() }} |
    ConvertTo-Json -Compress
""")

        assert out["parse"] == 0, out
        assert out["code"] == 7, out
        assert out["seen"] == f"{self.SHA}|1", out

    def test_a_piped_run_relaunches_the_installer_of_the_commit(self, tmp_path: Path) -> None:
        url = f"https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/{self.SHA}/install.ps1"
        out = _ps(tmp_path, f"""
$command = Get-RemoteOSRelaunchCommand -ScriptPath '' -InstallerUrl {_q(url)} -SourceRef '{self.SHA}'
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($command, [ref]$null, [ref]$errors)
@{{ parse = @($errors).Count; text = $command }} | ConvertTo-Json -Compress
""")

        assert out["parse"] == 0, out
        assert f"-Command 'irm ''{url}'' | iex'" in out["text"], out["text"]
        assert f"$env:REMOTEOS_MCP_REF = '{self.SHA}'" in out["text"], out["text"]

    def test_the_relaunch_asks_uac_for_that_command_and_returns_its_exit_code(self, tmp_path: Path) -> None:
        """Start-Process is stubbed (no UAC prompt in CI): the encoded command is the relaunch
        command, it goes through -Verb RunAs, and a refused elevation returns $null."""
        out = _ps(tmp_path, f"""
function Start-Process {{
    param($FilePath, $Verb, [switch]$PassThru, $ArgumentList)
    if ($script:refuse) {{ throw 'The operation was canceled by the user' }}
    $script:call = @{{ file = $FilePath; verb = $Verb; args = $ArgumentList }}
    $p = [pscustomobject]@{{ ExitCode = 5; Handle = 1 }}
    $p | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ }}
    return $p
}}
$script:refuse = $false
$path = 'C:\\x\\install.ps1'
$code = Invoke-RemoteOSElevatedRelaunch -ScriptPath $path -InstallerUrl 'unused' -SourceRef '{self.SHA}'
$decoded = [System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($script:call.args[-1]))
$expected = Get-RemoteOSRelaunchCommand -ScriptPath $path -InstallerUrl 'unused' -SourceRef '{self.SHA}'
$script:refuse = $true
$refused = Invoke-RemoteOSElevatedRelaunch -ScriptPath $path -InstallerUrl 'unused' -SourceRef '{self.SHA}'
@{{ code = $code; verb = $script:call.verb; file = $script:call.file; same = ($decoded -ceq $expected);
    flag = $script:call.args[-2]; refused = $refused }} | ConvertTo-Json -Compress
""")

        assert out["code"] == 5, out
        assert out["verb"] == "RunAs" and out["file"] == "powershell.exe", out
        assert out["flag"] == "-EncodedCommand" and out["same"] is True, out
        assert out["refused"] is None, out


class TestListenerSession:
    def test_the_listener_s_session_is_its_owning_process_s(self, tmp_path: Path) -> None:
        """The SSH install proves the server it started through the task: the :port listener's
        owning process must run in a session other than 0. A real listener here, and no listener
        reads as $null."""
        out = _ps(tmp_path, """
$port = 18096
$before = Get-RemoteOSListenerSessionId -Port $port
$p = Start-Process -FilePath PYTHON_EXE -ArgumentList '-m', 'http.server', "$port", '--bind', '127.0.0.1' `
    -WindowStyle Hidden -PassThru
try {
    $seen = $null
    for ($i = 0; $i -lt 40 -and $null -eq $seen; $i++) {
        Start-Sleep -Milliseconds 500
        $seen = Get-RemoteOSListenerSessionId -Port $port
    }
    $own = (Get-Process -Id $p.Id).SessionId
} finally {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
}
@{ before = $before; seen = $seen; own = $own } | ConvertTo-Json -Compress
""".replace("PYTHON_EXE", _q(sys.executable)))  # pytest's own interpreter, never a PATH/App Paths lookup

        assert out["before"] is None
        assert out["seen"] is not None, out
        assert out["seen"] == out["own"], out


class TestSourceRef:
    @pytest.mark.parametrize(
        ("ref", "valid"),
        [
            ("main", True),
            ("dev", True),
            ("v0.7.0", True),
            ("feature/x-1_2", True),
            ("0123456789abcdef0123456789abcdef01234567", True),
            ("", False),
            ("main'; Remove-Item x", False),
            ("main ref", False),
            ("../main", False),
            ("main\n", False),
        ],
    )
    def test_only_a_plain_ref_name_is_used_in_a_url_or_the_relaunch(self, tmp_path: Path, ref: str,
                                                                    valid: bool) -> None:
        """#12: $Ref (REMOTEOS_MCP_REF) goes into the GitHub API URL; anything but a plain ref name
        is refused before anything changes."""
        out = _ps(tmp_path, f"""
@{{ valid = (Test-RemoteOSRefName {_q(ref)}) }} | ConvertTo-Json -Compress
""")

        assert out["valid"] is valid
