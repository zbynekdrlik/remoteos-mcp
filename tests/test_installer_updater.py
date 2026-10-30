"""The installers set up the per-box updater (#65): Linux on the fake box, macOS's and Windows's text."""

from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path

import pytest
from linux_installer_harness import linux_box, mode_of, output

ROOT = Path(__file__).parent.parent


def ring_of(box) -> str:
    return json.loads((box.config_dir / "updater.json").read_text())["ring"]


def test_linux_gets_a_daily_randomised_boot_time_timer_for_root_and_ring_stable(tmp_path):
    box = linux_box(tmp_path)
    result = box.run()
    assert result.returncode == 0, output(result)
    service, timer = box.updater_service.read_text(), box.updater_timer.read_text()
    assert f"ExecStart={box.venv_link}/bin/python -m remoteos.updater\n" in service
    assert "Type=oneshot" in service and "User=" not in service
    for line in ("OnCalendar=daily", "RandomizedDelaySec=6h", "Persistent=true", "OnBootSec=10min",
                 "WantedBy=timers.target"):
        assert line in timer
    assert mode_of(box.updater_timer) == 0o644
    assert ring_of(box) == "stable"
    assert ["enable", "--now", "remoteos-mcp-updater.timer"] in box.calls("systemctl")


def test_the_unit_files_hold_no_key(tmp_path):
    box = linux_box(tmp_path)
    assert box.run().returncode == 0
    key = json.loads(box.config_file.read_text())["auth_key"]
    for path in (box.updater_service, box.updater_timer, box.config_dir / "updater.json"):
        assert key not in path.read_text()


def test_a_ring_is_set_kept_on_a_rerun_and_changed_only_when_named(tmp_path):
    box = linux_box(tmp_path)
    assert box.run(REMOTEOS_MCP_RING="canary").returncode == 0
    assert ring_of(box) == "canary"
    assert box.run().returncode == 0
    assert ring_of(box) == "canary"
    assert box.run(REMOTEOS_MCP_RING="stable").returncode == 0
    assert ring_of(box) == "stable"


def test_a_bad_ring_is_refused_before_anything_changes(tmp_path):
    box = linux_box(tmp_path)
    before = box.snapshot()
    result = box.run(REMOTEOS_MCP_RING="nightly")
    assert result.returncode == 1
    assert "REMOTEOS_MCP_RING must be stable or canary" in output(result)
    assert box.snapshot() == before
    assert not box.updater_timer.exists()


def test_camera_box_s_enable_only_boxes_get_no_timer(tmp_path):
    box = linux_box(tmp_path)
    result = box.run(REMOTEOS_MCP_POLICY="enable-only")
    assert result.returncode == 0, output(result)
    assert not box.updater_timer.exists()
    assert not (box.config_dir / "updater.json").exists()


def test_an_enable_only_run_removes_a_timer_a_default_run_left(tmp_path):
    box = linux_box(tmp_path)
    assert box.run().returncode == 0
    assert box.updater_timer.exists()
    result = box.run(REMOTEOS_MCP_POLICY="enable-only")
    assert result.returncode == 0, output(result)
    assert not box.updater_timer.exists() and not box.updater_service.exists()


def test_a_failed_proof_installs_no_timer(tmp_path):
    box = linux_box(tmp_path)
    box.configure(service={"start_fails": True})
    assert box.run().returncode != 0
    assert not box.updater_timer.exists()


def test_a_timer_that_will_not_enable_says_the_install_stands(tmp_path):
    box = linux_box(tmp_path)
    real = (box.shims / "systemctl").read_text().split("\n", 2)[2]
    box.shim("systemctl", 'if [[ "$*" == *updater.timer* ]]; then exit 1; fi\n' + real)
    result = box.run()
    assert result.returncode == 0, output(result)
    assert "stays on v" in output(result)
    assert "SETUP COMPLETE" in output(result)


def test_a_rerun_changes_no_updater_file_and_adds_no_reload(tmp_path):
    box = linux_box(tmp_path)
    assert box.run().returncode == 0
    stamp = box.updater_timer.stat().st_mtime_ns
    reloads = sum(argv == ["daemon-reload"] for argv in box.calls("systemctl"))
    assert box.run().returncode == 0
    assert box.updater_timer.stat().st_mtime_ns == stamp
    # the switch's own reload, none for the updater
    assert sum(argv == ["daemon-reload"] for argv in box.calls("systemctl")) == reloads + 1


def test_the_linux_uninstaller_removes_the_timer_before_the_venv(tmp_path):
    box = linux_box(tmp_path)
    assert box.run().returncode == 0
    result = box.run(script=box.uninstaller())
    assert result.returncode == 0, output(result)
    assert not box.updater_timer.exists() and not box.updater_service.exists()
    assert ["disable", "--now", "remoteos-mcp-updater.timer"] in box.calls("systemctl")


# --- macOS: the plist as install.sh renders it --------------------------------------------------


def _plist() -> bytes:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = re.search(r'write_private "\$UPDATER_PLIST" <<PLIST\n(.*?)\nPLIST\n', text, re.S).group(1)
    for name, value in {"${UPDATER_LABEL}": "com.remoteos-mcp.updater", "${PYTHON_PATH}": "/py/python3",
                        "${CONFIG_DIR}": "/Users/x/.remoteos-mcp", "$((RANDOM % 24))": "3",
                        "$((RANDOM % 60))": "41"}.items():
        body = body.replace(name, value)
    return body.encode()


def test_the_launch_agent_runs_the_updater_daily_and_at_login():
    plist = plistlib.loads(_plist())
    assert plist["ProgramArguments"] == ["/py/python3", "-m", "remoteos.updater", "--log-file",
                                         "/Users/x/.remoteos-mcp/updater.log"]
    assert plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 41}
    assert plist["RunAtLoad"] is True
    assert "KeepAlive" not in plist and "EnvironmentVariables" not in plist


def test_macos_checks_the_ring_before_python_and_sets_it_up_after_the_proof():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert text.index("REMOTEOS_MCP_RING must be") < text.index('echo "  [1/5] Checking Python..."')
    assert text.index('HEALTH_OK" != "true"') < text.index('UPDATER_CONFIG="$CONFIG_DIR/updater.json"')


def test_the_macos_uninstaller_removes_the_agent():
    text = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert 'launchctl unload "$UPDATER_PLIST"' in text and 'rm -f "$UPDATER_PLIST"' in text


# --- Windows: the order in the flow (the behaviour runs in tests_windows) ------------------------


@pytest.fixture(scope="module")
def flow() -> str:
    return (ROOT / "installers/install-ps1/90-flow.ps1").read_text(encoding="utf-8")


def test_the_trap_is_armed_before_the_stop_and_disarmed_only_after_the_proof(flow):
    arm = flow.index("$trapError = Start-RemoteOSRollbackTrap")
    stop = flow.index("Stop-RemoteOSServer -Port $Port\n$install")
    proof = flow.index("Health check OK:")
    disarm = flow.index("Stop-RemoteOSRollbackTrap -ConfigDir $ConfigDir\n", proof)
    assert arm < stop < proof < disarm
    assert flow.count("Stop-RemoteOSRollbackTrap") == 2  # the end of the run, the $rollback splat


def test_the_wheelhouses_left_the_profile_and_the_old_root_goes_only_after_the_proof(flow):
    assert "$wheelhouseRoot = Join-Path $dataDir 'wheelhouse'" in flow
    assert "Join-Path $ConfigDir 'wheelhouse'" in flow.replace("$legacyWheelhouseRoot = ", "")
    assert flow.index("Health check OK:") < flow.index("Remove-RemoteOSTree $legacyWheelhouseRoot")


def test_the_data_folder_is_protected_before_what_is_inside_it(flow):
    assert flow.index("-Root $dataDir -User") < flow.index("-Root $wheelhouseRoot -User")


def test_a_bad_ring_stops_before_the_first_change(flow):
    assert flow.index("Test-RemoteOSRing $Ring") < flow.index("$desktopUser = $null")


def test_every_windows_task_is_removed_by_the_uninstaller():
    text = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    assert all(f'"{task}"' in text for task in ("RemoteOSMCP", "RemoteOSMCP-Upgrade", "RemoteOSMCP-Updater",
                                                "RemoteOSMCP-Rollback"))


def test_the_installer_takes_the_lock_before_it_looks_at_the_box(flow):
    assert flow.index("$installLock = Enter-RemoteOSInstallLock") < flow.index("$desktopUser = $null")


def test_the_lock_is_released_at_the_end_of_the_flow(flow):
    """irm | iex leaves the console open: a held mutex would refuse every later run, and burn the updater's tries."""
    code = [line for line in flow.splitlines() if line.strip() and not line.startswith("#")]
    assert code[-2:] == ["$installLock.ReleaseMutex()", "$installLock.Dispose()"]


def test_the_uninstaller_is_gated_before_its_first_change_and_ascii():
    raw = (ROOT / "uninstall.ps1").read_bytes()
    assert raw.isascii()
    text = raw.decode()
    gate = text.index("if (-not $isAdmin) {")
    for change in ("schtasks /End", "Unregister-ScheduledTask", "Remove-NetFirewallRule", "Stop-Process -Id"):
        assert gate < text.index(change)
    assert "Remove-Item -Recurse -" not in text
    assert "$env:USERPROFILE\\" not in text


def test_the_uninstaller_s_link_safe_delete_is_the_installer_s():
    def function(text: str, name: str) -> str:
        return re.search(rf"function {name}\(.*?\n}}\n", text, re.S).group(0)

    ours = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
    theirs = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for name in ("Test-RemoteOSLink", "Remove-RemoteOSTree"):
        assert function(ours, name) == function(theirs, name)
