"""Gateway registry (who may reach which machine) and the master MCP config reader.

Two files feed the gateway, both on dev1, neither in git:

- The **registry** (``~/.config/remoteos-gateway/registry.json``, 0600) — owned by this
  project, edited only through the ``remoteos-gateway`` CLI::

      {
        "exposed": ["win-pjsnvs-pokladna"],
        "consumers": {
          "david3@subdev": {"sha256": ["<64 hex>"], "machines": ["win-pjsnvs-pokladna"],
                            "added": "2026-10-14"}
        }
      }

  A consumer is known ONLY by the sha256 of its bearer token; the raw token never reaches
  this box.

- The **machine source** (``~/.claude/mcp-configs/remoteos-mcp.mcp.json``, 0600) — the
  master MCP config every project already symlinks. The gateway READS it to resolve
  ``machine name -> upstream url + Authorization``; it never copies a key anywhere.

``LiveConfig`` holds the last good copy of both and reloads them when a file changes (or on
SIGHUP). A file that turns invalid is never applied: the last good copy stays and an ERROR is
logged. A deleted registry means an EMPTY registry (deny everything) — never "allow all".
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("remoteos.gateway.registry")

DEFAULT_REGISTRY_PATH = Path.home() / ".config" / "remoteos-gateway" / "registry.json"
DEFAULT_MACHINES_PATH = Path.home() / ".claude" / "mcp-configs" / "remoteos-mcp.mcp.json"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONSUMER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_REGISTRY_KEYS = frozenset({"exposed", "consumers"})
_CONSUMER_KEYS = frozenset({"sha256", "machines", "added"})


class RegistryError(Exception):
    """The registry file is unreadable/invalid, or a requested registry edit is refused."""


class MachineSourceError(Exception):
    """The master MCP config (machine source) is unreadable or invalid."""


def normalize_hash(value: str) -> str:
    """Return *value* as a lowercase 64-hex sha256 digest, or raise ``RegistryError``.

    This is the guard that keeps a RAW token from ever being registered by mistake: only
    something that already looks like a sha256 hex digest is accepted.
    """
    candidate = value.strip().lower()
    if not _HASH_RE.match(candidate):
        raise RegistryError(
            "--sha256 must be a sha256 hex digest (exactly 64 hex characters), "
            f"got {len(value.strip())} characters. Register the HASH of the token "
            '(printf %s "$TOKEN" | sha256sum), never the token itself.'
        )
    return candidate


def validate_consumer_name(name: str) -> str:
    if not _CONSUMER_RE.match(name):
        raise RegistryError(f"invalid consumer name {name!r}: use letters, digits and . _ @ - (e.g. david3@subdev)")
    return name


def validate_machine_name(name: str) -> str:
    if not _MACHINE_RE.match(name):
        raise RegistryError(f"invalid machine name {name!r}: use letters, digits and . _ -")
    return name


def token_sha256(token: str) -> str:
    """sha256 hex digest of the exact token bytes (UTF-8, no trailing newline)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Consumer:
    sha256: list[str] = field(default_factory=list)
    machines: list[str] = field(default_factory=list)
    added: str = ""

    def to_json(self) -> dict:
        return {"sha256": list(self.sha256), "machines": list(self.machines), "added": self.added}


@dataclass
class Registry:
    exposed: list[str] = field(default_factory=list)
    consumers: dict[str, Consumer] = field(default_factory=dict)

    # -- serialisation -----------------------------------------------------------------

    @classmethod
    def from_json(cls, data: object) -> Registry:
        """Strictly validate parsed JSON. Any problem raises ``RegistryError``."""
        if not isinstance(data, dict):
            raise RegistryError("registry top level must be a JSON object")
        unknown = set(data) - _REGISTRY_KEYS
        if unknown:
            raise RegistryError(f"registry has unknown top-level keys: {sorted(unknown)}")

        exposed = data.get("exposed", [])
        if not isinstance(exposed, list) or not all(isinstance(m, str) for m in exposed):
            raise RegistryError("registry 'exposed' must be a list of machine names")
        for machine in exposed:
            validate_machine_name(machine)

        consumers_raw = data.get("consumers", {})
        if not isinstance(consumers_raw, dict):
            raise RegistryError("registry 'consumers' must be an object")

        consumers: dict[str, Consumer] = {}
        hash_owner: dict[str, str] = {}
        for name, raw in consumers_raw.items():
            validate_consumer_name(name)
            if not isinstance(raw, dict):
                raise RegistryError(f"consumer {name!r} must be an object")
            unknown = set(raw) - _CONSUMER_KEYS
            if unknown:
                raise RegistryError(f"consumer {name!r} has unknown keys: {sorted(unknown)}")
            hashes = raw.get("sha256", [])
            if not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
                raise RegistryError(f"consumer {name!r}: 'sha256' must be a list of hex digests")
            for digest in hashes:
                if not _HASH_RE.match(digest):
                    raise RegistryError(f"consumer {name!r}: {digest[:12]!r}... is not a lowercase sha256 hex digest")
                if digest in hash_owner:
                    raise RegistryError(
                        f"token hash {digest[:12]}... is registered for both {hash_owner[digest]!r} and {name!r}"
                    )
                hash_owner[digest] = name
            machines = raw.get("machines", [])
            if not isinstance(machines, list) or not all(isinstance(m, str) for m in machines):
                raise RegistryError(f"consumer {name!r}: 'machines' must be a list of machine names")
            for machine in machines:
                validate_machine_name(machine)
            added = raw.get("added", "")
            if not isinstance(added, str):
                raise RegistryError(f"consumer {name!r}: 'added' must be a string")
            consumers[name] = Consumer(sha256=list(hashes), machines=list(machines), added=added)

        return cls(exposed=list(exposed), consumers=consumers)

    def to_json(self) -> dict:
        return {
            "exposed": list(self.exposed),
            "consumers": {name: consumer.to_json() for name, consumer in sorted(self.consumers.items())},
        }

    # -- auth --------------------------------------------------------------------------

    def authenticate(self, token: str) -> str | None:
        """Return the consumer whose registered hash matches *token*, else ``None``.

        Every registered hash is compared with ``hmac.compare_digest`` (no early exit), so
        the time taken does not depend on which hash — if any — matched.
        """
        digest = token_sha256(token)
        match: str | None = None
        for name, consumer in self.consumers.items():
            for registered in consumer.sha256:
                if hmac.compare_digest(digest, registered):
                    match = name
        return match

    # -- edits (used by the CLI; each raises RegistryError with an operator message) ---

    def add_consumer(self, name: str, sha256: str, *, today: str | None = None) -> bool:
        """Register *sha256* for *name* (creating the consumer). Returns False if already there."""
        validate_consumer_name(name)
        digest = normalize_hash(sha256)
        for other, consumer in self.consumers.items():
            if other != name and digest in consumer.sha256:
                raise RegistryError(f"this token hash is already registered for consumer {other!r}")
        consumer = self.consumers.get(name)
        if consumer is None:
            consumer = Consumer(added=today or _dt.date.today().isoformat())
            self.consumers[name] = consumer
        if digest in consumer.sha256:
            return False
        consumer.sha256.append(digest)
        return True

    def revoke_consumer(self, name: str, sha256: str | None = None) -> str:
        """Remove the whole consumer, or just one of its token hashes. Returns what was done."""
        consumer = self._require_consumer(name)
        if sha256 is None:
            del self.consumers[name]
            return (
                f"consumer {name!r} removed "
                f"({len(consumer.sha256)} token hash(es), {len(consumer.machines)} machine(s))"
            )
        digest = normalize_hash(sha256)
        if digest not in consumer.sha256:
            raise RegistryError(f"consumer {name!r} has no token hash {digest[:12]}...")
        consumer.sha256.remove(digest)
        return f"token hash {digest[:12]}... removed from {name!r} ({len(consumer.sha256)} left)"

    def allow(self, consumer_name: str, machine: str) -> bool:
        validate_machine_name(machine)
        consumer = self._require_consumer(consumer_name)
        if machine in consumer.machines:
            return False
        consumer.machines.append(machine)
        return True

    def deny(self, consumer_name: str, machine: str) -> bool:
        consumer = self._require_consumer(consumer_name)
        if machine not in consumer.machines:
            return False
        consumer.machines.remove(machine)
        return True

    def expose(self, machine: str) -> bool:
        validate_machine_name(machine)
        if machine in self.exposed:
            return False
        self.exposed.append(machine)
        return True

    def unexpose(self, machine: str) -> bool:
        if machine not in self.exposed:
            return False
        self.exposed.remove(machine)
        return True

    def _require_consumer(self, name: str) -> Consumer:
        consumer = self.consumers.get(name)
        if consumer is None:
            raise RegistryError(f"unknown consumer {name!r} (see `remoteos-gateway list`)")
        return consumer


# -- registry file I/O ----------------------------------------------------------------


def load_registry(path: Path) -> Registry:
    """Load and validate the registry. A MISSING file is an empty registry (deny all)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("registry %s does not exist — empty registry, every request is denied", path)
        return Registry()
    except OSError as exc:
        raise RegistryError(f"cannot read registry {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry {path} is not valid JSON: {exc}") from exc
    registry = Registry.from_json(data)
    _warn_if_loose_permissions(path)
    return registry


def save_registry(path: Path, registry: Registry) -> None:
    """Atomically write the registry with mode 0600 (its directory 0700 when we create it)."""
    _ensure_private_dir(path.parent)
    payload = json.dumps(registry.to_json(), indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    logger.info("registry %s written: %d exposed, %d consumer(s)", path, len(registry.exposed), len(registry.consumers))


@contextlib.contextmanager
def edit_registry(path: Path) -> Iterator[Registry]:
    """Lock, load, yield for editing, then save. Concurrent CLI edits serialise on the lock."""
    _ensure_private_dir(path.parent)
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock(lock_fd)
        registry = load_registry(path)
        yield registry
        save_registry(path, registry)
    finally:
        os.close(lock_fd)


def _lock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # non-POSIX: the gateway is a dev1 (Linux) service; no lock there
        logger.debug("fcntl unavailable — registry edit proceeds without a file lock")
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def _ensure_private_dir(directory: Path) -> None:
    """Create *directory* as 0700 if missing. An existing directory's mode is left alone."""
    if directory.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    logger.info("created private directory %s (0700)", directory)


def _warn_if_loose_permissions(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        logger.warning("registry %s has mode %o — expected 0600 (chmod 600 it)", path, mode)


# -- machine source (master MCP config, read-only) ------------------------------------


@dataclass(frozen=True)
class Upstream:
    """Where a machine's MCP lives. ``authorization`` is the machine key header — never logged."""

    url: str
    authorization: str | None = field(default=None, repr=False)


def load_machines(path: Path) -> dict[str, Upstream]:
    """Read ``machine name -> Upstream`` from the master MCP config (``mcpServers``).

    Entries without an http(s) URL are skipped with a warning (the file is shared with
    other tooling); a file that is unreadable, not JSON, or has no ``mcpServers`` object
    raises ``MachineSourceError``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MachineSourceError(f"cannot read machine source {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MachineSourceError(f"machine source {path} is not valid JSON: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise MachineSourceError(f"machine source {path} has no 'mcpServers' object")

    machines: dict[str, Upstream] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            logger.warning("machine source: entry %r is not an object — skipped", name)
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not _is_http_url(url):
            logger.warning("machine source: entry %r has no http(s) url — skipped", name)
            continue
        headers = entry.get("headers") or {}
        authorization = None
        if isinstance(headers, dict):
            for key, value in headers.items():
                if isinstance(key, str) and key.lower() == "authorization" and isinstance(value, str):
                    authorization = value
        if authorization is None:
            logger.warning("machine source: entry %r has no Authorization header — forwarded without a key", name)
        machines[name] = Upstream(url=url, authorization=authorization)
    return machines


def _is_http_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# -- live view with reload ------------------------------------------------------------


def file_signature(path: Path) -> tuple[int, int, int] | None:
    """(mtime_ns, size, inode) — changes on every write, including an atomic replace."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class LiveConfig:
    """The registry + machine source as the running gateway sees them.

    ``snapshot()`` re-stats both files and reloads whichever changed; ``reload(force=True)``
    is what SIGHUP calls. Construction fails loudly (raises) when either file is invalid —
    the gateway refuses to start on a broken config; after that a broken file only ever
    keeps the last good copy.
    """

    def __init__(self, registry_path: Path, machines_path: Path) -> None:
        self.registry_path = Path(registry_path)
        self.machines_path = Path(machines_path)
        self._registry_sig = file_signature(self.registry_path)
        self._registry = load_registry(self.registry_path)
        self._machines_sig = file_signature(self.machines_path)
        self._machines = load_machines(self.machines_path)
        logger.info(
            "config loaded: registry=%s (%d exposed: %s; %d consumer(s): %s) machines=%s (%d entries)",
            self.registry_path,
            len(self._registry.exposed),
            ",".join(self._registry.exposed) or "-",
            len(self._registry.consumers),
            ",".join(sorted(self._registry.consumers)) or "-",
            self.machines_path,
            len(self._machines),
        )
        for machine in self._registry.exposed:
            if machine not in self._machines:
                logger.error("exposed machine %r is missing from machine source %s", machine, self.machines_path)

    def snapshot(self) -> tuple[Registry, dict[str, Upstream]]:
        self.reload()
        return self._registry, self._machines

    def reload(self, force: bool = False) -> None:
        sig = file_signature(self.registry_path)
        if force or sig != self._registry_sig:
            self._registry_sig = sig
            try:
                registry = load_registry(self.registry_path)
            except RegistryError as exc:
                logger.error("registry reload FAILED — keeping the last good registry: %s", exc)
            else:
                self._registry = registry
                logger.info(
                    "registry reloaded%s: %d exposed (%s), %d consumer(s) (%s)",
                    " (forced)" if force else "",
                    len(registry.exposed),
                    ",".join(registry.exposed) or "-",
                    len(registry.consumers),
                    ",".join(sorted(registry.consumers)) or "-",
                )

        sig = file_signature(self.machines_path)
        if force or sig != self._machines_sig:
            self._machines_sig = sig
            try:
                machines = load_machines(self.machines_path)
            except MachineSourceError as exc:
                logger.error("machine source reload FAILED — keeping the last good copy: %s", exc)
            else:
                self._machines = machines
                logger.info("machine source reloaded: %d entries", len(machines))
