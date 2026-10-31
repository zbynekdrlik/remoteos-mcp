"""``remoteos-gateway enroll`` (#26): one command on dev1, one on the new box.

    remoteos-gateway enroll win-new-pc --projects songplayer,camera-box

prints the command for the box and waits. The installer enables SSH with dev1's key and posts its
registration; ``enroll-finish``, run through ``secret exec --stdin`` so the key reaches it on stdin
only, then does what a person did by hand: verify ``initialize`` 200, ``machine set-key``, add the
machine to the projects, ``sync-configs``, ``check``. The how and why: ``enroll.py``.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import click

from remoteos.gateway import cli_fleet
from remoteos.gateway.check import load_status
from remoteos.gateway.enroll import (
    SECRET_TTL_S,
    Airuleset,
    EnrollError,
    Registration,
    Tunnel,
    candidate_urls,
    endpoint_from_request,
    install_commands,
    parse_registration,
    read_ssh_key,
    ref_ok,
    secret_name,
    wait_for_registration,
)
from remoteos.gateway.keystore import load_keystore, mask_secret, set_key
from remoteos.gateway.manifest import ManifestError, add_machine, load_manifest
from remoteos.gateway.probe import STATE_OK, probe_one
from remoteos.gateway.registry import MachineSourceError, is_valid_machine_name, validate_http_url
from remoteos.release_channel import RELEASES_URL


def _project_list(projects: str) -> list[str]:
    names = [name.strip() for name in projects.split(",") if name.strip()]
    if not names:
        raise click.UsageError("--projects names no project")
    return list(dict.fromkeys(names))


def _check_request(paths, name: str, projects: list[str], replace: bool, url: str | None) -> None:
    """Everything dev1 can refuse before a box is touched."""
    if not is_valid_machine_name(name):
        raise click.ClickException(f"invalid machine name {name!r}: use letters, digits and . _ -")
    try:
        if url is not None:
            validate_http_url(url, what="--url")
        manifest = load_manifest(paths.manifest)
        known = load_keystore(paths.machines, missing_ok=True)
    except (ManifestError, MachineSourceError) as exc:
        raise click.ClickException(str(exc)) from exc
    missing = [project for project in projects if project not in manifest.projects]
    if missing:
        raise click.ClickException(f"not in the manifest: {', '.join(missing)}")
    if name in known and not replace:
        raise click.ClickException(f"{name} is in the key store already: --replace overwrites its url and key")


@contextlib.contextmanager
def _exit_on_signals() -> Iterator[None]:
    """A TERM or a lost terminal runs the cleanup too: python's default ends the process with no
    ``finally``, and the public tunnel would stay up."""
    def leave(signum, frame):  # noqa: ANN001 - signal handler signature
        raise SystemExit(128 + signum)

    previous = {number: signal.signal(number, leave) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _finish_command(paths, airuleset: Path, name: str, projects: list[str], url: str | None,
                    replace: bool) -> list[str]:
    """``enroll-finish`` with this run's paths, for ``secret exec``."""
    command = [sys.executable, "-m", "remoteos.gateway.cli", "--registry", str(paths.registry),
               "--machines", str(paths.machines), "--access-log", str(paths.access_log),
               "--manifest", str(paths.manifest), "enroll-finish", name, "--projects", ",".join(projects),
               "--airuleset", str(airuleset)]
    return command + (["--url", url] if url else []) + (["--replace"] if replace else [])


def _discard_earlier(airuleset: Airuleset, name: str) -> None:
    """A failed earlier run's value or request must not answer for this one."""
    if airuleset.state(name) != "absent":
        click.echo(f"dropping the earlier enrollment of {name}")
        airuleset.forget(name)


def _receive(airuleset: Airuleset, name: str, ssh_key: str, ref: str, timeout_s: float) -> None:
    """Open the receiver, print the commands, wait for the box, close the tunnel."""
    origin, token = endpoint_from_request(airuleset.request(name))
    tunnel = Tunnel(origin)
    try:
        click.echo("opening the one-time public URL (about 15 s)...")
        host = tunnel.wait_for_host()
        click.echo("\nRun this on the new box, once:\n")
        for label, command in install_commands(f"{host}/{token}/", ssh_key, ref).items():
            click.echo(f"  {label}:\n    {command}\n")
        click.echo(f"waiting up to {timeout_s:.0f} s for the box...")
        wait_for_registration(airuleset, name, tunnel, timeout_s)
    finally:
        tunnel.close()


@click.command("enroll")
@click.argument("name")
@click.option("--projects", required=True, help="The projects to add the machine to, comma separated.")
@click.option("--url", default=None, help="The machine's /mcp url, when the addresses it posts cannot be reached.")
@click.option("--ref", default="main", show_default=True, help="The installer's source: a branch, tag or commit.")
@click.option("--replace", is_flag=True, help="Overwrite a machine that is in the key store already.")
@click.option("--ssh-key", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="dev1's public key for the box. Default: ~/.ssh/id_ed25519.pub, else id_rsa.pub.")
@click.option("--timeout", type=click.IntRange(30, SECRET_TTL_S), default=SECRET_TTL_S, show_default=True,
              help="Seconds to wait for the box.")
@click.option("--airuleset", "airuleset_path", type=click.Path(dir_okay=False, path_type=Path),
              envvar="REMOTEOS_GATEWAY_AIRULESET", default=cli_fleet.DEFAULT_AIRULESET_PATH, show_default=True)
@click.pass_obj
def enroll_command(paths, name: str, projects: str, url: str | None, ref: str, replace: bool,
                   ssh_key: Path | None, timeout: int, airuleset_path: Path) -> None:
    """Start a one-time enrollment of machine NAME and finish it when the box posts."""
    wanted = _project_list(projects)
    _check_request(paths, name, wanted, replace, url)
    if not ref_ok(ref):
        raise click.ClickException("--ref must be a commit sha, a tag or a branch name")
    airuleset = Airuleset(airuleset_path.expanduser())
    secret = secret_name(name)
    finish = _finish_command(paths, airuleset.path, name, wanted, url, replace)
    try:
        key_line = read_ssh_key(ssh_key)
        _discard_earlier(airuleset, secret)
        with _exit_on_signals():
            _receive(airuleset, secret, key_line, ref, timeout)
    except EnrollError as exc:
        airuleset.forget(secret)
        raise click.ClickException(f"{exc} — enrollment stopped") from exc
    except BaseException:
        airuleset.forget(secret)
        raise
    click.echo("the box posted; finishing on dev1...")
    result = airuleset.exec_stdin(secret, finish)
    click.echo(result.stdout, nl=False)
    click.echo(result.stderr, nl=False, err=True)
    if result.returncode != 0:
        retry = _finish_command(paths, airuleset.path, name, wanted, url, True)  # the key may be stored already
        raise click.ClickException(
            f"enroll-finish failed (exit {result.returncode}). The registration stays in the vault for an hour:"
            " fix the cause, then run: "
            + shlex.join(["python3", str(airuleset.path), "secret", "exec", secret, "--stdin", "--", *retry])
        )
    airuleset.forget(secret)
    click.echo(f"{name} is enrolled")


def _choose_url(registration: Registration, explicit: str | None) -> str:
    """The first url where the machine accepts the posted key. Only private addresses get it."""
    candidates = [explicit] if explicit else candidate_urls(registration)
    if not candidates:
        raise EnrollError("the registration names no private address: pass --url")
    authorization = f"Bearer {registration.key}"
    for url in candidates:
        result = probe_one(url, authorization)
        click.echo(f"  {url}: {result.state} ({result.detail})")
        if result.state == STATE_OK:
            return url
    raise EnrollError("the machine accepted the key at no address: is it reachable from dev1? Pass --url")


@click.command("enroll-finish")
@click.argument("name")
@click.option("--projects", required=True)
@click.option("--url", default=None)
@click.option("--replace", is_flag=True)
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname)
@cli_fleet._notify_options
@cli_fleet._status_file_option
@click.pass_context
def enroll_finish_command(ctx: click.Context, name: str, projects: str, url: str | None, replace: bool,
                          local_host: str, airuleset_path: Path, notify_owner: str | None, status_file: Path) -> None:
    """Internal, run by ``enroll``: the registration comes on STDIN."""
    paths = ctx.obj
    wanted = _project_list(projects)
    _check_request(paths, name, wanted, replace, url)
    try:
        registration = parse_registration(sys.stdin.buffer.read())
        click.echo(f"registration: {registration.hostname} ({registration.os}), v{registration.version},"
                   f" ssh account {registration.account}")
        target = _choose_url(registration, url)
        outcome = set_key(paths.machines, name, registration.key, url=target)
        if outcome.created:
            cli_fleet._expose_new(paths, [name])  # before the manifest: a retry finds the machine known
        for project in wanted:
            added = add_machine(paths.manifest, project, name)
            click.echo(f"  {project}: {'added' if added else 'already lists it'}")
    except (EnrollError, MachineSourceError, ManifestError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{name}: key {mask_secret(registration.key)}, url {outcome.url}")
    ctx.invoke(cli_fleet.sync_configs_command, dry_run=False, local_host=local_host, prune=False,
               airuleset_path=airuleset_path, notify_owner=notify_owner)
    ctx.invoke(cli_fleet.check_command, status_file=status_file, gateway_url=None, airuleset_path=airuleset_path,
               notify_owner=notify_owner, local_host=local_host,
               releases_url=os.environ.get("REMOTEOS_RELEASES_URL", RELEASES_URL))
    machines = load_status(status_file.expanduser()).get("machines")
    record = machines.get(name) if isinstance(machines, dict) else None
    if not isinstance(record, dict) or record.get("state") != STATE_OK:
        raise click.ClickException(f"{name}: check does not report it ok: {record}")
