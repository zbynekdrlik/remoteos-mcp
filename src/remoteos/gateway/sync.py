"""``remoteos-gateway sync-configs`` (#51): generate every project's MCP config from the manifest.

For each ``[projects.<name>]`` of ``projects.toml``:

- **gateway mode** (default): a consumer ``<name>@<host>`` exists in the registry with ONE
  stable token (minted once, kept in the generated file) and an allow-list EXACTLY equal to
  the manifest's machines. Each manifest machine's entry becomes
  ``{"type": "http", "url": "<gateway>/<machine>/mcp", "headers": {"Authorization": "Bearer <token>"}}``
  under the SAME server name, so every tool name (``mcp__<machine>__Shell``) stays.
- **direct mode** (remoteos-mcp only, dev1 only): the entries carry the key store's url + key,
  so the project can debug a machine when the gateway is down.

Only fleet entries change. An entry is the sync's own when its url is one sync writes (a
gateway url, or — in direct mode — the key store's url): such an entry whose machine left the
manifest is removed. Every other entry keeps its ORIGINAL source text, byte for byte. Writes
are atomic, 0600, after a backup into ``.backup-<date>/`` (``fileops``); a file whose text
would not change is not written at all.

Fail closed: an invalid manifest or key store, or a manifest machine missing from the key
store, refuses the whole run before anything changes; an unreadable / unparseable config or a
missing ssh / sudo access fails that project only (its consumer is never given a token no file
holds, and its allow-list is only ever narrowed). No key or token is ever printed or logged.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from remoteos.gateway.keystore import authorization_header, bearer_value, mask_secret
from remoteos.gateway.manifest import MODE_DIRECT, MODE_GATEWAY, Manifest, Project
from remoteos.gateway.registry import (
    SYNC_MANAGER,
    Consumer,
    MachineEntry,
    Registry,
    RegistryTransaction,
    registry_transaction,
    token_sha256,
)
from remoteos.gateway.targets import Target, TargetError, target_for

logger = logging.getLogger("remoteos.gateway.sync")


class SyncError(Exception):
    """A project (or the whole run) is refused. The message never holds a secret."""


class ConfigFormatError(SyncError):
    """A project config is not JSON we can edit safely; it is never overwritten."""


def mint_token() -> str:
    return secrets.token_urlsafe(32)


# -- the mcpServers block, with each entry's ORIGINAL source text ---------------------------

_WS = re.compile(r"[ \t\n\r]*")
_DECODER = json.JSONDecoder()


@dataclass
class ServerEntry:
    name: str
    value: object
    raw: str  # the value's exact source text


@dataclass
class ParsedConfig:
    """A config split around its ``mcpServers`` object. ``text is None``: no file yet."""

    text: str | None
    entries: list[ServerEntry] = field(default_factory=list)
    has_block: bool = False
    start: int = 0  # index just after mcpServers' "{"
    end: int = 0  # index of mcpServers' "}"
    lead: str = ""  # whitespace between "{" and the first entry
    trail: str = ""  # whitespace between the last entry and "}"
    line_indent: str = ""  # indentation of the line holding "mcpServers"


def _ws(text: str, i: int) -> int:
    return _WS.match(text, i).end()


def _scan_object(text: str, i: int) -> tuple[list[tuple[str, int, int, object]], int, str, str]:
    """Members ``(key, value_start, value_end, value)`` of the JSON object whose "{" is at *i*,
    plus the index of its "}" and the whitespace after "{" / before "}"."""
    members: list[tuple[str, int, int, object]] = []
    j = _ws(text, i + 1)
    lead = text[i + 1 : j]
    if text[j] == "}":
        return members, j, lead, ""
    while True:
        if text[j] != '"':
            raise ConfigFormatError("unexpected JSON layout")
        key, j = _DECODER.raw_decode(text, j)
        j = _ws(text, j)
        if text[j] != ":":
            raise ConfigFormatError("unexpected JSON layout")
        value_start = _ws(text, j + 1)
        value, value_end = _DECODER.raw_decode(text, value_start)
        members.append((key, value_start, value_end, value))
        j = _ws(text, value_end)
        if text[j] == ",":
            j = _ws(text, j + 1)
            continue
        if text[j] == "}":
            return members, j, lead, text[value_end:j]
        raise ConfigFormatError("unexpected JSON layout")


def parse_config(text: str | None) -> ParsedConfig:
    if text is None:
        return ParsedConfig(text=None)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ConfigFormatError(f"not valid JSON ({exc.__class__.__name__}) — left untouched") from exc
    if not isinstance(data, dict):
        raise ConfigFormatError("not a JSON object — left untouched")
    if "mcpServers" not in data:
        return ParsedConfig(text=text)
    if not isinstance(data["mcpServers"], dict):
        raise ConfigFormatError("'mcpServers' is not an object — left untouched")
    top, _, _, _ = _scan_object(text, _ws(text, 0))
    blocks = [m for m in top if m[0] == "mcpServers"]
    if len(blocks) != 1:
        raise ConfigFormatError("'mcpServers' appears more than once — left untouched")
    _, value_start, _, _ = blocks[0]
    members, close, lead, trail = _scan_object(text, value_start)
    names = [m[0] for m in members]
    if len(set(names)) != len(names):
        raise ConfigFormatError("a server name appears twice in mcpServers — left untouched")
    line_start = text.rfind("\n", 0, value_start) + 1
    line_indent = text[line_start : _ws(text, line_start)].replace("\n", "").replace("\r", "")
    return ParsedConfig(
        text=text,
        entries=[ServerEntry(name=k, value=v, raw=text[s:e]) for k, s, e, v in members],
        has_block=True,
        start=value_start + 1,
        end=close,
        lead=lead,
        trail=trail,
        line_indent=line_indent,
    )


def render_entry(value: dict, indent: str) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)


def render_config(parsed: ParsedConfig, entries: list[tuple[str, str]]) -> str:
    """The config with mcpServers = *entries* ``(name, raw value text)``; everything outside
    mcpServers, and every raw text passed in, is kept byte for byte."""
    if not parsed.has_block:
        data = json.loads(parsed.text) if parsed.text is not None else {}
        data["mcpServers"] = {name: json.loads(raw) for name, raw in entries}
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    assert parsed.text is not None
    if not entries:
        return parsed.text[: parsed.start] + parsed.text[parsed.end :]
    indent = entry_indent(parsed)
    lead = parsed.lead if "\n" in parsed.lead else "\n" + indent
    trail = parsed.trail if (parsed.entries and "\n" in parsed.trail) else "\n" + parsed.line_indent
    body = ("," + lead).join(f"{json.dumps(name, ensure_ascii=False)}: {raw}" for name, raw in entries)
    return parsed.text[: parsed.start] + lead + body + trail + parsed.text[parsed.end :]


def entry_indent(parsed: ParsedConfig) -> str:
    if parsed.entries and "\n" in parsed.lead:
        return parsed.lead.rsplit("\n", 1)[1]
    return parsed.line_indent + "  "


# -- the plan for one project -----------------------------------------------------------------


def gateway_entry(url: str, token: str) -> dict:
    return {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}


def direct_entry(entry: MachineEntry) -> dict:
    return {"type": "http", "url": entry.url, "headers": {"Authorization": entry.authorization}}


def _url(value: object) -> object:
    return value.get("url") if isinstance(value, dict) else None


@dataclass
class ProjectPlan:
    project: Project
    target: Target
    old_text: str | None
    new_text: str
    token: str | None = field(default=None, repr=False)  # gateway mode
    new_token: bool = False
    lines: list[str] = field(default_factory=list)  # what changes in the file (masked)
    notes: list[str] = field(default_factory=list)  # warnings (masked)

    @property
    def file_changes(self) -> bool:
        return self.old_text != self.new_text


def plan_project(
    project: Project,
    target: Target,
    old_text: str | None,
    *,
    manifest: Manifest,
    keystore: dict[str, MachineEntry],
    registry: Registry,
    mint: Callable[[], str] = mint_token,
) -> ProjectPlan:
    parsed = parse_config(old_text)
    plan = ProjectPlan(project=project, target=target, old_text=old_text, new_text="")

    def owned(name: str, value: object) -> bool:
        """Did sync write this entry? A gateway url always; a key-store url in direct mode."""
        url = _url(value)
        if url == manifest.machine_url(name):
            return True
        return project.mode == MODE_DIRECT and name in keystore and url == keystore[name].url

    if project.mode == MODE_GATEWAY:
        consumer = registry.consumers.get(project.consumer)
        if consumer is not None and consumer.managed_by != SYNC_MANAGER:
            raise SyncError(
                f"consumer {project.consumer} already exists and is not managed by sync-configs — refusing to "
                f"take it over (revoke it with `remoteos-gateway consumer revoke {project.consumer}`, or rename)"
            )
        plan.token = _reusable_token(parsed, manifest, consumer)
        if plan.token is None:
            plan.token, plan.new_token = mint(), True
        desired = {m: gateway_entry(manifest.machine_url(m), plan.token) for m in project.machines}
    else:
        desired = {m: direct_entry(keystore[m]) for m in project.machines}

    indent = entry_indent(parsed)
    out: list[tuple[str, str]] = []
    kept: list[str] = []
    for entry in parsed.entries:
        if entry.name in desired:
            if entry.value == desired[entry.name]:
                out.append((entry.name, entry.raw))
            else:
                out.append((entry.name, render_entry(desired[entry.name], indent)))
                was = _url(entry.value)
                plan.lines.append(f"  ~ {_describe(entry.name, desired[entry.name], plan)}  (was {was})")
        elif owned(entry.name, entry.value):
            plan.lines.append(f"  - {entry.name}  (left the manifest)")
        else:
            out.append((entry.name, entry.raw))
            kept.append(entry.name)
            if entry.name in keystore:
                plan.notes.append(
                    f"  ! {entry.name} is in the key store but not in the manifest — left as it is"
                    " (add it to the project's machines to route it through the gateway)"
                )
    present = {entry.name for entry in parsed.entries}
    for machine in project.machines:
        if machine not in present:
            out.append((machine, render_entry(desired[machine], indent)))
            plan.lines.append(f"  + {_describe(machine, desired[machine], plan)}")
    if kept:
        plan.lines.append(f"  = {len(kept)} other entr{'y' if len(kept) == 1 else 'ies'} unchanged: {', '.join(kept)}")
    for machine in project.machines:
        if project.mode == MODE_GATEWAY and machine not in registry.exposed:
            plan.notes.append(
                f"  ! {machine} is not exposed — its requests get 404 until `remoteos-gateway expose {machine}`"
            )
    plan.new_text = render_config(parsed, out)
    return plan


def _reusable_token(parsed: ParsedConfig, manifest: Manifest, consumer: Consumer | None) -> str | None:
    """The token already in this project's gateway entries, when it is registered for its consumer."""
    if consumer is None:
        return None
    for entry in parsed.entries:
        if _url(entry.value) != manifest.machine_url(entry.name):
            continue
        token = bearer_value(authorization_header(entry.value) or "")
        if token and token_sha256(token) in consumer.sha256:
            return token
    return None


def _describe(name: str, value: dict, plan: ProjectPlan) -> str:
    """An entry for output: url + the masked credential (never the credential)."""
    auth = value["headers"]["Authorization"]
    credential = auth[len("Bearer ") :]
    shown = "<new token>" if plan.new_token and credential == plan.token else mask_secret(credential)
    return f"{name}  -> {value['url']}  Authorization: Bearer {shown}"


# -- running ----------------------------------------------------------------------------------


@dataclass
class _Saver:
    """Saves the locked registry only when it really changed (a no-op run writes nothing)."""

    transaction: RegistryTransaction
    saved: str = ""

    def __post_init__(self) -> None:
        self.saved = self._snapshot()

    def _snapshot(self) -> str:
        return json.dumps(self.transaction.registry.to_json(), sort_keys=True)

    def commit(self) -> bool:
        now = self._snapshot()
        if now == self.saved:
            return False
        self.transaction.save()
        self.saved = now
        return True


def validate_run(manifest: Manifest, keystore: dict[str, MachineEntry], *, local_host: str) -> None:
    """Whole-run checks, before anything is read or written."""
    missing = sorted(
        {f"{m} (project {p.name})" for p in manifest.projects.values() for m in p.machines if m not in keystore}
    )
    if missing:
        raise SyncError(
            "manifest machines missing from the key store: " + ", ".join(missing)
            + " — add them with `remoteos-gateway machine set-key <name> --url <url>`; nothing was changed"
        )
    for project in manifest.projects.values():
        if project.mode == MODE_DIRECT and project.host != local_host:
            raise SyncError(
                f"[projects.{project.name}]: mode = \"direct\" writes the machine keys themselves, so it is allowed "
                f"only on the gateway's host ({local_host}); {project.name} is on {project.host}. Nothing was changed."
            )


def sync_configs(
    manifest: Manifest,
    keystore: dict[str, MachineEntry],
    registry_path,
    *,
    local_host: str,
    dry_run: bool,
    today: str,
    echo: Callable[[str], None],
    mint: Callable[[], str] = mint_token,
) -> int:
    """Run the sync; returns the number of FAILED projects (0 = everything is in place)."""
    validate_run(manifest, keystore, local_host=local_host)
    failures = 0
    changed_projects: list[str] = []
    with registry_transaction(registry_path) as transaction:
        saver = _Saver(transaction)
        registry = transaction.registry
        for project in manifest.projects.values():
            target = target_for(project.host, project.account, local_host=local_host)
            head = f"{project.name} ({project.host}, {project.account}, {target.label}): {project.config}"
            try:
                plan = plan_project(
                    project,
                    target,
                    target.read(project.config),
                    manifest=manifest,
                    keystore=keystore,
                    registry=registry,
                    mint=mint,
                )
            except (TargetError, SyncError) as exc:
                failures += 1
                echo(f"{head}\n  FAILED: {exc}")
                if not dry_run:
                    _narrow_allow_list(registry, project)
                    saver.commit()
                logger.warning("sync-configs: project %s FAILED: %s", project.name, exc)
                continue
            consumer_line = _consumer_change(registry, plan)
            if not plan.file_changes and consumer_line is None:
                echo(f"{project.name}: unchanged")
                for note in plan.notes:
                    echo(note)
                continue
            echo(head)
            for line in plan.lines + plan.notes:
                echo(line)
            if consumer_line:
                echo(consumer_line)
            if dry_run:
                echo("  (dry run — nothing written)")
                continue
            try:
                written = _apply(plan, registry, saver, today)
            except TargetError as exc:
                failures += 1
                echo(f"  FAILED: {exc}")
                logger.warning("sync-configs: project %s write FAILED: %s", project.name, exc)
                continue
            if written is not None:
                changed_projects.append(project.name)
                echo(f"  written (backup: {written.get('backup') or 'none — new file'})")
            logger.info("sync-configs: project %s in place (%s)", project.name, target.label)

        gateway_consumers = {p.consumer for p in manifest.projects.values() if p.mode == MODE_GATEWAY}
        for name, consumer in sorted(registry.consumers.items()):
            if consumer.managed_by == SYNC_MANAGER and name not in gateway_consumers:
                echo(f"consumer {name}: its project left the manifest (or is direct now) — " + (
                    "would be revoked (dry run)" if dry_run else "revoked"
                ))
                if not dry_run:
                    del registry.consumers[name]
        if not dry_run:
            saver.commit()
    if dry_run:
        echo("dry run — nothing was written")
    elif changed_projects:
        echo(
            "Running Claude Code sessions of " + ", ".join(changed_projects) + " read their .mcp.json only at start:"
            " each needs ONE restart (or /mcp Reconnect). After that, a key change never reaches a session."
        )
    return failures


def _consumer_change(registry: Registry, plan: ProjectPlan) -> str | None:
    """What the sync changes on the project's consumer, as one masked line (None: nothing)."""
    project = plan.project
    if project.mode != MODE_GATEWAY or plan.token is None:
        return None
    consumer = registry.consumers.get(project.consumer)
    digest = token_sha256(plan.token)
    machines = list(project.machines)
    if consumer is not None and consumer.sha256 == [digest] and consumer.machines == machines:
        return None
    token = "<new token>" if plan.new_token else mask_secret(plan.token)
    state = "new" if consumer is None else "updated"
    return f"  consumer {project.consumer}: {state}, token {token}, machines: {', '.join(machines) or '(none)'}"


def _apply(plan: ProjectPlan, registry: Registry, saver: _Saver, today: str) -> dict | None:
    """Register the token, write the file, then drop stale token hashes — in that order, so no
    file ever carries a token the gateway does not know. A failed write rolls the token back."""
    project = plan.project
    consumer = None
    before: tuple[list[str], list[str]] | None = None
    if project.mode == MODE_GATEWAY:
        assert plan.token is not None
        consumer = registry.consumers.get(project.consumer)
        if consumer is not None:
            before = (list(consumer.sha256), list(consumer.machines))
        else:
            consumer = Consumer(added=today, managed_by=SYNC_MANAGER)
            registry.consumers[project.consumer] = consumer
        digest = token_sha256(plan.token)
        for name, other in registry.consumers.items():
            if other is not consumer and digest in other.sha256:  # pragma: no cover - a 256-bit collision
                raise TargetError(f"the token for {project.consumer} is already registered for {name}")
        consumer.managed_by = SYNC_MANAGER
        consumer.machines = list(project.machines)
        if digest not in consumer.sha256:
            consumer.sha256.append(digest)
        saver.commit()
    written = None
    if plan.file_changes:
        try:
            written = plan.target.write(project.config, plan.new_text, today)
        except TargetError:
            if consumer is not None:
                if before is None:
                    del registry.consumers[project.consumer]
                else:
                    consumer.sha256 = before[0]
                    consumer.machines = [m for m in before[1] if m in project.machines]
                saver.commit()
            raise
    if consumer is not None:
        consumer.sha256 = [token_sha256(plan.token)]
        saver.commit()
    return written


def _narrow_allow_list(registry: Registry, project: Project) -> None:
    """A failed project keeps its consumer, but never broader than the manifest."""
    consumer = registry.consumers.get(project.consumer)
    if consumer is not None and consumer.managed_by == SYNC_MANAGER:
        consumer.machines = [m for m in consumer.machines if m in project.machines]
