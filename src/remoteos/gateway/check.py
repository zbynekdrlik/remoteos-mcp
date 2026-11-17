"""``remoteos-gateway check`` (#51): the key-drift watchdog, run every 10 min by a user timer.

Each run probes every key-store machine DIRECTLY with its stored key (``initialize``) and the
gateway's own ``/healthz``, then writes ``~/.local/state/remoteos-gateway/machines-status.json``::

    {"checked_at": "...",
     "gateway":  {"state": "ok", "since": "...", "detail": "version 0.7.0.dev33", "notified": true},
     "machines": {"linux-cam2": {"state": "401", "since": "...", "detail": "...", "notified": true,
                                 "version": "0.7.0.dev33", "version_seen": "..."}}}

States: ``ok`` / ``401`` / ``unreachable`` / ``error`` (``probe.py``). ``since`` is when the
current state began. A state CHANGE is announced once — through ``airuleset.py notify --body``
when that script exists, else as a WARNING log line. A notification that did not go out stays
``notified: false`` and is retried on the next run (airuleset exits 0 even when the post failed,
so only its printed ``sent`` counts). A machine first seen ``ok`` is not news. A check that
cannot run (an invalid key store) is announced once per distinct failure, and so is its recovery.

A 401 is auto-repaired ONLY for a machine with an explicit ``repair`` recipe in the key store:
``ssh -o BatchMode=yes <ssh> [sudo -n] cat <key_file>`` reads the installer's ``config.json``,
the key in it is probed, and only a key the machine ACCEPTS is stored. Nothing is ever guessed.
No key is ever logged, printed, written to the status file or put in a notification.

Each machine's public ``/health`` gives its version and whether it has admin rights: ``drift.py``
reports a box older than the stable release (#63), ``no_admin.py`` a box without admin (#57).

With an airuleset that has ``--owner-notice`` (#102, ``owner_notice.py``) every open condition is
a footer-U notice: ONE per machine condition KIND listing its boxes (``fleet-down``,
``fleet-stale``, ``fleet-no-admin``, ``fleet_notice.py``, owner ruling 2026-11-16), plus
``gateway-health`` and ``check-failed``; ``_settle`` clears each one whose condition is gone at the
end of every run, and any slug that is never open any more: an old per-box slug, or a
``restart-<project>`` an older sync-configs sent (#109: those are printed only now).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from remoteos.gateway import drift as drift_report
from remoteos.gateway import fileops, fleet_notice, no_admin, owner_notice
from remoteos.gateway.drift import DriftPolicy
from remoteos.gateway.keystore import edit_keystore, load_keystore
from remoteos.gateway.messages import KEY_HINT, STATE_LABELS, split_to_fit
from remoteos.gateway.owner_notice import Notifier, Notify
from remoteos.gateway.probe import (
    STATE_OK,
    STATE_UNAUTHORIZED,
    ProbeResult,
    gather_probes,
    new_client,
    probe_healthz,
    probe_initialize,
    probe_version,
)
from remoteos.gateway.registry import MachineEntry, MachineSourceError, validate_key
from remoteos.gateway.targets import SSH_OPTIONS, last_line

logger = logging.getLogger("remoteos.gateway.check")

DEFAULT_STATUS_PATH = Path.home() / ".local" / "state" / "remoteos-gateway" / "machines-status.json"
DEFAULT_NOTIFY_OWNER = "zbynek"
REPAIR_TIMEOUT_S = 30
# The gateway's own record in the in-memory pending map: a key no machine name can be (names are
# [A-Za-z0-9][A-Za-z0-9._-]*), so a machine named "gateway" never collides with it.
GATEWAY_KEY = "<gateway>"
# Owner-notice slugs (#102): one footer-U item per open condition, cleared when it is gone. The
# machine conditions are one item per KIND (``fleet_notice``), never one per box.
GATEWAY_NOTICE = "gateway-health"
CHECK_FAILED = "check-failed"

_LABELS = STATE_LABELS


@dataclass
class Observation:
    state: str
    detail: str
    repaired: bool = False
    version: str | None = None  # from /health, this run; None = no answer
    admin: bool | None = None  # from /health, this run; None = no answer, or an older server


# -- probing + repair -------------------------------------------------------------------------


async def _probe_all(
    entries: dict[str, MachineEntry], gateway_url: str
) -> tuple[dict[str, ProbeResult], dict[str, ProbeResult], ProbeResult]:
    """Per machine the ``initialize`` result, the ``/health`` reads that answered, and the gateway's health."""
    async with new_client() as client:
        names = sorted(entries)
        results = await gather_probes(
            [
                *(probe_initialize(client, entries[n].url, entries[n].authorization) for n in names),
                *(probe_version(client, entries[n].url) for n in names),
                probe_healthz(client, gateway_url),
            ]
        )
    healths = {n: r for n, r in zip(names, results[len(names) : -1]) if r.ok}
    return dict(zip(names, results)), healths, results[-1]


def read_key_by_recipe(entry: MachineEntry, *, ssh_bin: str) -> tuple[str | None, str]:
    """Run the machine's repair recipe; returns ``(key or None, what happened)``. Never the key in text."""
    recipe = entry.repair
    assert recipe is not None
    remote = shlex.join([*(["sudo", "-n"] if recipe.sudo else []), "cat", recipe.key_file])
    argv = [ssh_bin, *SSH_OPTIONS, recipe.ssh, remote]
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=REPAIR_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"repair failed: ssh {recipe.ssh} did not run ({type(exc).__name__})"
    if proc.returncode != 0:
        return None, f"repair failed: ssh {recipe.ssh} exit {proc.returncode} ({last_line(proc.stderr)})"
    try:
        key = json.loads(proc.stdout.decode("utf-8")).get("auth_key")
        validate_key(key if isinstance(key, str) else "")
    except (ValueError, AttributeError, RecursionError, MachineSourceError):
        return None, f"repair failed: {recipe.key_file} on {recipe.ssh} holds no valid auth_key"
    return key, f"key re-read from {recipe.ssh}:{recipe.key_file}"


async def _repair(name: str, entry: MachineEntry, *, keystore_path: Path, ssh_bin: str) -> Observation:
    key, what = await asyncio.to_thread(read_key_by_recipe, entry, ssh_bin=ssh_bin)
    if key is None:
        return Observation(STATE_UNAUTHORIZED, what)
    async with new_client() as client:
        result = await probe_initialize(client, entry.url, f"Bearer {key}")
    if result.state != STATE_OK:
        return Observation(STATE_UNAUTHORIZED, f"repair failed: the key on the box is not accepted either ({what})")
    try:
        with edit_keystore(keystore_path) as entries:
            current = entries.get(name)
            # a set-key (a new key, or a new --url: the re-read key was verified at the OLD url)
            # won the race: leave it alone
            if current is None or (current.url, current.key) != (entry.url, entry.key):
                return Observation(STATE_UNAUTHORIZED, "repair skipped: the key store changed meanwhile")
            entries[name] = replace(current, key=key)
    except MachineSourceError as exc:  # it turned invalid since the load: the next run announces that
        return Observation(STATE_UNAUTHORIZED, f"repair skipped: the key store is unreadable now ({exc})")
    except OSError as exc:  # a read-only directory, a full disk, a lock file it cannot create
        reason = exc.strerror or type(exc).__name__
        return Observation(STATE_UNAUTHORIZED, f"repair skipped: the key store cannot be written ({reason})")
    logger.warning("check: %s repaired (%s)", name, what)
    return Observation(STATE_OK, f"repaired: {what}", repaired=True)


async def observe(
    entries: dict[str, MachineEntry], *, gateway_url: str, keystore_path: Path, ssh_bin: str
) -> tuple[dict[str, Observation], Observation]:
    results, healths, health = await _probe_all(entries, gateway_url)
    observations = {name: Observation(r.state, r.detail) for name, r in results.items()}
    for name in sorted(set(results) - set(healths)):
        logger.info("check: %s reported no version this run", name)

    async def guarded(name: str) -> Observation:
        try:
            return await _repair(name, entries[name], keystore_path=keystore_path, ssh_bin=ssh_bin)
        except Exception as exc:  # one box's repair never blinds the run for every other machine
            logger.error("check: %s repair failed unexpectedly: %s", name, type(exc).__name__)
            return Observation(STATE_UNAUTHORIZED, f"repair failed: {type(exc).__name__}")

    # Concurrently: a repair can take ~55 s (ssh + a re-probe); several slow boxes in a row would
    # outlast the unit's TimeoutStartSec, and systemd would kill the run before it says anything.
    # Each key-store edit is synchronous under its lock, so concurrent repairs never interleave one.
    repairable = [n for n, r in results.items() if r.state == STATE_UNAUTHORIZED and entries[n].repair is not None]
    for name, observation in zip(repairable, await asyncio.gather(*(guarded(n) for n in repairable))):
        observations[name] = observation
    for name, read in healths.items():
        observations[name].version, observations[name].admin = read.detail, read.admin
    return observations, Observation(health.state, health.detail)


# -- status file + notifications --------------------------------------------------------------


def load_status(path: Path) -> dict:
    """The previous run's status. Missing or unreadable means "nothing known yet" (logged)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        logger.error("check: status file %s unreadable (%s) — starting over", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _advance(previous: object, observation: Observation, now: str) -> dict:
    """The new status record for one item, carrying ``since``/``notified`` while the state holds.
    A record that is not what this code writes (a hand edit: a state that is not a string) is
    treated as unknown, never trusted."""
    prev = previous if isinstance(previous, dict) and isinstance(previous.get("state"), str) else None
    if prev is not None and not isinstance(prev.get("previous"), (str, type(None))):
        prev = {**prev, "previous": None}
    if prev is not None and prev.get("state") == observation.state and not observation.repaired:
        notified = bool(prev.get("notified"))
        return {
            "state": observation.state,
            "since": prev.get("since") or now,
            # an announcement still owed reports the CHANGE (e.g. "repaired: ..."), not this run
            "detail": observation.detail if notified else prev.get("detail") or observation.detail,
            "notified": notified,
            "previous": prev.get("previous"),
        }
    first_seen_ok = prev is None and observation.state == STATE_OK and not observation.repaired
    return {
        "state": observation.state,
        "since": now,
        "detail": observation.detail,
        "notified": first_seen_ok,
        "previous": prev.get("state") if prev is not None else None,
    }


_DETAIL_LIMIT = 200


def _change_line(name: str, record: dict) -> str:
    old = record.get("previous")
    before = _LABELS.get(old, old) if old else "nový"
    after = _LABELS.get(record["state"], record["state"])
    label = "brána (gateway /healthz)" if name == GATEWAY_KEY else name
    detail = str(record["detail"])[:_DETAIL_LIMIT]
    return f"• {label}: {before} → {after} [{old or 'new'} → {record['state']}] — {detail}"


def _hints(records: list[dict]) -> list[str]:
    hints = []
    if any(r["state"] == STATE_UNAUTHORIZED for r in records):
        hints.append(KEY_HINT)
    if any(r["state"] == STATE_UNAUTHORIZED or str(r["detail"]).startswith("repaired") for r in records):
        hints.append(
            "Projekty cez bránu sa nemenia; priamu kópiu kľúčov (projekt remoteos-mcp) obnoví"
            " `remoteos-gateway sync-configs`."
        )
    return hints


def compose_messages(pending: dict[str, dict], host: str, *, recovered: bool = False) -> list[tuple[str, list[str]]]:
    """The notification texts (Slovak, phone-readable; names, states, details — never a key), as
    ``(text, the names it announces)``, each text at most ``CHUNK_LIMIT`` characters."""
    title = f"MCP brána ({host}): " + ("kontrola strojov opäť beží" if recovered else "zmena stavu strojov")
    reserve = len(" (99/99)")

    def lines(names: list[str]) -> list[str]:
        return [_change_line(n, pending[n]) for n in names] + _hints([pending[n] for n in names])

    groups = split_to_fit(pending, lambda names: len("\n".join([title, *lines(names)])) + reserve)
    texts = []
    for index, names in enumerate(groups, start=1):
        head = title + (f" ({index}/{len(groups)})" if len(groups) > 1 else "")
        texts.append(("\n".join([head, *lines(names)]), names))
    return texts


def _state_messages(
    pending: dict[str, dict], host: str, recovered: bool, notifier: Notify | None
) -> list[tuple[str, list[str], str | None, bool]]:
    """``(text, names announced, owner-notice slug, carries the "running again" title)``: grouped
    as before, or — when the notifier sends per condition (#102) — the gateway as its own notice
    and the boxes back to ``ok`` as plain news. A box that went DOWN is not announced here: it is
    one line of the ``fleet-down`` notice (``fleet_notice.reconcile``), which marks it notified."""
    if not owner_notice.per_condition(notifier):
        texts = compose_messages(pending, host, recovered=recovered)
        return [(text, names, None, recovered and index == 0) for index, (text, names) in enumerate(texts)]
    messages = []
    if recovered:
        [(text, _names)] = compose_messages({}, host, recovered=True)
        messages.append((text, [], None, True))
    if GATEWAY_KEY in pending:
        record = pending[GATEWAY_KEY]
        notice = GATEWAY_NOTICE if record["state"] != STATE_OK else None
        for text, names in compose_messages({GATEWAY_KEY: record}, host):
            messages.append((text, names, notice, False))
    back = {n: r for n, r in pending.items() if n != GATEWAY_KEY and r["state"] == STATE_OK}
    if back:  # news, not an open condition: plain, and fleet-down is replaced or cleared
        for text, names in compose_messages(back, host):
            messages.append((text, names, None, False))
    return messages


def open_notices(status: dict) -> set[str]:
    """The owner-notice slugs of every condition still open after this run: the gateway's and one
    per machine condition KIND (an old per-box slug is never open, so ``_settle`` clears it)."""
    open_now = {GATEWAY_NOTICE} if status["gateway"]["state"] != STATE_OK else set()
    return open_now | fleet_notice.open_slugs(status["machines"])


def _reopen_gateway(status: dict, previous: dict, notifier: Notify | None) -> None:
    """The first run that can send per condition (#102) announces a down gateway once more if the
    older airuleset could not put it into U. ``status["owner_notices"]`` marks that this happened:
    later runs never re-send (a notice whose book entry could not be written must not go out every
    10 minutes). The machine kinds need no such pass: a kind with no delivered fingerprint in the
    status file is sent anyway (``fleet_notice.reconcile``)."""
    if previous.get("owner_notices") is True:  # kept through a run that had to send plain notices
        status["owner_notices"] = True
        return
    if not owner_notice.per_condition(notifier) or not isinstance(notifier, Notifier) or notifier.book is None:
        return
    status["owner_notices"] = True
    gateway = status["gateway"]
    if gateway["state"] != STATE_OK and gateway["notified"] and GATEWAY_NOTICE not in notifier.book.read():
        gateway["notified"] = False


def _settle(notifier: Notify | None, status: dict) -> None:
    """Clear the owner notices whose condition is gone (#102): every recorded slug that is not in
    ``open_notices`` — an old per-box slug and an old ``restart-<project>`` (#109) included, so each
    is cleared once and forgotten. Never costs the run its status write: a failure is logged, and
    what was not cleared is retried next run."""
    if not isinstance(notifier, Notifier):
        return
    open_now = open_notices(status)
    try:
        notifier.settle(lambda name, entry: name in open_now)
    except Exception:
        logger.exception("check: clearing owner notices failed — retried next run")


def _reconcile_kinds(status: dict, notifier: Notify | None, host: str) -> bool | None:
    """The fleet kind notices (#102): send what changed, clear a closed kind the book lost. Like
    drift, never costs the run its status write: a failure is logged, and retried next run."""
    try:
        sent = fleet_notice.reconcile(status, notifier, host)
        fleet_notice.clear_unbooked(status, notifier)
        return sent
    except Exception:
        logger.exception("check: the fleet notices failed — retried next run")
        return False


@dataclass
class CheckReport:
    status: dict
    sent: bool | None  # None = nothing to announce


def run_check(
    keystore_path: Path,
    status_path: Path,
    *,
    gateway_url: str,
    notifier: Notify | None,
    ssh_bin: str | None = None,
    host: str = "dev1",
    now: Callable[[], _dt.datetime] | None = None,
    drift: DriftPolicy | None = None,
) -> CheckReport:
    moment = (now or (lambda: _dt.datetime.now(_dt.timezone.utc)))()
    stamp = moment.isoformat(timespec="milliseconds")
    previous = load_status(status_path)
    try:
        entries = load_keystore(keystore_path)
    except MachineSourceError as exc:
        _report_failure(status_path, previous, str(exc), stamp, notifier, host)
        raise
    try:
        return _check_loaded(
            entries, previous, moment, keystore_path, status_path, gateway_url, notifier, ssh_bin, host, drift
        )
    except CheckError:
        raise
    except Exception as exc:  # the watchdog never goes silent: whatever broke, it is announced
        logger.exception("check: the run failed unexpectedly")
        reason = f"unexpected {type(exc).__name__} — see journalctl --user -u remoteos-gateway-check"
        _report_failure(status_path, previous, reason, stamp, notifier, host)
        raise CheckError(reason) from exc


def _check_loaded(
    entries: dict[str, MachineEntry],
    previous: dict,
    moment: _dt.datetime,
    keystore_path: Path,
    status_path: Path,
    gateway_url: str,
    notifier: Notify | None,
    ssh_bin: str | None,
    host: str,
    drift: DriftPolicy | None,
) -> CheckReport:
    stamp = moment.isoformat(timespec="milliseconds")
    observations, health = asyncio.run(
        observe(
            entries,
            gateway_url=gateway_url,
            keystore_path=keystore_path,
            ssh_bin=ssh_bin or os.environ.get("REMOTEOS_GATEWAY_SSH", "ssh"),
        )
    )
    old_machines = previous.get("machines") if isinstance(previous.get("machines"), dict) else {}
    status = {
        "checked_at": stamp,
        "gateway": _advance(previous.get("gateway"), health, stamp),
        "machines": {name: _advance(old_machines.get(name), obs, stamp) for name, obs in sorted(observations.items())},
    }
    for name, record in status["machines"].items():  # the company, for the fleet-down grouping (#105)
        if entries[name].org is not None:
            record["org"] = entries[name].org
    _reopen_gateway(status, previous, notifier)
    status[fleet_notice.STATUS_KEY] = fleet_notice.delivered_before(previous)  # kept through a plain run
    per_kind = owner_notice.per_condition(notifier)
    pending = {GATEWAY_KEY: status["gateway"]} if not status["gateway"]["notified"] else {}
    pending.update({n: r for n, r in status["machines"].items() if not r["notified"]})
    # The check could not run last time (or that recovery was not announced yet): say it is back.
    recovered = isinstance(previous.get("error"), dict) or previous.get("recovery_pending") is True
    for name, record in pending.items():
        logger.warning("check: %s is %s (was %s) — %s", name, record["state"], record["previous"], record["detail"])
    sent: bool | None = None
    if per_kind:  # the boxes that went down are announced by fleet-down below, not one by one
        pending = {n: r for n, r in pending.items() if n == GATEWAY_KEY or r["state"] == STATE_OK}
    if pending or recovered:
        sent = True
        for text, names, notice, says_recovered in _state_messages(pending, host, recovered, notifier):
            delivered = owner_notice.send(notifier, text, notice)
            sent = sent and delivered
            if delivered:  # only the machines of a DELIVERED message are done; the rest retry
                for name in names:
                    pending[name]["notified"] = True
            elif says_recovered:  # the message with the "running again" title
                status["recovery_pending"] = True
    versions = {name: observation.version for name, observation in observations.items()}
    drift_sent = drift_report.update(
        status["machines"], old_machines, versions, drift, stamp, moment, host, notifier, announce=not per_kind
    )
    admins = {name: observation.admin for name, observation in observations.items()}
    admin_sent = no_admin.update(status["machines"], old_machines, admins, stamp, host, notifier, announce=not per_kind)
    kinds_sent = _reconcile_kinds(status, notifier, host) if per_kind else None
    for extra in (drift_sent, admin_sent, kinds_sent):
        if extra is not None:
            sent = extra if sent is None else (sent and extra)
    try:
        _write_status(status_path, status)
    except OSError as exc:
        reason = f"cannot write the status file {status_path} ({exc.strerror or type(exc).__name__})"
        _report_failure(status_path, previous, reason, stamp, notifier, host)
        raise CheckError(reason) from exc
    _settle(notifier, status)  # after the write: slow clears must never cost the run its status
    return CheckReport(status=status, sent=sent)


class CheckError(Exception):
    """The check could not run to the end (the status file is not writable, or anything unexpected)."""


def _write_status(path: Path, status: dict) -> None:
    fileops.atomic_write(str(path), (json.dumps(status, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _report_failure(
    status_path: Path, previous: dict, message: str, stamp: str, notifier: Notify | None, host: str
) -> None:
    """The check itself cannot run: say so ONCE per distinct failure, so the watchdog never goes
    silent. The last machine states are kept. If even the status file cannot be written, the
    failure is announced on every run (there is nowhere to remember it was)."""
    old = previous.get("error") if isinstance(previous.get("error"), dict) else None
    same = old is not None and old.get("message") == message
    record = {
        "message": message,
        "since": old.get("since", stamp) if same else stamp,
        "notified": bool(old.get("notified")) if same else False,
    }
    logger.error("check: cannot run — %s", message)
    if not record["notified"]:
        text = f"MCP brána ({host}): kontrola strojov NEBEŽÍ — {message}"
        try:
            record["notified"] = owner_notice.send(notifier, text, CHECK_FAILED)
        except Exception as exc:  # the report of a failure must not itself become a new crash
            logger.error("check: the failure notice could not be sent (%s)", type(exc).__name__)
            record["notified"] = False
    try:
        _write_status(status_path, {**previous, "checked_at": stamp, "error": record})
    except OSError as exc:
        logger.error("check: the status file %s cannot be written either (%s)", status_path, exc.strerror or exc)
