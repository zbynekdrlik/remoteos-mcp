"""``remoteos-gateway machine remove`` / ``machine rename`` (#109): a box leaves the fleet or changes name.

    remoteos-gateway machine remove mac-propresenter [--dry-run]       # sold
    remoteos-gateway machine rename win-slovnormal-pc1 win-predaj2 [--dry-run]

Both are all or nothing and edit, in this order: the manifest (TEXT, comments kept, written only
when it parses to exactly the expected manifest), the key store, the registry, then the normal
``sync-configs``. ``remove`` refuses when a project would be left with no machines (it names the
project; the owner removes or edits it first). ``rename`` keeps org, key, repair recipe, exposure
and every consumer's access. The sync drops the old direct entries (``sync-direct.json``, plus the
``retired`` urls for a removal made before that record existed). ``check`` forgets the old name on
its next run: its status file and fleet notices are built from the key store.

A dry run edits nothing (the sync runs dry on the edited manifest and key store, in memory). Output
holds names and urls only, never a key or token.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import socket
import sys

import click

from remoteos.gateway.cli_fleet import _LOCAL_HOST_HELP, _RELOAD_NOTE, _fail, machine
from remoteos.gateway.keystore import KeystoreError, load_keystore
from remoteos.gateway.keystore import remove_machines as keystore_remove
from remoteos.gateway.keystore import rename_machine as keystore_rename
from remoteos.gateway.manifest import Manifest, ManifestError, load_manifest
from remoteos.gateway.manifest import remove_machines as manifest_remove
from remoteos.gateway.manifest import rename_machine as manifest_rename
from remoteos.gateway.registry import (
    MachineEntry,
    MachineSourceError,
    Registry,
    RegistryError,
    load_registry,
    registry_transaction,
)
from remoteos.gateway.sync import SyncError, sync_configs

logger = logging.getLogger("remoteos.gateway.machine_edit")

_ERRORS = (ManifestError, MachineSourceError, RegistryError, SyncError)


def _projects_holding(manifest: Manifest, names: set[str]) -> list[str]:
    return sorted(p.name for p in manifest.projects.values() if names & set(p.machines))


def _edit_registry(paths, edit) -> list[str]:
    """Apply *edit* to the locked registry; saved only when it changed (a save makes the gateway reload)."""
    with registry_transaction(paths.registry) as transaction:
        before = json.dumps(transaction.registry.to_json(), sort_keys=True)
        touched = edit(transaction.registry)
        if json.dumps(transaction.registry.to_json(), sort_keys=True) != before:
            transaction.save()
    return touched


def _sync(paths, manifest: Manifest, keystore: dict[str, MachineEntry], *, local_host: str, dry_run: bool,
          retired: dict[str, str]) -> None:
    click.echo("sync-configs" + (" (dry run):" if dry_run else ":"))
    failures = sync_configs(
        manifest,
        keystore,
        paths.registry,
        local_host=local_host,
        dry_run=dry_run,
        today=_dt.date.today().isoformat(),
        echo=click.echo,
        retired=retired,
    )
    if failures:
        click.echo(f"{failures} project(s) FAILED — see above; rerun `remoteos-gateway sync-configs` once fixed.")
        sys.exit(1)


@machine.command("remove")
@click.argument("names", nargs=-1, required=True)
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=_LOCAL_HOST_HELP)
@click.option("--dry-run", is_flag=True, help="Show what would change; write nothing.")
@click.pass_obj
def machine_remove(paths, names: tuple[str, ...], local_host: str, dry_run: bool) -> None:
    """Take machines NAMES out of the fleet: the manifest, the key store, the registry, every config."""
    wanted = list(dict.fromkeys(names))
    try:
        keystore = load_keystore(paths.machines)
        unknown = [name for name in wanted if name not in keystore]
        if unknown:
            raise KeystoreError(f"not in the key store {paths.machines}: {', '.join(unknown)} — nothing changed")
        projects = _projects_holding(load_manifest(paths.manifest), set(wanted))
        manifest = manifest_remove(paths.manifest, wanted, dry_run=True)  # refuses before anything is written
        retired = {name: keystore[name].url for name in wanted}
        if dry_run:
            touched = {name: load_registry(paths.registry).forget_machine(name) for name in wanted}
            _report_remove(wanted, retired, projects, touched, dry_run=True)
            after = {name: entry for name, entry in keystore.items() if name not in wanted}
            _sync(paths, manifest, after, local_host=local_host, dry_run=True, retired=retired)
            return
        manifest = manifest_remove(paths.manifest, wanted)
        keystore_remove(paths.machines, wanted)
        touched = {name: _edit_registry(paths, lambda r, n=name: r.forget_machine(n)) for name in wanted}
        _report_remove(wanted, retired, projects, touched, dry_run=False)
        logger.info("machine remove: %s (projects %s)", ", ".join(wanted), ", ".join(projects) or "-")
        _sync(paths, manifest, load_keystore(paths.machines), local_host=local_host, dry_run=False, retired=retired)
    except _ERRORS as exc:
        raise _fail(exc) from exc
    click.echo(_RELOAD_NOTE)
    click.echo("The next `remoteos-gateway check` forgets them (status file and fleet notices).")


def _report_remove(wanted, retired, projects, touched, *, dry_run: bool) -> None:
    verb = "would remove" if dry_run else "removed"
    for name in wanted:
        consumers = ", ".join(touched[name]) or "none"
        click.echo(f"{name}: {verb} — url {retired[name]}; consumers that allowed it: {consumers}")
    click.echo(f"projects: {', '.join(projects) or 'none'}")


@machine.command("rename")
@click.argument("old")
@click.argument("new")
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=_LOCAL_HOST_HELP)
@click.option("--dry-run", is_flag=True, help="Show what would change; write nothing.")
@click.pass_obj
def machine_rename(paths, old: str, new: str, local_host: str, dry_run: bool) -> None:
    """Rename machine OLD to NEW everywhere: key store (url, key, org kept), manifest, exposure, every
    consumer's access, every config (the OLD entry goes, NEW comes)."""
    try:
        keystore = load_keystore(paths.machines)
        if old not in keystore:
            raise KeystoreError(f"machine {old!r} is not in the key store {paths.machines} — nothing changed")
        if new in keystore:
            raise KeystoreError(f"machine {new!r} is already in the key store {paths.machines} — nothing changed")
        manifest = manifest_rename(paths.manifest, old, new, dry_run=True)  # validates NEW, refuses before writing
        projects = _projects_holding(manifest, {new})
        retired = {old: keystore[old].url}
        if dry_run:
            registry: Registry = load_registry(paths.registry)
            exposed = old in registry.exposed
            touched = registry.rename_machine(old, new)
            _report_rename(old, new, retired[old], projects, touched, exposed, dry_run=True)
            after = {(new if name == old else name): entry for name, entry in keystore.items()}
            _sync(paths, manifest, after, local_host=local_host, dry_run=True, retired=retired)
            return
        manifest = manifest_rename(paths.manifest, old, new)
        keystore_rename(paths.machines, old, new)
        exposed = old in load_registry(paths.registry).exposed
        touched = _edit_registry(paths, lambda r: r.rename_machine(old, new))
        _report_rename(old, new, retired[old], projects, touched, exposed, dry_run=False)
        logger.info("machine rename: %s -> %s (projects %s)", old, new, ", ".join(projects) or "-")
        _sync(paths, manifest, load_keystore(paths.machines), local_host=local_host, dry_run=False, retired=retired)
    except _ERRORS as exc:
        raise _fail(exc) from exc
    click.echo(_RELOAD_NOTE)
    click.echo(f"The next `remoteos-gateway check` reports {new} and forgets {old}.")


def _report_rename(old, new, url, projects, touched, exposed, *, dry_run: bool) -> None:
    verb = "would rename" if dry_run else "renamed"
    click.echo(f"{old} -> {new}: {verb} — url {url}; {'exposed' if exposed else 'NOT exposed (kept so)'}")
    click.echo(f"  consumers moved to {new}: {', '.join(touched) or 'none'}; projects: {', '.join(projects) or 'none'}")
