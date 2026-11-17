#!/usr/bin/env python3
"""Build the one-file installers from their source parts (#71).

`curl ... | sudo bash` and `irm ... | iex` deliver ONE file, and camera-box installs pinned older
refs, so an installer cannot load a second file at run time. The parts live in `installers/`;
this script concatenates them, in file-name order, into the committed `install-linux.sh` and
`install.ps1`. Edit the parts, run this, commit both. `--check` changes nothing and exits 1 when
a committed file differs from its parts (`tests/test_installer_generated.py` runs it).

The permanent enrollment wrappers (#101) `enroll.ps1`, `enroll-linux.sh` and `enroll.sh` (the
README one-liners) are built the same way. Their `@@NAME@@` placeholders take the values of
`installers/enroll-values.conf` (the endpoint and dev1's public SSH key), so the three carry the
same values; a placeholder with no value fails the build.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALUES = "installers/enroll-values.conf"
# output file -> its parts directory and the parts' glob patterns (each sorted, in this order)
INSTALLERS = {
    "install-linux.sh": ("installers/install-linux", ("*.sh",)),
    "install.ps1": ("installers/install-ps1", ("*.ps1",)),
    "enroll.ps1": ("installers/enroll-ps1", ("*.ps1",)),
    "enroll-linux.sh": ("installers/enroll-sh", ("00-linux.sh", "50-*.sh")),
    "enroll.sh": ("installers/enroll-sh", ("00-macos.sh", "50-*.sh")),
}
_PLACEHOLDER = re.compile(rb"@@([A-Z_]+)@@")


def values(root: Path) -> dict[bytes, bytes]:
    """``NAME=value`` lines of the values file (``#`` lines are comments)."""
    out = {}
    for line in (root / VALUES).read_text(encoding="ascii").splitlines():
        if line.strip() and not line.startswith("#"):
            name, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Z_]+", name) or not value or "@@" in value:
                raise SystemExit(f"{VALUES}: not a NAME=value line: {name!r}")
            out[name.encode()] = value.encode()
    return out


def build(root: Path, parts_dir: str, patterns: tuple[str, ...]) -> bytes:
    """The installer's bytes: its parts in order, each placeholder replaced by its value."""
    parts = [part for pattern in patterns for part in sorted((root / parts_dir).glob(pattern))]
    if not parts:
        raise SystemExit(f"{parts_dir} holds no {', '.join(patterns)} part")
    content = b"".join(part.read_bytes() for part in parts)
    if _PLACEHOLDER.search(content) is None:
        return content
    known = values(root)

    def fill(match: re.Match) -> bytes:
        if match.group(1) not in known:
            raise SystemExit(f"{parts_dir}: @@{match.group(1).decode()}@@ has no value in {VALUES}")
        return known[match.group(1)]

    return _PLACEHOLDER.sub(fill, content)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only compare; exit 1 when a file differs")
    args = parser.parse_args(argv)
    stale = []
    for name, (parts_dir, patterns) in INSTALLERS.items():
        target, content = root / name, build(root, parts_dir, patterns)
        if target.exists() and target.read_bytes() == content:
            continue
        if args.check:
            stale.append(name)
        else:
            target.write_bytes(content)
            print(f"wrote {name}")
    if stale:
        print(f"{', '.join(stale)} differ from their parts: run scripts/build_installers.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
