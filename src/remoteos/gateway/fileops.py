"""Private files: atomic 0600 writes, dated backups, an exclusive lock (#51).

STDLIB ONLY, Python 3.8+, no package imports: besides being imported by the gateway, this
module's SOURCE is what ``targets.py`` runs on another account (``sudo -n -u <account>
python3 -c <source> ...``) or another host (``ssh <account>@<host> python3 -c <source>
...``). So a config file is written by the same code on dev1, under another account and on
dev2. File contents travel on stdin/stdout only, never in an argv (``ps`` shows argvs).

Command line (``python3 -c <source> <op> ...``):

- ``read <path>``        — the file's bytes on stdout; exit 3 when it does not exist.
- ``write <path> <tag>`` — stdin becomes the file (atomic, 0600). An existing file is first
  copied to ``<dir>/.backup-<tag>/<name>`` (0600, dir 0700; ``<name>.1``, ``.2``... when that
  backup exists already). Prints ``{"path": ..., "backup": ...}`` as JSON.

``~`` means the home of the account the code runs as (from the password database, not
``$HOME``, which ``sudo`` may keep from the caller). A symlinked path is written through to
its target, so the symlink a project's ``.mcp.json`` is stays a symlink.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
EXIT_MISSING = 3

_TAG_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def expand(path: str) -> str:
    """Absolute, ``~``-expanded (for the CURRENT account) and symlink-resolved *path*."""
    if path == "~" or path.startswith("~/"):
        import pwd  # POSIX only; imported here so Windows can still import this module

        path = pwd.getpwuid(os.getuid()).pw_dir + path[1:]
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute or start with ~/: {path!r}")
    return os.path.realpath(path)


def ensure_private_dir(directory: str) -> bool:
    """Create *directory* (and parents) as 0700 when missing. Returns True when it created it.

    An existing directory's mode is left alone: it may be shared on purpose.
    """
    if os.path.isdir(directory):
        return False
    os.makedirs(directory, mode=PRIVATE_DIR_MODE, exist_ok=True)
    os.chmod(directory, PRIVATE_DIR_MODE)  # makedirs' mode is filtered by the umask
    return True


def atomic_write(path: str, data: bytes) -> None:
    """Replace *path* with *data* atomically: a 0600 temp file in the same directory, fsync,
    rename, fsync the directory. A reader sees the old file or the new one, never a mix."""
    directory = os.path.dirname(path)
    ensure_private_dir(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_bytes(path: str):
    """The file's bytes, or None when it does not exist."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def backup(path: str, tag: str):
    """Copy an existing *path* into ``.backup-<tag>/`` next to it. Returns the copy's path or None."""
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"backup tag must be YYYY-MM-DD, got {tag!r}")
    data = read_bytes(path)
    if data is None:
        return None
    backup_dir = os.path.join(os.path.dirname(path), ".backup-" + tag)
    ensure_private_dir(backup_dir)
    name = os.path.basename(path)
    target = os.path.join(backup_dir, name)
    suffix = 0
    while os.path.exists(target):  # never overwrite an earlier backup of the same day
        suffix += 1
        target = os.path.join(backup_dir, f"{name}.{suffix}")
    atomic_write(target, data)
    return target


def write_with_backup(path: str, data: bytes, tag: str) -> dict:
    """Back *path* up, then replace it atomically. Returns ``{"path", "backup"}``."""
    saved = backup(path, tag)
    atomic_write(path, data)
    return {"path": path, "backup": saved}


@contextlib.contextmanager
def exclusive_lock(lock_path: str):
    """Hold an exclusive ``flock`` on *lock_path* (created 0600). Edits of one file serialise on it."""
    ensure_private_dir(os.path.dirname(lock_path))
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
    try:
        try:
            import fcntl
        except ImportError:  # non-POSIX: the gateway is a dev1 (Linux) service
            yield
            return
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main(argv) -> int:
    if len(argv) == 3 and argv[1] == "read":
        data = read_bytes(expand(argv[2]))
        if data is None:
            return EXIT_MISSING
        sys.stdout.buffer.write(data)
        return 0
    if len(argv) == 4 and argv[1] == "write":
        result = write_with_backup(expand(argv[2]), sys.stdin.buffer.read(), argv[3])
        sys.stdout.write(json.dumps(result) + "\n")
        return 0
    sys.stderr.write("usage: read <path> | write <path> <YYYY-MM-DD>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
