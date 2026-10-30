"""File operations behind ``FileHash`` and ``FileRename``, the chunked ``put``'s last steps (#67).

``remoteos-call put`` uploads to ``<remote>.part``, checks it with ``hash_file`` and moves it over
the target with ``replace_file``, so a failed run never touches the target.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_READ_BYTES = 1024 * 1024


def hash_file(path: Path) -> tuple[str, int]:
    """The file's sha256 (hex) and size, read in pieces: a ``.deb`` must not sit in memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while piece := handle.read(_READ_BYTES):
            digest.update(piece)
            size += len(piece)
    return digest.hexdigest(), size


def _take_over_attributes(source: Path, target: str) -> None:
    """Give *source* what an in-place write would have left on an existing *target*: its mode
    (a 0600 file must not widen to the upload's default) and, as root, its owner."""
    try:
        existing = os.stat(target)
    except FileNotFoundError:
        return
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(source, existing.st_uid, existing.st_gid)  # first: chown clears the setuid bit
    os.chmod(source, stat.S_IMODE(existing.st_mode))


def replace_file(source: Path, destination: str) -> None:
    """Move *source* over *destination* in one step, with its data on disk first.

    A symlink destination is written through, like the plain write ``put`` used to be, so the link
    stays. Both ends must be on one filesystem (``os.replace``); otherwise the call fails and the
    destination is untouched.
    """
    target = os.path.realpath(destination)
    if os.path.isdir(target):
        raise IsADirectoryError(f"{destination} is a directory")
    with source.open("r+b") as handle:  # before the mode: Windows cannot open a read-only file for this
        os.fsync(handle.fileno())
    _take_over_attributes(source, target)
    os.replace(source, target)
