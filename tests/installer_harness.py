"""Shared harness for the installer tests (install.sh, install.ps1, and the pieces
tests/linux_installer_harness.py builds on for install-linux.sh).

The installers are copied to tmp_path with root-only paths redirected, then run
under bash with a FAKE PATH of shim scripts — no root, no network, no system
changes.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fake_pip import dist_field, dist_files, find_dists, write_dist

ROOT = Path(__file__).resolve().parent.parent
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
# git shim: `ls-remote` answers like GitHub does for refs/heads/main; anything
# else prints a version line.
_FAKE_GIT = f"""\
if [[ "$1" == "ls-remote" ]]; then
    printf '%s\\trefs/heads/main\\n' "{FAKE_SHA}"
    exit 0
fi
echo "git version 2.43.0"
"""
INSTALLER_LINUX = ROOT / "install-linux.sh"
INSTALLER_MACOS = ROOT / "install.sh"
INSTALLER_WINDOWS = ROOT / "install.ps1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shim(path: Path, content: str) -> None:
    """Write an executable bash shim."""
    path.write_text(f"#!/bin/bash\n{content}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _real_python() -> str:
    """Absolute path to the real Python interpreter running pytest."""
    return sys.executable


def _prepare_macos_installer(tmp_path: Path) -> Path:
    """Copy install.sh (macOS) to tmp with test patches (tmp dirs)."""
    content = INSTALLER_MACOS.read_text()

    config_dir = tmp_path / "dot-remoteos-mcp"
    config_dir.mkdir(parents=True, exist_ok=True)
    launch_dir = tmp_path / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)

    # Redirect writable paths
    content = content.replace(
        'CONFIG_DIR="$HOME/.remoteos-mcp"',
        f'CONFIG_DIR="{config_dir}"',
    )
    content = content.replace(
        'PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"',
        f'PLIST_PATH="{launch_dir}/com.remoteos-mcp.plist"',
    )
    # The auth-key line is NOT patched: it used to be swapped for a python
    # one-liner here, which hid that `tr … | head -c 32` dies of SIGPIPE under
    # pipefail on macOS just as on Linux (a fresh mac aborted at [3/5], #12).
    # mkdir for LaunchAgents — ensure directory exists under tmp HOME
    content = content.replace(
        'mkdir -p "$HOME/Library/LaunchAgents"',
        f'mkdir -p "{launch_dir}"',
    )

    script = tmp_path / "install-macos.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


def _stateless_site(shim_dir: Path, installed_version: str) -> Path:
    """The stateless box's site dir: just the installed remoteos-mcp dist-info,
    so the installer's rollback helper (importlib.metadata) finds the version
    its `pip show` reports — never the test interpreter's own packages."""
    site = shim_dir.parent / "stateless-site"
    write_dist(site, "remoteos-mcp", installed_version, {})
    return site


def _create_macos_shims(
    shim_dir: Path,
    invocations: Path,
    *,
    installed_version: str = "0.7.0.dev13",
) -> None:
    """Create the full set of shims for install.sh (macOS) testing."""
    real_py = _real_python()
    stateless_site = _stateless_site(shim_dir, installed_version)

    # python3 — same pattern as Linux
    _make_shim(
        shim_dir / "python3",
        f"""\
case "$1" in
    --version)
        echo "Python 3.12.0"
        exit 0
        ;;
    -m)
        if [[ "$2" == "pip" ]]; then
            shift 2
            echo "python3 -m pip $*" >> "{invocations}"
            case "$1" in
                --version)
                    echo "pip 24.0 from /test (python 3.12)"
                    exit 0
                    ;;
                install)
                    # --dry-run --report <file>: an empty resolution report
                    prev=""
                    for arg in "$@"; do
                        if [[ "$prev" == "--report" ]]; then echo '{{"version": "1", "install": []}}' > "$arg"; fi
                        prev="$arg"
                    done
                    echo "Successfully installed remoteos-mcp-{installed_version}"
                    exit 0
                    ;;
                show)
                    echo "Name: remoteos-mcp"
                    echo "Version: {installed_version}"
                    exit 0
                    ;;
                *)
                    exit 0
                    ;;
            esac
        else
            exec "{real_py}" "$@"
        fi
        ;;
    -c)
        # The installer's post-install import check: this stateless box's
        # package always imports (never the test interpreter's own packages).
        if [[ "$2" == *"import remoteos"* ]]; then
            exit 0
        fi
        exec "{real_py}" "$@"
        ;;
    */rollback.py)
        # The installer's rollback helper sees only this box's site dir.
        PYTHONPATH="{stateless_site}" exec "{real_py}" -S -B "$@"
        ;;
    *)
        exec "{real_py}" "$@"
        ;;
esac
""",
    )

    # launchctl — no-op
    _make_shim(
        shim_dir / "launchctl",
        f'echo "launchctl $*" >> "{invocations}"\nexit 0',
    )

    # ipconfig (macOS) — return test IP
    _make_shim(shim_dir / "ipconfig", 'echo "192.168.1.100"')

    # hostname
    _make_shim(
        shim_dir / "hostname",
        """\
if [[ "$1" == "-s" ]]; then
    echo "testmac"
elif [[ "$1" == "-I" ]]; then
    echo "192.168.1.100"
else
    echo "testmac"
fi
""",
    )

    # brew — not available
    _make_shim(shim_dir / "brew", "exit 1")

    # git — never the real one (it would ls-remote GitHub)
    _make_shim(shim_dir / "git", _FAKE_GIT)


def _run_installer(
    script: Path,
    shim_dir: Path,
    tmp_path: Path,
    *,
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run an installer script with controlled PATH (see `_installer_env`), in
    its own session: a fake pip may signal the installer's whole process group
    (as Ctrl-C / a tty hang-up / a killpg timeout do) without hitting pytest."""
    return subprocess.run(
        _installer_argv(script),
        capture_output=True,
        text=True,
        env={**_installer_env(shim_dir, tmp_path), **(extra_env or {})},
        timeout=timeout,
        start_new_session=True,
    )


def start_installer(script: Path, shim_dir: Path, tmp_path: Path) -> subprocess.Popen:
    """Start an installer with stdout+stderr on ONE pipe the test owns — close
    it to simulate the console going away (ssh drop with no tty, a killed ssh)."""
    return subprocess.Popen(
        _installer_argv(script),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_installer_env(shim_dir, tmp_path),
        start_new_session=True,
    )


def _installer_argv(script: Path) -> list[str]:
    # $INSTALLER_TEST_BASH picks the bash binary (e.g. bash 3.2, the macOS /bin/bash).
    return [os.environ.get("INSTALLER_TEST_BASH", "bash"), str(script)]


def _installer_env(shim_dir: Path, tmp_path: Path) -> dict[str, str]:
    """Controlled PATH (shims first); the `mktemp -d` work dir lands in
    `tmp_path/tmp` (see `FakePipBox.work_dirs`)."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    return {
        "PATH": f"{shim_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "TERM": "dumb",
        "LANG": "C",
        "TMPDIR": str(tmpdir),
    }


def _find_first_code_line(content: str, *keywords: str, exclude: str | None = None) -> int | None:
    """Return the 0-based line index of the first non-comment code line that
    contains ALL of the given keywords (and does NOT contain exclude), or None."""
    for i, line in enumerate(content.split("\n")):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("REM") or "Write-Host" in stripped:
            continue
        if all(kw in stripped for kw in keywords):
            if exclude and exclude in stripped:
                continue
            return i
    return None


# ---------------------------------------------------------------------------
# Stateful fake pip (tests/fake_pip.py): a Debian-pip stand-in with a real
# fake site-packages, for the preflight / rollback tests
# ---------------------------------------------------------------------------



FAKE_PIP = Path(__file__).resolve().parent / "fake_pip.py"
OLD_VERSION = "0.7.0.dev20"
OLD_FASTMCP_SLIM = "4.0.2"
NEW_VERSION = "0.7.0.dev24"
# The installers pin one run to ONE commit of main (git ls-remote): the code
# and the constraints file both come from that commit.
PINNED_CONSTRAINTS = f"https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/{FAKE_SHA}/constraints.txt"
GIT_URL = f"git+https://github.com/zbynekdrlik/remoteos-mcp.git@{FAKE_SHA}"


@dataclass
class FakePipBox:
    """A fake Debian-style box: pip's site dir (/usr/local/...), the apt site dir
    after it on sys.path, the console-script dir, the fake pip's call log, and
    the dir the installer's `mktemp -d` work dir lands in."""

    site: Path
    apt_site: Path
    bin_dir: Path
    log: Path
    tmp: Path

    @property
    def sites(self) -> list[Path]:
        return [self.site, self.apt_site]

    def entries(self) -> list[dict]:
        """Every pip call: {"argv": [...], "PIP_CONSTRAINT": ..., "TMPDIR": ...}."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def calls(self, verb: str) -> list[dict]:
        """The calls of one verb: show / dry-run / install / uninstall."""
        return [e for e in self.entries() if _verb(e["argv"]) == verb]

    def verbs(self) -> list[str]:
        """The state-relevant pip call sequence as verbs (`show` / `--version`
        queries left out)."""
        return [v for v in (_verb(e["argv"]) for e in self.entries()) if v not in ("show", "--version")]

    def versions(self, name: str) -> list[str]:
        """Versions of every dist-info installed for `name`, both site dirs, in
        sys.path order (stale ones too)."""
        return [dist_field(d, "Version") for d in find_dists(self.sites, name)]

    def files(self) -> dict[str, tuple[bytes, int]]:
        """Every file under both site dirs + bin, with its bytes and mode bits."""
        result = {}
        for root in (self.site, self.apt_site, self.bin_dir):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    result[str(path)] = (path.read_bytes(), path.stat().st_mode & 0o777)
        return result

    def work_dirs(self) -> list[Path]:
        """The installer's `mktemp -d` work dirs still present after the run."""
        return sorted(self.tmp.iterdir()) if self.tmp.exists() else []


def _verb(argv: list[str]) -> str:
    if argv[:1] == ["install"]:
        return "dry-run" if "--dry-run" in argv else "install"
    return argv[0] if argv else ""


def install_old_remoteos(box: FakePipBox) -> None:
    """The box before the upgrade: remoteos-mcp OLD_VERSION (with its console
    script outside site-packages, as RECORD lists it) + fastmcp-slim
    OLD_FASTMCP_SLIM — both pip-owned and importable."""
    write_dist(
        box.site,
        "remoteos-mcp",
        OLD_VERSION,
        {
            **dist_files("remoteos-mcp", OLD_VERSION),
            "remoteos/server.py": "def main():\n    return 'old server'\n",
            "../../../bin/remoteos-mcp": "#!/usr/bin/python3\nfrom remoteos.__main__ import cli\ncli()\n",
        },
        executable=("../../../bin/remoteos-mcp",),
    )
    write_dist(box.site, "fastmcp-slim", OLD_FASTMCP_SLIM, dist_files("fastmcp-slim", OLD_FASTMCP_SLIM))


def install_dep(box: FakePipBox, name: str, version: str, *, installer: str = "pip") -> None:
    """A dependency already on the box.  `installer="debian"` = apt-owned: in
    the apt site dir, no RECORD (as Debian ships it); anything else lands in
    pip's site dir with a RECORD (pip, uv, ...)."""
    if installer == "debian":
        write_dist(box.apt_site, name, version, dist_files(name, version), installer="debian", record=False)
    else:
        write_dist(box.site, name, version, dist_files(name, version), installer=installer)


def setup_fake_pip(tmp_path: Path, shim_dir: Path, **config: object) -> FakePipBox:
    """Route the python3 shim at the fake box (call AFTER _create_*_shims: it
    replaces their stateless python3 shim):

    - `python3 -m pip ...`          → fake_pip.py with `config` (keys: see there)
    - `python3 -c "import remoteos..."` (the installer's import check) and
      `python3 .../rollback.py ...` (its rollback helper) → the real interpreter
      with ONLY the box's two site dirs on sys.path (`-S`: no real site-packages;
      `-B`: no __pycache__ in the fake site, so file comparisons stay exact).
    """
    prefix = tmp_path / "prefix"
    box = FakePipBox(
        site=prefix / "lib" / "python3.14" / "dist-packages",
        apt_site=tmp_path / "usr-lib-python3" / "dist-packages",
        bin_dir=prefix / "bin",
        log=tmp_path / "pip-calls.jsonl",
        tmp=tmp_path / "tmp",
    )
    for path in (box.site, box.apt_site, box.bin_dir):
        path.mkdir(parents=True)
    cfg_path = tmp_path / "fake-pip.json"
    cfg_path.write_text(json.dumps({
        "site": str(box.site),
        "apt_site": str(box.apt_site),
        "log": str(box.log),
        "new_version": NEW_VERSION,
        **config,
    }))
    real_py = _real_python()
    box_path = f"{box.site}:{box.apt_site}"
    _make_shim(
        shim_dir / "python3",
        f"""\
case "$1" in
    --version)
        echo "Python 3.14.4"
        exit 0
        ;;
    -m)
        if [[ "$2" == "pip" ]]; then
            shift 2
            FAKE_PIP_CONFIG="{cfg_path}" exec "{real_py}" "{FAKE_PIP}" "$@"
        elif [[ "$2" == "ensurepip" ]]; then
            exit 0
        fi
        exec "{real_py}" "$@"
        ;;
    -c)
        if [[ "$2" == *"import remoteos"* ]]; then
            PYTHONPATH="{box_path}" exec "{real_py}" -S -B -c "$2"
        fi
        exec "{real_py}" "$@"
        ;;
    */rollback.py)
        PYTHONPATH="{box_path}" exec "{real_py}" -S -B "$@"
        ;;
    *)
        exec "{real_py}" "$@"
        ;;
esac
""",
    )
    return box


def health_shim(shim_dir: Path, version: str) -> None:
    """curl answering /health with `version` (the post-install self-check)."""
    _make_shim(
        shim_dir / "curl",
        f"""\
if [[ "$*" == *"health"* ]]; then
    echo '{{"status":"ok","version":"{version}"}}'
fi
exit 0
""",
    )


_LOGGED_COMMANDS = (
    "awk basename cat chmod chown cmp cp cut date dirname env find grep head install ln ls "
    "mkdir mktemp mv plutil readlink realpath rm sed seq sleep sort stat tail tar tee touch tr uname wc xargs"
).split()


def log_every_command(shim_dir: Path, records: Path) -> None:
    """Record the argv of every process the installer starts by name: each shim in `shim_dir`
    is wrapped, and each command of `_LOGGED_COMMANDS` gets a shim in front of the real one.
    Read the records with `fake_linux_tools.records(records)` (`[tool, arg, ...]` each); the
    environment each one was started with is in `<records>.env` (`export -p` text)."""
    real_dir = shim_dir / ".real"
    real_dir.mkdir()
    log = (f'printf "%s\\x1f" "${{0##*/}}" "$@" >> "{records}"; printf "\\x1e" >> "{records}"; '
           f'export -p >> "{records}.env"\n')
    for shim in sorted(p for p in shim_dir.iterdir() if p.is_file()):
        target = real_dir / shim.name
        shim.rename(target)
        _make_shim(shim, f'{log}exec "{target}" "$@"\n')
    for name in _LOGGED_COMMANDS:
        real = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
        if real and not (shim_dir / name).exists():
            _make_shim(shim_dir / name, f'{log}exec "{real}" "$@"\n')


def rollback_helper(path: Path, installer: Path = INSTALLER_MACOS) -> Path:
    """Write the installer's embedded rollback helper (its `cat >"$ROLLBACK_PY"`
    heredoc) to `path`, to run it directly against a real environment."""
    text = installer.read_text()
    start = text.index("<<'PY'\n", text.index('cat >"$ROLLBACK_PY"')) + len("<<'PY'\n")
    path.write_text(text[start : text.index("\nPY\n", start) + 1])
    return path
