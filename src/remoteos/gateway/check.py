"""``remoteos-gateway check`` (#51): the key-drift watchdog, run every 10 min by a user timer.

Each run probes every key-store machine DIRECTLY with its stored key (``initialize``) and the
gateway's own ``/healthz``, then writes ``~/.local/state/remoteos-gateway/machines-status.json``::

    {"checked_at": "...",
     "gateway":  {"state": "ok", "since": "...", "detail": "version 0.7.0.dev33", "notified": true},
     "machines": {"linux-cam2": {"state": "401", "since": "...", "detail": "...", "notified": true}}}

States: ``ok`` / ``401`` / ``unreachable`` / ``error`` (``probe.py``). ``since`` is when the
current state began. A state CHANGE is announced once — through ``airuleset.py notify --body``
when that script exists, else as a WARNING log line. A notification that did not go out stays
``notified: false`` and is retried on the next run. A machine first seen ``ok`` is not news.

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
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remoteos.gateway import fileops
from remoteos.gateway.keystore import edit_keystore, load_keystore
from remoteos.gateway.probe import (
    STATE_OK,
    STATE_UNAUTHORIZED,
    ProbeResult,
    new_client,
    probe_healthz,
    probe_initialize,
)
from remoteos.gateway.registry import MachineEntry, MachineSourceError, validate_key

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
        results = await asyncio.gather(
            *(probe_initialize(client, entries[n].url, entries[n].authorization) for n in names),
            probe_healthz(client, gateway_url),
        )
    return dict(zip(names, results[:-1])), results[-1]


def read_key_by_recipe(entry: MachineEntry, *, ssh_bin: str) -> tuple[str | None, str]:
    """Run the machine's repair recipe; returns ``(key or None, what happened)``. Never the key in text."""
    recipe = entry.repair
    assert recipe is not None
    remote = shlex.join([*(["sudo", "-n"] if recipe.sudo else []), "cat", recipe.key_file])
    argv = [ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", recipe.ssh, remote]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=REPAIR_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"repair failed: ssh {recipe.ssh} did not run ({type(exc).__name__})"
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        reason = "".join(ch if ch.isprintable() else "?" for ch in (lines[-1] if lines else ""))[:200]
        return None, f"repair failed: ssh {recipe.ssh} exit {proc.returncode} ({reason})"
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
    with edit_keystore(keystore_path) as entries:
        current = entries.get(name)
        if current is None or current.key != entry.key:  # a set-key won the race: leave it alone
            return Observation(STATE_UNAUTHORIZED, "repair skipped: the key store changed meanwhile")
        entries[name] = MachineEntry(url=current.url, key=key, repair=current.repair)
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
        return {
            "state": observation.state,
            "since": prev.get("since") or now,
            "detail": observation.detail,
            "notified": bool(prev.get("notified")),
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


def compose_message(pending: dict[str, dict], host: str) -> str:
    """The notification text (Slovak, phone-readable): names, states, details — never a key."""
    lines = [f"MCP brána ({host}): zmena stavu strojov"]
    for name, record in pending.items():
        old = record.get("previous")
        before = _LABELS.get(old, old) if old else "nový"
        after = _LABELS.get(record["state"], record["state"])
        label = "brána (gateway /healthz)" if name == GATEWAY_KEY else name
        lines.append(f"• {label}: {before} → {after} [{old or 'new'} → {record['state']}] — {record['detail']}")
    if any(r["state"] == STATE_UNAUTHORIZED for r in pending.values()):
        lines.append("Kľúč: `remoteos-gateway machine set-key <stroj>` (kľúč na stdin) — projekty sa nemenia.")
    return "\n".join(lines)


def airuleset_notifier(script: Path, owner: str | None) -> Callable[[str], bool] | None:
    """``python3 <airuleset.py> notify --body <text>``; None when the script is not there."""
    if not script.is_file():
        return None
    python = shutil.which("python3") or sys.executable

    def send(body: str) -> bool:
        argv = [python, str(script), "notify", "--body", body]
        if owner:
            argv += ["--owner-name", owner]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=NOTIFY_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("check: notify did not run (%s) — retried on the next run", type(exc).__name__)
            return False
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
            logger.error("check: notify FAILED (%s) — retried on the next run", tail[0][:200])
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
    entries = load_keystore(keystore_path)
    observations, health = asyncio.run(
        observe(
            entries,
            gateway_url=gateway_url,
            keystore_path=keystore_path,
            ssh_bin=ssh_bin or os.environ.get("REMOTEOS_GATEWAY_SSH", "ssh"),
        )
    )
    stamp = (now or (lambda: _dt.datetime.now(_dt.timezone.utc)))().isoformat(timespec="seconds")
    previous = load_status(status_path)
    old_machines = previous.get("machines") if isinstance(previous.get("machines"), dict) else {}
    status = {
        "checked_at": stamp,
        "gateway": _advance(previous.get("gateway"), health, stamp),
        "machines": {name: _advance(old_machines.get(name), obs, stamp) for name, obs in sorted(observations.items())},
    }
    pending = {GATEWAY_KEY: status["gateway"]} if not status["gateway"]["notified"] else {}
    pending.update({n: r for n, r in status["machines"].items() if not r["notified"]})
    for name, record in pending.items():
        logger.warning("check: %s is %s (was %s) — %s", name, record["state"], record["previous"], record["detail"])
    sent: bool | None = None
    if pending:
        sent = notifier(compose_message(pending, host)) if notifier is not None else True
        if sent:
            for record in pending.values():
                record["notified"] = True
    fileops.atomic_write(str(status_path), (json.dumps(status, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return CheckReport(status=status, sent=sent)
