"""``remoteos-gateway machine remove`` / ``machine rename`` (#109): a box leaves the fleet or changes name.

    remoteos-gateway machine remove mac-propresenter [--dry-run]       # sold
    remoteos-gateway machine rename win-slovnormal-pc1 win-predaj2 [--dry-run]

Everything is validated before the first write (all or nothing up to there). Then, in this order:
the retired urls go into ``sync-direct.json`` (so the old direct entries stay owned whatever fails
later), the manifest (TEXT, comments kept, written only when it parses to exactly the expected
manifest), the key store, the registry, and the normal ``sync-configs``, which drops the old entries.
``remove`` refuses when a project would be left with no machines (it names the project; the owner
removes or edits it first). ``rename`` keeps org, key, repair recipe, exposure and every consumer's
access, and refuses a NEW name still left in the registry (a leftover would hand the box to it).

A step that fails says which steps were done and what finishes the job: before the registry step,
the SAME command (each step skips what is already done); after it, ``sync-configs``. ``check``
forgets the old name on its next run: its status file and fleet notices are built from the key store.

A dry run writes nothing, not even the manifest lock (the sync runs dry on the edited manifest and key
store, in memory). Output holds names and urls only, never a key or token.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import socket
import sys

import click

from remoteos.gateway.cli_fleet import LOCAL_HOST_HELP, RELOAD_NOTE, fail, machine
from remoteos.gateway.direct_record import load_record, record_path, remember_retired
from remoteos.gateway.keystore import KeystoreError, load_keystore
from remoteos.gateway.keystore import remove_machines as keystore_remove
from remoteos.gateway.keystore import rename_machine as keystore_rename
from remoteos.gateway.manifest import MODE_DIRECT, Manifest, ManifestError, load_manifest
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

_ERRORS = (ManifestError, MachineSourceError, RegistryError, SyncError, OSError)


class _Steps:
    """The steps done so far, so a failure says where it stopped and that a rerun finishes it."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.done: list[str] = []

    def fail(self, exc: Exception) -> click.ClickException:
        if not self.done:
            return fail(exc)
        logger.error("%s stopped after %s: %s", self.command, ", ".join(self.done), exc)
        # every edit is in place once the registry step is done: only the sync is left to repeat
        rerun = "sync-configs" if "registry" in self.done else self.command
        done = ", ".join(self.done)
        return click.ClickException(f"{exc}\n  done: {done} — rerun `remoteos-gateway {rerun}` to finish")


def _in_registry(registry: Registry, name: str) -> bool:
    return name in registry.exposed or any(name in c.machines for c in registry.consumers.values())


def _recorded(paths, name: str, url: str) -> bool:
    record, _ = load_record(record_path(paths.registry))
    return any(entries.get(name) == url for entries in record.values())


def _retire_first(paths, manifest: Manifest, retired: dict[str, str]) -> bool:
    """Record the retired direct urls before anything else changes (under the registry lock).
    False when there is nothing to record (no direct project)."""
    direct = [p.name for p in manifest.projects.values() if p.mode == MODE_DIRECT]
    if not direct or not retired:
        return False  # nothing to record: no lock taken either
    with registry_transaction(paths.registry):
        remember_retired(record_path(paths.registry), direct, retired)
    return True


def _edit_registry(paths, edit):
    """Apply *edit* to the locked registry; saved only when it changed (a save makes the gateway reload)."""
    with registry_transaction(paths.registry) as transaction:
        before = json.dumps(transaction.registry.to_json(), sort_keys=True)
        result = edit(transaction.registry)
        if json.dumps(transaction.registry.to_json(), sort_keys=True) != before:
            transaction.save()
    return result


def _sync(paths, manifest: Manifest, keystore: dict[str, MachineEntry], *, local_host: str, dry_run: bool) -> None:
    click.echo("sync-configs" + (" (dry run):" if dry_run else ":"))
    failures = sync_configs(
        manifest,
        keystore,
        paths.registry,
        local_host=local_host,
        dry_run=dry_run,
        today=_dt.date.today().isoformat(),
        echo=click.echo,
    )
    if failures:
        click.echo(f"{failures} project(s) FAILED — see above; rerun `remoteos-gateway sync-configs` once fixed.")
        sys.exit(1)


def _projects_holding(manifest: Manifest, names: set[str]) -> list[str]:
    return sorted(p.name for p in manifest.projects.values() if names & set(p.machines))


# -- remove ---------------------------------------------------------------------------------------


@machine.command("remove")
@click.argument("names", nargs=-1, required=True)
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=LOCAL_HOST_HELP)
@click.option("--dry-run", is_flag=True, help="Show what would change; write nothing.")
@click.pass_obj
def machine_remove(paths, names: tuple[str, ...], local_host: str, dry_run: bool) -> None:
    """Take machines NAMES out of the fleet: the manifest, the key store, the registry, every config."""
    wanted = list(dict.fromkeys(names))
    steps = _Steps("machine remove " + " ".join(wanted))
    try:
        keystore = load_keystore(paths.machines)
        before = load_manifest(paths.manifest)
        registry = load_registry(paths.registry)
        # a name the key store already lost is a rerun after a failed step, if anything still holds it
        unknown = [
            n
            for n in wanted
            if n not in keystore and not _projects_holding(before, {n}) and not _in_registry(registry, n)
        ]
        if unknown:
            raise KeystoreError(f"not in the key store {paths.machines}: {', '.join(unknown)} — nothing changed")
        manifest = manifest_remove(paths.manifest, wanted, dry_run=True)  # refuses before anything is written
        projects = _projects_holding(before, set(wanted))
        retired = {name: keystore[name].url for name in wanted if name in keystore}
        in_store = [name for name in wanted if name in keystore]
        if dry_run:
            touched = {name: registry.forget_machine(name) for name in wanted}
            _report_remove(wanted, retired, projects, touched, dry_run=True)
            after = {name: entry for name, entry in keystore.items() if name not in wanted}
            _sync(paths, manifest, after, local_host=local_host, dry_run=True)
            return
        if _retire_first(paths, before, retired):
            steps.done.append("direct record")
        manifest = manifest_remove(paths.manifest, wanted)
        steps.done.append("manifest")
        if in_store:
            keystore_remove(paths.machines, in_store)
        steps.done.append("key store")
        touched = _edit_registry(paths, lambda r: {name: r.forget_machine(name) for name in wanted})
        steps.done.append("registry")
        _report_remove(wanted, retired, projects, touched, dry_run=False)
        logger.info("machine remove: %s (projects %s)", ", ".join(wanted), ", ".join(projects) or "-")
        _sync(paths, manifest, load_keystore(paths.machines), local_host=local_host, dry_run=False)
    except _ERRORS as exc:
        raise steps.fail(exc) from exc
    click.echo(RELOAD_NOTE)
    click.echo("The next `remoteos-gateway check` forgets them (status file and fleet notices).")


def _report_remove(wanted, retired, projects, touched, *, dry_run: bool) -> None:
    verb = "would remove" if dry_run else "removed"
    for name in wanted:
        consumers = ", ".join(touched[name]) or "none"
        url = retired.get(name, "(already out of the key store)")
        click.echo(f"{name}: {verb} — url {url}; consumers that allowed it: {consumers}")
    click.echo(f"projects: {', '.join(projects) or 'none'}")


# -- rename ---------------------------------------------------------------------------------------


@machine.command("rename")
@click.argument("old")
@click.argument("new")
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname, help=LOCAL_HOST_HELP)
@click.option("--dry-run", is_flag=True, help="Show what would change; write nothing.")
@click.pass_obj
def machine_rename(paths, old: str, new: str, local_host: str, dry_run: bool) -> None:
    """Rename machine OLD to NEW everywhere: key store (url, key, org kept), manifest, exposure, every
    consumer's access, every config (the OLD entry goes, NEW comes)."""
    steps = _Steps(f"machine rename {old} {new}")
    try:
        keystore = load_keystore(paths.machines)
        registry = load_registry(paths.registry)
        fresh = old in keystore and new not in keystore
        # a rerun after the key store step: only with evidence of the earlier run (OLD still in the
        # registry, or the url the first step recorded) — never a typo for OLD next to an existing NEW
        resumed = new in keystore and old not in keystore and (
            _in_registry(registry, old) or _recorded(paths, old, keystore[new].url)
        )
        if not (fresh or resumed):
            if old in keystore:
                problem = f"{new!r} is already in the key store"
            elif new in keystore:
                problem = f"{old!r} is not in the key store and no earlier rename of it is in progress"
            else:
                problem = f"neither {old!r} nor {new!r} is in the key store"
            raise KeystoreError(f"{problem} ({paths.machines}) — nothing changed")
        manifest = manifest_rename(paths.manifest, old, new, dry_run=True)  # validates NEW, refuses before writing
        exposed = old in registry.exposed or (resumed and new in registry.exposed)
        carried = _in_registry(registry, old)  # read before the in-memory rename below changes the copy
        touched = registry.rename_machine(old, new, fresh=fresh)  # in memory: refuses a NEW left in the registry
        projects = _projects_holding(manifest, {new})
        retired = {old: keystore[old].url if fresh else keystore[new].url}
        if dry_run:
            _report_rename(old, new, retired[old], projects, touched, exposed, dry_run=True)
            after = {(new if name == old else name): entry for name, entry in keystore.items()}
            _sync(paths, manifest, after, local_host=local_host, dry_run=True)
            return
        if _retire_first(paths, load_manifest(paths.manifest), retired):
            steps.done.append("direct record")
        manifest = manifest_rename(paths.manifest, old, new)
        steps.done.append("manifest")
        if fresh:
            keystore_rename(paths.machines, old, new)
        steps.done.append("key store")

        def carry_over(r: Registry) -> tuple[list[str], bool]:
            was_exposed = old in r.exposed
            moved = r.rename_machine(old, new, fresh=fresh)
            return moved, was_exposed or new in r.exposed

        if not carried:  # nothing to carry: a failure from here on needs only sync-configs
            steps.done.append("registry")
        touched, exposed = _edit_registry(paths, carry_over)  # rechecked under the lock (saved only on change)
        if carried:
            steps.done.append("registry")
        _report_rename(old, new, retired[old], projects, touched, exposed, dry_run=False)
        logger.info("machine rename: %s -> %s (projects %s)", old, new, ", ".join(projects) or "-")
        _sync(paths, manifest, load_keystore(paths.machines), local_host=local_host, dry_run=False)
    except _ERRORS as exc:
        raise steps.fail(exc) from exc
    click.echo(RELOAD_NOTE)
    click.echo(f"The next `remoteos-gateway check` reports {new} and forgets {old}.")


def _report_rename(old, new, url, projects, touched, exposed, *, dry_run: bool) -> None:
    verb = "would rename" if dry_run else "renamed"
    click.echo(f"{old} -> {new}: {verb} — url {url}; {'exposed' if exposed else 'NOT exposed (kept so)'}")
    click.echo(f"  consumers moved to {new}: {', '.join(touched) or 'none'}; projects: {', '.join(projects) or 'none'}")
