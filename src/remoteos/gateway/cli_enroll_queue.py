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
import re
import socket
import sys
from pathlib import Path

import click

from remoteos.gateway import cli_fleet
from remoteos.gateway.cli_enroll import _check_request, _project_list, finish_registration
from remoteos.gateway.enroll import EnrollError
from remoteos.gateway.enroll_listen import DEFAULT_LISTEN_HOST, DEFAULT_LISTEN_PORT, create_app
from remoteos.gateway.enroll_queue import DEFAULT_ENROLL_DIR, EnrollQueue, source_group, source_text
from remoteos.gateway.registry import ORGS


def _enroll_dir_option(function):
    return click.option(
        "--enroll-dir", type=click.Path(file_okay=False, path_type=Path), envvar="REMOTEOS_GATEWAY_ENROLL_DIR",
        default=DEFAULT_ENROLL_DIR, show_default=True,
        help="The pending enrollments and install logs (0700).",
    )(function)


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """Text a box posted, safe for the operator's terminal: control characters (an ESC sequence could
    rewrite the screen) become ``?``; newlines and tabs stay."""
    return _CONTROL.sub("?", text)


def _queue_error(queue: EnrollQueue, exc: OSError) -> click.ClickException:
    return click.ClickException(f"the enrollment queue {queue.root} cannot be read or written"
                                f" ({type(exc).__name__}: {exc.strerror or 'error'})")


def _pending(queue: EnrollQueue) -> list:
    try:
        return queue.pending()
    except OSError as exc:
        raise _queue_error(queue, exc) from exc


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
    entries = _pending(queue)
    if not entries:
        click.echo("no pending registration")
    for entry in entries:
        r = entry.registration
        addresses = ", ".join(r.addresses) or "no private address (accept needs --url)"
        click.echo(f"{entry.id}  {_when(entry.received_at)}  {r.hostname}  {r.os}  v{r.version}  port {r.port}"
                   f"  ssh account {r.account}  from {entry.source}  addresses: {addresses}")
    try:
        logs = queue.logs()
    except OSError as exc:
        raise _queue_error(queue, exc) from exc
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
    except OSError as exc:
        raise _queue_error(queue, exc) from exc
    click.echo(f"registration {entry.id}: posted from {entry.source} at {_when(entry.received_at)}")
    notify = {"airuleset_path": airuleset_path, "notify_owner": notify_owner,
              "notify_project_dir": notify_project_dir, "notices_file": notices_file}
    stored = []

    def drop_registration() -> None:  # the key store holds the key now: the queued copy goes
        stored.append(True)
        try:
            queue.remove(entry.id)
        except OSError as exc:  # the machine is enrolled anyway: say what is left, go on
            click.echo(f"[!] registration {entry.id} could not be removed from the queue ({type(exc).__name__}):"
                       f" reject it by hand with `remoteos-gateway enroll-reject {entry.id}`")
            return
        click.echo(f"registration {entry.id} done: removed from the queue")

    try:
        finish_registration(ctx, entry.registration, name=name, org=org, projects=wanted, url=url,
                            local_host=local_host, notify=notify, status_file=status_file, check_hostname=True,
                            enroll_dir=enroll_dir, after_store=drop_registration)
    except click.ClickException as exc:
        if stored:
            exc.message += f" — {name} is stored and in the projects: fix the cause, then run sync-configs / check"
        else:
            exc.message += (f" — the registration {entry.id} stays queued: fix the cause and run enroll-accept"
                            " again (with --replace once the key is stored)")
        raise
    click.echo(f"{name} is enrolled")


@click.command("enroll-reject")
@click.argument("entry_id", required=False)
@click.option("--source", default=None,
              help="Reject every registration posted from this address (IPv6: its whole /64, as the limits count).")
@click.option("--all", "reject_all", is_flag=True, help="Reject every pending registration.")
@_enroll_dir_option
def enroll_reject_command(entry_id: str | None, source: str | None, reject_all: bool, enroll_dir: Path) -> None:
    """Delete the pending registration ENTRY_ID, or all from --source, or --all (their keys go with them)."""
    if sum((entry_id is not None, source is not None, reject_all)) != 1:
        raise click.UsageError("name ONE of: an id, --source <address>, --all")
    queue = EnrollQueue(enroll_dir)
    if entry_id is not None:
        try:
            removed = queue.remove(entry_id)
        except OSError as exc:
            raise _queue_error(queue, exc) from exc
        if not removed:
            raise click.ClickException("no pending registration with that id: see `remoteos-gateway enroll-list`")
        click.echo(f"registration {entry_id} rejected and deleted")
        return
    group = source_group(source_text(source)) if source is not None else None
    chosen = [entry for entry in _pending(queue) if reject_all or source_group(entry.source) == group]
    try:
        for entry in chosen:
            queue.remove(entry.id)
    except OSError as exc:
        raise _queue_error(queue, exc) from exc
    click.echo(f"{len(chosen)} registration(s) rejected and deleted")


@click.command("enroll-log")
@click.argument("log_id", required=False)
@_enroll_dir_option
def enroll_log_command(log_id: str | None, enroll_dir: Path) -> None:
    """List the install logs boxes posted, or print the one LOG_ID."""
    queue = EnrollQueue(enroll_dir)
    try:
        _print_logs(queue, log_id)
    except OSError as exc:
        raise _queue_error(queue, exc) from exc


def _print_logs(queue: EnrollQueue, log_id: str | None) -> None:
    if log_id is None:
        logs = queue.logs()
        if not logs:
            click.echo("no install log")
        for entry in logs:
            click.echo(f"{entry.id}  {_when(entry.received_at)}  {entry.hostname}  {entry.os}"
                       f"  exit {entry.exit_code or '?'}  {entry.size} chars  from {entry.source}")
        return
    try:
        click.echo(printable(queue.read_log(log_id)), nl=False)
    except EnrollError as exc:
        raise click.ClickException(str(exc)) from exc
