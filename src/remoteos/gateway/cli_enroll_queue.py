"""The permanent enrollment (#101): ``enroll-listen`` and the commands that decide its queue.

    remoteos-gateway enroll-list
    remoteos-gateway enroll-accept predaj3 --name win-predaj3 --org slovnormal --projects pos
    remoteos-gateway enroll-reject 3f9a0c12
    remoteos-gateway enroll-log [ID]

A box runs the README line; its registration waits in the queue (``enroll_queue.py``) and ``check``
puts ONE item into the owner's U while anything waits. ``enroll-accept`` is ``enroll-finish`` on a
queued registration, plus a check that the box at the private address says (``/health``) it is the
host that posted, BEFORE the key is sent there. The name and the projects come from this command
line: nothing the box posted picks them.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import socket
import sys
from pathlib import Path

import click

from remoteos.gateway import cli_fleet
from remoteos.gateway.cli_enroll import _check_request, _project_list, finish_registration
from remoteos.gateway.enroll import EnrollError
from remoteos.gateway.enroll_listen import DEFAULT_LISTEN_HOST, DEFAULT_LISTEN_PORT, create_app
from remoteos.gateway.enroll_queue import DEFAULT_ENROLL_DIR, EnrollQueue
from remoteos.gateway.registry import ORGS


def _enroll_dir_option(function):
    return click.option(
        "--enroll-dir", type=click.Path(file_okay=False, path_type=Path), envvar="REMOTEOS_GATEWAY_ENROLL_DIR",
        default=DEFAULT_ENROLL_DIR, show_default=True,
        help="The pending enrollments and install logs (0700).",
    )(function)


def _when(stamp: float) -> str:
    return _dt.datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # a name, not a literal: refused below
        return False


def _loopback_only(host: str) -> str:
    """The listener sits behind cloudflared on this box: a loopback literal, nothing else."""
    if not _is_loopback(host):
        raise click.BadParameter(f"refusing to bind {host!r}: the enrollment listener binds loopback only "
                                 f"(default {DEFAULT_LISTEN_HOST}); cloudflared publishes it", param_hint="--host")
    return host


@click.command("enroll-listen")
@click.option("--host", default=DEFAULT_LISTEN_HOST, show_default=True)
@click.option("--port", type=click.IntRange(1, 65535), default=DEFAULT_LISTEN_PORT, show_default=True)
@click.option("--log-level", type=click.Choice(["info", "warning", "error"]), default="info", show_default=True)
@_enroll_dir_option
def enroll_listen_command(host: str, port: int, log_level: str, enroll_dir: Path) -> None:
    """Run the permanent enrollment endpoint (foreground; remoteos-gateway-enroll.service runs it).
    No debug level: uvicorn's would print request bodies, and a registration holds a key."""
    _loopback_only(host)
    logging.basicConfig(level=getattr(logging, log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    import uvicorn

    from remoteos import __version__

    logging.getLogger("remoteos.gateway").info("enroll-listen v%s: bind=%s:%d queue=%s", __version__, host, port,
                                               enroll_dir)
    uvicorn.run(create_app(EnrollQueue(enroll_dir)), host=host, port=port, log_config=None, log_level=log_level,
                access_log=False, proxy_headers=False, server_header=False, timeout_graceful_shutdown=5)


@click.command("enroll-list")
@_enroll_dir_option
def enroll_list_command(enroll_dir: Path) -> None:
    """The registrations waiting for enroll-accept (never their keys) and the install logs."""
    queue = EnrollQueue(enroll_dir)
    entries = queue.pending()
    if not entries:
        click.echo("no pending registration")
    for entry in entries:
        r = entry.registration
        addresses = ", ".join(r.addresses) or "no private address (accept needs --url)"
        click.echo(f"{entry.id}  {_when(entry.received_at)}  {r.hostname}  {r.os}  v{r.version}  port {r.port}"
                   f"  ssh account {r.account}  from {entry.source}  addresses: {addresses}")
    logs = queue.logs()
    if logs:
        newest = logs[-1]
        click.echo(f"install logs: {len(logs)}, newest {newest.id} from {newest.hostname} ({newest.os}),"
                   f" exit {newest.exit_code or '?'} at {_when(newest.received_at)} — `remoteos-gateway enroll-log`")


@click.command("enroll-accept")
@click.argument("ref")
@click.option("--name", required=True, help="The machine name (win-, linux- or mac-).")
@click.option("--org", type=click.Choice(ORGS), required=True, help="The company the machine belongs to (#105).")
@click.option("--projects", required=True, help="The projects to add the machine to, comma separated.")
@click.option("--url", default=None, help="The machine's /mcp url, when the addresses it posted cannot be reached.")
@click.option("--replace", is_flag=True, help="Overwrite a machine that is in the key store already.")
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname)
@_enroll_dir_option
@cli_fleet._notify_options
@cli_fleet._status_file_option
@click.pass_context
def enroll_accept_command(ctx: click.Context, ref: str, name: str, org: str, projects: str, url: str | None,
                          replace: bool, local_host: str, enroll_dir: Path, airuleset_path: Path,
                          notify_owner: str | None, notify_project_dir: Path, notices_file: Path,
                          status_file: Path) -> None:
    """Enroll the pending registration REF (its id, or its hostname when only one has it)."""
    wanted = _project_list(projects)
    _check_request(ctx.obj, name, org, wanted, replace, url)
    queue = EnrollQueue(enroll_dir)
    try:
        entry = queue.get(ref)
    except EnrollError as exc:
        raise click.ClickException(str(exc)) from exc
    notify = {"airuleset_path": airuleset_path, "notify_owner": notify_owner,
              "notify_project_dir": notify_project_dir, "notices_file": notices_file}
    try:
        finish_registration(ctx, entry.registration, name=name, org=org, projects=wanted, url=url,
                            local_host=local_host, notify=notify, status_file=status_file, check_hostname=True)
    except click.ClickException as exc:
        exc.message += (f" — the registration {entry.id} stays queued: fix the cause and run enroll-accept again"
                        " (with --replace once the key is stored)")
        raise
    queue.remove(entry.id)
    click.echo(f"{name} is enrolled (registration {entry.id} done)")


@click.command("enroll-reject")
@click.argument("entry_id")
@_enroll_dir_option
def enroll_reject_command(entry_id: str, enroll_dir: Path) -> None:
    """Delete the pending registration ENTRY_ID (its key goes with it)."""
    if not EnrollQueue(enroll_dir).remove(entry_id):
        raise click.ClickException("no pending registration with that id: see `remoteos-gateway enroll-list`")
    click.echo(f"registration {entry_id} rejected and deleted")


@click.command("enroll-log")
@click.argument("log_id", required=False)
@_enroll_dir_option
def enroll_log_command(log_id: str | None, enroll_dir: Path) -> None:
    """List the install logs boxes posted, or print the one LOG_ID."""
    queue = EnrollQueue(enroll_dir)
    if log_id is None:
        logs = queue.logs()
        if not logs:
            click.echo("no install log")
        for entry in logs:
            click.echo(f"{entry.id}  {_when(entry.received_at)}  {entry.hostname}  {entry.os}"
                       f"  exit {entry.exit_code or '?'}  {entry.size} chars  from {entry.source}")
        return
    try:
        click.echo(queue.read_log(log_id), nl=False)
    except EnrollError as exc:
        raise click.ClickException(str(exc)) from exc
