"""The pending enrollments (#101): what boxes posted to the permanent endpoint, until dev1 decides.

The endpoint is public (README one-liner), so ANYONE can post. A registration only lands here, in
one 0600 file per registration under the gateway's state dir, and nothing is exposed until the
operator runs ``enroll-accept``. A registration holds the box's agent key, so a file lives at most
``PENDING_TTL_S`` (24 h) and is removed at accept or reject. The queue has a size cap so a flood
of posts cannot fill the disk.

A failed install posts its log tail to ``/enroll/log`` (``add_log``): plain text, newest
``MAX_LOGS`` kept, so a crash on a box nobody can read (dsg, #100) is visible on dev1.

Every value read back from a file is validated again (``parse_registration``): a hand-edited or
truncated file is skipped with a warning, never trusted. Ids are 8 hex characters and are checked
before they reach a path.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remoteos.gateway.enroll import EnrollError, Registration, parse_registration

logger = logging.getLogger("remoteos.gateway.enroll_queue")

DEFAULT_ENROLL_DIR = Path.home() / ".local" / "state" / "remoteos-gateway" / "enroll"
PENDING_TTL_S = 24 * 3600
LOG_TTL_S = 14 * 24 * 3600
MAX_PENDING = 50
MAX_PER_SOURCE = 5  # a site's new boxes share its public address; a flooder needs many addresses
MAX_LOGS = 100
_ID = re.compile(r"[0-9a-f]{8}")
_LOG_HOST = re.compile(r"[A-Za-z0-9._-]{1,64}")
_EXIT = re.compile(r"-?[0-9]{1,10}")
LOG_OSES = ("windows", "linux", "macos")


class QueueFull(EnrollError):
    """The queue holds ``MAX_PENDING`` registrations: the next one is refused until one is decided."""


@dataclass(frozen=True)
class Pending:
    id: str
    received_at: float
    source: str
    registration: Registration


@dataclass(frozen=True)
class LogEntry:
    id: str
    received_at: float
    source: str
    hostname: str
    os: str
    exit_code: str
    size: int


def id_ok(value: str) -> bool:
    return _ID.fullmatch(value) is not None


def source_text(value: object) -> str:
    """The poster's address as text: an IP literal, else ``unknown`` (it goes into a file and a U item)."""
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return "unknown"


def source_group(address: str) -> str:
    """Who a poster is for the limits: the IPv4 address, or the IPv6 /64 (one host holds a whole /64,
    so a per-address limit alone would not hold)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def log_meta(hostname: object, os_name: object, exit_code: object) -> tuple[str, str, str]:
    """The headers of a log post, validated: anything else becomes ``unknown`` / empty."""
    host = hostname if isinstance(hostname, str) and _LOG_HOST.fullmatch(hostname) else "unknown"
    system = os_name if isinstance(os_name, str) and os_name in LOG_OSES else "unknown"
    code = exit_code if isinstance(exit_code, str) and _EXIT.fullmatch(exit_code) else ""
    return host, system, code


def _registration_json(registration: Registration) -> dict:
    """The registration in the shape the box posted, so a read goes through ``parse_registration``."""
    return {"hostname": registration.hostname, "ips": list(registration.addresses), "port": registration.port,
            "version": registration.version, "os": registration.os, "account": registration.account,
            "auth_key": registration.key}


def _write_private(path: Path, data: dict) -> None:
    """Write *data* to *path* 0600 through a temp file and a rename: a reader sees all or nothing."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class EnrollQueue:
    """The pending registrations and the install logs under *root* (both 0700 folders)."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time):
        self.root = root.expanduser()
        self.pending_dir = self.root / "pending"
        self.logs_dir = self.root / "logs"
        self._clock = clock

    def _ensure(self) -> None:
        for folder in (self.root, self.pending_dir, self.logs_dir):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.chmod(0o700)

    # -- registrations -----------------------------------------------------------------------

    def _read_pending(self, path: Path) -> Pending | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            raise  # the queue cannot be read: never "empty" (check would clear the owner notice)
        except (OSError, ValueError) as exc:
            logger.warning("enroll queue: %s cannot be read (%s) — skipped", path.name, type(exc).__name__)
            return None
        try:
            entry = Pending(data["id"], float(data["received_at"]), source_text(data["source"]),
                            parse_registration(json.dumps(data["registration"]).encode()))
        except (ValueError, KeyError, TypeError, EnrollError) as exc:
            logger.warning("enroll queue: %s is not a pending registration (%s) — skipped", path.name,
                           type(exc).__name__)
            return None
        if entry.id != path.stem or not id_ok(entry.id):
            logger.warning("enroll queue: %s holds another id — skipped", path.name)
            return None
        return entry

    def _files(self, folder: Path) -> list[Path]:
        """The ``*.json`` files of *folder*; a folder that cannot be listed RAISES (``Path.glob``
        would answer "none", and an unreadable queue must never read as an empty one)."""
        if not folder.exists():
            return []
        with os.scandir(folder) as entries:
            return sorted(folder / entry.name for entry in entries
                          if entry.name.endswith(".json") and not entry.name.startswith("."))

    def pending(self) -> list[Pending]:
        """Every registration younger than ``PENDING_TTL_S``, oldest first; an expired one is deleted
        (``check`` calls this every run: the timer is the expiry clock). Raises ``OSError`` when the
        queue cannot be read."""
        now, kept = self._clock(), []
        for path in self._files(self.pending_dir):
            entry = self._read_pending(path)
            if entry is None:
                continue
            if now - entry.received_at > PENDING_TTL_S:
                logger.info("enroll queue: %s (%s) expired unaccepted — deleted", entry.id,
                            entry.registration.hostname)
                path.unlink(missing_ok=True)
                continue
            kept.append(entry)
        return sorted(kept, key=lambda entry: entry.received_at)

    def add(self, registration: Registration, source: str) -> tuple[Pending, bool]:
        """Queue *registration*; ``(entry, created)``. The same hostname + key again (a box's retry)
        is the entry already there. Raises ``QueueFull``."""
        self._ensure()
        current = self.pending()
        for entry in current:
            same = entry.registration.hostname == registration.hostname
            if same and secrets.compare_digest(entry.registration.key, registration.key):
                return entry, False
        if len(current) >= MAX_PENDING:
            raise QueueFull(f"the enrollment queue is full ({MAX_PENDING}): decide the pending ones first")
        group = source_group(source_text(source))
        if sum(1 for entry in current if source_group(entry.source) == group) >= MAX_PER_SOURCE:
            raise QueueFull(f"{MAX_PER_SOURCE} registrations from this address wait already")
        entry = Pending(secrets.token_hex(4), self._clock(), source_text(source), registration)
        _write_private(self.pending_dir / f"{entry.id}.json", {
            "id": entry.id, "received_at": entry.received_at, "source": entry.source,
            "registration": _registration_json(registration),
        })
        return entry, True

    def get(self, ref: str) -> Pending:
        """The registration with id *ref*, else the ONE with hostname *ref* (case-insensitive)."""
        entries = self.pending()
        if id_ok(ref):
            for entry in entries:
                if entry.id == ref:
                    return entry
        matches = [entry for entry in entries if entry.registration.hostname.lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            ids = ", ".join(entry.id for entry in matches)
            raise EnrollError(f"{len(matches)} pending registrations name that hostname ({ids}): pass the id")
        raise EnrollError("no pending registration with that id or hostname: see `remoteos-gateway enroll-list`")

    def remove(self, entry_id: str) -> bool:
        """Delete the registration *entry_id* (accepted or rejected); False when there is none."""
        if not id_ok(entry_id):
            return False
        path = self.pending_dir / f"{entry_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- install logs ------------------------------------------------------------------------

    def add_log(self, text: str, *, hostname: str, os_name: str, exit_code: str, source: str) -> str:
        """Keep one install log; the oldest beyond ``MAX_LOGS`` (or ``LOG_TTL_S``) are deleted."""
        self._ensure()
        log_id = secrets.token_hex(4)
        _write_private(self.logs_dir / f"{log_id}.json", {
            "id": log_id, "received_at": self._clock(), "source": source_text(source), "hostname": hostname,
            "os": os_name, "exit": exit_code, "text": text,
        })
        self._prune_logs()
        return log_id

    def _read_log(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["id"] != path.stem or not id_ok(data["id"]) or not isinstance(data["text"], str):
                raise ValueError("not a log record")
            float(data["received_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("enroll queue: %s is not an install log (%s) — skipped", path.name, type(exc).__name__)
            return None
        return data

    def _log_records(self) -> list[tuple[Path, dict]]:
        records = []
        for path in self._files(self.logs_dir):
            data = self._read_log(path)
            if data is not None:
                records.append((path, data))
        return sorted(records, key=lambda item: float(item[1]["received_at"]))

    def _prune_logs(self) -> None:
        records = self._log_records()
        now = self._clock()
        for index, (path, data) in enumerate(records):
            if index < len(records) - MAX_LOGS or now - float(data["received_at"]) > LOG_TTL_S:
                path.unlink(missing_ok=True)

    def logs(self) -> list[LogEntry]:
        """The kept install logs, oldest first."""
        out = []
        for _, data in self._log_records():
            host, system, code = log_meta(data.get("hostname"), data.get("os"), data.get("exit"))
            out.append(LogEntry(data["id"], float(data["received_at"]), source_text(data.get("source")), host,
                                system, code, len(data["text"])))
        return out

    def read_log(self, log_id: str) -> str:
        if id_ok(log_id):
            for path, data in self._log_records():
                if data["id"] == log_id:
                    return data["text"]
        raise EnrollError("no install log with that id: see `remoteos-gateway enroll-log`")
