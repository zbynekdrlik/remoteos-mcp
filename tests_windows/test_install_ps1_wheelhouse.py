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
    def _read(self, tmp_path: Path, config: Path) -> dict | None:
        return _ps(tmp_path, f"""
$p = Get-RemoteOSRollbackPoint -ConfigDir {_q(config)}
if ($p) {{ @{{ ref = $p.Ref; path = $p.Path; version = $p.Version }} | ConvertTo-Json -Compress }}
else {{ @{{ none = $true }} | ConvertTo-Json -Compress }}
""")

    def test_the_installed_commit_s_kept_wheelhouse_is_the_rollback_point(self, tmp_path: Path) -> None:
        config = tmp_path / "cfg"
        _wheelhouse(config / "wheelhouse" / SHA_OLD, "0.7.0.dev36")
        (config / "installed-ref.txt").write_text(SHA_OLD + "\n")

        assert self._read(tmp_path, config) == {
            "ref": SHA_OLD, "path": str(config / "wheelhouse" / SHA_OLD), "version": "0.7.0.dev36"}

    @pytest.mark.parametrize("case", ["no-ref-file", "not-a-sha", "no-wheelhouse", "no-constraints", "two-wheels"])
    def test_without_a_complete_wheelhouse_there_is_none(self, tmp_path: Path, case: str) -> None:
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
        else:
            _wheel(folder, "remoteos-mcp", "1.1", _REMOTEOS)

        assert self._read(tmp_path, config) == {"none": True}


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
