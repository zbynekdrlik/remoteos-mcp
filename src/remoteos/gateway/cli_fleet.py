"""``remoteos-gateway`` fleet commands (#51): the key store, generated configs, the drift check.

    remoteos-gateway machine set-key linux-cam2 < key-file      # the key on STDIN, never an argv
    remoteos-gateway machine set-key new-box --url http://new-box.lan:8092/mcp < key-file
    remoteos-gateway machine list                                # keys masked
    remoteos-gateway machine import [--from <file.mcp.json> ...] [--dry-run]
    remoteos-gateway machine repair-recipe linux-cam2 --ssh root@cam2.lan [--sudo]

The commands are registered on ``cli.main`` and receive its paths object (``--registry``,
``--machines`` = the key store). No key or token is ever printed: output shows
``mask_secret`` (length + a sha256 prefix, comparable with ``remoteos-gateway list``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

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
from remoteos.gateway.probe import STATE_OK
from remoteos.gateway.registry import (
    LEGACY_MACHINES_PATH,
    MachineEntry,
    MachineSourceError,
    RegistryError,
    RepairRecipe,
    edit_registry,
    parse_repair,
)

DEFAULT_GATEWAY_URL = "http://100.104.8.125:8796"  # dev1's tailscale address + the gateway port

_RELOAD_NOTE = "A running gateway applies this on its next request (or SIGHUP) — no restart needed."


def _fail(exc: Exception) -> click.ClickException:
    return click.ClickException(str(exc))


def _expose(paths, machines: list[str]) -> list[str]:
    """Expose *machines* in the registry; returns the ones that were not exposed before."""
    try:
        with edit_registry(paths.registry) as registry:
            return [m for m in machines if registry.expose(m)]
    except RegistryError as exc:
        raise _fail(exc) from exc


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
        newly = _expose(paths, [name])
        if newly:
            click.echo(f"  {name} exposed in the registry (add it to a project in projects.toml, then sync-configs).")
    click.echo(_RELOAD_NOTE)
    click.echo(
        "Projects routed through the gateway use the new key on their next call — no config change, no reconnect."
        " A `direct` project (remoteos-mcp) holds its own copy: run `remoteos-gateway sync-configs` for it."
    )


@machine.command("list")
@click.pass_obj
def machine_list(paths) -> None:
    """Show the key store: url, the key MASKED, the repair recipe."""
    try:
        entries = load_keystore(paths.machines, missing_ok=True)
    except MachineSourceError as exc:
        raise _fail(exc) from exc
    click.echo(f"key store: {paths.machines}  ({len(entries)} machine(s))")
    for name, entry in sorted(entries.items()):
        repair = _describe_recipe(entry.repair) if entry.repair else "-"
        click.echo(f"  {name}  {entry.url}  key {mask_secret(entry.key)}  repair: {repair}")
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
@click.option("--match", default=DEFAULT_IMPORT_MATCH, show_default=True, help="Only entries whose name fully matches.")
@click.option(
    "--gateway-url", default=DEFAULT_GATEWAY_URL, show_default=True, help="Entries pointing here are skipped (tokens)."
)
@click.option("--dry-run", is_flag=True, help="Report the choice per machine; write nothing.")
@click.pass_obj
def machine_import(paths, sources: tuple[Path, ...], match: str, gateway_url: str, dry_run: bool) -> None:
    """Build the key store from per-project MCP configs. For each machine, the copy the machine
    ACCEPTS (initialize -> 200) wins; conflicts and unverified machines are reported."""
    files = list(sources) or sorted(LEGACY_MACHINES_PATH.parent.glob("*.mcp.json"))
    texts: list[tuple[str, str]] = []
    for path in files:
        try:
            texts.append((str(path), Path(path).expanduser().read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            click.echo(f"  {path}: cannot read ({type(exc).__name__}) — skipped")
    _import_and_report(paths, texts, match=match, gateway_url=gateway_url, dry_run=dry_run)


def _import_and_report(paths, texts: list[tuple[str, str]], *, match: str, gateway_url: str, dry_run: bool) -> None:
    scan = scan_sources(texts, match=match, skip_url_prefix=gateway_url)
    try:
        existing = load_keystore(paths.machines, missing_ok=True)
    except MachineSourceError as exc:
        raise _fail(exc) from exc
    decisions = resolve_import(scan, existing)
    click.echo(f"read {len(texts)} config(s); {len(decisions)} machine(s) found")
    for note in scan.notes:
        click.echo(f"  note: {note}")
    for decision in decisions:
        click.echo(describe_decision(decision))
    flagged = [d.name for d in decisions if d.flagged]
    if flagged:
        click.echo(f"{len(flagged)} machine(s) NOT verified: {', '.join(flagged)} — fix them before routing projects")
    if dry_run:
        click.echo("dry run — nothing written")
        return
    apply_import(paths.machines, decisions)
    click.echo(f"key store {paths.machines} written ({len(decisions)} machine(s) imported)")
    newly = _expose(paths, [d.name for d in decisions])
    if newly:
        click.echo(f"exposed in the registry: {', '.join(newly)}")
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
