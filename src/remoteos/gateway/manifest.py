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

import os
import posixpath
import re
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

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
        project = projects[name] = _parse_project(name, raw, source)
        where = (project.host, project.account, _same_file_key(project.account, project.config))
        if where in files:
            raise ManifestError(f"{source}: projects {files[where]!r} and {name!r} write the same config file")
        files[where] = name
    return Manifest(gateway_url=gateway_url.rstrip("/"), projects=projects)


def _same_file_key(account: str, config: str) -> str:
    """One spelling per file, for the duplicate check: ``/home/<account>/x`` (``/root/x`` for
    root) is ``~/x``, and ``.`` / ``..`` / doubled slashes are folded. Symlinks on another host
    cannot be resolved here; two spellings through a symlink are the owner's to avoid."""
    path = posixpath.normpath(config)
    home = "/root" if account == "root" else f"/home/{account}"
    if path == home or path.startswith(home + "/"):
        path = "~" + path[len(home) :]
    return path


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


def add_machine(path: Path, project: str, machine: str) -> bool:
    """Add *machine* to *project*'s ``machines`` in the manifest file; False when it is listed.

    The file is the owner's, comments and layout included, so this edits the text. It is written
    only when the edited text parses to the same manifest plus that one machine."""
    manifest = load_manifest(path)
    current = manifest.projects.get(project)
    if current is None:
        raise ManifestError(f"{path}: no project {project!r}")
    if machine in current.machines:
        return False
    text = path.read_text(encoding="utf-8")
    edited = _insert_machine(text, project, machine)
    try:
        after = parse_manifest(tomllib.loads(edited), path)
    except (tomllib.TOMLDecodeError, ManifestError) as exc:
        raise ManifestError(f"{path}: could not add {machine!r} to {project!r}: {exc}") from exc
    expected = {**manifest.projects, project: replace(current, machines=(*current.machines, machine))}
    if after.projects != expected or after.gateway_url != manifest.gateway_url:
        raise ManifestError(f"{path}: could not add {machine!r} to {project!r}: edit it by hand")
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(edited, encoding="utf-8")
        temp.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def _insert_machine(text: str, project: str, machine: str) -> str:
    """*text* with ``"machine"`` after the last value of the project's ``machines`` array."""
    name = re.escape(project)
    header = re.search(rf"""^[ \t]*\[projects\.(?:{name}|"{name}"|'{name}')\][ \t]*(?:#.*)?$""", text, re.MULTILINE)
    if header is None:
        raise ManifestError(f"no [projects.{project}] line found: edit the manifest by hand")
    following = re.compile(r"^[ \t]*\[", re.MULTILINE).search(text, header.end())
    end = following.start() if following else len(text)
    key = re.compile(r"^[ \t]*machines[ \t]*=[ \t]*\[", re.MULTILINE).search(text, header.end(), end)
    if key is None:
        raise ManifestError(f"no machines array found in [projects.{project}]: edit the manifest by hand")
    last_value, trailing_comma, i = -1, False, key.end()
    while i < len(text) and text[i] != "]":
        char = text[i]
        if char == "#":
            i = text.index("\n", i) if "\n" in text[i:] else len(text)
        elif char in "\"'":
            i = text.index(char, i + 1) + 1
            last_value, trailing_comma = i, False
        else:
            trailing_comma = trailing_comma or char == ","
            i += 1
    if last_value < 0:
        raise ManifestError(f"[projects.{project}] has an empty machines array: edit the manifest by hand")
    if not trailing_comma:
        return f'{text[:last_value]}, "{machine}"{text[last_value:]}'
    comma = text.index(",", last_value)
    line_start = text.rfind("\n", 0, last_value) + 1
    indent = text[line_start : len(text[line_start:]) - len(text[line_start:].lstrip(" \t")) + line_start]
    return f'{text[:comma + 1]}\n{indent}"{machine}",{text[comma + 1:]}'
