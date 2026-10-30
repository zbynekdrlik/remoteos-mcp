"""SelfUpgrade (#71): the per-OS runner argv, the ref check, Tier 3 only, no key anywhere."""

from __future__ import annotations

import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest
from fake_console_child import popen_answered_by, run_result
from fastmcp.exceptions import ToolError

import remoteos.__main__ as server
from remoteos import selfupgrade
from remoteos.platform import command
from remoteos.tiers import LINUX_EXCLUDED_TOOLS, TOOL_TIERS

ROOT = Path(__file__).resolve().parent.parent
STAMP = "20261030T210500"
RAW = "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp"


class TestRef:
    @pytest.mark.parametrize(
        "ref",
        ["main", "dev", "v1.0", "release/0.7", "a9c497d", "0123456789abcdef" * 2 + "01234567", "a" * 100, "a_b-c.d"],
    )
    def test_a_plain_ref_passes(self, ref):
        assert selfupgrade.check_ref(ref) == ref

    def test_empty_means_main(self):
        assert selfupgrade.check_ref("") == "main"

    @pytest.mark.parametrize(
        "ref",
        [
            "-x", "--help", "a..b", "..", ".hidden", "/abs", "a/", "a//b", "a/.b", "a b", "a;b", "a|b", "a&b", "$(id)",
            "`id`", "a'b", 'a"b', "a\nb", "main\n", "a\\b", "a%b", "a" * 101, "é",
        ],
    )
    def test_anything_else_is_refused(self, ref):
        with pytest.raises(ValueError, match="commit sha, a tag or a branch"):
            selfupgrade.check_ref(ref)

    def test_every_ref_the_tool_passes_install_linux_sh_ref_ok_too(self):
        """The first refusal is the tool's, never a started run's: the bash check, run as written."""
        script = (ROOT / "install-linux.sh").read_text()
        ref_ok = script[script.index("ref_ok() {") : script.index("# --- the key")]
        for ref in ("main", "release/0.7", "v1.0", "a_b-c.d"):
            subprocess.run(["bash", "-c", f"shopt -s globasciiranges; {ref_ok}\nref_ok {shlex.quote(ref)}"], check=True)


class TestLinux:
    def test_a_user_unit_goes_through_sudo_n_in_its_own_unit(self):
        plan = selfupgrade.linux_plan("dev", STAMP, 1000)
        unit = f"remoteos-mcp-upgrade-{STAMP}"
        assert plan.argv[:6] == ("sudo", "-n", "systemd-run", "--unit", unit, "--collect")
        assert "--setenv=REMOTEOS_MCP_REF=dev" in plan.argv
        assert plan.argv[-3:-1] == ("/bin/bash", "-c")
        assert plan.argv[-1] == f"set -o pipefail; curl -fsSL --max-time 120 {RAW}/dev/install-linux.sh | bash"
        assert plan.log_path == f"/var/log/{unit}.log"
        assert f"--property=StandardOutput=file:{plan.log_path}" in plan.argv
        assert plan.ref_file is None

    def test_a_root_unit_needs_no_sudo(self):
        assert selfupgrade.linux_plan("main", STAMP, 0).argv[0] == "systemd-run"

    def test_the_command_line_gives_systemd_nothing_to_expand(self):
        line = selfupgrade.linux_plan("main", STAMP, 0).argv[-1]
        assert "$" not in line and "%" not in line

    def test_the_unit_name_passes_the_installers_in_service_guard(self):
        """The guard greps `/remoteos-mcp\\.service`: the transient unit's cgroup must not match."""
        script = (ROOT / "install-linux.sh").read_text()
        assert '! grep -q "/${SERVICE_NAME}\\.service" "$CGROUP_FILE"' in script
        unit = selfupgrade.linux_plan("main", STAMP, 0).argv[3]
        cgroup = f"0::/system.slice/{unit}.service\n"
        found = subprocess.run(["grep", "-q", "/remoteos-mcp\\.service"], input=cgroup, text=True, check=False)
        assert found.returncode == 1


class TestMacos:
    plan = selfupgrade.macos_plan("dev", STAMP, "/Users/my user", "/opt/homebrew/bin")

    def test_a_submitted_job_with_its_log(self):
        label = f"com.remoteos-mcp.upgrade-{STAMP}"
        log_path = f"/Users/my user/.remoteos-mcp/upgrade-{STAMP}.log"
        assert self.plan.argv[:9] == ("launchctl", "submit", "-l", label, "-o", log_path, "-e", log_path, "--")
        assert self.plan.argv[9:11] == ("/bin/bash", "-c")
        assert self.plan.log_path == log_path

    @pytest.mark.parametrize("ref", ["a9c497d", "0123456789abcdef"])
    def test_a_short_sha_is_refused_because_install_sh_cannot_resolve_one(self, ref):
        with pytest.raises(ValueError, match="full 40-character"):
            selfupgrade.macos_plan(ref, STAMP, "/Users/u", "/opt/homebrew/bin")

    def test_a_full_sha_and_a_branch_named_like_hex_length_pass(self):
        for ref in ("0123456789abcdef0123456789abcdef01234567", "abc"):
            selfupgrade.macos_plan(ref, STAMP, "/Users/u", "/opt/homebrew/bin")

    def test_the_job_removes_itself_last_or_launchd_reruns_it_forever(self):
        steps = self.plan.argv[-1].split("; ")
        assert steps[-2] == f"curl -fsSL --max-time 120 {RAW}/dev/install.sh | bash"
        assert steps[-1] == f"launchctl remove com.remoteos-mcp.upgrade-{STAMP}"

    def test_the_shell_line_is_valid_and_quotes_the_paths(self, tmp_path):
        """Real bash, with a logging launchctl and curl on PATH."""
        (tmp_path / "curl").write_text("#!/bin/bash\necho 'echo REF=$REMOTEOS_MCP_REF HOME=$HOME'\n")
        (tmp_path / "launchctl").write_text("#!/bin/bash\necho removed \"$@\"\n")
        for tool in ("curl", "launchctl"):
            (tmp_path / tool).chmod(0o755)
        plan = selfupgrade.macos_plan("dev", STAMP, "/Users/my user", str(tmp_path))
        argv = ["/bin/bash", "-c", plan.argv[-1]]
        done = subprocess.run(argv, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        assert done.returncode == 0, done.stderr
        assert "REF=dev HOME=/Users/my user" in done.stdout
        assert f"removed remove com.remoteos-mcp.upgrade-{STAMP}" in done.stdout


class TestWindows:
    def test_the_task_with_the_ref_in_a_file_never_in_its_argv(self):
        plan = selfupgrade.windows_plan("dev", "/home/u")
        assert plan.argv == ("schtasks", "/Run", "/TN", "RemoteOSMCP-Upgrade")
        assert plan.ref_file == "/home/u/.remoteos-mcp/upgrade-ref.txt"
        assert plan.log_path == "/home/u/.remoteos-mcp/upgrade.log"

    def test_the_names_are_the_ones_install_ps1_registers(self):
        text = (ROOT / "install.ps1").read_text()
        for name in (selfupgrade.WINDOWS_TASK, selfupgrade.WINDOWS_REF_FILE, selfupgrade.WINDOWS_LOG_FILE):
            assert name in text


class TestThisMachine:
    @pytest.mark.parametrize(
        ("windows", "macos", "runner"), [(True, False, "schtasks"), (False, True, "launchctl"), (False, False, None)]
    )
    def test_the_plan_of_each_os(self, monkeypatch, windows, macos, runner):
        monkeypatch.setattr(selfupgrade, "is_windows", lambda: windows)
        monkeypatch.setattr(selfupgrade, "is_macos", lambda: macos)
        monkeypatch.setattr(selfupgrade.os, "geteuid", lambda: 1000, raising=False)
        argv = selfupgrade.upgrade_plan("").argv
        assert argv[0] == (runner or "sudo")

    def test_a_bad_ref_never_reaches_a_runner(self, monkeypatch):
        monkeypatch.setattr(command.subprocess, "Popen", lambda *a, **k: pytest.fail("a runner started"))
        with pytest.raises(ToolError, match="commit sha, a tag or a branch"):
            server.SelfUpgrade("a;b")


@pytest.fixture
def child(monkeypatch):
    """Popen answered at the OS boundary; -> the argvs started. `child.answer` = (returncode, stderr)."""

    class Children(list):
        answer = (0, "")
        before = staticmethod(lambda: None)

    children = Children()

    def run(argv, **kwargs):
        children.before()
        children.append(list(argv))
        return run_result(kwargs, children.answer[0], "", children.answer[1])

    monkeypatch.setattr(command.subprocess, "Popen", popen_answered_by(run))
    return children


class TestStart:
    def test_the_reply_names_the_ref_and_the_log(self, child):
        plan = selfupgrade.linux_plan("dev", STAMP, 0)
        reply = selfupgrade.start(plan)
        assert child == [list(plan.argv)]
        assert "Upgrade to dev started" in reply and plan.log_path in reply and "/health" in reply

    def test_windows_writes_the_ref_before_it_starts_the_task(self, child, tmp_path):
        (tmp_path / ".remoteos-mcp").mkdir()
        plan = selfupgrade.windows_plan("dev", str(tmp_path))
        seen = []
        child.before = lambda: seen.append(Path(plan.ref_file).read_text())
        selfupgrade.start(plan)
        assert seen == ["dev\n"]

    def test_a_runner_that_fails_is_a_tool_error_with_its_stderr(self, child):
        child.answer = (1, "sudo: a password is required\n")
        with pytest.raises(ToolError, match="sudo exit 1.*a password is required.*over SSH"):
            selfupgrade.start(selfupgrade.linux_plan("main", STAMP, 1000))

    def test_a_real_child_runs_and_its_failure_reaches_the_caller(self):
        ok = selfupgrade.UpgradePlan("main", ("true",), "/tmp/log")
        assert "started" in selfupgrade.start(ok)
        bad = selfupgrade.UpgradePlan("main", ("false",), "/tmp/log")
        with pytest.raises(ToolError, match="false exit 1"):
            selfupgrade.start(bad)


class TestTheKeyAndTheGate:
    @pytest.mark.parametrize(
        "plan",
        [
            selfupgrade.linux_plan("dev", STAMP, 1000),
            selfupgrade.macos_plan("dev", STAMP, "/Users/u", "/opt/homebrew/bin"),
            selfupgrade.windows_plan("dev", "/home/u"),
        ],
    )
    def test_no_argv_has_a_key_word(self, plan):
        text = " ".join(plan.argv).lower()
        assert "key" not in text and "auth" not in text and "token" not in text

    def test_the_source_never_reads_the_key(self):
        assert "AUTH_KEY" not in (ROOT / "src/remoteos/selfupgrade.py").read_text()

    def test_tier_3_only_and_on_every_box(self):
        assert "SelfUpgrade" in TOOL_TIERS["tier3"]
        assert "SelfUpgrade" not in TOOL_TIERS["tier1"] | TOOL_TIERS["tier2"]
        assert "SelfUpgrade" not in LINUX_EXCLUDED_TOOLS

    def test_the_default_policy_leaves_it_out(self):
        from remoteos.tiers import resolve_enabled_tools

        assert "SelfUpgrade" not in resolve_enabled_tools()
        assert "SelfUpgrade" in resolve_enabled_tools(enable_tier3=True)


def test_install_ps1_registers_the_task_in_its_auto_start_step():
    text = (ROOT / "install.ps1").read_text()
    flow = text[text.index("[6/7] Setting up auto-start") : text.index("[7/7] Configuring firewall")]
    assert "Register-RemoteOSUpgradeTask -ConfigDir $ConfigDir -User $desktopUser" in flow
    assert "Could not register the scheduled task 'RemoteOSMCP-Upgrade'" in flow
    block = textwrap.dedent(text[text.index("function Register-RemoteOSUpgradeTask") :])
    block = block[: block.index("\n}\n")]
    assert "-RunLevel Highest" in block and "-LogonType Interactive" in block
    assert "New-ScheduledTaskTrigger" not in block


def test_install_ps1_upgrade_command_cannot_hang_on_a_prompt_and_rechecks_the_ref():
    text = (ROOT / "install.ps1").read_text()
    command = text[text.index("function Get-RemoteOSUpgradeCommand") :]
    command = command[: command.index("function Get-RemoteOSUpgradeTaskArguments")]
    assert "$env:REMOTEOS_MCP_RELAUNCHED = " in command
    assert "-cnotmatch" in command and "$ref.Length -gt 100" in command
    assert command.index("-cnotmatch") < command.index("Invoke-RestMethod")
    assert "AUTH_KEY" not in command
    assert "-NonInteractive" in text


def test_install_ps1_stop_never_kills_the_upgrade_task_itself():
    text = (ROOT / "install.ps1").read_text()
    assert '$_.Id -ne $PID -and $_.MainWindowTitle -match "RemoteOS|WinRemote"' in text


def test_uninstall_ps1_removes_the_task():
    assert 'Unregister-ScheduledTask -TaskName "RemoteOSMCP-Upgrade"' in (ROOT / "uninstall.ps1").read_text()
