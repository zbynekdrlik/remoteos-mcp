"""Gateway access log — one JSON line per request, for audit (``remoteos-gateway log``).

Each line carries: time, consumer, machine, HTTP method, JSON-RPC ``method``,
``params.name`` for ``tools/call``, status, outcome and duration. It NEVER carries the
bearer token, the machine key, or any tool argument. The writer rotates the file to
``<name>.1`` once it passes ``max_bytes`` so an unattended service cannot fill the disk.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from pathlib import Path

logger = logging.getLogger("remoteos.gateway.accesslog")

DEFAULT_ACCESS_LOG_PATH = Path.home() / ".local" / "state" / "remoteos-gateway" / "access.jsonl"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


class AccessLog:
    def __init__(self, path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            logger.info("created access-log directory %s (0700)", self.path.parent)

    def write(self, record: dict) -> None:
        """Append *record* as one line. A write failure is logged, never raised to the request."""
        line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._rotate_if_needed()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as exc:
            logger.error("access log write to %s FAILED (%s); record was: %s", self.path, exc, line.decode().strip())

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size >= self.max_bytes:
            rotated = self.path.with_name(self.path.name + ".1")
            os.replace(self.path, rotated)
            logger.info("access log rotated: %s -> %s (%d bytes)", self.path, rotated, size)


def read_access_log(path: Path, consumer: str | None = None, tail: int = 20) -> list[dict]:
    """The last *tail* records (oldest first), optionally only for *consumer*.

    Reads the rotated ``.1`` file first, then the live one. Unparseable lines are skipped
    with a warning (a crash mid-write can leave a partial last line).
    """
    path = Path(path)
    keep: deque[dict] = deque(maxlen=max(tail, 0))
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            handle = candidate.open(encoding="utf-8")
        except FileNotFoundError:
            continue
        with handle:
            for lineno, raw in enumerate(handle, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("access log %s:%d is not valid JSON — skipped", candidate, lineno)
                    continue
                if consumer is not None and record.get("consumer") != consumer:
                    continue
                keep.append(record)
    return list(keep)
