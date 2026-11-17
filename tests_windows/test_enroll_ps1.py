"""enroll.ps1 (#101), the README one-liner for Windows, in real PowerShell: its functions are parsed out
of the real generated file, as in test_install_ps1_key.py. The UAC relaunch and the real installer
run are the supervisor's live box (predaj3, #111); everything around them is here.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "enroll.ps1"

_PRELUDE = """\
$ErrorActionPreference = 'Stop'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$parseErrors)
if ($parseErrors) {{ throw "enroll.ps1 does not parse: $($parseErrors -join '; ')" }}
$isFunction = {{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }}
foreach ($definition in $ast.FindAll($isFunction, $false)) {{
    . ([ScriptBlock]::Create($definition.Extent.Text))
}}
$EnrollSshKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake dev1'
$EnrollLogTailChars = 200000
"""


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps(tmp_path: Path, body: str) -> dict:
    """Run *body* after loading enroll.ps1's functions; its last output line is JSON."""
    script = tmp_path / "probe.ps1"
    script.write_text(_PRELUDE.format(script=str(SCRIPT).replace("'", "''")) + body, encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("ref", "ok"), [
    ("main", True), ("dev", True), ("v1.2/x", True), ("a" * 101, False), ("main;calc", False), ("a..b", False),
    ("main'", False), ("", False),
])
def test_enroll_ref(tmp_path, ref, ok):
    assert _ps(tmp_path, f"@{{ ok = Test-RemoteOSEnrollRef {_q(ref)} }} | ConvertTo-Json -Compress")["ok"] is ok


@pytest.mark.parametrize(("name", "expected"), [("PREDAJ3", "PREDAJ3"), ("bad name", "unknown"), ("", "unknown")])
def test_log_hostname(tmp_path, name, expected):
    got = _ps(tmp_path, f"@{{ n = Get-RemoteOSEnrollHostName {_q(name)} }} | ConvertTo-Json -Compress")
    assert got["n"] == expected


def test_the_relaunch_runs_this_script_again_at_the_same_ref_and_waits(tmp_path):
    command = _ps(tmp_path, "@{ c = Get-RemoteOSEnrollRelaunchCommand -Raw 'https://raw.example/r' -Ref 'dev' }"
                            " | ConvertTo-Json -Compress")["c"]
    assert "$env:REMOTEOS_MCP_REF = 'dev'" in command
    assert "irm 'https://raw.example/r/dev/enroll.ps1' | iex" in command
    assert "Read-Host" in command


class _Receiver:
    """A local endpoint standing in for dev1's: records each POST, answers *reply*."""

    def __init__(self, reply: bytes = b"ok", status: int = 200) -> None:
        self.posts: list[dict] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                return

            def do_POST(self):  # noqa: N802 - stdlib naming
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                receiver.posts.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(status)
                if status == 302:
                    self.send_header("location", "https://login.example/")
                self.send_header("content-length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/enroll/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_the_install_log_tail_goes_to_the_log_route_with_its_headers(tmp_path):
    receiver = _Receiver()
    try:
        got = _ps(tmp_path, f"""
$text = ('x' * 300000) + "`nZltY kon [X] the end"
$ok = Send-RemoteOSInstallLog -Url {_q(receiver.url)} -Text $text -HostName 'PREDAJ3' -ExitCode 3
@{{ ok = $ok }} | ConvertTo-Json -Compress
""")
    finally:
        receiver.close()
    assert got["ok"] is True
    [post] = receiver.posts
    headers = {k.lower(): v for k, v in post["headers"].items()}
    assert post["path"] == "/enroll/log"
    assert headers["x-remoteos-host"] == "PREDAJ3" and headers["x-remoteos-os"] == "windows"
    assert headers["x-remoteos-exit"] == "3" and headers["user-agent"] == "remoteos-enroll"
    assert len(post["body"]) == 200000 and post["body"].endswith(b"[X] the end")  # the TAIL


def test_a_redirect_is_never_taken_for_a_delivered_log(tmp_path):
    receiver = _Receiver(status=302)
    try:
        got = _ps(tmp_path, f"@{{ ok = Send-RemoteOSInstallLog -Url {_q(receiver.url)} -Text 'x' -HostName 'pc'"
                            " -ExitCode 1 } | ConvertTo-Json -Compress")
    finally:
        receiver.close()
    assert got["ok"] is False


def test_the_installer_runs_as_a_child_with_the_values_then_they_leave_the_environment(tmp_path):
    installer = tmp_path / "install.ps1"
    installer.write_text(
        "Write-Host \"enroll=$env:REMOTEOS_ENROLL\"\n"
        "Write-Host \"sshkey=$env:REMOTEOS_ENROLL_SSH_KEY\"\n"
        "Write-Host \"ref=$env:REMOTEOS_MCP_REF\"\n"
        "Write-Error 'pip failed'\n"
        "exit 3\n", encoding="ascii")
    log = tmp_path / "install.log"
    got = _ps(tmp_path, f"""
$EnrollUrl = 'https://enroll.example/enroll/'
$code = Invoke-RemoteOSEnrolledInstall -InstallerPath {_q(installer)} -LogPath {_q(log)} -Ref 'dev'
@{{ code = $code; left = [bool]($env:REMOTEOS_ENROLL -or $env:REMOTEOS_ENROLL_SSH_KEY -or $env:REMOTEOS_MCP_REF);
   log = [string](Get-Content -LiteralPath {_q(log)} -Raw) }} | ConvertTo-Json -Compress
""")
    assert got["code"] == 3
    assert got["left"] is False
    assert "enroll=https://enroll.example/enroll/" in got["log"]
    assert "sshkey=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake dev1" in got["log"]
    assert "ref=dev" in got["log"] and "pip failed" in got["log"]


@pytest.mark.parametrize(("path", "code"), [("/dev/enroll.ps1", 7), ("/missing", 1)])
def test_the_elevated_window_exits_with_the_run_s_code_and_1_when_the_download_fails(tmp_path, path, code):
    served = b"$global:LASTEXITCODE = 7\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            return

        def do_GET(self):  # noqa: N802 - stdlib naming
            ok = self.path == path
            self.send_response(200 if ok else 404)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(served) if ok else 0))
            self.end_headers()
            if ok:
                self.wfile.write(served)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        raw = f"http://127.0.0.1:{server.server_address[1]}"
        command = _ps(tmp_path, f"@{{ c = Get-RemoteOSEnrollRelaunchCommand -Raw {_q(raw)} -Ref 'dev' }}"
                                " | ConvertTo-Json -Compress")["c"]
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == code, result.stdout + result.stderr
