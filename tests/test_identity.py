"""The server announces its real hostname, OS and shell (#59).

stream-snv was reinstalled from Windows to Ubuntu but kept its ``win-stream-snv`` name, and clients
kept sending PowerShell to a bash box. ``identity`` builds the MCP ``instructions`` from the live
facts. Here the facts come from faked ``platform`` / ``socket`` answers (the OS boundary); the
real-OS read of the Linux runner is in ``tests/test_startup_flags.py`` (``initialize`` against the
real server) and of the Windows runner in ``tests_windows/test_identity_windows.py``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from fake_console_child import assert_runs_powershell, returned
from tool_surface import server

from remoteos import identity

CAPABILITIES = (
    "Provides desktop control, window management, shell execution, file operations, network tools, "
    "registry, services, and system management tools."
)


@pytest.fixture
def machine(monkeypatch):
    """Fake the OS boundary identity reads: the OS predicate, hostname, architecture, release facts."""

    def _set(os_kind: str, **facts):
        monkeypatch.setattr(identity, "is_windows", lambda: os_kind == "windows")
        monkeypatch.setattr(identity, "is_macos", lambda: os_kind == "macos")
        monkeypatch.setattr(identity.socket, "gethostname", lambda: facts.get("hostname", "box"))
        monkeypatch.setattr(identity.platform, "machine", lambda: facts.get("arch", "x86_64"))
        for name in ("release", "version", "system", "win32_edition"):
            if name in facts:
                monkeypatch.setattr(identity.platform, name, lambda value=facts[name]: value)
        if "mac_ver" in facts:
            monkeypatch.setattr(identity.platform, "mac_ver", lambda: facts["mac_ver"])
        if "os_release" in facts:
            answer = facts["os_release"]

            def _os_release():
                if isinstance(answer, Exception):
                    raise answer
                return dict(answer)

            monkeypatch.setattr(identity.platform, "freedesktop_os_release", _os_release)

    return _set


def test_linux_announces_its_distribution_hostname_and_bash(machine):
    ubuntu = {"NAME": "Ubuntu", "PRETTY_NAME": "Ubuntu 24.04.3 LTS"}
    machine("linux", hostname="stream-snv", arch="x86_64", os_release=ubuntu)
    facts = identity.machine_identity()
    assert facts == {
        "hostname": "stream-snv",
        "os_name": "Ubuntu 24.04.3 LTS",
        "arch": "x86_64",
        "shell": "/bin/bash -c",
    }
    assert identity.server_instructions(**facts) == (
        "Remote OS MCP Server on stream-snv — Ubuntu 24.04.3 LTS, x86_64. The Shell tool runs /bin/bash -c. "
        + CAPABILITIES
    )


def test_windows_announces_release_build_and_powershell(machine):
    machine("windows", hostname="RESOLUME-PP", arch="AMD64", release="11", version="10.0.26100", win32_edition="Core")
    facts = identity.machine_identity()
    assert facts["os_name"] == "Windows 11 (build 10.0.26100)"
    assert identity.server_instructions(**facts) == (
        "Remote OS MCP Server on RESOLUME-PP — Windows 11 (build 10.0.26100), AMD64. The Shell tool runs "
        "PowerShell (powershell -NoProfile -Command, through the UTF-8 console trampoline). " + CAPABILITIES
    )


@pytest.mark.parametrize("edition", ["ServerDatacenter", "ServerStandard"])
def test_a_windows_server_is_named_by_its_build_never_by_the_release_guess(machine, edition):
    # CPython 3.12 says "2022Server" for every Server build from 20348 up, a Server 2025 included
    machine("windows", release="2022Server", version="10.0.26100", win32_edition=edition)
    assert identity.machine_identity()["os_name"] == "Windows Server (build 10.0.26100)"


@pytest.mark.parametrize("edition", [None, "Professional"], ids=["unreadable-edition", "professional"])
def test_a_windows_client_or_an_unreadable_edition_keeps_the_release(machine, edition):
    machine("windows", release="10", version="10.0.19045", win32_edition=edition)
    assert identity.machine_identity()["os_name"] == "Windows 10 (build 10.0.19045)"


def test_macos_announces_its_version_and_zsh(machine):
    machine("macos", hostname="propresenter", arch="arm64", mac_ver=("15.1", ("", "", ""), "arm64"))
    facts = identity.machine_identity()
    assert facts == {"hostname": "propresenter", "os_name": "macOS 15.1", "arch": "arm64", "shell": "/bin/zsh -c"}
    assert identity.server_instructions(**facts) == (
        "Remote OS MCP Server on propresenter — macOS 15.1, arm64. The Shell tool runs /bin/zsh -c. " + CAPABILITIES
    )


def test_macos_without_a_product_version_names_the_darwin_release(machine):
    machine("macos", mac_ver=("", ("", "", ""), ""), release="24.1.0")
    assert identity.machine_identity()["os_name"] == "macOS (Darwin 24.1.0)"


@pytest.mark.parametrize(
    "os_release",
    [
        OSError(2, "Unable to read files /etc/os-release, /usr/lib/os-release"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        {"NAME": "Linux"},
        {"PRETTY_NAME": ""},
    ],
    ids=["no-os-release-file", "os-release-not-utf8", "no-pretty-name", "empty-pretty-name"],
)
def test_linux_without_a_pretty_name_falls_back_to_kernel_system_and_release(machine, os_release):
    machine("linux", system="Linux", release="6.8.0-45-generic", os_release=os_release)
    assert identity.machine_identity()["os_name"] == "Linux 6.8.0-45-generic"


def test_an_empty_hostname_or_architecture_is_named_unknown(machine):
    machine("linux", hostname="", arch="", os_release={"PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)"})
    facts = identity.machine_identity()
    assert (facts["hostname"], facts["arch"]) == ("unknown host", "unknown architecture")


def test_the_old_every_os_tail_is_gone():
    text = identity.server_instructions(hostname="h", os_name="o", arch="a", shell="s")
    assert "for Windows, macOS, and Linux machines" not in text
    assert "for Windows, macOS, and Linux machines" not in server.mcp.instructions


def test_the_running_server_announces_this_machine():
    assert server.mcp.instructions == identity.server_instructions(**identity.machine_identity())


@pytest.fixture
def popen(monkeypatch):
    """Record the argv the Shell tool hands ``subprocess.Popen`` (the OS process boundary)."""
    calls: list[list[str]] = []

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            calls.append(list(args))
            self.kwargs = kwargs

        def communicate(self, timeout=None):
            return (returned(self.kwargs, "ok\n"), returned(self.kwargs, ""))

    monkeypatch.setattr(server.subprocess, "Popen", _Proc)
    return calls


@pytest.mark.parametrize("os_kind", ["windows", "macos", "linux"])
def test_shell_runs_exactly_the_shell_identity_announces(monkeypatch, popen, windows_python, os_kind):
    for module in (server, identity):
        monkeypatch.setattr(module, "is_windows", lambda: os_kind == "windows")
        monkeypatch.setattr(module, "is_macos", lambda: os_kind == "macos")
    announced = identity.shell_text()
    assert server.Shell("whoami") == "ok"
    [argv] = popen
    if os_kind == "windows":
        assert_runs_powershell(argv, windows_python, "whoami")
        assert announced.startswith("PowerShell (" + " ".join(argv[8:11]) + ",")  # powershell -NoProfile -Command
    else:
        assert argv == [*announced.split(" "), "whoami"]
        assert announced == {"macos": "/bin/zsh -c", "linux": "/bin/bash -c"}[os_kind]


class TestAdmin:
    """``has_admin`` (#57): the ``admin`` of ``/health``, cached so a public endpoint is no process factory."""

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setattr(identity, "_admin_cache", None)
        monkeypatch.setattr(identity, "is_windows", lambda: False)
        monkeypatch.setattr(identity.os, "geteuid", lambda: 1000, raising=False)
        self.sudo_calls = 0

    def sudo(self, monkeypatch, returncode=0, error=None):
        def run(argv, timeout):
            assert argv == ["sudo", "-n", "true"]
            self.sudo_calls += 1
            if error is not None:
                raise error
            return SimpleNamespace(returncode=returncode)

        monkeypatch.setattr(identity, "run_child", run)

    def test_root_needs_no_sudo(self, monkeypatch):
        self.sudo(monkeypatch)
        monkeypatch.setattr(identity.os, "geteuid", lambda: 0, raising=False)
        assert identity.has_admin() is True
        assert self.sudo_calls == 0

    @pytest.mark.parametrize(("code", "expected"), [(0, True), (1, False)])
    def test_a_user_has_admin_when_sudo_asks_no_password(self, monkeypatch, code, expected):
        self.sudo(monkeypatch, returncode=code)
        assert identity.has_admin() is expected

    @pytest.mark.parametrize("error", [FileNotFoundError("sudo"), subprocess.TimeoutExpired("sudo", 5)])
    def test_no_sudo_or_a_hanging_one_reads_false_and_never_raises(self, monkeypatch, error):
        self.sudo(monkeypatch, error=error)
        assert identity.has_admin() is False

    @pytest.mark.parametrize(("token", "expected"), [(1, True), (0, False)])
    def test_windows_reads_the_elevated_token(self, monkeypatch, token, expected):
        monkeypatch.setattr(identity, "is_windows", lambda: True)
        shell32 = SimpleNamespace(IsUserAnAdmin=lambda: token)
        monkeypatch.setattr(identity.ctypes, "windll", SimpleNamespace(shell32=shell32), raising=False)
        assert identity.has_admin() is expected

    def test_a_minute_of_health_calls_costs_one_sudo_and_then_it_is_read_again(self, monkeypatch):
        """Read again, never once: the sudoers drop-in lands AFTER the installer's proof, no restart."""
        clock = [100.0]
        monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
        self.sudo(monkeypatch, returncode=1)
        assert identity.has_admin() is False
        clock[0] += identity.ADMIN_CACHE_S - 1
        self.sudo_calls, _ = 0, identity.has_admin()
        assert self.sudo_calls == 0
        self.sudo(monkeypatch, returncode=0)
        clock[0] += 2
        assert identity.has_admin() is True
        assert self.sudo_calls == 1

    def test_a_failed_read_keeps_the_last_answer(self, monkeypatch):
        """A slow sudo must not announce a lost admin."""
        clock = [100.0]
        monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
        self.sudo(monkeypatch, returncode=0)
        assert identity.has_admin() is True
        self.sudo(monkeypatch, error=subprocess.TimeoutExpired("sudo", 5))
        clock[0] += identity.ADMIN_CACHE_S + 1
        assert identity.has_admin() is True
