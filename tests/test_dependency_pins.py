"""Regression tests for #12 — dependency pinning.

Asserts that every entry in [project].dependencies uses exact == pins
and that constraints.txt exists and parses as valid pip constraints.
"""

from __future__ import annotations

import pathlib
import re
import sys

# tomllib is stdlib from 3.11+; fall back to tomli for 3.10
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestDependencyPins:
    """Every direct dependency must be pinned to an exact version."""

    def _load_pyproject(self) -> dict:
        with open(ROOT / "pyproject.toml", "rb") as f:
            return tomllib.load(f)

    def test_all_direct_deps_are_exact_pins(self) -> None:
        """Each entry in [project].dependencies must use == (not >= or ~=)."""
        data = self._load_pyproject()
        deps: list[str] = data["project"]["dependencies"]

        for dep in deps:
            # Strip environment markers (everything after the first ;)
            spec = dep.split(";")[0].strip()
            # The version specifier must contain == and NOT >= or ~= or <=
            assert "==" in spec, (
                f"Dependency {dep!r} is not pinned to an exact version (expected ==)"
            )
            # Ensure it's not >= masquerading (e.g. someone writes >=X,==X)
            # The == must be the primary operator
            # Extract version part after package name
            match = re.match(r"^[A-Za-z0-9_\-\.\[\]]+\s*(.*)", spec)
            assert match, f"Cannot parse dependency spec: {dep!r}"
            version_part = match.group(1)
            assert version_part.startswith("=="), (
                f"Dependency {dep!r} version specifier {version_part!r} "
                f"does not start with == (must be an exact pin)"
            )

    def test_no_floor_pins_remain(self) -> None:
        """No dependency should use >= (the old unpinned floor style)."""
        data = self._load_pyproject()
        deps: list[str] = data["project"]["dependencies"]

        for dep in deps:
            spec = dep.split(";")[0].strip()
            assert ">=" not in spec, (
                f"Dependency {dep!r} still uses a >= floor — must be == pinned"
            )


class TestConstraintsFile:
    """constraints.txt must exist, be non-empty, and parse as valid pip constraints."""

    CONSTRAINTS_PATH = ROOT / "constraints.txt"

    def test_constraints_file_exists(self) -> None:
        assert self.CONSTRAINTS_PATH.exists(), (
            "constraints.txt is missing from the repo root"
        )

    def test_constraints_file_is_nonempty(self) -> None:
        content = self.CONSTRAINTS_PATH.read_text(encoding="utf-8")
        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(lines) > 0, "constraints.txt has no non-comment, non-empty lines"

    def test_constraints_file_has_pinned_entries(self) -> None:
        """Every non-comment line must be a valid constraint (package==version [; marker])."""
        content = self.CONSTRAINTS_PATH.read_text(encoding="utf-8")
        pin_pattern = re.compile(
            r"^[A-Za-z0-9_\-\.]+==[\d\.]+[A-Za-z0-9\.]*"
        )
        # Lines that are indented with spaces are "via" comments
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Indented lines (starting with spaces) are comments from uv
            if line.startswith("    "):
                continue
            assert pin_pattern.match(stripped), (
                f"constraints.txt line does not look like a valid pin: {stripped!r}"
            )

    def test_constraints_has_direct_deps(self) -> None:
        """constraints.txt must include at least the direct dependencies."""
        content = self.CONSTRAINTS_PATH.read_text(encoding="utf-8").lower()
        expected_packages = [
            "fastmcp",
            "psutil",
            "pillow",
            "click",
            "python-dotenv",
            "thefuzz",
            "tabulate",
            "markdownify",
        ]
        for pkg in expected_packages:
            assert pkg in content, (
                f"Direct dependency {pkg!r} not found in constraints.txt"
            )
