"""The project manifest (#51): which project reaches which machines, and where its config lives.

``~/.config/remoteos-gateway/projects.toml`` on dev1::

    [gateway]
    url = "http://100.104.8.125:8796"

    [projects.camera-box]
    host = "dev1"                                    # where the project's sessions run
    account = "newlevel"                             # whose ~/.claude it is
    config = "~/.claude/mcp-configs/camera-box.mcp.json"
    machines = ["win-stream-snv", "linux-cam1"]

    [projects.remoteos-mcp]                          # debugs machines when the gateway is down
    host = "dev1"
    account = "newlevel"
    config = "~/.claude/mcp-configs/remoteos-mcp.mcp.json"
    mode = "direct"
    machines = ["win-stream-snv", "linux-cam1"]

Adding a machine or a project = one line here + ``remoteos-gateway sync-configs``. ``~`` in
``config`` is the home of ``account`` on ``host``. ``mode = "direct"`` writes the machine
keys themselves, so it is allowed only for the gateway's own account (``sync.py`` checks).
A project lists at least one machine; to take a project off, remove its table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from remoteos.gateway.registry import RegistryError, is_valid_machine_name, validate_consumer_name

DEFAULT_MANIFEST_PATH = Path.home() / ".config" / "remoteos-gateway" / "projects.toml"
DEFAULT_GATEWAY_URL = "http://100.104.8.125:8796"  # dev1's tailscale address + the gateway port

MODE_GATEWAY = "gateway"
MODE_DIRECT = "direct"

_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_ACCOUNT_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_GATEWAY_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+")
_TOP_KEYS = frozenset({"gateway", "projects"})
_GATEWAY_KEYS = frozenset({"url"})
_PROJECT_KEYS = frozenset({"host", "account", "config", "machines", "mode"})
_REQUIRED = ("host", "account", "config", "machines")


class ManifestError(Exception):
    """The manifest is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Project:
    name: str
    host: str
    account: str
    config: str
    machines: tuple[str, ...]
    mode: str = MODE_GATEWAY

    @property
    def consumer(self) -> str:
        """The gateway consumer this project's sessions authenticate as."""
        return f"{self.name}@{self.host}"


@dataclass(frozen=True)
class Manifest:
    gateway_url: str
    projects: dict[str, Project]

    def machine_url(self, machine: str) -> str:
        return f"{self.gateway_url}/{machine}/mcp"


def load_manifest(path: Path) -> Manifest:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManifestError(
            f"no manifest at {path} — write one (README 'MCP gateway (dev1)': one [projects.<name>] per project)"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid TOML: {exc}") from exc
    return parse_manifest(data, path)


def parse_manifest(data: dict, source: object = "manifest") -> Manifest:
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ManifestError(f"{source}: unknown top-level keys {sorted(unknown)} (use [gateway] and [projects.<name>])")
    gateway = data.get("gateway", {})
    if not isinstance(gateway, dict) or set(gateway) - _GATEWAY_KEYS:
        raise ManifestError(f"{source}: [gateway] may only set url")
    gateway_url = gateway.get("url", DEFAULT_GATEWAY_URL)
    if not isinstance(gateway_url, str) or not _GATEWAY_URL_RE.fullmatch(gateway_url.rstrip("/")):
        raise ManifestError(f"{source}: [gateway] url must be http(s)://host[:port], got {gateway_url!r}")
    projects_raw = data.get("projects", {})
    if not isinstance(projects_raw, dict):
        raise ManifestError(f"{source}: projects must be tables: [projects.<name>]")

    projects: dict[str, Project] = {}
    files: dict[tuple[str, str, str], str] = {}
    for name, raw in projects_raw.items():
        projects[name] = _parse_project(name, raw, source)
        where = (projects[name].host, projects[name].account, projects[name].config)
        if where in files:
            raise ManifestError(f"{source}: projects {files[where]!r} and {name!r} write the same config file")
        files[where] = name
    return Manifest(gateway_url=gateway_url.rstrip("/"), projects=projects)


def _parse_project(name: str, raw: object, source: object) -> Project:
    where = f"{source}: [projects.{name}]"
    if not _PROJECT_RE.fullmatch(name):
        raise ManifestError(f"{source}: invalid project name {name!r}: use letters, digits and . _ -")
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be a table")
    unknown = set(raw) - _PROJECT_KEYS
    if unknown:
        raise ManifestError(f"{where} has unknown keys {sorted(unknown)}")
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ManifestError(f"{where} is missing {missing}")
    host, account, config = raw["host"], raw["account"], raw["config"]
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
        raise ManifestError(f"{where} host must be a host name (dev1 / dev2), got {host!r}")
    if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
        raise ManifestError(f"{where} account must be a unix account name, got {account!r}")
    if not isinstance(config, str) or not config.startswith(("/", "~/")) or not config.isprintable():
        raise ManifestError(f"{where} config must be an absolute path or start with ~/, got {config!r}")
    machines = raw["machines"]
    if not isinstance(machines, list) or not all(isinstance(m, str) and is_valid_machine_name(m) for m in machines):
        raise ManifestError(f"{where} machines must be a list of machine names")
    if not machines:
        raise ManifestError(f"{where} lists no machines — remove the project from the manifest instead")
    if len(set(machines)) != len(machines):
        raise ManifestError(f"{where} lists a machine twice")
    mode = raw.get("mode", MODE_GATEWAY)
    if mode not in (MODE_GATEWAY, MODE_DIRECT):
        raise ManifestError(f"{where} mode must be {MODE_GATEWAY!r} or {MODE_DIRECT!r}, got {mode!r}")
    project = Project(name=name, host=host, account=account, config=config, machines=tuple(machines), mode=mode)
    try:
        validate_consumer_name(project.consumer)
    except RegistryError as exc:  # say which manifest entry produced the bad name
        raise ManifestError(f"{where}: {exc}") from exc
    return project
