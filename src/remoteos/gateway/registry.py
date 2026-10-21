"""Gateway registry (who may reach which machine) and the machine source reader.

Two files feed the gateway, both on dev1, neither in git:

- The **registry** (``~/.config/remoteos-gateway/registry.json``, 0600) — owned by this
  project, edited only through the ``remoteos-gateway`` CLI::

      {
        "exposed": ["win-pjsnvs-pokladna"],
        "consumers": {
          "david3@subdev": {"sha256": ["<64 hex>"], "machines": ["win-pjsnvs-pokladna"],
                            "added": "2026-10-14"},
          "camera-box@dev1": {"sha256": ["<64 hex>"], "machines": ["linux-cam1"],
                              "added": "2026-10-21", "managed_by": "sync-configs"}
        }
      }

  A consumer is known ONLY by the sha256 of its bearer token; the raw token never reaches
  this box. ``managed_by`` marks a consumer that ``remoteos-gateway sync-configs`` owns (#51).

- The **machine source** is the KEY STORE (``~/.config/remoteos-gateway/machines.json``,
  0600, #51): ``{"linux-cam1": {"url": "http://cam1.lan:8092/mcp", "key": "<key>"}}``. It
  resolves ``machine name -> upstream url + key``; ``machine set-key`` edits it, and the
  running gateway picks the edit up on its next request. For the transition the old shape, an
  MCP config with ``mcpServers`` (``~/.claude/mcp-configs/remoteos-mcp.mcp.json``), still loads.

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
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from remoteos.gateway import fileops

logger = logging.getLogger("remoteos.gateway.registry")

DEFAULT_REGISTRY_PATH = Path.home() / ".config" / "remoteos-gateway" / "registry.json"
DEFAULT_MACHINES_PATH = Path.home() / ".config" / "remoteos-gateway" / "machines.json"  # the key store
LEGACY_MACHINES_PATH = Path.home() / ".claude" / "mcp-configs" / "remoteos-mcp.mcp.json"
SYNC_MANAGER = "sync-configs"  # ``managed_by`` of a consumer that sync-configs owns
DEFAULT_REPAIR_KEY_FILE = "/etc/remoteos-mcp/config.json"  # the Linux installer layout (#12)

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_CONSUMER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
_MACHINE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# RFC 7235 token68: what can follow "Bearer " in a header. Machine keys are letters and digits
# (the installers) or token_urlsafe output (win-stream-snv); a space or "Bearer " is refused.
_KEY_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,256}")
_SSH_DEST_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}")

_REGISTRY_KEYS = frozenset({"exposed", "consumers"})
_CONSUMER_KEYS = frozenset({"sha256", "machines", "added", "managed_by"})
_KEYSTORE_ENTRY_KEYS = frozenset({"url", "key", "repair"})
_REPAIR_KEYS = frozenset({"ssh", "key_file", "sudo"})


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
    if not _HASH_RE.fullmatch(candidate):
        raise RegistryError(
            "--sha256 must be a sha256 hex digest (exactly 64 hex characters), "
            f"got {len(value.strip())} characters. Register the HASH of the token "
            '(printf %s "$TOKEN" | sha256sum), never the token itself.'
        )
    return candidate


def validate_consumer_name(name: str) -> str:
    if not _CONSUMER_RE.fullmatch(name):
        raise RegistryError(f"invalid consumer name {name!r}: use letters, digits and . _ @ - (e.g. david3@subdev)")
    return name


def is_valid_machine_name(name: str) -> bool:
    """Letters, digits and ``. _ -`` only — so a machine name is always safe to log and route."""
    return _MACHINE_RE.fullmatch(name) is not None


def validate_machine_name(name: str) -> str:
    if not is_valid_machine_name(name):
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
    managed_by: str = ""  # "" = edited by hand; SYNC_MANAGER = owned by sync-configs (#51)

    def to_json(self) -> dict:
        data = {"sha256": list(self.sha256), "machines": list(self.machines), "added": self.added}
        if self.managed_by:  # omitted otherwise: a hand-edited consumer keeps the #25 shape
            data["managed_by"] = self.managed_by
        return data


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
                if not _HASH_RE.fullmatch(digest):
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
            managed_by = raw.get("managed_by", "")
            if managed_by not in ("", SYNC_MANAGER):
                raise RegistryError(f"consumer {name!r}: 'managed_by' must be {SYNC_MANAGER!r} when present")
            consumers[name] = Consumer(
                sha256=list(hashes), machines=list(machines), added=added, managed_by=managed_by
            )

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
        self._refuse_if_managed(name)
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
        self._refuse_if_managed(consumer_name)
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

    def _refuse_if_managed(self, name: str) -> None:
        """A hand edit must never widen a consumer sync-configs owns (its allow-list = the manifest)."""
        consumer = self.consumers.get(name)
        if consumer is not None and consumer.managed_by == SYNC_MANAGER:
            raise RegistryError(
                f"consumer {name!r} is managed by sync-configs: its token and machines come from projects.toml"
                " — edit that file and run `remoteos-gateway sync-configs`"
            )

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
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(f"cannot read registry {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise RegistryError(f"registry {path} is not valid JSON: {exc}") from exc
    registry = Registry.from_json(data)
    warn_if_loose_permissions(path)
    return registry


def save_registry(path: Path, registry: Registry) -> None:
    """Atomically write the registry with mode 0600 (its directory 0700 when we create it)."""
    _ensure_private_dir(path.parent)
    payload = json.dumps(registry.to_json(), indent=2, sort_keys=False) + "\n"
    fileops.atomic_write(str(path), payload.encode("utf-8"))
    logger.info("registry %s written: %d exposed, %d consumer(s)", path, len(registry.exposed), len(registry.consumers))


@dataclass
class RegistryTransaction:
    """The registry, loaded under its lock. ``save()`` may run more than once: sync-configs
    saves between the steps of a token change, so no step leaves a written token unknown."""

    path: Path
    registry: Registry

    def save(self) -> None:
        save_registry(self.path, self.registry)


@contextlib.contextmanager
def registry_transaction(path: Path) -> Iterator[RegistryTransaction]:
    """Hold the registry lock while the caller loads, edits and saves it."""
    _ensure_private_dir(path.parent)
    with fileops.exclusive_lock(str(path.with_name(path.name + ".lock"))):
        yield RegistryTransaction(path=path, registry=load_registry(path))


@contextlib.contextmanager
def edit_registry(path: Path) -> Iterator[Registry]:
    """Lock, load, yield for editing, then save. Concurrent CLI edits serialise on the lock."""
    with registry_transaction(path) as transaction:
        yield transaction.registry
        transaction.save()


def _ensure_private_dir(directory: Path) -> None:
    """Create *directory* as 0700 if missing. An existing directory's mode is left alone."""
    if fileops.ensure_private_dir(str(directory)):
        logger.info("created private directory %s (0700)", directory)


def warn_if_loose_permissions(path: Path, what: str = "registry") -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        logger.warning("%s %s has mode %o — expected 0600 (chmod 600 it)", what, path, mode)


# -- machine source (the key store, or the legacy master MCP config) -------------------


@dataclass(frozen=True)
class Upstream:
    """Where a machine's MCP lives. ``authorization`` is the machine key header — never logged."""

    url: str
    authorization: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RepairRecipe:
    """How ``check`` may re-read a machine's real key after a 401 (#51), set explicitly per machine.

    ``ssh -o BatchMode=yes <ssh> [sudo -n] cat <key_file>`` must print the installer's
    ``config.json`` (``{"auth_key": ...}``). Without a recipe a 401 is only reported.
    """

    ssh: str
    key_file: str = DEFAULT_REPAIR_KEY_FILE
    sudo: bool = False

    def to_json(self) -> dict:
        return {"ssh": self.ssh, "key_file": self.key_file, "sudo": self.sudo}


@dataclass(frozen=True)
class MachineEntry:
    """One key-store entry. ``key`` is the machine's bearer key — never logged, never printed."""

    url: str
    key: str = field(repr=False)
    repair: RepairRecipe | None = None

    @property
    def authorization(self) -> str:
        return f"Bearer {self.key}"

    def to_json(self) -> dict:
        data: dict = {"url": self.url, "key": self.key}
        if self.repair is not None:
            data["repair"] = self.repair.to_json()
        return data


def validate_key(key: str, *, what: str = "key") -> str:
    """Return *key* when it can follow ``Bearer `` in a header; never echo it in the error."""
    if not _KEY_RE.fullmatch(key):
        raise MachineSourceError(
            f"{what} must be 1-256 characters of letters, digits and ._~+/=- (got {len(key)} characters;"
            " give the key alone, without 'Bearer ', spaces or a line break)"
        )
    return key


def validate_http_url(url: str, *, what: str = "url") -> str:
    """Return *url* when it is a printable http(s) url. The error never repeats the value: a key
    pasted into a url field by a hand edit would otherwise reach a log line or a notification."""
    if not isinstance(url, str) or not _is_http_url(url) or not url.isprintable():
        if isinstance(url, str):
            scheme = urlsplit(url).scheme if url.isprintable() else ""
            got = f"{len(url)} characters, " + (f"scheme {scheme!r}" if scheme.isalnum() else "no http(s) scheme")
        else:
            got = type(url).__name__
        raise MachineSourceError(f"{what} must be an http(s) url (got {got})")
    return url


def parse_repair(raw: object, name: str) -> RepairRecipe:
    if not isinstance(raw, dict) or set(raw) - _REPAIR_KEYS:
        raise MachineSourceError(f"key store: machine {name!r} 'repair' must be an object with {sorted(_REPAIR_KEYS)}")
    ssh = raw.get("ssh")
    if not isinstance(ssh, str) or not _SSH_DEST_RE.fullmatch(ssh):
        raise MachineSourceError(f"key store: machine {name!r} repair 'ssh' must be user@host (letters, digits, . _ -)")
    key_file = raw.get("key_file", DEFAULT_REPAIR_KEY_FILE)
    if not isinstance(key_file, str) or not key_file.startswith("/") or not key_file.isprintable():
        raise MachineSourceError(f"key store: machine {name!r} repair 'key_file' must be an absolute path")
    sudo = raw.get("sudo", False)
    if not isinstance(sudo, bool):
        raise MachineSourceError(f"key store: machine {name!r} repair 'sudo' must be true or false")
    return RepairRecipe(ssh=ssh, key_file=key_file, sudo=sudo)


def parse_keystore(data: object, source: object = "key store") -> dict[str, MachineEntry]:
    """Strictly validate the key-store shape ``{name: {url, key[, repair]}}``.

    Any problem raises ``MachineSourceError`` naming the machine, never the key.
    """
    if not isinstance(data, dict):
        raise MachineSourceError(f"{source}: the key store must be a JSON object of machines")
    entries: dict[str, MachineEntry] = {}
    for name, raw in data.items():
        if not is_valid_machine_name(name):
            raise MachineSourceError(f"{source}: invalid machine name {name!r}")
        if not isinstance(raw, dict):
            raise MachineSourceError(f"{source}: machine {name!r} must be an object")
        unknown = set(raw) - _KEYSTORE_ENTRY_KEYS
        if unknown:
            raise MachineSourceError(f"{source}: machine {name!r} has unknown keys: {sorted(unknown)}")
        url = validate_http_url(raw.get("url"), what=f"{source}: machine {name!r} 'url'")
        key = raw.get("key")
        if not isinstance(key, str):
            raise MachineSourceError(f"{source}: machine {name!r} has no 'key' string")
        validate_key(key, what=f"{source}: machine {name!r} 'key'")
        repair = parse_repair(raw["repair"], name) if "repair" in raw else None
        entries[name] = MachineEntry(url=url, key=key, repair=repair)
    return entries


def read_json_file(path: Path, what: str) -> object:
    """Parse *path* as JSON; any failure is a ``MachineSourceError`` (never echoing content)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MachineSourceError(f"cannot read {what} {path}: {exc}") from exc
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise MachineSourceError(f"{what} {path} is not valid JSON: {exc}") from exc


def authorization_header(entry: object) -> str | None:
    """An MCP-config entry's ``Authorization`` header value (any case; the last one wins), else None."""
    headers = entry.get("headers") if isinstance(entry, dict) else None
    found = None
    if isinstance(headers, dict):
        for name, value in headers.items():
            if isinstance(name, str) and name.lower() == "authorization" and isinstance(value, str):
                found = value
    return found


def is_legacy_mcp_config(data: object) -> bool:
    """True for the pre-#51 machine source: an MCP config (``mcpServers``), not a key store."""
    return isinstance(data, dict) and "mcpServers" in data


def load_machines(path: Path) -> dict[str, Upstream]:
    """Read ``machine name -> Upstream`` from the machine source.

    The key store (``{name: {url, key}}``) is validated strictly: one bad entry refuses the
    whole file (the gateway then keeps its last good copy). The legacy MCP-config shape
    (``mcpServers``) is still accepted for the transition; there, entries without an http(s)
    URL are skipped with a warning, since that file is shared with other tooling. A file that
    is unreadable or not JSON raises ``MachineSourceError``.
    """
    data = read_json_file(path, "machine source")
    if not is_legacy_mcp_config(data):
        entries = parse_keystore(data, f"machine source {path}")
        warn_if_loose_permissions(path, "key store")
        return {name: Upstream(url=entry.url, authorization=entry.authorization) for name, entry in entries.items()}
    servers = data["mcpServers"]
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
        authorization = authorization_header(entry)
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
