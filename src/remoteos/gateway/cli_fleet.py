"""``remoteos-gateway`` fleet commands (#51): the key store, generated configs, the drift check.

    remoteos-gateway machine set-key linux-cam2 < key-file      # the key on STDIN, never an argv
    remoteos-gateway machine set-key new-box --url http://new-box.lan:8092/mcp < key-file
    remoteos-gateway machine list                                # keys masked
    remoteos-gateway machine import [--from <file.mcp.json> ... | --from-manifest] [--dry-run]
    remoteos-gateway machine repair-recipe linux-cam2 --ssh root@cam2.lan [--sudo]
    remoteos-gateway sync-configs [--dry-run]                    # every project's config, from projects.toml
    remoteos-gateway check                                       # the drift watchdog (timer, every 10 min)

The commands are registered on ``cli.main`` and receive its paths object (``--registry``,
``--machines`` = the key store, ``--manifest``). No key or token is ever printed: output shows
``mask_secret`` (length + a sha256 prefix, comparable with ``remoteos-gateway list``).
"""

from __future__ import annotations

import datetime as _dt
import logging
import socket
import sys
from pathlib import Path

import click

from remoteos.gateway.check import (
    CHUNK_LIMIT,
    DEFAULT_AIRULESET_PATH,
    DEFAULT_STATUS_PATH,
    CheckError,
    airuleset_notifier,
    load_status,
    run_check,
)
from remoteos.gateway.drift import DriftPolicy
from remoteos.gateway.keystore import (
    DEFAULT_IMPORT_MATCH,
    apply_import,
    describe_decision,
    edit_keystore,
    load_keystore,
    mask_secret,
    resolve_import,
    scan_sources,
    set_key,
)
from remoteos.gateway.manifest import DEFAULT_GATEWAY_URL, ManifestError, load_manifest
from remoteos.gateway.probe import STATE_OK
from remoteos.gateway.registry import (
    LEGACY_MACHINES_PATH,
    MachineEntry,
    MachineSourceError,
    RegistryError,
    RepairRecipe,
    edit_registry,
    load_registry,
    parse_repair,
)
from remoteos.gateway.sync import SyncError, sync_configs
from remoteos.gateway.targets import TargetError, target_for
from remoteos.release_channel import RELEASES_URL, fetch_releases, soak_days

_LOCAL_HOST_HELP = "This host's name as the manifest's `host` spells it (dev1); other hosts are reached over ssh."
_GATEWAY_URL_HELP = f"The gateway (default: the manifest's [gateway] url, else {DEFAULT_GATEWAY_URL})."


def _notify_options(function):
    function = click.option(
        "--notify-owner", envvar="REMOTEOS_GATEWAY_NOTIFY_OWNER", default=None, help="notify --owner-name."
    )(function)
    return click.option(
        "--airuleset",
        "airuleset_path",
        type=click.Path(dir_okay=False, path_type=Path),
        envvar="REMOTEOS_GATEWAY_AIRULESET",
        default=DEFAULT_AIRULESET_PATH,
        show_default=True,
        help="airuleset.py whose `notify --body` tells the owner; missing = printed or logged instead.",
    )(function)


def _status_file_option(function):
    return click.option(
        "--status-file",
        type=click.Path(dir_okay=False, path_type=Path),
        envvar="REMOTEOS_GATEWAY_STATUS_FILE",
        default=DEFAULT_STATUS_PATH,
        show_default=True,
        help="Where the machine states (ok / 401 / unreachable + since) and last seen versions are kept.",
    )(function)


def _gateway_url(paths, explicit: str | None, *, strict: bool) -> str:
    """--gateway-url, else the manifest's [gateway] url, else the built-in default. An invalid
    manifest is an error when *strict*, else (the unattended check) a warning + the default."""
    if explicit:
        return explicit.rstrip("/")
    if not paths.manifest.exists():
        return DEFAULT_GATEWAY_URL
    try:
        return load_manifest(paths.manifest).gateway_url
    except ManifestError as exc:
        if strict:
            raise _fail(exc) from exc
        log = logging.getLogger("remoteos.gateway.check")
        log.warning("manifest unreadable (%s) — using %s", exc, DEFAULT_GATEWAY_URL)
        return DEFAULT_GATEWAY_URL

_RELOAD_NOTE = "A running gateway applies this on its next request (or SIGHUP) — no restart needed."


def _fail(exc: Exception) -> click.ClickException:
    return click.ClickException(str(exc))


def _exposure_plan(registry, machines: list[str]) -> tuple[list[str], list[str]]:
    """``(to_expose, held)`` for machines NEW to the key store. One already in a consumer's
    allow-list but not exposed has its kill switch engaged (e.g. since before #51): it is
    held, never opened by an import or a set-key."""
    allowed = {m for consumer in registry.consumers.values() for m in consumer.machines}
    pending = [m for m in machines if m not in registry.exposed]
    return [m for m in pending if m not in allowed], [m for m in pending if m in allowed]


def _expose_new(paths, machines: list[str], *, dry_run: bool = False) -> None:
    """Expose the machines NEW to the key store (never one behind a kill switch), and say so."""
    if not machines:
        return
    try:
        if dry_run:
            to_expose, held = _exposure_plan(load_registry(paths.registry), machines)
        else:
            with edit_registry(paths.registry) as registry:
                to_expose, held = _exposure_plan(registry, machines)
                for machine in to_expose:
                    registry.expose(machine)
    except RegistryError as exc:
        raise _fail(exc) from exc
    if to_expose:
        click.echo(("would expose: " if dry_run else "exposed in the registry: ") + ", ".join(to_expose))
    for machine in held:
        click.echo(
            f"  {machine}: a consumer is allowed it but it is NOT exposed (the kill switch) — left unexposed;"
            f" `remoteos-gateway expose {machine}` opens it"
        )


def _read_key_from_stdin() -> str:
    if sys.stdin.isatty():
        return click.prompt("machine key (not echoed)", hide_input=True).strip()
    return sys.stdin.readline().strip()


# -- machine ----------------------------------------------------------------------------


@click.group()
def machine() -> None:
    """The key store: one url + key per machine, read live by the gateway."""


@machine.command("set-key")
@click.argument("name")
@click.option("--url", default=None, help="The machine's /mcp url (required to ADD a machine; changes it otherwise).")
@click.option("--force", is_flag=True, help="Store the key even though the machine rejects it (HTTP 401).")
@click.pass_obj
def machine_set_key(paths, name: str, url: str | None, force: bool) -> None:
    """Set machine NAME's key, read from STDIN (one line). Never pass a key as an argument."""
    key = _read_key_from_stdin()
    try:
        outcome = set_key(paths.machines, name, key, url=url, force=force)
    except MachineSourceError as exc:
        raise _fail(exc) from exc
    click.echo(f"{name}: {'added' if outcome.created else 'key set'} — key {mask_secret(key)}, url {outcome.url}")
    click.echo(f"  probe with the new key: {outcome.probe.state} ({outcome.probe.detail})")
    if outcome.probe.state != STATE_OK:
        click.echo(
            "  WARNING: not verified — the machine did not accept the key just now."
            " `remoteos-gateway check` will tell."
        )
    if outcome.created:
        _expose_new(paths, [name])
        click.echo(f"  next: add {name} to a project's machines in projects.toml, then run sync-configs.")
    click.echo(_RELOAD_NOTE)
    click.echo(
        "Projects routed through the gateway use the new key on their next call — no config change, no reconnect."
        " A `direct` project (remoteos-mcp) holds its own copy: run `remoteos-gateway sync-configs` for it."
    )


@machine.command("list")
@_status_file_option
@click.pass_obj
def machine_list(paths, status_file: Path) -> None:
    """Show the key store: url, the key MASKED, the repair recipe, the version `check` last saw."""
    try:
        entries = load_keystore(paths.machines, missing_ok=True)
    except MachineSourceError as exc:
        raise _fail(exc) from exc
    records = load_status(status_file.expanduser()).get("machines")
    records = records if isinstance(records, dict) else {}
    click.echo(f"key store: {paths.machines}  ({len(entries)} machine(s))")
    for name, entry in sorted(entries.items()):
        repair = _describe_recipe(entry.repair) if entry.repair else "-"
        record = records.get(name)
        version = record.get("version") if isinstance(record, dict) else None
        shown = version if isinstance(version, str) else "?"
        click.echo(f"  {name}  {entry.url}  key {mask_secret(entry.key)}  version {shown}  repair: {repair}")
    if not entries:
        click.echo("  (empty — build it with `remoteos-gateway machine import`)")


@machine.command("import")
@click.option(
    "--from",
    "sources",
    multiple=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"An MCP config to take machine keys from (repeatable). Default: {LEGACY_MACHINES_PATH.parent}/*.mcp.json",
)
@click.option(
    "--from-manifest",
    is_flag=True,
    help="Also read every project config of the manifest where it lives (dev2 over ssh, other accounts via sudo).",
)
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=_LOCAL_HOST_HELP)
@click.option("--match", default=DEFAULT_IMPORT_MATCH, show_default=True, help="Only entries whose name fully matches.")
@click.option("--gateway-url", default=None, help=_GATEWAY_URL_HELP + " Entries pointing there are skipped (tokens).")
@click.option(
    "--accept-url-change",
    is_flag=True,
    help="Also consider copies at another url than the key store's (the machine really moved).",
)
@click.option("--dry-run", is_flag=True, help="Report the choice per machine; write nothing.")
@click.pass_obj
def machine_import(
    paths,
    sources: tuple[Path, ...],
    from_manifest: bool,
    local_host: str,
    match: str,
    gateway_url: str | None,
    accept_url_change: bool,
    dry_run: bool,
) -> None:
    """Build the key store from per-project MCP configs. For each machine, the copy the machine
    ACCEPTS (initialize -> 200) wins; conflicts and unverified machines are reported."""
    gateway_url = _gateway_url(paths, gateway_url, strict=True)
    files = list(sources) or ([] if from_manifest else sorted(LEGACY_MACHINES_PATH.parent.glob("*.mcp.json")))
    texts: list[tuple[str, str]] = []
    unreadable: list[str] = []
    for path in files:
        try:
            texts.append((str(path), Path(path).expanduser().read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{path} ({type(exc).__name__})")
    if unreadable and sources:  # a file the owner NAMED: a typo must not import half a fleet
        raise click.ClickException("cannot read " + ", ".join(unreadable) + " — nothing written")
    for item in unreadable:
        click.echo(f"  {item}: cannot read — skipped")
    if from_manifest:
        try:
            manifest = load_manifest(paths.manifest)
        except ManifestError as exc:
            raise _fail(exc) from exc
        for project in manifest.projects.values():
            target = target_for(project.host, project.account, local_host=local_host)
            label = f"{project.name} ({target.label}) {project.config}"
            try:
                text = target.read(project.config)
            except TargetError as exc:
                click.echo(f"  {label}: cannot read — {exc}")
                continue
            if text is not None:
                texts.append((label, text))
    _import_and_report(
        paths, texts, match=match, gateway_url=gateway_url, dry_run=dry_run, accept_url_change=accept_url_change
    )


def _import_and_report(
    paths, texts: list[tuple[str, str]], *, match: str, gateway_url: str, dry_run: bool, accept_url_change: bool
) -> None:
    scan = scan_sources(texts, match=match, skip_url_prefix=gateway_url)
    try:
        existing = load_keystore(paths.machines, missing_ok=True)
    except MachineSourceError as exc:
        raise _fail(exc) from exc
    decisions = resolve_import(scan, existing, accept_url_change=accept_url_change)
    click.echo(f"read {len(texts)} config(s); {len(decisions)} machine(s) found")
    for note in scan.notes:
        click.echo(f"  note: {note}")
    for decision in decisions:
        click.echo(describe_decision(decision))
    if not decisions:
        # Never a key store with no machines: the gateway would start serving nothing, where with
        # no key store at all it refuses to start.
        raise click.ClickException(f"no fleet machine found in {len(texts)} config(s) — nothing written")
    flagged = [d.name for d in decisions if d.flagged]
    if flagged:
        click.echo(f"{len(flagged)} machine(s) NOT verified: {', '.join(flagged)} — fix them before routing projects")
    # Only machines NEW to the key store may be exposed: a known machine may be unexposed on
    # purpose (the kill switch), and a re-import must not undo that.
    new_machines = [d.name for d in decisions if d.name not in existing]
    if dry_run:
        _expose_new(paths, new_machines, dry_run=True)
        click.echo("dry run — nothing written")
        return
    for name in apply_import(paths.machines, decisions, snapshot=existing):
        click.echo(f"  {name}: its key-store entry changed while the import probed — left as it is now")
    click.echo(f"key store {paths.machines} up to date ({len(decisions)} machine(s) considered)")
    _expose_new(paths, new_machines)
    click.echo(_RELOAD_NOTE)


@machine.command("repair-recipe")
@click.argument("name")
@click.option("--ssh", "ssh_dest", default=None, help="user@host that may read the key file (ssh key login).")
@click.option("--key-file", default=None, help="The installer's config.json on the machine.")
@click.option("--sudo", is_flag=True, help="Read the key file with `sudo -n`.")
@click.option("--clear", is_flag=True, help="Remove the recipe: a 401 is then only reported.")
@click.pass_obj
def machine_repair_recipe(
    paths, name: str, ssh_dest: str | None, key_file: str | None, sudo: bool, clear: bool
) -> None:
    """Set (or --clear) the explicit recipe `check` may use to re-read NAME's key after a 401."""
    if clear == (ssh_dest is not None):
        raise click.UsageError("give either --ssh user@host or --clear")
    try:
        recipe = None
        if not clear:
            raw = {"ssh": ssh_dest, "sudo": sudo, **({"key_file": key_file} if key_file else {})}
            recipe = parse_repair(raw, name)
        with edit_keystore(paths.machines) as entries:
            entry = entries.get(name)
            if entry is None:
                raise MachineSourceError(f"machine {name!r} is not in the key store {paths.machines}")
            entries[name] = MachineEntry(url=entry.url, key=entry.key, repair=recipe)
    except MachineSourceError as exc:
        raise _fail(exc) from exc
    if recipe is None:
        click.echo(f"{name}: repair recipe removed — a 401 is only reported.")
    else:
        click.echo(f"{name}: repair recipe set — {_describe_recipe(recipe)}")


def _describe_recipe(recipe: RepairRecipe) -> str:
    return f"ssh {recipe.ssh} {'sudo -n ' if recipe.sudo else ''}cat {recipe.key_file}"


# -- sync-configs -----------------------------------------------------------------------


@click.command("sync-configs")
@click.option("--dry-run", is_flag=True, help="Show what would change (every secret masked); write nothing.")
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=_LOCAL_HOST_HELP)
@click.option(
    "--prune",
    is_flag=True,
    help="Remove the consumers of projects that left the manifest (by default they only lose their machines).",
)
@_notify_options
@click.pass_obj
def sync_configs_command(
    paths, dry_run: bool, local_host: str, prune: bool, airuleset_path: Path, notify_owner: str | None
) -> None:
    """Generate every project's MCP config from the manifest: fleet entries through the gateway
    (one token per project, allow-list = the manifest); every other entry is left byte-equal.
    The owner is told which live Claude sessions of the written configs need a restart."""
    try:
        manifest = load_manifest(paths.manifest)
        keystore = load_keystore(paths.machines)
        failures = sync_configs(
            manifest,
            keystore,
            paths.registry,
            local_host=local_host,
            dry_run=dry_run,
            today=_dt.date.today().isoformat(),
            echo=click.echo,
            prune=prune,
            notifier=airuleset_notifier(airuleset_path.expanduser(), notify_owner),
            message_limit=CHUNK_LIMIT,
        )
    except (ManifestError, MachineSourceError, RegistryError, SyncError) as exc:
        raise _fail(exc) from exc
    if failures:
        click.echo(f"{failures} project(s) FAILED — see above; the others are in place.")
        sys.exit(1)


# -- check ------------------------------------------------------------------------------


@click.command("check")
@_status_file_option
@click.option("--gateway-url", default=None, help=_GATEWAY_URL_HELP + " Its /healthz is probed too.")
@_notify_options
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=_LOCAL_HOST_HELP)
@click.option(
    "--releases-url",
    envvar="REMOTEOS_RELEASES_URL",
    default=RELEASES_URL,
    show_default=True,
    help="The GitHub release list; stable is judged from it (a failed read only skips drift for that run).",
)
@click.pass_obj
def check_command(
    paths,
    status_file: Path,
    gateway_url: str | None,
    airuleset_path: Path,
    notify_owner: str | None,
    local_host: str,
    releases_url: str,
) -> None:
    """Probe every machine directly with its stored key + the gateway's /healthz; record the
    states and versions, announce each change ONCE, and report a box older than stable.
    A 401 is repaired only by an explicit recipe."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    gateway_url = _gateway_url(paths, gateway_url, strict=False)  # the watchdog never stops on the manifest
    try:
        report = run_check(
            paths.machines,
            status_file.expanduser(),
            gateway_url=gateway_url,
            notifier=airuleset_notifier(airuleset_path.expanduser(), notify_owner),
            host=local_host,
            drift=DriftPolicy(fetch=lambda: fetch_releases(releases_url), soak_days=soak_days()),
        )
    except (MachineSourceError, CheckError) as exc:
        raise _fail(exc) from exc
    status = report.status
    gateway = status["gateway"]
    click.echo(f"checked {status['checked_at']}: gateway {gateway_url} {gateway['state']} since {gateway['since']}")
    for name, record in status["machines"].items():
        click.echo(f"  {name:24} {record['state']:12} since {record['since']}  {record['detail']}")
    bad = [n for n, r in status["machines"].items() if r["state"] != STATE_OK]
    click.echo(f"{len(status['machines']) - len(bad)} ok, {len(bad)} not ok" + (f": {', '.join(bad)}" if bad else ""))
    if report.sent is False:
        click.echo("a notification did NOT go out — it is retried on the next run")
