"""Harness for the Linux installer tests (install-linux.sh, #12: venv layout, key never in a
unit or an argv).

`linux_box(tmp_path)` builds a fake box under tmp_path, then runs a patched copy of the real
installer under bash. Only the OS/tool boundary is faked (see tests/fake_linux_tools.py):

- the root check is turned off;
- /etc/remoteos-mcp, the unit file, /opt/remoteos-mcp-venv and /opt/remoteos-mcp-venvs live
  under tmp_path;
- PATH starts with shims: python3 (the SYSTEM python, whose pip is tests/fake_pip.py on a fake
  /usr/local site: the old system-python layout), systemctl, curl, id, ss, apt-get, loginctl,
  findmnt, hostname, git, ufw, firewall-cmd;
- every other command the installer may start (mv, cat, tar, env, ...) gets a logging shim
  in front of the real one, so `box.argvs()` holds the argv of every process it started by
  name. That is what the "the key is never in an argv" checks read.

The same box also runs the pre-#12 installer (git ls-remote, the system pip, a unit with
`--auth-key`), which is what makes the RED tests fail for the right reason.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import fake_linux_tools
from fake_pip import write_dist
from installer_harness import FAKE_SHA, INSTALLER_LINUX, FakePipBox, setup_fake_pip

TESTS_DIR = Path(__file__).resolve().parent
FAKE_TOOLS = TESTS_DIR / "fake_linux_tools.py"
UNINSTALLER_LINUX = TESTS_DIR.parent / "uninstall-linux.sh"

# The source the installer downloads by default (REMOTEOS_MCP_REF unset = main).
MAIN_SHA7 = "abc1234"
MAIN_SOURCE_ID = f"zbynekdrlik-remoteos-mcp-{MAIN_SHA7}"
SOURCE_VERSION = "0.7.0.dev32"
# What camera-box's lib pinned strih-pp/stream-pp to (8b4ce58, 0.7.0.dev20).
CB_SHA7 = "8b4ce58"
CB_SOURCE_ID = f"zbynekdrlik-remoteos-mcp-{CB_SHA7}"
CB_VERSION = "0.7.0.dev20"
LEGACY_VERSION = "0.7.0.dev31"

SERVICE_USER = "testuser"
SERVICE_UID = 1000

# Commands the installer may start besides the dedicated shims: each gets a logging shim.
_LOGGED = (
    "awk basename cat chmod chown cmp cp cut date dirname env find grep head install ln ls "
    "mkdir mktemp mv readlink realpath rm sed seq sleep sort stat tail tar tee touch tr uname wc xargs"
).split()

# Each shim records its argv, and the environment it was started with (`export -p`, a builtin,
# into <records>.env): the "never in a child's environment" checks read that.
# One write per record: the shims of a pipeline (`ss … | grep … | sort`) run at the same time,
# and two appends per record let their fields interleave.
_LOG_ARGV = (
    'printf -v _argv_record "%s\\x1f" "${{0##*/}}" "$@"; printf "%s\\x1e" "$_argv_record" >> "{records}"; '
    'export -p >> "{records}.env"\n'
)


def _shim(path: Path, body: str, records: Path) -> None:
    path.write_text("#!/bin/bash\n" + _LOG_ARGV.format(records=records) + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def camera_box_unit(venv: Path, env_file: Path, *, user: str = SERVICE_USER, uid: int = SERVICE_UID,
                    mode: str = "desktop") -> str:
    """remoteos-mcp.service as camera-box's scripts/lib/remoteos-mcp.sh writes it
    (remoteos_mcp_unit_text, read 20.10.2026)."""
    lines = ["[Unit]", "Description=RemoteOS MCP control agent (camera-box scripts/lib/remoteos-mcp.sh, venv)"]
    lines.append("After=network.target graphical-session.target" if mode == "desktop" else "After=network.target")
    lines += ["Wants=network.target", "[Service]", "Type=simple", f"User={user}"]
    if mode == "desktop":
        lines += ["Environment=DISPLAY=:0", f"Environment=XDG_RUNTIME_DIR=/run/user/{uid}"]
    lines += [
        f"EnvironmentFile={env_file}",
        f"ExecStart={venv}/bin/python -m remoteos --transport streamable-http --enable-all --host 0.0.0.0 --port 8092",
        "Restart=always", "RestartSec=5", "StandardOutput=journal", "StandardError=journal",
        "[Install]", "WantedBy=multi-user.target",
    ]
    return "\n".join(lines) + "\n"


def config_json_text(key: str) -> str:
    """config.json exactly as camera-box's lib (and the new installer) writes it."""
    return f'{{\n  "port": 8092,\n  "auth_key": "{key}",\n  "host": "0.0.0.0"\n}}\n'


@dataclass
class LinuxBox:
    root: Path
    script: Path
    shims: Path
    config_dir: Path
    unit_path: Path
    venv_link: Path
    venv_store: Path
    cfg_path: Path
    pip: FakePipBox
    system_extra: Path
    env: dict[str, str] = field(default_factory=dict)
    pristine: str = ""

    # --- paths -------------------------------------------------------------
    @property
    def env_file(self) -> Path:
        return self.config_dir / "remoteos-mcp.env"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    # --- the fake's config -------------------------------------------------
    def cfg(self) -> dict:
        return json.loads(self.cfg_path.read_text())

    def configure(self, **values: object) -> None:
        cfg = self.cfg()
        cfg.update(values)
        self.cfg_path.write_text(json.dumps(cfg, indent=1))

    def shim(self, name: str, body: str) -> None:
        """Replace the shim `name` (its argv is still logged)."""
        _shim(self.shims / name, body, Path(self.cfg()["records"]))

    @property
    def updater_service(self) -> Path:
        return self.root / "remoteos-mcp-updater.service"

    @property
    def updater_timer(self) -> Path:
        return self.root / "remoteos-mcp-updater.timer"

    @property
    def sudoers_file(self) -> Path:
        return self.root / "sudoers.d" / "90-remoteos-mcp"

    @property
    def lock_file(self) -> Path:
        return self.root / "install.lock"

    def child_environments(self) -> str:
        """Every environment a shimmed child was started with (`export -p` text)."""
        log = Path(str(self.cfg()["records"]) + ".env")
        return log.read_text() if log.exists() else ""

    def uninject(self) -> None:
        """Undo every inject_after: the installer copy as linux_box made it."""
        self.script.write_text(self.pristine)

    def inject_after(self, line: str, code: str) -> None:
        """Insert `code` after every line of the installer copy that is exactly `line` (indent
        aside), e.g. a `kill -HUP $$` between two steps."""
        lines = self.script.read_text().split("\n")
        hits = [i for i, text in enumerate(lines) if text.strip() == line]
        assert hits, f"the installer has no line {line!r}"
        for i in reversed(hits):
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines.insert(i + 1, indent + code)
        self.script.write_text("\n".join(lines))

    def add_source(self, ref: str, sha7: str, version: str) -> str:
        """A GitHub API tarball for `ref` (top dir zbynekdrlik-remoteos-mcp-<sha7>) holding a
        pyproject.toml of `version` and a constraints.txt. Returns its source id."""
        source_id = f"zbynekdrlik-remoteos-mcp-{sha7}"
        path = self.root / "tarballs" / f"{source_id}.tar.gz"
        path.parent.mkdir(exist_ok=True)
        files = {
            "pyproject.toml": f'[project]\nname = "remoteos-mcp"\nversion = "{version}"\n',
            "constraints.txt": "fastmcp-slim==4.0.3\n",
            "src/remoteos/__init__.py": "",
        }
        with tarfile.open(path, "w:gz") as tar:
            for rel, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(f"{source_id}/{rel}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        cfg = self.cfg()
        cfg["tarballs"][ref] = str(path)
        self.cfg_path.write_text(json.dumps(cfg, indent=1))
        return source_id

    # --- what is on the box ------------------------------------------------
    def make_venv(self, path: Path, version: str, *, marker: tuple[str, str] | None = None) -> Path:
        """A REAL venv at `path` with remoteos-mcp `version` installed (through the fake's own
        venv role, so it is exactly what the installer would build)."""
        subprocess.run([sys.executable, str(FAKE_TOOLS), str(self.cfg_path), "venv", str(path)], check=True)
        site = Path(fake_linux_tools._venv_purelib(path))
        write_dist(site, "remoteos-mcp", version, {
            "remoteos/__init__.py": "", "remoteos/__main__.py": "def cli():\n    return None\n",
        })
        write_dist(site, "fastmcp-slim", "4.0.3", {"fastmcp/__init__.py": "class FastMCP:\n    pass\n"})
        if marker:
            (path / marker[0]).write_text(marker[1] + "\n")
        return path

    def install_legacy(self, key: str, *, user: str = SERVICE_USER) -> None:
        """The pre-#12 layout our install-linux.sh left: remoteos-mcp in the SYSTEM python, the
        key in config.json (0600) AND on the unit's ExecStart."""
        write_dist(self.pip.site, "remoteos-mcp", LEGACY_VERSION, {
            "remoteos/__init__.py": "", "remoteos/__main__.py": "def cli():\n    return None\n",
        })
        write_dist(self.pip.site, "fastmcp-slim", "4.0.3", {"fastmcp/__init__.py": "class FastMCP:\n    pass\n"})
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps({"port": 8092, "auth_key": key, "host": "0.0.0.0"}, indent=2))
        self.config_file.chmod(0o600)
        self.unit_path.write_text(
            "[Unit]\nDescription=RemoteOS MCP Server\nAfter=network.target\n\n[Service]\nType=simple\n"
            f"ExecStart={self.shims}/python3 -m remoteos --transport streamable-http --enable-all --host 0.0.0.0"
            f" --port 8092 --auth-key {key}\nRestart=always\nRestartSec=5\nUser={user}\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        self.unit_path.chmod(0o644)  # written by root under umask 022
        self.boot()

    def install_camera_box(self, key: str, *, mode: str = "desktop", source_id: str = CB_SOURCE_ID,
                           version: str = CB_VERSION, user: str = SERVICE_USER) -> None:
        """camera-box's layout (scripts/lib/remoteos-mcp.sh): a real venv DIRECTORY at
        /opt/remoteos-mcp-venv with its .camera-box-source marker, the key in 0600 config.json
        and EnvironmentFile, and its unit (a cam's runs as root)."""
        self.make_venv(self.venv_link, version, marker=(".camera-box-source", source_id))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        for path, text in ((self.config_file, config_json_text(key)), (self.env_file, f"REMOTEOS_AUTH_KEY={key}\n")):
            path.write_text(text)
            path.chmod(0o600)
        self.unit_path.write_text(camera_box_unit(self.venv_link, self.env_file, user=user, mode=mode))
        self.unit_path.chmod(0o644)  # _remoteos_mcp_write ... 644
        self.boot()

    def boot(self) -> None:
        """The running box: systemd loaded the unit at boot and the service is up."""
        state = self.cfg()["state"]
        Path(state).unlink(missing_ok=True)
        for verb in (["daemon-reload"], ["enable", "remoteos-mcp"], ["start", "remoteos-mcp"]):
            fake_linux_tools.systemctl(self.cfg(), verb)
        assert self.state()["active"], self.state()
        Path(self.cfg()["records"]).unlink(missing_ok=True)

    def hang(self) -> None:
        """The running service stops answering (it stays active) until it is (re)started."""
        state_path = Path(self.cfg()["state"])
        state = json.loads(state_path.read_text())
        state["hung"] = True
        state_path.write_text(json.dumps(state, indent=1))

    # --- running -----------------------------------------------------------
    def run_inside_the_service(self) -> None:
        """The next runs come from the agent's own Shell: inside remoteos-mcp.service's cgroup,
        where the unit's restart (or stop) kills them."""
        (self.root / "cgroup").write_text("0::/system.slice/remoteos-mcp.service\n")

    def uninstaller(self) -> Path:
        """uninstall-linux.sh, redirected to this box's paths."""
        return _redirected(UNINSTALLER_LINUX, self.root, self.config_dir, self.unit_path, self.venv_link,
                           self.venv_store)

    def run(self, *, script: Path | None = None, timeout: int = 120, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [os.environ.get("INSTALLER_TEST_BASH", "bash"), str(script or self.script)],
            capture_output=True, text=True, timeout=timeout, start_new_session=True,
            env={**self.env, **env},
        )

    def run_and_drop_console(self, marker: str, release: Path) -> int:
        """Run the installer with stdout+stderr on ONE pipe, close it after the line holding
        `marker` (a dropped `ssh box 'curl … | sudo bash'` has no tty and sends no SIGHUP: the
        installer just writes into a dead pipe), then create `release` so a fake waiting on it
        goes on."""
        proc = subprocess.Popen(
            [os.environ.get("INSTALLER_TEST_BASH", "bash"), str(self.script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env, start_new_session=True,
        )
        assert proc.stdout is not None
        seen = b""
        for line in proc.stdout:
            seen += line
            if marker.encode() in line:
                break
        proc.stdout.close()
        release.touch()
        returncode = proc.wait(timeout=120)
        assert marker.encode() in seen, f"the installer never printed {marker!r}:\n{seen.decode(errors='replace')}"
        return returncode

    # --- reading the result ------------------------------------------------
    def state(self) -> dict:
        return fake_linux_tools._load(self.cfg()["state"])

    def argvs(self) -> list[list[str]]:
        return fake_linux_tools.records(self.cfg()["records"])

    def calls(self, tool: str) -> list[list[str]]:
        return [r[1:] for r in self.argvs() if r and r[0] == tool]

    def venv_pip(self) -> list[dict]:
        log = Path(self.cfg()["venv_pip_log"])
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def venv_installs(self) -> list[dict]:
        return [c for c in self.venv_pip() if c["argv"][:1] == ["install"]]

    def curl_requests(self) -> list[dict]:
        log = Path(self.cfg()["curl_log"])
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def unit(self) -> str:
        return self.unit_path.read_text()

    def env_file_key(self) -> str:
        lines = [line for line in self.env_file.read_text().splitlines() if line]
        assert lines and lines[0].startswith("REMOTEOS_AUTH_KEY="), lines
        return lines[0].split("=", 1)[1]

    def config_key(self) -> str:
        return json.loads(self.config_file.read_text())["auth_key"]

    def venv_target(self) -> Path:
        """What /opt/remoteos-mcp-venv resolves to."""
        return self.venv_link.resolve()

    def venv_python(self, code: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.venv_link / "bin" / "python"), "-c", code],
                              capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"})

    def snapshot(self) -> dict[str, tuple[int, int, bytes | str]]:
        """inode, mtime and content (a link's target) of every config, unit and venv path: an
        unchanged box has an identical snapshot."""
        result = {}
        for top in (self.config_dir, self.unit_path, self.venv_link, self.venv_store):
            paths = [top] + (sorted(top.rglob("*")) if top.is_dir() and not top.is_symlink() else [])
            for path in paths:
                if path.is_symlink():
                    st = path.lstat()
                    result[str(path)] = (st.st_ino, st.st_mtime_ns, os.readlink(path))
                elif path.is_file():
                    st = path.stat()
                    result[str(path)] = (st.st_ino, st.st_mtime_ns, path.read_bytes())
                elif path.is_dir():
                    result[str(path)] = (path.stat().st_ino, 0, "dir")
        return result


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _redirected(script: Path, tmp_path: Path, config_dir: Path, unit_path: Path, venv_link: Path,
                venv_store: Path) -> Path:
    """A copy of `script` (the installer or the uninstaller) with the root check off and the
    box's paths under tmp_path."""
    content = script.read_text()
    for old, new in {
        'if [[ "$EUID" -ne 0 ]]; then': "if false; then",
        'CONFIG_DIR="/etc/remoteos-mcp"': f'CONFIG_DIR="{config_dir}"',
        'UNIT_PATH="/etc/systemd/system/remoteos-mcp.service"': f'UNIT_PATH="{unit_path}"',
        'SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"': f'SERVICE_FILE="{unit_path}"',
        'VENV_LINK="/opt/remoteos-mcp-venv"': f'VENV_LINK="{venv_link}"',
        'VENV_STORE="/opt/remoteos-mcp-venvs"': f'VENV_STORE="{venv_store}"',
        'LOCK_FILE="/run/lock/remoteos-mcp-install.lock"': f'LOCK_FILE="{tmp_path / "install.lock"}"',
        'LOCK_FILE="/run/remoteos-mcp-install.lock"': f'LOCK_FILE="{tmp_path / "install.lock"}"',
        'SUDOERS_FILE="/etc/sudoers.d/90-remoteos-mcp"': f'SUDOERS_FILE="{tmp_path / "sudoers.d" / "90-remoteos-mcp"}"',
        'UPDATER_SERVICE="/etc/systemd/system/remoteos-mcp-updater.service"':
            f'UPDATER_SERVICE="{tmp_path / "remoteos-mcp-updater.service"}"',
        'UPDATER_TIMER="/etc/systemd/system/remoteos-mcp-updater.timer"':
            f'UPDATER_TIMER="{tmp_path / "remoteos-mcp-updater.timer"}"',
        'UPGRADE_LOGS="/var/log/remoteos-mcp-upgrade"': f'UPGRADE_LOGS="{tmp_path / "remoteos-mcp-upgrade"}"',
        "HEALTH_TRIES=12": "HEALTH_TRIES=3",
        "HEALTH_SLEEP=5": "HEALTH_SLEEP=0",
        'CGROUP_FILE="/proc/self/cgroup"': f'CGROUP_FILE="{tmp_path / "cgroup"}"',
    }.items():
        content = content.replace(old, new)
    assert str(config_dir) in content and str(unit_path) in content, f"{script.name}'s paths were not redirected"
    copy = tmp_path / script.name
    copy.write_text(content)
    copy.chmod(0o755)
    return copy


def linux_box(tmp_path: Path, *, graphical: bool | str = False, display: str = ":0",
              installer: Path = INSTALLER_LINUX) -> LinuxBox:
    """A fresh fake Linux box (nothing of remoteos on it) with the installer ready to run.
    `graphical`: False (no session), True / "x11" (an X11 session on `display`), "wayland"."""
    shims = tmp_path / "shims"
    shims.mkdir()
    records = tmp_path / "argv.records"
    config_dir = tmp_path / "etc-remoteos-mcp"
    unit_path = tmp_path / "remoteos-mcp.service"
    venv_link = tmp_path / "opt" / "remoteos-mcp-venv"
    venv_store = tmp_path / "opt" / "remoteos-mcp-venvs"
    venv_link.parent.mkdir()
    system_extra = tmp_path / "system-extra"
    system_extra.mkdir()
    # /proc/self/cgroup of the run: an ssh session's (LinuxBox.run_inside_the_service changes it).
    (tmp_path / "cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")

    # The SYSTEM python's pip (the pre-#12 layout) is fake_pip.py on a fake /usr/local site.
    pip = setup_fake_pip(tmp_path, shims, new_version=LEGACY_VERSION)
    cfg_path = tmp_path / "fake-box.json"
    cfg_path.write_text(json.dumps({
        "state": str(tmp_path / "systemd-state.json"),
        "unit_path": str(unit_path),
        "system_site": str(pip.site),
        "records": str(records),
        "curl_log": str(tmp_path / "curl.jsonl"),
        "venv_pip_log": str(tmp_path / "venv-pip.jsonl"),
        "venv_pip_calls": str(tmp_path / "venv-pip-calls.jsonl"),
        "tests_dir": str(TESTS_DIR),
        "tarballs": {},
        "uids": {SERVICE_USER: SERVICE_UID, "root": 0, "newlevel": 1000},
    }, indent=1))

    real_py = sys.executable
    tools = f'exec "{real_py}" "{FAKE_TOOLS}" "{cfg_path}"'
    box_path = f"{pip.site}:{pip.apt_site}"
    fake_pip_cfg, fake_pip = tmp_path / "fake-pip.json", TESTS_DIR / "fake_pip.py"
    _shim(shims / "python3", f"""\
case "$1" in
    --version) echo "Python 3.12.3"; exit 0 ;;
    -m)
        case "$2" in
            pip) shift 2; FAKE_PIP_CONFIG="{fake_pip_cfg}" exec "{real_py}" "{fake_pip}" "$@" ;;
            venv) shift 2; {tools} venv "$@" ;;
            ensurepip) exit 0 ;;
        esac
        ;;
    -c) PYTHONPATH="{box_path}:{system_extra}" exec "{real_py}" -S -B "$@" ;;
    */rollback.py) PYTHONPATH="{box_path}" exec "{real_py}" -S -B "$@" ;;
esac
exec "{real_py}" "$@"
""", records)
    for role in ("systemctl", "curl", "id", "ss"):
        _shim(shims / role, f'{tools} {role} "$@"\n', records)
    ls_remote = f'printf "%s\\trefs/heads/main\\n" "{FAKE_SHA}"'
    _shim(shims / "git", f'if [[ "$1" == "ls-remote" ]]; then {ls_remote}; fi\nexit 0\n', records)
    _shim(shims / "apt-get", "exit 0\n", records)
    _shim(shims / "visudo", "exit 0\n", records)
    _shim(shims / "findmnt", 'echo "rw,relatime"\n', records)
    _shim(shims / "hostname", 'case "$1" in -I) echo "192.168.1.100" ;; *) echo "testhost" ;; esac\n', records)
    for name in ("ufw", "firewall-cmd"):
        _shim(shims / name, "exit 1\n", records)
    if graphical:
        session_type = "x11" if graphical is True else graphical
        session_display = display if session_type == "x11" else ""
        loginctl = f"""\
case "$1" in
    list-sessions) echo "2 1000 testuser seat0 tty2" ;;
    show-session)
        case "$*" in
            *Type*) echo {session_type} ;; *Class*) echo user ;; *Name*) echo testuser ;;
            *Display*) echo "{session_display}" ;;
        esac ;;
esac
exit 0
"""
    else:
        loginctl = 'if [[ "$1" == "list-sessions" ]]; then echo ""; fi\nexit 0\n'
    _shim(shims / "loginctl", loginctl, records)
    for name in _LOGGED:
        real = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
        if real and not (shims / name).exists():
            _shim(shims / name, f'exec "{real}" "$@"\n', records)

    script = _redirected(installer, tmp_path, config_dir, unit_path, venv_link, venv_store)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    box = LinuxBox(
        root=tmp_path, script=script, shims=shims, config_dir=config_dir, unit_path=unit_path,
        venv_link=venv_link, venv_store=venv_store, cfg_path=cfg_path, pip=pip, system_extra=system_extra,
        env={
            "PATH": f"{shims}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path),
            "TERM": "dumb",
            "LANG": "C",
            "TMPDIR": str(tmpdir),
            "SUDO_USER": SERVICE_USER,
        },
        pristine=script.read_text(),
    )
    box.add_source("main", MAIN_SHA7, SOURCE_VERSION)
    return box


# ---------------------------------------------------------------------------
# Shared by the Linux installer test modules
# ---------------------------------------------------------------------------

K_UNIT = "UnitKey0123456789abcdefghijklmno"
K_ENV = "EnvFileKey0123456789abcdefghijkl"
K_CONFIG = "ConfigKey0123456789abcdefghijklm"
K_EXPLICIT = "ExplicitKey0123456789abcdefghijk"


def output(result) -> str:
    return f"{result.stdout}\n{result.stderr}"


def assert_never_in_an_argv(box, secret: str) -> None:
    for argv in box.argvs():
        assert not any(secret in arg for arg in argv), f"the key is in an argv: {argv}"


def exec_start(box: LinuxBox) -> str:
    return (f"ExecStart={box.venv_link}/bin/python -m remoteos --transport streamable-http --enable-all"
            " --host 0.0.0.0 --port 8092")


def without_description(unit: str) -> list[str]:
    return [line for line in unit.splitlines() if line and not line.startswith("Description=")]


def write_config(box: LinuxBox, key: str) -> None:
    box.config_dir.mkdir(parents=True, exist_ok=True)
    box.config_file.write_text(f'{{"port": 8092, "auth_key": "{key}", "host": "0.0.0.0"}}')
    box.config_file.chmod(0o600)


def builds(box: LinuxBox) -> list[Path]:
    return sorted(box.venv_store.iterdir()) if box.venv_store.exists() else []


def restarted(box: LinuxBox) -> bool:
    return any(argv[:1] == ["restart"] for argv in box.calls("systemctl"))


def legacy_removals(box: LinuxBox) -> list[list[str]]:
    return [a for a in box.calls("python3") if a[:3] == ["-m", "pip", "uninstall"]]
