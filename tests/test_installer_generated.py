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
    parts_dir, suffix = build_installers.INSTALLERS[name]
    parts = sorted((ROOT / parts_dir).glob(f"*{suffix}"))
    assert len(parts) > 1
    assert (ROOT / name).read_bytes() == b"".join(p.read_bytes() for p in parts)
    assert f"from {parts_dir}/" in (ROOT / name).read_text().split("\n\n")[0]


@pytest.fixture
def tree(tmp_path):
    """A copy of the parts + the installers, so a test can change them."""
    for name, (parts_dir, _) in build_installers.INSTALLERS.items():
        shutil.copytree(ROOT / parts_dir, tmp_path / parts_dir)
        shutil.copy2(ROOT / name, tmp_path / name)
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
