"""The committed one-file installers are exactly their parts (#71, scripts/build_installers.py)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "build_installers.py"

_spec = importlib.util.spec_from_file_location("build_installers", GENERATOR)
assert _spec and _spec.loader
build_installers = importlib.util.module_from_spec(_spec)
sys.modules["build_installers"] = build_installers
_spec.loader.exec_module(build_installers)


def test_the_committed_installers_equal_their_parts():
    result = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["install-linux.sh", "install.ps1"])
def test_every_installer_is_generated_and_says_so(name):
    parts_dir, patterns = build_installers.INSTALLERS[name]
    parts = [p for pattern in patterns for p in sorted((ROOT / parts_dir).glob(pattern))]
    assert len(parts) > 1
    assert (ROOT / name).read_bytes() == b"".join(p.read_bytes() for p in parts)
    assert f"from {parts_dir}/" in (ROOT / name).read_text().split("\n\n")[0]


@pytest.mark.parametrize("name", ["enroll.ps1", "enroll-linux.sh", "enroll.sh"])
def test_every_enroll_wrapper_is_generated_says_so_and_has_no_placeholder_left(name):
    parts_dir, _ = build_installers.INSTALLERS[name]
    text = (ROOT / name).read_text()
    assert f"from {parts_dir}/" in text.split("\n\n")[0]
    assert "@@" not in text
    assert "https://enroll-dev1.newlevel.media/enroll/" in text


def test_the_two_bash_wrappers_differ_only_in_their_header():
    linux, macos = (ROOT / "enroll-linux.sh").read_text(), (ROOT / "enroll.sh").read_text()
    common = (ROOT / "installers/enroll-sh/50-run.sh").read_text().split("ENROLL_URL=")[0]
    assert common in linux and common in macos
    assert 'INSTALLER_NAME="install-linux.sh"' in linux and 'INSTALLER_NAME="install.sh"' in macos


def test_a_placeholder_with_no_value_fails_the_build(tree):
    part = tree / "installers/enroll-ps1/00-header.ps1"
    part.write_text(part.read_text() + "$x = '@@NO_SUCH_VALUE@@'\n")
    before = (tree / "enroll.ps1").read_bytes()
    with pytest.raises(SystemExit, match="NO_SUCH_VALUE"):
        build_installers.main([], root=tree)
    assert (tree / "enroll.ps1").read_bytes() == before


def test_a_changed_value_changes_all_three_wrappers(tree):
    values = tree / build_installers.VALUES
    values.write_text(values.read_text().replace("enroll-dev1.newlevel.media", "enroll-other.example"))
    assert build_installers.main(["--check"], root=tree) == 1
    assert build_installers.main([], root=tree) == 0
    for name in ("enroll.ps1", "enroll-linux.sh", "enroll.sh"):
        assert "https://enroll-other.example/enroll/" in (tree / name).read_text()


@pytest.fixture
def tree(tmp_path):
    """A copy of the parts + the installers, so a test can change them."""
    for name, (parts_dir, _) in build_installers.INSTALLERS.items():
        shutil.copytree(ROOT / parts_dir, tmp_path / parts_dir, dirs_exist_ok=True)
        shutil.copy2(ROOT / name, tmp_path / name)
    shutil.copy2(ROOT / build_installers.VALUES, tmp_path / build_installers.VALUES)
    return tmp_path


def test_check_fails_on_a_changed_part_and_changes_nothing(tree, capsys):
    part = sorted((tree / "installers/install-linux").glob("*.sh"))[1]
    part.write_text(part.read_text() + "# one more line\n")
    before = (tree / "install-linux.sh").read_bytes()
    assert build_installers.main(["--check"], root=tree) == 1
    assert "install-linux.sh" in capsys.readouterr().err
    assert (tree / "install-linux.sh").read_bytes() == before


def test_check_fails_on_a_hand_edited_installer(tree):
    target = tree / "install.ps1"
    target.write_text(target.read_text() + "# edited by hand\n")
    assert build_installers.main(["--check"], root=tree) == 1


def test_a_build_writes_the_parts_in_name_order(tree):
    parts_dir = tree / "installers/install-ps1"
    (parts_dir / "99-tail.ps1").write_text("# the tail\n")
    assert build_installers.main([], root=tree) == 0
    assert (tree / "install.ps1").read_text().endswith("# the tail\n")
    assert build_installers.main(["--check"], root=tree) == 0


def test_a_missing_parts_directory_is_an_error_never_an_empty_installer(tree):
    shutil.rmtree(tree / "installers/install-ps1")
    before = (tree / "install.ps1").read_bytes()
    with pytest.raises(SystemExit, match="holds no"):
        build_installers.main([], root=tree)
    assert (tree / "install.ps1").read_bytes() == before
