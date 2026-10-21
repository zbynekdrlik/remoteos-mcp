"""``remoteos-gateway check`` (#51): the key-drift watchdog, run every 10 min by a user timer.

Each run probes every key-store machine DIRECTLY with its stored key (``initialize``) and the
gateway's own ``/healthz``, then writes ``~/.local/state/remoteos-gateway/machines-status.json``::

    {"checked_at": "...",
     "gateway":  {"state": "ok", "since": "...", "detail": "version 0.7.0.dev33", "notified": true},
     "machines": {"linux-cam2": {"state": "401", "since": "...", "detail": "...", "notified": true}}}

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
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remoteos.gateway import fileops
from remoteos.gateway.keystore import edit_keystore, load_keystore
from remoteos.gateway.probe import (
    STATE_OK,
    STATE_UNAUTHORIZED,
    ProbeResult,
    gather_probes,
    new_client,
    probe_healthz,
    probe_initialize,
)
from remoteos.gateway.registry import MachineEntry, MachineSourceError, validate_key
from remoteos.gateway.targets import SSH_OPTIONS, last_line

logger = logging.getLogger("remoteos.gateway.check")

DEFAULT_STATUS_PATH = Path.home() / ".local" / "state" / "remoteos-gateway" / "machines-status.json"
DEFAULT_AIRULESET_PATH = Path.home() / "devel" / "airuleset" / "airuleset.py"
REPAIR_TIMEOUT_S = 30
NOTIFY_TIMEOUT_S = 60
GATEWAY_KEY = "gateway"

_LABELS = {"ok": "funguje", "401": "odmieta kľúč (401)", "unreachable": "nedostupný", "error": "chyba"}


@dataclass
class Observation:
    state: str
    detail: str
    repaired: bool = False


# -- probing + repair -------------------------------------------------------------------------


async def _probe_all(entries: dict[str, MachineEntry], gateway_url: str) -> tuple[dict[str, ProbeResult], ProbeResult]:
    async with new_client() as client:
        names = sorted(entries)
        results = await gather_probes(
            [
                *(probe_initialize(client, entries[n].url, entries[n].authorization) for n in names),
                probe_healthz(client, gateway_url),
            ]
        )
    return dict(zip(names, results[:-1])), results[-1]


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
    except (ValueError, AttributeError, MachineSourceError):
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
            entries[name] = MachineEntry(url=current.url, key=key, repair=current.repair)
    except MachineSourceError as exc:  # it turned invalid since the load: the next run announces that
        return Observation(STATE_UNAUTHORIZED, f"repair skipped: the key store is unreadable now ({exc})")
    logger.warning("check: %s repaired (%s)", name, what)
    return Observation(STATE_OK, f"repaired: {what}", repaired=True)


async def observe(
    entries: dict[str, MachineEntry], *, gateway_url: str, keystore_path: Path, ssh_bin: str
) -> tuple[dict[str, Observation], Observation]:
    results, health = await _probe_all(entries, gateway_url)
    observations = {name: Observation(r.state, r.detail) for name, r in results.items()}
    for name, result in results.items():
        if result.state == STATE_UNAUTHORIZED and entries[name].repair is not None:
            observations[name] = await _repair(name, entries[name], keystore_path=keystore_path, ssh_bin=ssh_bin)
    return observations, Observation(health.state, health.detail)


# -- status file + notifications --------------------------------------------------------------


def load_status(path: Path) -> dict:
    """The previous run's status. Missing or unreadable means "nothing known yet" (logged)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.error("check: status file %s unreadable (%s) — starting over", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _advance(previous: object, observation: Observation, now: str) -> dict:
    """The new status record for one item, carrying ``since``/``notified`` while the state holds."""
    prev = previous if isinstance(previous, dict) else None
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


# airuleset cuts mention + body at 1900 characters and STILL prints "sent", so one message stays
# well under that: a fleet-wide change goes out in several, each fully delivered or retried.
CHUNK_LIMIT = 1700
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
        hints.append("Kľúč: `remoteos-gateway machine set-key <stroj>` (kľúč na stdin).")
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
    groups: list[list[str]] = [[]]
    for name in pending:
        candidate = groups[-1] + [name]
        lines = [_change_line(n, pending[n]) for n in candidate] + _hints([pending[n] for n in candidate])
        if groups[-1] and len("\n".join([title, *lines])) + reserve > CHUNK_LIMIT:
            groups.append([name])
        else:
            groups[-1] = candidate
    texts = []
    for index, names in enumerate(groups, start=1):
        head = title + (f" ({index}/{len(groups)})" if len(groups) > 1 else "")
        lines = [_change_line(n, pending[n]) for n in names] + _hints([pending[n] for n in names])
        texts.append(("\n".join([head, *lines]), names))
    return texts


# What `airuleset.py notify --body` prints (send()'s status). It EXITS 0 EVEN WHEN THE POST FAILED,
# so only these printed statuses count as delivered ("dedup" = already delivered under that key).
_DELIVERED = frozenset({"sent", "dedup"})


def airuleset_notifier(script: Path, owner: str | None) -> Callable[[str], bool] | None:
    """``python3 <airuleset.py> notify --body <text> --dedup-key <per attempt>``; None when the
    script is not there. Delivered = it ran AND printed ``sent`` (or ``dedup``)."""
    if not script.is_file():
        return None
    python = shutil.which("python3") or sys.executable

    def send(body: str) -> bool:
        # one key per ATTEMPT: a retry of an undelivered message must never be deduped away
        attempt = f"remoteos-gateway-check#{time.time_ns()}#{secrets.token_hex(4)}"
        argv = [python, str(script), "notify", "--body", body, "--dedup-key", attempt]
        if owner:
            argv += ["--owner-name", owner]
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=NOTIFY_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("check: notify did not run (%s) — retried on the next run", type(exc).__name__)
            return False
        status = last_line(proc.stdout)
        if proc.returncode != 0 or status not in _DELIVERED:
            if proc.returncode == 0:
                reason = f"printed {status!r}"
            else:
                reason = last_line(proc.stderr) or f"exit {proc.returncode}"
            logger.error("check: notify NOT delivered (%s) — retried on the next run", reason)
            return False
        return True

    return send


@dataclass
class CheckReport:
    status: dict
    sent: bool | None  # None = nothing to announce


def run_check(
    keystore_path: Path,
    status_path: Path,
    *,
    gateway_url: str,
    notifier: Callable[[str], bool] | None,
    ssh_bin: str | None = None,
    host: str = "dev1",
    now: Callable[[], _dt.datetime] | None = None,
) -> CheckReport:
    stamp = (now or (lambda: _dt.datetime.now(_dt.timezone.utc)))().isoformat(timespec="milliseconds")
    previous = load_status(status_path)
    try:
        entries = load_keystore(keystore_path)
    except MachineSourceError as exc:
        _report_failure(status_path, previous, str(exc), stamp, notifier, host)
        raise
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
    pending = {GATEWAY_KEY: status["gateway"]} if not status["gateway"]["notified"] else {}
    pending.update({n: r for n, r in status["machines"].items() if not r["notified"]})
    # The check could not run last time (or that recovery was not announced yet): say it is back.
    recovered = isinstance(previous.get("error"), dict) or previous.get("recovery_pending") is True
    for name, record in pending.items():
        logger.warning("check: %s is %s (was %s) — %s", name, record["state"], record["previous"], record["detail"])
    sent: bool | None = None
    if pending or recovered:
        sent = True
        for index, (text, names) in enumerate(compose_messages(pending, host, recovered=recovered)):
            delivered = notifier(text) if notifier is not None else True
            sent = sent and delivered
            if delivered:  # only the machines of a DELIVERED message are done; the rest retry
                for name in names:
                    pending[name]["notified"] = True
            elif index == 0 and recovered:  # the first message carries the "running again" title
                status["recovery_pending"] = True
    try:
        _write_status(status_path, status)
    except OSError as exc:
        reason = f"cannot write the status file {status_path} ({exc.strerror or type(exc).__name__})"
        _report_failure(status_path, previous, reason, stamp, notifier, host)
        raise CheckError(reason) from exc
    return CheckReport(status=status, sent=sent)


class CheckError(Exception):
    """The check could not keep its state (the status file is not writable)."""


def _write_status(path: Path, status: dict) -> None:
    fileops.atomic_write(str(path), (json.dumps(status, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _report_failure(
    status_path: Path, previous: dict, message: str, stamp: str, notifier: Callable[[str], bool] | None, host: str
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
        record["notified"] = notifier(text) if notifier is not None else True
    try:
        _write_status(status_path, {**previous, "checked_at": stamp, "error": record})
    except OSError as exc:
        logger.error("check: the status file %s cannot be written either (%s)", status_path, exc.strerror or exc)
