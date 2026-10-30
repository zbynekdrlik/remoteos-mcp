"""install.ps1's preflight wheelhouse, offline install and rollback in real Windows PowerShell 5.1 (#64).

install.ps1 stopped the server, pre-uninstalled remoteos-mcp/fastmcp/fastmcp-slim and only THEN
downloaded everything in one `pip install <archive>.zip`. On resolume-pp's slow site link pip
read-timed-out on PyPI and the box stayed without MCP (~10 min, then ~1 h). Now the download is
a preflight into a per-commit wheelhouse while the old server still runs, the install is offline
from it, and a failure after the stop reinstalls the previous commit's wheelhouse.

These tests run install.ps1's own functions against a real pip in a throwaway venv, with a local
"index" (a find-links folder of wheels and source archives built here) and no network: the
source archives use an in-tree build backend with no build requirements, so `pip wheel` builds
them offline as pip builds the real remoteos archive and pyautogui online.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from test_install_ps1_key import _PRELUDE, INSTALLER, _q

SHA_OLD = "1111111111111111111111111111111111111111"
SHA_NEW = "2222222222222222222222222222222222222222"
# Any attempt to reach an index fails at once: the install and the rollback must be offline
_NO_NETWORK = "$env:PIP_INDEX_URL = 'http://127.0.0.1:9/simple'\n"

_WHEEL_FILE = "Wheel-Version: 1.0\nGenerator: remoteos-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"

# A PEP 517 backend inside the source tree (backend-path), with no build requirements: pip's
# isolated build env needs nothing from an index. It builds the wheel described in dist.json.
_BACKEND = '''\
import json, os, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist.json")))
    dist, version = d["name"].replace("-", "_"), d["version"]
    info = dist + "-" + version + ".dist-info"
    meta = "Metadata-Version: 2.1\\nName: " + d["name"] + "\\nVersion: " + version + "\\n"
    meta += "".join("Requires-Dist: " + r + "\\n" for r in d["requires"])
    entries = dict(d["files"])
    entries[info + "/METADATA"] = meta
    entries[info + "/WHEEL"] = d["wheel"]
    name = dist + "-" + version + "-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as z:
        for path, text in entries.items():
            z.writestr(path, text)
        z.writestr(info + "/RECORD", "".join(p + ",,\\n" for p in entries) + info + "/RECORD,,\\n")
    return name
'''
_PYPROJECT = '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'


def _wheel(folder: Path, name: str, version: str, files: dict[str, str], requires: tuple[str, ...] = ()) -> Path:
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    entries = dict(files)
    entries[f"{info}/METADATA"] = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                                   + "".join(f"Requires-Dist: {r}\n" for r in requires))
    entries[f"{info}/WHEEL"] = _WHEEL_FILE
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        for member, text in entries.items():
            z.writestr(member, text)
        z.writestr(f"{info}/RECORD", "".join(f"{m},,\n" for m in entries) + f"{info}/RECORD,,\n")
    return path


def _sdist(folder: Path, name: str, version: str, files: dict[str, str], requires: tuple[str, ...] = ()) -> Path:
    """A source archive pip can only use by building it (like pyautogui, or the remoteos archive)."""
    top = f"{name}-{version}"
    members = {
        "pyproject.toml": _PYPROJECT,
        "backend.py": _BACKEND,
        "PKG-INFO": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        "dist.json": json.dumps({"name": name, "version": version, "requires": list(requires), "files": files,
                                 "wheel": _WHEEL_FILE}),
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{top}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for member, text in members.items():
            data = text.encode()
            entry = tarfile.TarInfo(f"{top}/{member}")
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return path


_REMOTEOS = {"remoteos/__init__.py": ""}
_FASTMCP = {"fastmcp/__init__.py": "class FastMCP:\n    pass\n"}


@pytest.fixture
def venv(tmp_path: Path) -> Path:
    """A throwaway venv with pip: the box's Python, never the test interpreter's packages."""
    target = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True, timeout=300)
    python = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    assert python.exists(), python
    return python


@pytest.fixture(scope="module")
def shared_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A venv for tests that only read it (an offline dry run installs nothing)."""
    target = tmp_path_factory.mktemp("shared") / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True, timeout=300)
    return target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    script = tmp_path / "probe.ps1"
    script.write_text(_PRELUDE.format(installer=str(INSTALLER).replace("'", "''")) + body, encoding="utf-8-sig")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
    )


def _ps(tmp_path: Path, body: str) -> dict:
    result = _run(tmp_path, body)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _installed(tmp_path: Path, python: Path) -> str | None:
    return _ps(tmp_path, f"@{{ v = Get-RemoteOSInstalledVersion {_q(python)} }} | ConvertTo-Json -Compress")["v"]


def _wheelhouse(folder: Path, version: str, *, requires: tuple[str, ...] = ("fastmcp",)) -> Path:
    """A complete wheelhouse as the preflight leaves it: wheels and the commit's constraints.txt."""
    _wheel(folder, "remoteos-mcp", version, _REMOTEOS, requires)
    _wheel(folder, "fastmcp", "9.0", _FASTMCP)
    (folder / "constraints.txt").write_text("fastmcp==9.0\n")
    return folder


class TestPreflight:
    def _index(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        index = tmp_path / "index"
        _wheel(index, "fastmcp", "9.0", _FASTMCP)
        _wheel(index, "fakedep", "1.0", {"fakedep/__init__.py": ""})
        _wheel(index, "fakedep", "2.0", {"fakedep/__init__.py": ""})
        _sdist(index, "fakesdist", "1.0", {"fakesdist/__init__.py": ""})
        constraints = tmp_path / "constraints.txt"
        constraints.write_text("fakedep==1.0\n")
        return index, constraints, tmp_path / "home" / "wheelhouse"

    def _preflight(self, tmp_path: Path, python: Path, index: Path, root: Path, source: Path,
                   constraints: Path) -> dict:
        return _ps(tmp_path, f"""
$env:PIP_NO_INDEX = '1'
$env:PIP_FIND_LINKS = {_q(index)}
$r = New-RemoteOSWheelhouse -Python {_q(python)} -Root {_q(root)} -Sha '{SHA_NEW}' -SourceUrl {_q(source)} `
    -ConstraintsUrl {_q(constraints.as_uri())}
@{{ ok = $r.Ok; version = $r.Version; reused = $r.Reused; error = $r.Error; output = $r.Output; path = $r.Path }} |
    ConvertTo-Json -Compress
""")

    def test_every_wheel_is_downloaded_and_every_source_archive_built_into_the_commit_s_wheelhouse(
            self, tmp_path: Path, venv: Path) -> None:
        index, constraints, root = self._index(tmp_path)
        source = _sdist(tmp_path / "archive", "remoteos-mcp", "2.0", _REMOTEOS, ("fastmcp", "fakedep", "fakesdist"))

        out = self._preflight(tmp_path, venv, index, root, source, constraints)

        assert out["ok"] is True, out
        assert out["version"] == "2.0" and out["reused"] is False
        assert Path(out["path"]) == root / SHA_NEW
        assert sorted(p.name for p in (root / SHA_NEW).iterdir()) == [
            "constraints.txt", "fakedep-1.0-py3-none-any.whl", "fakesdist-1.0-py3-none-any.whl",
            "fastmcp-9.0-py3-none-any.whl", "remoteos_mcp-2.0-py3-none-any.whl",
        ], "a source archive is left in the wheelhouse, or the constraints were not applied"
        assert (root / SHA_NEW / "constraints.txt").read_text() == "fakedep==1.0\n"
        assert [p.name for p in root.iterdir()] == [SHA_NEW], "a partial download was left behind"
        assert _installed(tmp_path, venv) is None, "the preflight installed something"

    def test_a_complete_wheelhouse_is_reused_without_a_download(self, tmp_path: Path, venv: Path) -> None:
        index, constraints, root = self._index(tmp_path)
        _wheelhouse(root / SHA_NEW, "2.0")
        empty = tmp_path / "empty-index"
        empty.mkdir()

        out = self._preflight(tmp_path, venv, empty, root, tmp_path / "missing.tar.gz", tmp_path / "missing.txt")

        assert out["ok"] is True and out["reused"] is True and out["version"] == "2.0", out

    def test_a_kept_wheelhouse_that_does_not_install_on_this_python_is_rebuilt(self, tmp_path: Path,
                                                                                venv: Path) -> None:
        """Review round 1: a wheelhouse built for another interpreter (the box's Python changed
        since) failed only AFTER the stop. Its offline dry run fails now, before any change, and
        the preflight builds it again."""
        index, constraints, root = self._index(tmp_path)
        _wheelhouse(root / SHA_NEW, "2.0", requires=("fastmcp", "missingdep"))
        source = _sdist(tmp_path / "archive", "remoteos-mcp", "2.0", _REMOTEOS, ("fastmcp",))

        out = self._preflight(tmp_path, venv, index, root, source, constraints)

        assert out["ok"] is True and out["reused"] is False and out["version"] == "2.0", out
        assert sorted(p.name for p in (root / SHA_NEW).iterdir()) == [
            "constraints.txt", "fastmcp-9.0-py3-none-any.whl", "remoteos_mcp-2.0-py3-none-any.whl"]

    def test_a_failed_download_changes_nothing(self, tmp_path: Path, venv: Path) -> None:
        index, constraints, root = self._index(tmp_path)
        old = _wheelhouse(root / SHA_OLD, "1.0")
        before = {p.name: p.read_bytes() for p in old.iterdir()}
        source = _sdist(tmp_path / "archive", "remoteos-mcp", "2.0", _REMOTEOS, ("fastmcp", "nosuchdep"))

        out = self._preflight(tmp_path, venv, index, root, source, constraints)

        assert out["ok"] is False, out
        assert "pip download" in out["error"], out
        assert "nosuchdep" in out["output"], "pip's own output is not kept for the message"
        assert [p.name for p in root.iterdir()] == [SHA_OLD], "the failed download left a wheelhouse behind"
        assert {p.name: p.read_bytes() for p in old.iterdir()} == before
        assert _installed(tmp_path, venv) is None


class TestOfflineInstallAndRollback:
    def _point(self, path: Path, version: str) -> str:
        # In parentheses: in argument mode a bare [pscustomobject]@{...} is a string
        return f"([pscustomobject]@{{ Ref = '{SHA_OLD}'; Path = {_q(path)}; Version = '{version}' }})"

    _FAKES = """
$global:calls = @()
$stop = { $global:calls += 'stop' }
$start = { $global:calls += 'start'; $null }
$health = { param($Version) $global:calls += "health $Version"; [pscustomobject]@{ Ok = $true; Message = "v$Version" } }
"""

    def test_a_failed_install_rolls_back_to_the_previous_wheelhouse_offline(self, tmp_path: Path, venv: Path) -> None:
        old = _wheelhouse(tmp_path / "wh" / SHA_OLD, "1.0")
        # The new wheelhouse is broken (a dependency is missing): the install fails AFTER the
        # pre-uninstall, the exact state that left resolume-pp without a server
        new = _wheelhouse(tmp_path / "wh" / SHA_NEW, "2.0", requires=("fastmcp", "fakedep==2.0"))

        out = _ps(tmp_path, _NO_NETWORK + self._FAKES + f"""
$first = Install-RemoteOSWheelhouse -Python {_q(venv)} -Wheelhouse {_q(old)} -Version '1.0'
$v1 = Get-RemoteOSInstalledVersion {_q(venv)}
$failed = Install-RemoteOSWheelhouse -Python {_q(venv)} -Wheelhouse {_q(new)} -Version '2.0'
$between = Get-RemoteOSInstalledVersion {_q(venv)}
$rollback = Invoke-RemoteOSRollback -Point {self._point(old, '1.0')} -Python {_q(venv)} -StopServer $stop `
    -StartServer $start -ProveHealth $health
@{{ first = $first.Ok; v1 = $v1; failed = $failed.Ok; error = $failed.Error; output = $failed.Output;
    between = $between; rollback = $rollback.Ok; rollbackError = $rollback.Error;
    after = (Get-RemoteOSInstalledVersion {_q(venv)}); calls = $global:calls }} | ConvertTo-Json -Compress
""")

        assert out["first"] is True and out["v1"] == "1.0", out
        assert out["failed"] is False and "2.0" in out["error"], out
        assert "fakedep" in out["output"], "pip's own output is not kept"
        assert out["between"] is None, "the failed install did not leave the box without the package (the test setup)"
        assert out["rollback"] is True, out
        assert out["after"] == "1.0"
        assert out["calls"] == ["stop", "start", "health 1.0"], "rollback order: stop, reinstall, start, prove"

    def test_the_rollback_reports_a_failing_old_health_check(self, tmp_path: Path, venv: Path) -> None:
        old = _wheelhouse(tmp_path / "wh" / SHA_OLD, "1.0")

        out = _ps(tmp_path, _NO_NETWORK + f"""
$bad = {{ param($Version) [pscustomobject]@{{ Ok = $false; Message = 'did not respond' }} }}
$r = Invoke-RemoteOSRollback -Point {self._point(old, '1.0')} -Python {_q(venv)} -StopServer {{ }} `
    -StartServer {{ $null }} -ProveHealth $bad
@{{ ok = $r.Ok; error = $r.Error }} | ConvertTo-Json -Compress
""")

        assert out == {"ok": False, "error": "did not respond"}

    def test_a_failure_after_the_stop_says_it_rolled_back_and_exits_1(self, tmp_path: Path, venv: Path) -> None:
        old = _wheelhouse(tmp_path / "wh" / SHA_OLD, "1.0")

        result = _run(tmp_path, _NO_NETWORK + self._FAKES + f"""
Exit-RemoteOSAfterFailure -Reason 'pip install failed' -Output 'the pip log' -Point {self._point(old, '1.0')} `
    -Python {_q(venv)} -StopServer $stop -StartServer $start -ProveHealth $health -Pause $false
""")

        assert result.returncode == 1, result.stdout + result.stderr
        assert "[X] pip install failed -- rolled back to 1.0" in result.stdout, result.stdout
        assert "the pip log" in result.stdout

    def test_with_no_previous_wheelhouse_the_failure_says_so_and_rolls_nothing_back(self, tmp_path: Path) -> None:
        result = _run(tmp_path, self._FAKES + """
Exit-RemoteOSAfterFailure -Reason 'pip install failed' -Output '' -Point $null -Python 'python' `
    -StopServer $stop -StartServer $start -ProveHealth $health -Pause $false
""")

        assert result.returncode == 1, result.stdout + result.stderr
        assert "no previous wheelhouse to roll back to" in result.stdout, result.stdout
        assert "rolled back to" not in result.stdout


class TestRollbackPoint:
    def _read(self, tmp_path: Path, config: Path, python: Path) -> dict | None:
        return _ps(tmp_path, _NO_NETWORK + f"""
$p = Get-RemoteOSRollbackPoint -ConfigDir {_q(config)} -Root {_q(config / "wheelhouse")} -Python {_q(python)}
if ($p) {{ @{{ ref = $p.Ref; path = $p.Path; version = $p.Version }} | ConvertTo-Json -Compress }}
else {{ @{{ none = $true }} | ConvertTo-Json -Compress }}
""")

    def test_the_installed_commit_s_kept_wheelhouse_is_the_rollback_point(self, tmp_path: Path,
                                                                          shared_venv: Path) -> None:
        config = tmp_path / "cfg"
        _wheelhouse(config / "wheelhouse" / SHA_OLD, "0.7.0.dev36")
        (config / "installed-ref.txt").write_text(SHA_OLD + "\n")

        assert self._read(tmp_path, config, shared_venv) == {
            "ref": SHA_OLD, "path": str(config / "wheelhouse" / SHA_OLD), "version": "0.7.0.dev36"}

    @pytest.mark.parametrize("case", ["no-ref-file", "not-a-sha", "no-wheelhouse", "no-constraints", "two-wheels",
                                      "not-installable"])
    def test_without_a_complete_installable_wheelhouse_there_is_none(self, tmp_path: Path, case: str,
                                                                     shared_venv: Path) -> None:
        """not-installable (review round 1): a wheelhouse this Python cannot install offline (built
        for another interpreter) would fail the rollback the same way, after the stop."""
        config = tmp_path / "cfg"
        folder = _wheelhouse(config / "wheelhouse" / SHA_OLD, "1.0")
        (config / "installed-ref.txt").write_text(SHA_OLD + "\n")
        if case == "no-ref-file":
            (config / "installed-ref.txt").unlink()
        elif case == "not-a-sha":
            (config / "installed-ref.txt").write_text("main\n")
        elif case == "no-wheelhouse":
            for p in folder.iterdir():
                p.unlink()
            folder.rmdir()
        elif case == "no-constraints":
            (folder / "constraints.txt").unlink()
        elif case == "two-wheels":
            _wheel(folder, "remoteos-mcp", "1.1", _REMOTEOS)
        else:
            _wheelhouse(folder, "1.0", requires=("fastmcp", "missingdep"))

        assert self._read(tmp_path, config, shared_venv) == {"none": True}


class TestPruning:
    def test_the_current_and_the_previous_wheelhouse_stay_every_other_goes(self, tmp_path: Path) -> None:
        root = tmp_path / "wheelhouse"
        names = [SHA_OLD, SHA_NEW, "3" * 40, "4" * 40, "5" * 40 + ".partial"]
        for name in names:
            _wheelhouse(root / name, "1.0")

        out = _ps(tmp_path, f"""
$removed = Remove-RemoteOSStaleWheelhouses -Root {_q(root)} -Keep @('{SHA_NEW}', '{SHA_OLD}')
@{{ removed = @($removed) }} | ConvertTo-Json -Compress
""")

        assert sorted(p.name for p in root.iterdir()) == sorted([SHA_OLD, SHA_NEW])
        assert sorted(out["removed"]) == sorted(names[2:])

    def test_with_no_previous_wheelhouse_only_the_current_one_stays(self, tmp_path: Path) -> None:
        root = tmp_path / "wheelhouse"
        for name in (SHA_OLD, SHA_NEW):
            _wheelhouse(root / name, "1.0")

        _ps(tmp_path, f"""
$null = Remove-RemoteOSStaleWheelhouses -Root {_q(root)} -Keep @('{SHA_NEW}', $null)
@{{ ok = $true }} | ConvertTo-Json -Compress
""")

        assert [p.name for p in root.iterdir()] == [SHA_NEW]


class TestPythonVersion:
    @pytest.mark.parametrize(("text", "ok"), [
        ("Python 3.12.10", True), ("Python 3.11.0", True), ("Python 3.13.1", True),
        ("Python 3.10.11", False), ("Python 3.9.13", False), ("Python 2.7.18", False), ("", False),
    ])
    def test_python_3_11_or_newer_is_required(self, tmp_path: Path, text: str, ok: bool) -> None:
        out = _ps(tmp_path, f"@{{ ok = (Test-RemoteOSPythonVersion {_q(text)}) }} | ConvertTo-Json -Compress")

        assert out["ok"] is ok


class _Health(BaseHTTPRequestHandler):
    version = "1.0"

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        body = json.dumps({"status": "ok", "version": self.version}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class TestHealthProof:
    """The health proof is shared by the install and the rollback (which proves the OLD version)."""

    def _prove(self, tmp_path: Path, port: int, version: str) -> dict:
        return _ps(tmp_path, f"""
$h = Test-RemoteOSServerHealth -Port {port} -Version '{version}' -StartDirect $true -Attempts 2 -DelaySeconds 0
@{{ ok = $h.Ok; message = $h.Message }} | ConvertTo-Json -Compress
""")

    def test_the_proof_passes_only_for_the_expected_version(self, tmp_path: Path) -> None:
        server = HTTPServer(("127.0.0.1", 0), _Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            good = self._prove(tmp_path, port, "1.0")
            bad = self._prove(tmp_path, port, "2.0")
        finally:
            server.shutdown()
            server.server_close()

        assert good["ok"] is True and "v1.0" in good["message"], good
        assert bad == {"ok": False, "message": "Installed 2.0 but service reports 1.0"}

    def test_no_answer_fails_the_proof(self, tmp_path: Path) -> None:
        server = HTTPServer(("127.0.0.1", 0), _Health)
        port = server.server_address[1]
        server.server_close()

        out = self._prove(tmp_path, port, "1.0")

        assert out["ok"] is False and "did not respond" in out["message"], out


class TestRollbackSplat:
    """Review round 1: the flow's own `$rollback` splat (scriptblocks that read the script's
    $Port, $ConfigDir and $startDirect by dynamic scope) was never run by a test. Here it is the
    real statement out of install.ps1, run with the server functions replaced by recorders."""

    def test_the_flow_s_rollback_reaches_the_server_functions_with_the_script_s_values(self, tmp_path: Path) -> None:
        result = _run(tmp_path, f"""
$isRollback = {{ param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left.Extent.Text -eq '$rollback' }}
$assignments = @($ast.FindAll($isRollback, $true))
if ($assignments.Count -ne 1) {{ throw "install.ps1 has $($assignments.Count) `$rollback assignments, not 1" }}
function Stop-RemoteOSServer([int]$Port) {{ Write-Host "STOP port=$Port" }}
function Install-RemoteOSWheelhouse([string]$Python, [string]$Wheelhouse, [string]$Version) {{
    Write-Host "INSTALL $Python $Wheelhouse $Version"
    [pscustomobject]@{{ Ok = $true; Error = $null; Output = '' }}
}}
function Start-RemoteOSServer([string]$ConfigDir, [bool]$StartDirect) {{ Write-Host "START $ConfigDir $StartDirect" }}
function Test-RemoteOSServerHealth([int]$Port, [string]$Version, [bool]$StartDirect) {{
    Write-Host "HEALTH port=$Port $Version $StartDirect"
    [pscustomobject]@{{ Ok = $true; Message = "v$Version" }}
}}
$Port = 8092
$ConfigDir = 'C:\\Users\\box\\.remoteos-mcp'
$startDirect = $true
$python = 'the-python'
$pauseOnExit = $false
$rollbackPoint = [pscustomobject]@{{ Ref = '{SHA_OLD}'; Path = 'C:\\wh\\old'; Version = '1.0' }}
. ([ScriptBlock]::Create($assignments[0].Extent.Text))
Exit-RemoteOSAfterFailure -Reason 'pip install failed' -Output '' @rollback
""")

        assert result.returncode == 1, result.stdout + result.stderr
        calls = [line for line in result.stdout.splitlines() if line.startswith(("STOP", "INSTALL", "START", "HEALTH"))]
        assert calls == [
            "STOP port=8092",
            "INSTALL the-python C:\\wh\\old 1.0",
            "START C:\\Users\\box\\.remoteos-mcp True",
            "HEALTH port=8092 1.0 True",
        ], result.stdout
        assert "[X] pip install failed -- rolled back to 1.0" in result.stdout


class TestWheelhouseAcl:
    """Review round 1: the wheelhouses feed an ELEVATED pip, and they live in the desktop user's
    profile. A desktop user who is not an administrator must not be able to plant a wheel there."""

    _STATE = """
$sid = [System.Security.Principal.SecurityIdentifier]
$acl = Get-Acl -LiteralPath $root
$user = ([System.Security.Principal.NTAccount]$env:USERNAME).Translate($sid).Value
$rules = @($acl.Access | ForEach-Object { @{ sid = $_.IdentityReference.Translate($sid).Value;
    rights = $_.FileSystemRights.ToString() } })
@{ err = $err; before = $before; trusted = (Test-RemoteOSWheelhouseRootTrusted $root);
    owner = $acl.GetOwner($sid).Value; protected = $acl.AreAccessRulesProtected; user = $user; rules = $rules } |
    ConvertTo-Json -Compress -Depth 4
"""

    def _protect(self, tmp_path: Path, root: Path) -> dict:
        return _ps(tmp_path, f"""
$root = {_q(root)}
$before = $false
if (Test-Path -LiteralPath $root) {{ $before = Test-RemoteOSWheelhouseRootTrusted $root }}
$err = Protect-RemoteOSWheelhouseRoot -Root $root -User $env:USERNAME
""" + self._STATE)

    def test_the_root_is_owned_by_administrators_and_read_only_for_the_desktop_user(self, tmp_path: Path) -> None:
        out = self._protect(tmp_path, tmp_path / "wheelhouse")

        assert out["err"] is None and out["trusted"] is True and out["protected"] is True, out
        assert out["owner"] == "S-1-5-32-544", out
        rights = {rule["sid"]: rule["rights"] for rule in out["rules"]}
        assert "FullControl" in rights["S-1-5-18"] and "FullControl" in rights["S-1-5-32-544"], rights
        assert "ReadAndExecute" in rights[out["user"]], rights
        assert not any(word in rights[out["user"]] for word in ("Write", "Modify", "FullControl", "Delete")), rights

    @pytest.mark.parametrize("change", [
        "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("
        "(New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545')), 'Write', 'Allow')))",
        "$acl.SetOwner((New-Object System.Security.Principal.NTAccount($env:USERNAME)))",
        # GENERIC_WRITE: FileSystemAccessRule refuses a mask above FullControl, so through SDDL (GW)
        "$acl.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm('Access') + '(A;OICI;GW;;;BU)', "
        "'Access')",
    ], ids=["users-write", "user-owned", "generic-write"])
    def test_a_protected_root_with_a_foreign_writer_or_owner_is_not_trusted(self, tmp_path: Path,
                                                                            change: str) -> None:
        """Review round 2: the owner check and the write-rights check were never exercised."""
        root = tmp_path / "wheelhouse"
        planted = root / SHA_NEW / "remoteos_mcp-6.6.6-py3-none-any.whl"

        out = _ps(tmp_path, f"""
$root = {_q(root)}
$null = Protect-RemoteOSWheelhouseRoot -Root $root -User $env:USERNAME
$acl = Get-Acl -LiteralPath $root
{change}
Set-Acl -LiteralPath $root -AclObject $acl
New-Item -ItemType Directory -Path {_q(planted.parent)} -Force | Out-Null
Set-Content -LiteralPath {_q(planted)} -Value 'not a wheel'
$before = Test-RemoteOSWheelhouseRootTrusted $root
$err = Protect-RemoteOSWheelhouseRoot -Root $root -User $env:USERNAME
""" + self._STATE)

        assert out["before"] is False, f"trusted after: {change}"
        assert out["err"] is None and out["trusted"] is True and out["owner"] == "S-1-5-32-544", out
        assert not planted.exists(), "a wheel planted in an untrusted root survived"

    def test_a_planted_link_is_removed_and_its_target_left_alone(self, tmp_path: Path) -> None:
        """Review round 2: Get-Acl/Set-Acl follow a junction, so the ACL rewrite and the deletes
        would hit the target."""
        root = tmp_path / "wheelhouse"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")

        out = _ps(tmp_path, _LINK + f"""
New-Item -ItemType $linkType -Path {_q(root)} -Target {_q(outside)} | Out-Null
$root = {_q(root)}
$before = $false
$err = Protect-RemoteOSWheelhouseRoot -Root $root -User $env:USERNAME
""" + self._STATE)

        assert out["err"] is None and out["trusted"] is True, out
        assert (outside / "keep.txt").read_text() == "precious"
        assert not (root / "keep.txt").exists(), "the root is still the link"
        acl = _ps(tmp_path, f"@{{ p = (Get-Acl -LiteralPath {_q(outside)}).AreAccessRulesProtected }} | "
                            "ConvertTo-Json -Compress")
        assert acl["p"] is False, "the link target's ACL was rewritten"

    def test_an_untrusted_root_holding_a_link_is_deleted_without_entering_it(self, tmp_path: Path) -> None:
        """Review round 3: the recursive branch ([System.IO.Directory]::Delete($p, $true)) must
        delete a junction inside the root without entering it."""
        root = tmp_path / "wheelhouse"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")

        out = _ps(tmp_path, _LINK + f"""
New-Item -ItemType $linkType -Path {_q(root / SHA_NEW)} -Target {_q(outside)} | Out-Null
$root = {_q(root)}
$before = Test-RemoteOSWheelhouseRootTrusted $root
$err = Protect-RemoteOSWheelhouseRoot -Root $root -User $env:USERNAME
""" + self._STATE)

        assert out["before"] is False and out["err"] is None and out["trusted"] is True, out
        assert (outside / "keep.txt").read_text() == "precious", "the recursive delete entered the link"
        assert not (root / SHA_NEW).exists()

    def test_a_config_folder_that_is_a_link_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        (outside / "wheelhouse").mkdir(parents=True)
        (outside / "wheelhouse" / "keep.txt").write_text("precious")
        config = tmp_path / "cfg"

        out = _ps(tmp_path, _LINK + f"""
New-Item -ItemType $linkType -Path {_q(config)} -Target {_q(outside)} | Out-Null
@{{ err = (Protect-RemoteOSWheelhouseRoot -Root {_q(config / "wheelhouse")} -User $env:USERNAME) }} |
    ConvertTo-Json -Compress
""")

        assert out["err"] and "link" in out["err"], out
        assert (outside / "wheelhouse" / "keep.txt").read_text() == "precious"

    def test_a_root_someone_else_made_is_replaced_with_everything_in_it(self, tmp_path: Path) -> None:
        planted = tmp_path / "wheelhouse" / SHA_NEW / "remoteos_mcp-6.6.6-py3-none-any.whl"
        planted.parent.mkdir(parents=True)
        planted.write_bytes(b"not a wheel")

        out = self._protect(tmp_path, tmp_path / "wheelhouse")

        assert out["before"] is False, "a root with the profile's inherited ACL was trusted"
        assert out["err"] is None and out["trusted"] is True, out
        assert not planted.exists(), "a wheel planted in an untrusted root survived"


# --- review round 2: links, the trusted check itself, the marked version line -----------------

_LINK = "$linkType = 'Junction'; if ($IsLinux) { $linkType = 'SymbolicLink' }\n"


class TestNoLinkIsFollowed:
    """An elevated delete must never follow a link the desktop user planted (Windows PowerShell
    5.1's Remove-Item -Recurse enters a junction and deletes the target's contents)."""

    def test_pruning_removes_a_planted_link_never_its_target(self, tmp_path: Path) -> None:
        root = tmp_path / "wheelhouse"
        _wheelhouse(root / SHA_NEW, "1.0")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")

        out = _ps(tmp_path, _LINK + f"""
New-Item -ItemType $linkType -Path {_q(root / ("3" * 40))} -Target {_q(outside)} | Out-Null
$removed = Remove-RemoteOSStaleWheelhouses -Root {_q(root)} -Keep @('{SHA_NEW}')
@{{ removed = @($removed) }} | ConvertTo-Json -Compress
""")

        assert out["removed"] == ["3" * 40]
        assert (outside / "keep.txt").read_text() == "precious", "the prune followed the link"
        assert sorted(p.name for p in root.iterdir()) == [SHA_NEW]

    def test_pruning_a_root_swapped_for_a_link_deletes_nothing(self, tmp_path: Path) -> None:
        """Review round 3: the root was checked once, minutes before the prune. Swapped for a
        junction to a system folder meanwhile, its real subfolders would be deleted as admin."""
        system = tmp_path / "system"
        (system / "Vendor").mkdir(parents=True)
        (system / "Vendor" / "keep.txt").write_text("precious")
        (system / ("4" * 40)).mkdir()
        root = tmp_path / "cfg" / "wheelhouse"
        root.parent.mkdir()

        out = _ps(tmp_path, _LINK + f"""
New-Item -ItemType $linkType -Path {_q(root)} -Target {_q(system)} | Out-Null
$removed = Remove-RemoteOSStaleWheelhouses -Root {_q(root)} -Keep @('{SHA_NEW}')
@{{ removed = @($removed) }} | ConvertTo-Json -Compress
""")

        assert out["removed"] == []
        assert (system / "Vendor" / "keep.txt").read_text() == "precious"
        assert (system / ("4" * 40)).is_dir()

    def test_pruning_deletes_only_wheelhouse_names(self, tmp_path: Path) -> None:
        root = tmp_path / "wheelhouse"
        for name in (SHA_NEW, "5" * 40 + ".partial", "Vendor", "ABCDEF" + "0" * 34):
            (root / name).mkdir(parents=True)

        out = _ps(tmp_path, f"""
$removed = Remove-RemoteOSStaleWheelhouses -Root {_q(root)} -Keep @('{SHA_NEW}')
@{{ removed = @($removed) }} | ConvertTo-Json -Compress
""")

        assert out["removed"] == ["5" * 40 + ".partial"]
        assert sorted(p.name for p in root.iterdir()) == sorted([SHA_NEW, "Vendor", "ABCDEF" + "0" * 34])


class TestInstalledVersion:
    def test_a_late_warning_on_stderr_is_not_the_version(self, tmp_path: Path, venv: Path) -> None:
        """Review round 2: the marked line was only text-matched. A .pth atexit hook writes a
        warning to stderr AFTER the version line, as a late DeprecationWarning would."""
        wheelhouse = _wheelhouse(tmp_path / "wh", "1.0")
        subprocess.run([str(venv), "-m", "pip", "install", "-q", "--no-index", "--find-links", str(wheelhouse),
                        "remoteos-mcp==1.0"], check=True, timeout=300)
        site = subprocess.run([str(venv), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, check=True).stdout.strip()
        Path(site, "late_warning.pth").write_text(
            "import atexit, sys; atexit.register(lambda: sys.stderr.write('DeprecationWarning: late\\n'))\n")

        assert _installed(tmp_path, venv) == "1.0"
