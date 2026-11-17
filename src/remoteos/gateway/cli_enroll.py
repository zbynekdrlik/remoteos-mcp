"""``remoteos-gateway enroll`` (#26): one command on dev1, one on the new box.

    remoteos-gateway enroll win-new-pc --org nlmedia --projects songplayer,camera-box

prints the command for the box and waits. The installer enables SSH with dev1's key and posts its
registration; ``enroll-finish``, run through ``secret exec --stdin`` so the key reaches it on stdin
only, then does what a person did by hand: verify ``initialize`` 200, ``machine set-key``, add the
machine to the projects, ``sync-configs``, ``check``. The how and why: ``enroll.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import socket
import sys
from collections.abc import Callable, Iterator
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
from remoteos.gateway.enroll_queue import DEFAULT_ENROLL_DIR
from remoteos.gateway.keystore import load_keystore, mask_secret, set_key
from remoteos.gateway.manifest import ManifestError, add_machine, load_manifest
from remoteos.gateway.probe import STATE_OK, new_client, probe_one, probe_version
from remoteos.gateway.registry import (
    ORG_MISSING_HINT,
    ORGS,
    MachineSourceError,
    is_valid_machine_name,
    validate_http_url,
)
from remoteos.gateway.sync import CROSS_ORG_PROJECTS
from remoteos.release_channel import RELEASES_URL


def _project_list(projects: str) -> list[str]:
    names = [name.strip() for name in projects.split(",") if name.strip()]
    if not names:
        raise click.UsageError("--projects names no project")
    return list(dict.fromkeys(names))

MACHINE_PREFIXES = ("win-", "linux-", "mac-")


def _check_request(paths, name: str, org: str, projects: list[str], replace: bool, url: str | None) -> None:
    """Everything dev1 can refuse before a box is touched."""
    if not is_valid_machine_name(name):
        raise click.ClickException(f"invalid machine name {name!r}: use letters, digits and . _ -")
    if not name.startswith(MACHINE_PREFIXES):  # the names remoteos-call knows, and that say the OS (#59)
        raise click.ClickException(f"invalid machine name {name!r}: it must start with win-, linux- or mac-")
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
    # #105: a project holds machines of ONE org (sync-configs would refuse the run after the box
    # was set up); refused here, before any box is touched. A known machine keeps its org (a change
    # is `machine set-org`), and every project already holding it is checked too.
    current = known[name].org if name in known else None
    if current is not None and current != org:
        raise click.ClickException(
            f"{name} is {current} in the key store, not {org}: change it with `remoteos-gateway machine set-org`"
        )
    holding = [p.name for p in manifest.projects.values() if name in p.machines]
    for project in dict.fromkeys([*projects, *holding]):
        if project in CROSS_ORG_PROJECTS:
            continue
        clash = [m for m in manifest.projects[project].machines if m != name and m in known and known[m].org != org]
        if clash:
            unset = all(known[m].org is None for m in clash)
            raise click.ClickException(
                f"{project} holds machines of another org than {org}: "
                + ", ".join(f"{m} ({known[m].org or 'no org'})" for m in sorted(clash))
                + " — a project may hold ONE org's machines (#105)"
                + (f"; {ORG_MISSING_HINT}" if unset else "")
            )


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


def _finish_command(paths, airuleset: Path, name: str, org: str, projects: list[str], url: str | None,
                    replace: bool) -> list[str]:
    """``enroll-finish`` with this run's paths, for ``secret exec``."""
    command = [sys.executable, "-m", "remoteos.gateway.cli", "--registry", str(paths.registry),
               "--machines", str(paths.machines), "--access-log", str(paths.access_log),
               "--manifest", str(paths.manifest), "enroll-finish", name, "--org", org,
               "--projects", ",".join(projects), "--airuleset", str(airuleset)]
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
@click.option("--org", type=click.Choice(ORGS), required=True, help="The company the machine belongs to (#105).")
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
def enroll_command(paths, name: str, org: str, projects: str, url: str | None, ref: str, replace: bool,
                   ssh_key: Path | None, timeout: int, airuleset_path: Path) -> None:
    """Start a one-time enrollment of machine NAME and finish it when the box posts."""
    wanted = _project_list(projects)
    _check_request(paths, name, org, wanted, replace, url)
    if not ref_ok(ref):
        raise click.ClickException("--ref must be a commit sha, a tag or a branch name")
    airuleset = Airuleset(airuleset_path.expanduser())
    secret = secret_name(name)
    finish = _finish_command(paths, airuleset.path, name, org, wanted, url, replace)
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
        retry = _finish_command(paths, airuleset.path, name, org, wanted, url, True)  # the key may be stored already
        raise click.ClickException(
            f"enroll-finish failed (exit {result.returncode}). The registration stays in the vault for an hour:"
            " fix the cause, then run: "
            + shlex.join(["python3", str(airuleset.path), "secret", "exec", secret, "--stdin", "--", *retry])
        )
    airuleset.forget(secret)
    click.echo(f"{name} is enrolled")


def _hostname_matches(url: str, registration: Registration) -> bool:
    """The box at *url* says, on its public ``/health``, that it is the host that posted (#101).
    A server too old to report its hostname does not match: an accept must see the name."""
    result = asyncio.run(_health(url))
    reported = (result.hostname or "").lower() if result.state == STATE_OK else ""
    posted = registration.hostname.lower()
    match = bool(reported) and (reported == posted or reported.split(".")[0] == posted.split(".")[0])
    click.echo(f"  {url}: /health hostname {result.hostname or '(none)'}"
               f" — {'matches' if match else 'does NOT match'} {registration.hostname}")
    return match


async def _health(url: str):
    async with new_client() as client:
        return await probe_version(client, url)


def _choose_url(registration: Registration, explicit: str | None, *, check_hostname: bool = False) -> str:
    """The first url where the machine accepts the posted key (and, for a public registration, says
    it is the posted host). Only private addresses get the key."""
    candidates = [explicit] if explicit else candidate_urls(registration)
    if not candidates:
        raise EnrollError("the registration names no private address: pass --url")
    authorization = f"Bearer {registration.key}"
    for url in candidates:
        if check_hostname and not _hostname_matches(url, registration):
            continue  # the key is never sent to a box that is not the one that posted
        result = probe_one(url, authorization)
        click.echo(f"  {url}: {result.state} ({result.detail})")
        if result.state == STATE_OK:
            return url
    raise EnrollError("the machine accepted the key at no address: is it reachable from dev1? Pass --url")


def finish_registration(ctx: click.Context, registration: Registration, *, name: str, org: str, projects: list[str],
                        url: str | None, local_host: str, notify: dict, status_file: Path,
                        check_hostname: bool = False, enroll_dir: Path | None = None,
                        after_store: Callable[[], None] | None = None) -> None:
    """What a person did by hand once a box posted: verify the key, store it, expose a new machine,
    add it to the projects, ``sync-configs``, ``check``. Shared by ``enroll-finish`` and
    ``enroll-accept``; *after_store* runs once the key store and the manifest hold the machine,
    before the sync (the accept drops the queued registration there). Raises
    ``click.ClickException``; the key never reaches the output."""
    paths = ctx.obj
    try:
        click.echo(f"registration: {registration.hostname} ({registration.os}), v{registration.version},"
                   f" ssh account {registration.account}")
        target = _choose_url(registration, url, check_hostname=check_hostname)
        outcome = set_key(paths.machines, name, registration.key, url=target, org=org)
        if outcome.created:
            cli_fleet._expose_new(paths, [name])  # before the manifest: a retry finds the machine known
        for project in projects:
            added = add_machine(paths.manifest, project, name)
            click.echo(f"  {project}: {'added' if added else 'already lists it'}")
    except (EnrollError, MachineSourceError, ManifestError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{name}: key {mask_secret(registration.key)}, url {outcome.url}")
    if after_store is not None:
        after_store()
    ctx.invoke(cli_fleet.sync_configs_command, dry_run=False, local_host=local_host, prune=False)
    # ctx.invoke fills a parameter's DEFAULT, never its envvar: pass what the command line would read
    ctx.invoke(cli_fleet.check_command, status_file=status_file, gateway_url=None, **notify, local_host=local_host,
               releases_url=os.environ.get("REMOTEOS_RELEASES_URL", RELEASES_URL),
               enroll_dir=enroll_dir or Path(os.environ.get("REMOTEOS_GATEWAY_ENROLL_DIR") or DEFAULT_ENROLL_DIR))
    machines = load_status(status_file.expanduser()).get("machines")
    record = machines.get(name) if isinstance(machines, dict) else None
    if not isinstance(record, dict) or record.get("state") != STATE_OK:
        raise click.ClickException(f"{name}: check does not report it ok: {record}")


@click.command("enroll-finish")
@click.argument("name")
@click.option("--org", type=click.Choice(ORGS), required=True)
@click.option("--projects", required=True)
@click.option("--url", default=None)
@click.option("--replace", is_flag=True)
@click.option("--local-host", envvar="REMOTEOS_GATEWAY_LOCAL_HOST", default=socket.gethostname)
@cli_fleet._notify_options
@cli_fleet._status_file_option
@click.pass_context
def enroll_finish_command(ctx: click.Context, name: str, org: str, projects: str, url: str | None, replace: bool,
                          local_host: str, airuleset_path: Path, notify_owner: str | None, notify_project_dir: Path,
                          notices_file: Path, status_file: Path) -> None:
    """Internal, run by ``enroll``: the registration comes on STDIN."""
    wanted = _project_list(projects)
    _check_request(ctx.obj, name, org, wanted, replace, url)
    try:
        registration = parse_registration(sys.stdin.buffer.read())
    except EnrollError as exc:
        raise click.ClickException(str(exc)) from exc
    notify = {"airuleset_path": airuleset_path, "notify_owner": notify_owner,
              "notify_project_dir": notify_project_dir, "notices_file": notices_file}
    finish_registration(ctx, registration, name=name, org=org, projects=wanted, url=url, local_host=local_host,
                        notify=notify, status_file=status_file)
