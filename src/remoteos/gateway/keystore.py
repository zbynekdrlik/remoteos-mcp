"""The machine key store (#51): ONE place for every machine's url + bearer key.

``~/.config/remoteos-gateway/machines.json`` on dev1, 0600 in a 0700 directory, never in git::

    {
      "linux-cam1": {"url": "http://cam1.lan:8092/mcp", "key": "<key>"},
      "linux-cam2": {"url": "http://cam2.lan:8092/mcp", "key": "<key>",
                     "repair": {"ssh": "root@cam2.lan", "key_file": "/etc/remoteos-mcp/config.json"}}
    }

It is the gateway's machine source (``serve --machines``): a key changed here reaches every
consumer on its next call, with no consumer-side change. It is edited only through the CLI:

- ``machine set-key <name>`` — the key on stdin (never an argv), probed before it is stored;
- ``machine import``         — the one-time build from today's per-project MCP configs;
- ``machine repair-recipe``  — the explicit 401 auto-repair recipe ``check`` may use.

Keys are never logged or printed: output shows ``mask_secret`` (length + a sha256 prefix).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from remoteos.gateway import fileops
from remoteos.gateway.probe import (
    STATE_OK,
    STATE_UNAUTHORIZED,
    STATE_UNREACHABLE,
    ProbeResult,
    probe_many,
    probe_one,
)
from remoteos.gateway.registry import (
    DEFAULT_MACHINES_PATH,
    MachineEntry,
    MachineSourceError,
    authorization_header,
    is_legacy_mcp_config,
    is_valid_machine_name,
    parse_keystore,
    read_json_file,
    validate_http_url,
    validate_key,
    warn_if_loose_permissions,
)

logger = logging.getLogger("remoteos.gateway.keystore")

DEFAULT_KEYSTORE_PATH = DEFAULT_MACHINES_PATH
# Fleet machines are named win-* / linux-* / mac-*. Other MCP servers in a project config
# (n8n, codex-bridge behind Cloudflare Access, ...) must never land in the key store.
DEFAULT_IMPORT_MATCH = r"(win|linux|mac)-[A-Za-z0-9._-]+"


class KeystoreError(MachineSourceError):
    """The key store is unreadable/invalid, or a requested key-store edit is refused."""


def mask_secret(value: str) -> str:
    """What output may show of a key or token: its length and a 12-hex sha256 prefix."""
    return f"<{len(value)} chars, sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}...>"


def bearer_value(header: str) -> str | None:
    """The credential of an ``Authorization: Bearer <x>`` header value, else None."""
    scheme, _, rest = header.strip().partition(" ")
    rest = rest.strip()
    return rest if scheme.lower() == "bearer" and rest else None


# -- file I/O ---------------------------------------------------------------------------


def load_keystore(path: Path, *, missing_ok: bool = False) -> dict[str, MachineEntry]:
    """Load and strictly validate the key store. A missing file is empty only with *missing_ok*."""
    if missing_ok and not path.exists():
        return {}
    data = read_json_file(path, "key store")
    if is_legacy_mcp_config(data):
        raise KeystoreError(
            f"{path} is an MCP config (mcpServers), not the key store. Build the key store with "
            "`remoteos-gateway machine import` and point --machines at it."
        )
    entries = parse_keystore(data, f"key store {path}")
    warn_if_loose_permissions(path, "key store")
    return entries


def _payload(entries: dict[str, MachineEntry]) -> str:
    return json.dumps({name: entries[name].to_json() for name in sorted(entries)}, indent=2) + "\n"


def save_keystore(path: Path, entries: dict[str, MachineEntry]) -> None:
    """Atomically write the key store, 0600 (its directory 0700 when created)."""
    fileops.atomic_write(str(path), _payload(entries).encode("utf-8"))
    logger.info("key store %s written: %d machine(s)", path, len(entries))


@contextlib.contextmanager
def edit_keystore(path: Path) -> Iterator[dict[str, MachineEntry]]:
    """Lock, load (missing = empty), yield the dict for editing, then save it — only when it
    changed: every write makes the running gateway reload, and a no-op must not."""
    with fileops.exclusive_lock(str(path.with_name(path.name + ".lock"))):
        entries = load_keystore(path, missing_ok=True)
        before = _payload(entries) if path.exists() else None
        yield entries
        if _payload(entries) != before:
            save_keystore(path, entries)


# -- set-key ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetKeyOutcome:
    name: str
    created: bool
    url: str
    probe: ProbeResult


def set_key(
    path: Path,
    name: str,
    key: str,
    *,
    url: str | None = None,
    force: bool = False,
    probe: Callable[[str, str | None], ProbeResult] = probe_one,
) -> SetKeyOutcome:
    """Store *key* for machine *name* after probing it. A key the machine REJECTS (401) is
    refused unless *force*: a typo must not cut every project off that machine. An unreachable
    machine is stored anyway (a rotation may be staged before the box is up)."""
    if not is_valid_machine_name(name):
        raise KeystoreError(f"invalid machine name {name!r}: use letters, digits and . _ -")
    validate_key(key)
    current = load_keystore(path, missing_ok=True).get(name)
    if current is None and url is None:
        raise KeystoreError(f"machine {name!r} is not in the key store {path} — add it with --url <its /mcp url>")
    target = validate_http_url(url, what="--url") if url is not None else current.url
    result = probe(target, f"Bearer {key}")
    if result.state == STATE_UNAUTHORIZED and not force:
        raise KeystoreError(
            f"{name}: the machine REJECTS this key ({result.detail}) — nothing changed. "
            "Check the key, or store it anyway with --force."
        )
    with edit_keystore(path) as entries:
        existing = entries.get(name)
        entries[name] = MachineEntry(url=target, key=key, repair=existing.repair if existing else None)
    logger.info("key store: %s %s (probe %s)", name, "added" if current is None else "key set", result.state)
    return SetKeyOutcome(name=name, created=current is None, url=target, probe=result)


# -- import from the per-project MCP configs ----------------------------------------------


@dataclass(frozen=True)
class KeyCopy:
    url: str
    key: str = field(repr=False)


@dataclass
class ImportDecision:
    """How one machine's key was chosen. ``verdict``: ``verified`` (a copy answered 200) or
    the reason none did (``unreachable`` / ``401`` / ``error``) — then the key store's own entry,
    else the most common copy, is kept."""

    name: str
    chosen: KeyCopy
    chosen_sources: list[str]
    verdict: str
    detail: str
    copies: int
    distinct: int
    stale_sources: list[str]
    kept_store: bool = False  # nothing verified, so the key store's own entry was kept
    other_url_sources: list[str] = field(default_factory=list)  # copies at ANOTHER url, ignored

    @property
    def flagged(self) -> bool:
        return self.verdict != "verified"


@dataclass
class ImportScan:
    copies: dict[str, dict[KeyCopy, list[str]]] = field(default_factory=dict)  # name -> copy -> sources
    notes: list[str] = field(default_factory=list)  # skipped sources / entries (never a key)


def scan_sources(
    sources: Iterable[tuple[str, str]], *, match: str = DEFAULT_IMPORT_MATCH, skip_url_prefix: str | None = None
) -> ImportScan:
    """Collect every machine-key copy from MCP-config texts ``(label, text)``.

    A copy is an ``mcpServers`` entry whose NAME fully matches *match*, with an http(s) url
    and an ``Authorization: Bearer <key>`` header. Entries pointing at the gateway itself
    (*skip_url_prefix*) carry a project token, not a machine key, and are skipped.
    """
    pattern = re.compile(match)
    scan = ImportScan()
    for label, text in sources:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as exc:
            scan.notes.append(f"{label}: not valid JSON ({exc}) — skipped")
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            scan.notes.append(f"{label}: no mcpServers object — skipped")
            continue
        for name, entry in servers.items():
            if not isinstance(name, str) or not pattern.fullmatch(name) or not is_valid_machine_name(name):
                continue
            url = entry.get("url") if isinstance(entry, dict) else None
            if not isinstance(url, str):
                scan.notes.append(f"{label}: {name} has no url — skipped")
                continue
            if skip_url_prefix and url.startswith(skip_url_prefix.rstrip("/") + "/"):
                continue  # a generated gateway entry: a project token, not a machine key
            try:
                validate_http_url(url)
            except MachineSourceError:
                scan.notes.append(f"{label}: {name} url is not http(s) — skipped")
                continue
            key = bearer_value(authorization_header(entry) or "")
            try:
                validate_key(key or "")
            except MachineSourceError:
                scan.notes.append(f"{label}: {name} has no usable 'Authorization: Bearer <key>' — skipped")
                continue
            scan.copies.setdefault(name, {}).setdefault(KeyCopy(url=url, key=key), []).append(label)
    return scan


def resolve_import(
    scan: ImportScan,
    existing: dict[str, MachineEntry],
    *,
    prober: Callable[[list[tuple[object, str, str | None]]], dict[object, ProbeResult]] | None = None,
    accept_url_change: bool = False,
) -> list[ImportDecision]:
    """Pick one copy per machine: the copy that answers ``initialize`` with 200 wins (several:
    the most common of them). When none does, the key store's current entry is kept, else the
    most common copy — FLAGGED. The key store's own entry counts as one more copy (``key store``).

    For a machine the key store already has, only copies at ITS url count: a 200 from another
    url proves the key fits that url, not that it is this machine (a stale IP now used by
    another box). Those copies are listed, never probed (the key is not sent there) and never
    chosen, unless *accept_url_change*."""
    candidates: dict[str, dict[KeyCopy, list[str]]] = {}
    other_url: dict[str, list[str]] = {}
    for name, copies in scan.copies.items():
        merged: dict[KeyCopy, list[str]] = {}
        if name in existing:
            merged[KeyCopy(url=existing[name].url, key=existing[name].key)] = ["key store"]
        for copy, labels in copies.items():
            if name in existing and not accept_url_change and copy.url != existing[name].url:
                other_url.setdefault(name, []).extend(labels)
                continue
            merged.setdefault(copy, []).extend(labels)
        candidates[name] = merged

    targets = [((name, copy), copy.url, f"Bearer {copy.key}") for name, copies in candidates.items() for copy in copies]
    if prober is None:
        import asyncio

        results = asyncio.run(probe_many(targets))
    else:
        results = prober(targets)

    decisions: list[ImportDecision] = []
    for name in sorted(candidates):
        copies = candidates[name]
        order = list(copies)  # first appearance breaks ties
        ranked = sorted(order, key=lambda c: (-len(copies[c]), order.index(c)))
        ok = [c for c in ranked if results[(name, c)].state == STATE_OK]
        store_copy = KeyCopy(url=existing[name].url, key=existing[name].key) if name in existing else None
        kept_store = False
        if ok:
            chosen, verdict, detail = ok[0], "verified", results[(name, ok[0])].detail
        else:
            # Nothing verifies: an entry already in the key store (maybe a set-key made while the
            # box was down) is never replaced by an unverified copy; else the most common copy.
            kept_store = store_copy is not None
            chosen = store_copy if store_copy is not None else ranked[0]
            states = {results[(name, c)].state for c in ranked}
            if STATE_UNREACHABLE in states:
                verdict = STATE_UNREACHABLE
            elif states == {STATE_UNAUTHORIZED}:
                verdict = STATE_UNAUTHORIZED
            else:
                verdict = "error"
            detail = results[(name, chosen)].detail
        stale = sorted({label for c in order if c != chosen for label in copies[c]})
        decisions.append(
            ImportDecision(
                name=name,
                chosen=chosen,
                chosen_sources=list(copies[chosen]),
                verdict=verdict,
                detail=detail,
                copies=sum(len(v) for v in copies.values()),
                distinct=len(copies),
                stale_sources=stale,
                kept_store=kept_store,
                other_url_sources=sorted(set(other_url.get(name, []))),
            )
        )
    return decisions


def apply_import(
    path: Path, decisions: list[ImportDecision], *, snapshot: dict[str, MachineEntry] | None = None
) -> list[str]:
    """Write the decisions into the key store (keeping each machine's repair recipe).

    *snapshot* is the key store the decisions were made from. Under the lock, a machine whose
    entry changed since (a set-key or a repair while the import probed) is left as it is now,
    and a decision that only repeats the snapshot is a no-op. Returns the machines left alone
    because they changed meanwhile."""
    changed_meanwhile: list[str] = []
    with edit_keystore(path) as entries:
        for decision in decisions:
            existing = entries.get(decision.name)
            if snapshot is not None:
                before = snapshot.get(decision.name)
                if _pair(existing) != _pair(before):
                    changed_meanwhile.append(decision.name)
                    continue
                if _pair(before) == (decision.chosen.url, decision.chosen.key):
                    continue
            entries[decision.name] = MachineEntry(
                url=decision.chosen.url, key=decision.chosen.key, repair=existing.repair if existing else None
            )
    return changed_meanwhile


def _pair(entry: MachineEntry | None) -> tuple[str, str] | None:
    return (entry.url, entry.key) if entry is not None else None


def describe_decision(decision: ImportDecision) -> str:
    """One report line — sources and states only, never a key."""
    copies = f"{decision.copies} cop{'y' if decision.copies == 1 else 'ies'}"
    if decision.distinct > 1:
        copies += f", {decision.distinct} DIFFERENT (conflict)"
    if decision.flagged:
        kept = "the key store's current copy" if decision.kept_store else "the most common copy"
        head = f"NOT VERIFIED ({decision.verdict}: {decision.detail}) — kept {kept}"
    else:
        head = f"verified ({decision.detail})"
    line = f"  {decision.name}: {copies} — {head}, from {', '.join(decision.chosen_sources)}; url {decision.chosen.url}"
    if decision.stale_sources:
        line += f"\n      a DIFFERENT copy is in: {', '.join(decision.stale_sources)}"
    if decision.other_url_sources:
        line += (
            f"\n      a copy at ANOTHER url is in: {', '.join(decision.other_url_sources)} — ignored (not probed);"
            " re-run with --accept-url-change if the machine really moved"
        )
    return line
