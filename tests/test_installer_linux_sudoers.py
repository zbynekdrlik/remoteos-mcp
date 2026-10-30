"""install-linux.sh keeps the service account's admin (#71, owner decision on #67): the drop-in
/etc/sudoers.d/90-remoteos-mcp survives a reprovision, and visudo judges it before anything changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from linux_installer_harness import K_ENV, SERVICE_USER, linux_box, mode_of, output, restarted

LINE = f"{SERVICE_USER} ALL=(ALL) NOPASSWD:ALL\n"


def installed(box) -> None:
    result = box.run()
    assert result.returncode == 0, output(result)


def hand_made(box, text: str = LINE, mode: int = 0o440) -> Path:
    box.sudoers_file.parent.mkdir(exist_ok=True)
    box.sudoers_file.write_text(text)
    box.sudoers_file.chmod(mode)
    return box.sudoers_file


def test_a_desktop_box_gets_the_drop_in_for_its_unit_account(tmp_path):
    box = linux_box(tmp_path, graphical=True)
    installed(box)
    assert box.sudoers_file.read_text() == LINE
    assert mode_of(box.sudoers_file) == 0o440
    assert not list(box.sudoers_file.parent.glob("*.new"))


def test_the_rendered_line_passes_the_real_visudo(tmp_path):
    visudo = shutil.which("visudo", path="/usr/sbin:/usr/bin:/sbin:/bin")
    assert visudo, "this test needs the real visudo (the sudo package)"
    box = linux_box(tmp_path, graphical=True)
    box.shim("visudo", f'exec "{visudo}" "$@"\n')
    installed(box)
    assert box.sudoers_file.read_text() == LINE


def test_visudo_checks_the_rendered_file_before_the_venv_is_touched(tmp_path):
    box = linux_box(tmp_path)
    installed(box)
    records = box.argvs()
    tools = [r[0] for r in records]
    check = records[tools.index("visudo")]
    assert check[1] == "-cf" and Path(check[2]).name == "sudoers"
    assert tools.index("visudo") < tools.index("curl")
    assert tools.index("visudo") < tools.index("systemctl", tools.index("curl"))


@pytest.mark.parametrize("env", [{"REMOTEOS_MCP_USER": "root"}, {"SUDO_USER": ""}])
def test_a_root_unit_gets_none(tmp_path, env):
    box = linux_box(tmp_path)
    result = box.run(**env)
    assert result.returncode == 0, output(result)
    assert "User=root" in box.unit()
    assert not box.sudoers_file.exists() and box.calls("visudo") == []


def test_a_failed_visudo_stops_the_run_before_the_switch_on_a_working_box(tmp_path):
    box = linux_box(tmp_path)
    box.install_camera_box(K_ENV)
    before = box.snapshot()
    box.shim("visudo", 'echo ">>> sudoers.tmp: syntax error near line 1 <<<" >&2\nexit 1\n')
    result = box.run()
    assert result.returncode == 1
    assert "nothing was changed" in output(result) and "syntax error near line 1" in output(result)
    assert box.snapshot() == before
    assert not restarted(box) and not box.sudoers_file.exists() and box.curl_requests() == []


def test_no_visudo_is_refused_before_anything_changes(tmp_path):
    box = linux_box(tmp_path)
    (box.shims / "visudo").unlink()
    before = box.snapshot()
    result = box.run(PATH=f"{box.shims}:/usr/bin:/bin")
    assert result.returncode == 1 and "visudo (the sudo package) is missing" in output(result)
    assert box.snapshot() == before


def test_a_hand_made_identical_file_is_adopted_untouched(tmp_path):
    """strih-pp, stream-pp, strih-snv, stream-snv: the file of the 2026-10-24 comment on #67."""
    box = linux_box(tmp_path)
    file = hand_made(box)
    st = file.stat()
    installed(box)
    assert (file.stat().st_ino, file.stat().st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert box.calls("install") == []


def test_another_text_is_replaced(tmp_path):
    box = linux_box(tmp_path)
    file = hand_made(box, "someoneelse ALL=(ALL) ALL\n")
    result = box.run()
    assert result.returncode == 0, output(result)
    assert "replacing what" in output(result)
    assert file.read_text() == LINE and mode_of(file) == 0o440


def test_the_right_text_with_the_wrong_mode_is_put_right(tmp_path):
    box = linux_box(tmp_path)
    file = hand_made(box, mode=0o644)
    installed(box)
    assert mode_of(file) == 0o440


def test_a_failed_proof_leaves_no_drop_in(tmp_path):
    box = linux_box(tmp_path)
    box.configure(service={"start_fails": True})
    result = box.run()
    assert result.returncode == 1
    assert not box.sudoers_file.exists()


def test_adding_the_drop_in_alone_restarts_nothing(tmp_path):
    box = linux_box(tmp_path)
    installed(box)
    box.sudoers_file.unlink()
    box.configure()
    Path(box.cfg()["records"]).unlink()
    installed(box)
    assert box.sudoers_file.read_text() == LINE
    assert not restarted(box)


def test_enable_only_gets_it_too(tmp_path):
    box = linux_box(tmp_path)
    result = box.run(REMOTEOS_MCP_POLICY="enable-only")
    assert result.returncode == 0, output(result)
    assert box.sudoers_file.read_text() == LINE


def test_a_signal_during_the_write_leaves_no_temp_name(tmp_path):
    box = linux_box(tmp_path)
    box.inject_after('install -m 0440 "$SUDOERS_TMP" "$WRITE_TMP"', "kill -TERM $$")
    assert box.run().returncode != 0
    assert list(box.sudoers_file.parent.iterdir()) == []


def test_the_uninstaller_removes_the_drop_in(tmp_path):
    box = linux_box(tmp_path)
    installed(box)
    log = box.root / "remoteos-mcp-upgrade-20261030T210500.log"
    log.write_text("a SelfUpgrade run\n")
    result = box.run(script=box.uninstaller())
    assert result.returncode == 0, output(result)
    assert not box.sudoers_file.exists() and not log.exists()
    assert "Removed" in output(result)
