"""File operations behind ``FileHash`` and ``FileRename``, the chunked ``put``'s last steps (#67).

``remoteos-call put`` uploads to ``<remote>.part``, checks it with ``hash_file`` and moves it over
the target with ``replace_file``, so a failed run never touches the target.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from pathlib import Path

log = logging.getLogger("remoteos.file_transfer")

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


def start_file_like(path: Path, model: str) -> None:
    """Start *path* empty with the permission bits of an existing *model* file.

    The chunks of a put over a 0600 file must not sit in a 0644 ``.part``. A leftover file goes
    first, whatever its mode: a read-only ``.part`` would refuse the next put for good. The owner
    keeps write access until ``replace_file`` applies the final mode (the chunks still follow).
    """
    if os.path.isdir(model):
        raise IsADirectoryError(f"{model} is a directory")
    path.unlink(missing_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(model).st_mode) | stat.S_IWUSR
    except FileNotFoundError:
        return  # a new target: the upload's own default applies
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode))


def _take_over_attributes(source: Path, target: str) -> None:
    """Give *source* what an in-place write would have left on an existing *target*: its mode, its
    group and, as root, its owner. A group the server's account is not in cannot be kept: logged."""
    try:
        existing = os.stat(target)
    except FileNotFoundError:
        return
    if hasattr(os, "chown"):  # before chmod: chown clears the setuid bit
        as_root = os.geteuid() == 0
        try:
            os.chown(source, existing.st_uid if as_root else -1, existing.st_gid)
        except PermissionError:
            if as_root:
                raise
            log.warning("FileRename could not keep the target's group")  # never the path (#31)
    os.chmod(source, stat.S_IMODE(existing.st_mode))


def _sync_directory(directory: str) -> None:
    """Make the rename durable (POSIX; Windows cannot open a directory). The file itself is
    complete and in place, so a failure here is logged, not fatal."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        log.debug("directory fsync skipped: %s", type(exc).__name__)


def replace_file(source: Path, destination: str) -> None:
    """Move *source* over *destination* in one step, with its data on disk first.

    A symlink destination is written through, like the plain write ``put`` used to be, so the link
    stays. A destination this account may not write is refused, as that write was. Both ends must
    be on one filesystem (``os.replace``); otherwise the call fails and the destination is
    untouched. It is a new file: ACLs and extended attributes of the old one are not carried over.
    """
    target = os.path.realpath(destination)
    if os.path.isdir(target):
        raise IsADirectoryError(f"{destination} is a directory")
    if os.path.exists(target) and not os.access(target, os.W_OK):
        raise PermissionError(f"{destination} is not writable")
    with source.open("r+b") as handle:  # before the mode: Windows cannot open a read-only file for this
        os.fsync(handle.fileno())
    _take_over_attributes(source, target)
    os.replace(source, target)
    _sync_directory(os.path.dirname(target))
