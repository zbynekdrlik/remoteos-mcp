"""``remoteos-gateway`` — run the MCP gateway and manage who may reach which machine.

    remoteos-gateway serve                                   # bind 100.104.8.125:8796
    remoteos-gateway consumer add david3@subdev --sha256 <64-hex>
    remoteos-gateway expose win-pjsnvs-pokladna
    remoteos-gateway allow david3@subdev win-pjsnvs-pokladna
    remoteos-gateway list
    remoteos-gateway log --consumer david3@subdev --tail 50
    remoteos-gateway deny david3@subdev win-pjsnvs-pokladna
    remoteos-gateway consumer revoke david3@subdev [--sha256 <64-hex>]

Every edit is written atomically to the registry; a running gateway picks it up on its next
request (file change) or on SIGHUP — no restart needed.

The fleet commands (#51) — ``machine set-key|list|import|repair-recipe``, ``sync-configs``
and ``check`` — live in ``cli_fleet.py`` and are registered on this group.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from remoteos import __version__
from remoteos.gateway import cli_fleet
from remoteos.gateway.accesslog import DEFAULT_ACCESS_LOG_PATH, read_access_log
from remoteos.gateway.registry import (
    DEFAULT_MACHINES_PATH,
    DEFAULT_REGISTRY_PATH,
    MachineSourceError,
    RegistryError,
    edit_registry,
    load_machines,
    load_registry,
)

DEFAULT_HOST = "100.104.8.125"  # dev1's tailscale address — never the LAN, never 0.0.0.0
DEFAULT_PORT = 8796

_RELOAD_NOTE = "A running gateway applies this on its next request (or SIGHUP) — no restart needed."


@dataclass
class _Paths:
    registry: Path
    machines: Path
    access_log: Path


def check_bind_host(host: str) -> str:
    """Refuse a wildcard bind: the gateway must listen on the tailscale address only."""
    candidate = host.strip().strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
        mapped = getattr(address, "ipv4_mapped", None)  # ::ffff:0.0.0.0 is a wildcard too
        unspecified = address.is_unspecified or (mapped is not None and mapped.is_unspecified)
    except ValueError:
        # Not a canonical literal: also catch the wildcard spellings the resolver accepts
        # ("", "*", and inet_aton shorthands such as "0" or "0.0").
        unspecified = candidate in ("", "*")
        if not unspecified:
            try:
                unspecified = socket.inet_aton(candidate) == b"\x00\x00\x00\x00"
            except OSError:
                unspecified = False
    if unspecified:
        raise click.BadParameter(
            f"refusing to bind {host!r} (all interfaces). The gateway must listen on dev1's "
            f"tailscale address only (default {DEFAULT_HOST}), never 0.0.0.0 / :: / the LAN.",
            param_hint="--host",
        )
    return host


@click.group()
@click.option(
    "--registry",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="REMOTEOS_GATEWAY_REGISTRY",
    default=DEFAULT_REGISTRY_PATH,
    show_default=True,
    help="Consumer/exposure registry (JSON, 0600).",
)
@click.option(
    "--machines",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="REMOTEOS_GATEWAY_MACHINES",
    default=DEFAULT_MACHINES_PATH,
    show_default=True,
    help="The key store (machine url + key). A legacy MCP config (mcpServers) is still read.",
)
@click.option(
    "--access-log",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="REMOTEOS_GATEWAY_ACCESS_LOG",
    default=DEFAULT_ACCESS_LOG_PATH,
    show_default=True,
    help="JSONL access log.",
)
@click.version_option(__version__, prog_name="remoteos-gateway")
@click.pass_context
def main(ctx: click.Context, registry: Path, machines: Path, access_log: Path) -> None:
    """remoteos MCP gateway — expose managed machines to remote fleet consumers."""
    ctx.obj = _Paths(registry=registry.expanduser(), machines=machines.expanduser(), access_log=access_log.expanduser())


# -- serve ----------------------------------------------------------------------------


@main.command()
@click.option("--host", envvar="REMOTEOS_GATEWAY_HOST", default=DEFAULT_HOST, show_default=True)
@click.option("--port", envvar="REMOTEOS_GATEWAY_PORT", type=int, default=DEFAULT_PORT, show_default=True)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"]),
    default="info",
    show_default=True,
)
@click.pass_obj
def serve(paths: _Paths, host: str, port: int, log_level: str) -> None:
    """Run the gateway (foreground; the user systemd unit runs this)."""
    check_bind_host(host)
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("remoteos.gateway")

    import uvicorn

    from remoteos.gateway.accesslog import AccessLog
    from remoteos.gateway.app import create_app
    from remoteos.gateway.registry import LiveConfig

    log.info(
        "remoteos-gateway v%s: bind=%s:%d registry=%s machines=%s access_log=%s",
        __version__,
        host,
        port,
        paths.registry,
        paths.machines,
        paths.access_log,
    )
    try:
        config = LiveConfig(paths.registry, paths.machines)
    except (RegistryError, MachineSourceError) as exc:
        log.error("refusing to start: %s", exc)
        raise click.ClickException(f"refusing to start: {exc}") from exc
    app = create_app(config, AccessLog(paths.access_log))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # our logging.basicConfig above formats uvicorn's loggers too
        log_level=log_level,
        access_log=False,  # the gateway writes its own access log (no token, no arguments)
        proxy_headers=False,  # nothing sits in front of the gateway; never trust X-Forwarded-*
        server_header=False,  # upstream Server/Date headers pass through unchanged
        date_header=False,
        timeout_graceful_shutdown=5,
    )


# -- registry edits ---------------------------------------------------------------------


def _known_machines(paths: _Paths) -> dict:
    try:
        return load_machines(paths.machines)
    except MachineSourceError as exc:
        raise click.ClickException(str(exc)) from exc


def _require_known_machine(paths: _Paths, machine: str) -> None:
    if machine not in _known_machines(paths):
        raise click.ClickException(
            f"machine {machine!r} is not in the machine source {paths.machines} — check the name "
            "(`remoteos-gateway list` shows the exposed ones)"
        )


@main.group()
def consumer() -> None:
    """Register / revoke consumers (by token HASH only)."""


@consumer.command("add")
@click.argument("name")
@click.option("--sha256", "sha256", required=True, help="sha256 hex digest of the consumer's token (64 hex chars).")
@click.pass_obj
def consumer_add(paths: _Paths, name: str, sha256: str) -> None:
    """Register NAME with a token hash (adding a second hash = token rotation)."""
    try:
        with edit_registry(paths.registry) as registry:
            added = registry.add_consumer(name, sha256)
            machines = registry.consumers[name].machines
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    if added:
        click.echo(f"consumer {name!r}: token hash {sha256.strip().lower()[:12]}... registered.")
    else:
        click.echo(f"consumer {name!r}: that token hash was already registered — nothing changed.")
    if not machines:
        click.echo(f"  {name!r} has no machines yet — grant one with: remoteos-gateway allow {name} <machine>")
    click.echo(_RELOAD_NOTE)


@consumer.command("revoke")
@click.argument("name")
@click.option("--sha256", "sha256", default=None, help="Revoke only this token hash (default: the whole consumer).")
@click.pass_obj
def consumer_revoke(paths: _Paths, name: str, sha256: str | None) -> None:
    """Revoke consumer NAME entirely, or one of its token hashes."""
    try:
        with edit_registry(paths.registry) as registry:
            message = registry.revoke_consumer(name, sha256)
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(message + ".")
    click.echo(_RELOAD_NOTE)


@main.command()
@click.argument("consumer_name", metavar="CONSUMER")
@click.argument("machine")
@click.pass_obj
def allow(paths: _Paths, consumer_name: str, machine: str) -> None:
    """Allow CONSUMER to reach MACHINE."""
    _require_known_machine(paths, machine)
    try:
        with edit_registry(paths.registry) as registry:
            changed = registry.allow(consumer_name, machine)
            exposed = machine in registry.exposed
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{consumer_name!r} -> {machine!r}: " + ("allowed." if changed else "was already allowed."))
    if not exposed:
        click.echo(
            f"  note: {machine!r} is not exposed yet — requests get 404 until: remoteos-gateway expose {machine}"
        )
    click.echo(_RELOAD_NOTE)


@main.command()
@click.argument("consumer_name", metavar="CONSUMER")
@click.argument("machine")
@click.pass_obj
def deny(paths: _Paths, consumer_name: str, machine: str) -> None:
    """Remove MACHINE from CONSUMER's allow-list."""
    try:
        with edit_registry(paths.registry) as registry:
            changed = registry.deny(consumer_name, machine)
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{consumer_name!r} -> {machine!r}: " + ("denied." if changed else "was not allowed — nothing changed."))
    click.echo(_RELOAD_NOTE)


@main.command()
@click.argument("machine")
@click.pass_obj
def expose(paths: _Paths, machine: str) -> None:
    """Make MACHINE routable through the gateway (it must exist in the machine source)."""
    _require_known_machine(paths, machine)
    try:
        with edit_registry(paths.registry) as registry:
            changed = registry.expose(machine)
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{machine!r}: " + ("exposed." if changed else "was already exposed."))
    click.echo(_RELOAD_NOTE)


@main.command()
@click.argument("machine")
@click.pass_obj
def unexpose(paths: _Paths, machine: str) -> None:
    """Stop routing MACHINE for everyone (kill switch; allow-lists are kept)."""
    try:
        with edit_registry(paths.registry) as registry:
            changed = registry.unexpose(machine)
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{machine!r}: " + ("no longer exposed." if changed else "was not exposed — nothing changed."))
    click.echo(_RELOAD_NOTE)


# -- inspection -----------------------------------------------------------------------


@main.command("list")
@click.pass_obj
def list_cmd(paths: _Paths) -> None:
    """Show exposed machines and consumers (hash prefixes only)."""
    try:
        registry = load_registry(paths.registry)
    except RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        machines = load_machines(paths.machines)
        machines_error = None
    except MachineSourceError as exc:
        machines, machines_error = {}, str(exc)

    click.echo(f"registry: {paths.registry}")
    click.echo(f"machines: {paths.machines}" + (f"  (UNREADABLE: {machines_error})" if machines_error else ""))
    click.echo("")
    click.echo(f"exposed machines ({len(registry.exposed)}):")
    for machine in registry.exposed:
        upstream = machines.get(machine)
        target = upstream.url if upstream else "MISSING from the machine source — requests get 503"
        click.echo(f"  {machine}  ->  {target}")
    if not registry.exposed:
        click.echo("  (none)")
    click.echo("")
    click.echo(f"consumers ({len(registry.consumers)}):")
    for name, entry in sorted(registry.consumers.items()):
        hashes = ", ".join(h[:12] + "..." for h in entry.sha256) or "NONE (cannot authenticate)"
        allowed = ", ".join(entry.machines) or "none"
        click.echo(f"  {name}  added {entry.added or '?'}")
        click.echo(f"    token hashes: {hashes}")
        click.echo(f"    machines:     {allowed}")
    if not registry.consumers:
        click.echo("  (none)")


@main.command("log")
@click.option("--consumer", "consumer_name", default=None, help="Only this consumer's requests.")
@click.option("--tail", type=click.IntRange(min=1), default=20, show_default=True, help="Last N records.")
@click.pass_obj
def log_cmd(paths: _Paths, consumer_name: str | None, tail: int) -> None:
    """Show the access log (time, consumer, machine, method, tool, status, duration)."""
    records = read_access_log(paths.access_log, consumer=consumer_name, tail=tail)
    if not records:
        click.echo(f"no matching records in {paths.access_log}")
        return
    for r in records:
        rpc = _printable(r.get("rpc_method") or "-")
        if r.get("tool"):
            rpc = f"{rpc}({_printable(r['tool'])})"
        click.echo(
            f"{_printable(r.get('ts', '?'))}  {_printable(r.get('consumer') or '-')}  "
            f"{_printable(r.get('machine', '?'))}  {_printable(r.get('http_method', '?'))} {rpc}  "
            f"status={_printable(r.get('status'))}  {_printable(r.get('outcome', '?'))}  "
            f"{_printable(r.get('duration_ms', '?'))}ms"
        )


def _printable(value: object) -> str:
    """Log fields may hold consumer-supplied text: never echo raw control characters."""
    text = str(value)
    if text.isprintable():
        return text
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


main.add_command(cli_fleet.machine)

if __name__ == "__main__":  # pragma: no cover
    main()
