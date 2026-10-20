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
