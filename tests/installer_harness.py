"""Shared harness for the installer tests (install-linux.sh, install.sh, install.ps1).

The installers are copied to tmp_path with root-only paths redirected, then run
under bash with a FAKE PATH of shim scripts — no root, no network, no system
changes.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fake_pip import dist_field, find_dists, write_dist

ROOT = Path(__file__).resolve().parent.parent
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


def _prepare_linux_installer(tmp_path: Path) -> Path:
    """Copy install-linux.sh to tmp with test patches (no-root, tmp dirs)."""
    content = INSTALLER_LINUX.read_text()

    etc_dir = tmp_path / "etc-remoteos-mcp"
    etc_dir.mkdir(parents=True, exist_ok=True)

    # Disable EUID root check — tests run as unprivileged user
    content = content.replace(
        'if [[ "$EUID" -ne 0 ]]; then',
        "if false; then",
    )
    content = content.replace(
        'ACTUAL_USER="${SUDO_USER:-root}"',
        'ACTUAL_USER="testuser"',
    )
    # Redirect writable paths to tmp
    content = content.replace(
        'CONFIG_DIR="/etc/remoteos-mcp"',
        f'CONFIG_DIR="{etc_dir}"',
    )
    content = content.replace(
        'SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"',
        f'SERVICE_FILE="{tmp_path}/remoteos-mcp.service"',
    )

    script = tmp_path / "install-linux.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


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
    # The macOS auth-key generation uses `tr -dc ... | head -c 32` which
    # causes SIGPIPE+pipefail on Linux (works on macOS).  Replace with the
    # Python approach the Linux installer already uses.
    content = content.replace(
        "AUTH_KEY=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)",
        'AUTH_KEY=$("$PYTHON" -c "import secrets,string; '
        "print(''.join(secrets.choice(string.ascii_letters+string.digits) "
        'for _ in range(32)))")',
    )
    # mkdir for LaunchAgents — ensure directory exists under tmp HOME
    content = content.replace(
        'mkdir -p "$HOME/Library/LaunchAgents"',
        f'mkdir -p "{launch_dir}"',
    )

    script = tmp_path / "install-macos.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


def _create_linux_shims(
    shim_dir: Path,
    invocations: Path,
    *,
    installed_version: str = "0.7.0.dev13",
) -> None:
    """Create the full set of shims for install-linux.sh testing."""
    real_py = _real_python()

    # python3 — intercept pip calls, delegate -c to real python
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
        elif [[ "$2" == "ensurepip" ]]; then
            exit 0
        else
            exec "{real_py}" "$@"
        fi
        ;;
    -c)
        exec "{real_py}" "$@"
        ;;
    *)
        exec "{real_py}" "$@"
        ;;
esac
""",
    )

    # git
    _make_shim(shim_dir / "git", 'echo "git version 2.43.0"')

    # apt-get — logs all invocations
    _make_shim(shim_dir / "apt-get", f'echo "apt-get $*" >> "{invocations}"\nexit 0')

    # systemctl — logs all invocations
    _make_shim(
        shim_dir / "systemctl",
        f'echo "systemctl $*" >> "{invocations}"\nexit 0',
    )

    # loginctl — no graphical sessions (headless)
    _make_shim(
        shim_dir / "loginctl",
        """\
if [[ "$1" == "list-sessions" ]]; then
    echo ""
fi
exit 0
""",
    )

    # hostname
    _make_shim(
        shim_dir / "hostname",
        """\
if [[ "$1" == "-I" ]]; then
    echo "192.168.1.100"
elif [[ "$1" == "-s" ]]; then
    echo "testhost"
else
    echo "testhost"
fi
""",
    )

    # ufw / firewall-cmd — not available
    _make_shim(shim_dir / "ufw", "exit 1")
    _make_shim(shim_dir / "firewall-cmd", "exit 1")


def _create_macos_shims(
    shim_dir: Path,
    invocations: Path,
    *,
    installed_version: str = "0.7.0.dev13",
) -> None:
    """Create the full set of shims for install.sh (macOS) testing."""
    real_py = _real_python()

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
        exec "{real_py}" "$@"
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


def _run_installer(
    script: Path,
    shim_dir: Path,
    tmp_path: Path,
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run an installer script with controlled PATH."""
    env = {
        "PATH": f"{shim_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "TERM": "dumb",
        "LANG": "C",
    }

    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


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
MAIN_CONSTRAINTS = "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/constraints.txt"
GIT_URL = "git+https://github.com/zbynekdrlik/remoteos-mcp.git@main"


@dataclass
class FakePipBox:
    """A fake box: site-packages + console-script dir + the fake pip's call log."""

    site: Path
    bin_dir: Path
    log: Path

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
        """Versions of every dist-info installed for `name` (stale ones too)."""
        return [dist_field(d, "Version") for d in find_dists(self.site, name)]

    def files(self) -> dict[str, tuple[bytes, int]]:
        """Every file under site-packages + bin, with its bytes and mode bits."""
        result = {}
        for root in (self.site, self.bin_dir):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    result[str(path)] = (path.read_bytes(), path.stat().st_mode & 0o777)
        return result


def _verb(argv: list[str]) -> str:
    if argv[:1] == ["install"]:
        return "dry-run" if "--dry-run" in argv else "install"
    return argv[0] if argv else ""


def install_old_remoteos(box: FakePipBox) -> None:
    """The box before the upgrade: remoteos-mcp OLD_VERSION (with its console
    script outside site-packages, as RECORD lists it) + fastmcp-slim
    OLD_FASTMCP_SLIM."""
    write_dist(
        box.site,
        "remoteos-mcp",
        OLD_VERSION,
        {
            "remoteos/__init__.py": f"# remoteos {OLD_VERSION}\n",
            "remoteos/server.py": "def main():\n    return 'old server'\n",
            "../../../bin/remoteos-mcp": "#!/usr/bin/python3\nfrom remoteos.__main__ import cli\ncli()\n",
        },
        executable=("../../../bin/remoteos-mcp",),
    )
    write_dist(
        box.site, "fastmcp-slim", OLD_FASTMCP_SLIM, {"fastmcp/__init__.py": f"# fastmcp-slim {OLD_FASTMCP_SLIM}\n"}
    )


def setup_fake_pip(tmp_path: Path, shim_dir: Path, **config: object) -> FakePipBox:
    """Route the python3 shim's `-m pip` to fake_pip.py with `config` (keys:
    see fake_pip.py).  Call AFTER _create_*_shims: it replaces their
    stateless python3 shim."""
    prefix = tmp_path / "prefix"
    box = FakePipBox(
        site=prefix / "lib" / "python3.14" / "dist-packages",
        bin_dir=prefix / "bin",
        log=tmp_path / "pip-calls.jsonl",
    )
    box.site.mkdir(parents=True)
    box.bin_dir.mkdir(parents=True)
    cfg_path = tmp_path / "fake-pip.json"
    cfg_path.write_text(json.dumps({"site": str(box.site), "log": str(box.log), "new_version": NEW_VERSION, **config}))
    real_py = _real_python()
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
