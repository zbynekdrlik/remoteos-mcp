"""What ``sync-configs`` last wrote into each DIRECT project config (#109): ``sync-direct.json``.

A direct entry carries the key store's url, so once its machine leaves the key store nothing tells
it apart from a hand-made entry. This record — ``{project: {machine: url}}``, next to the registry,
urls only (never a key) — says which entries sync wrote, so the next sync removes a recorded entry
whose machine is gone while a hand-made one (never recorded) is kept, as gateway.md requires.

- Rewritten after every run that is not a dry run: a confirmed write or an unchanged config records
  exactly what the project now holds; a failed write keeps the UNION of the old record and the new
  entries (the file may hold either).
- First run with no record (or an unreadable one): a project's record is SEEDED from its config's
  entries whose url is the key store's url for that name. Those are sync's anyway, so the first run
  removes nothing extra; it only makes today's entries owned for the next removal.
- Read failures fall back to seeding: the safe direction (an entry is kept, never deleted wrongly).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from remoteos.gateway import fileops

logger = logging.getLogger("remoteos.gateway.direct_record")

RECORD_NAME = "sync-direct.json"

Record = dict[str, dict[str, str]]


def record_path(registry_path: Path) -> Path:
    return Path(registry_path).with_name(RECORD_NAME)


def load_record(path: Path) -> tuple[Record, bool]:
    """``(record, usable)``. Missing or unusable = ``({}, False)``: every direct project is seeded."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("direct record %s unreadable (%s) — seeding it again", path, exc.__class__.__name__)
        return {}, False
    if not isinstance(data, dict) or not all(
        isinstance(entries, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items())
        for entries in data.values()
    ):
        logger.warning("direct record %s is not {project: {machine: url}} — seeding it again", path)
        return {}, False
    return {project: dict(entries) for project, entries in data.items()}, True


def save_record(path: Path, record: Record) -> None:
    """Atomically, 0600 (``fileops.atomic_write``). Raises ``OSError``."""
    payload = json.dumps({p: dict(sorted(e.items())) for p, e in sorted(record.items())}, indent=2) + "\n"
    fileops.atomic_write(str(path), payload.encode("utf-8"))
    logger.info("direct record %s written: %d project(s)", path, len(record))
