#!/usr/bin/env python3
"""Build the one-file installers from their source parts (#71).

`curl ... | sudo bash` and `irm ... | iex` deliver ONE file, and camera-box installs pinned older
refs, so an installer cannot load a second file at run time. The parts live in `installers/`;
this script concatenates them, in file-name order, into the committed `install-linux.sh` and
`install.ps1`. Edit the parts, run this, commit both. `--check` changes nothing and exits 1 when
a committed file differs from its parts (`tests/test_installer_generated.py` runs it).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# output file -> its parts directory and the parts' suffix
INSTALLERS = {
    "install-linux.sh": ("installers/install-linux", ".sh"),
    "install.ps1": ("installers/install-ps1", ".ps1"),
}


def build(root: Path, parts_dir: str, suffix: str) -> bytes:
    """The installer's bytes: its parts in file-name order, unchanged."""
    parts = sorted((root / parts_dir).glob(f"*{suffix}"))
    if not parts:
        raise SystemExit(f"{parts_dir} holds no *{suffix} part")
    return b"".join(part.read_bytes() for part in parts)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only compare; exit 1 when a file differs")
    args = parser.parse_args(argv)
    stale = []
    for name, (parts_dir, suffix) in INSTALLERS.items():
        target, content = root / name, build(root, parts_dir, suffix)
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
