"""Fleet version drift for ``remoteos-gateway check`` (#63): who is older than stable, for how long.

Each machine's status record gains ``version`` + ``version_seen`` (the last ``/health`` answer, kept
while the box does not answer) and, while it is reported, ``drift``::

    "drift": {"since": "<when it fell behind>", "stable": "v0.7.0.dev40", "notified": true}

A box older than stable for more than ``GRACE_DAYS`` is announced ONCE, until a fresh version
shows it caught up; an undelivered announcement stays ``notified: false`` and is retried. Only a
version read in THIS run is judged: a box that does not answer keeps what it had, and so does
every box in a run whose release list could not be fetched.
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Callable
from dataclasses import dataclass

from remoteos.gateway.messages import split_to_fit
from remoteos.release_channel import Release, ReleaseFetchError, behind_since, choose, version_key

logger = logging.getLogger("remoteos.gateway.drift")

GRACE_DAYS = 3


@dataclass(frozen=True)
class DriftPolicy:
    fetch: Callable[[], list[Release]]
    soak_days: int


def read_releases(policy: DriftPolicy | None, moment: _dt.datetime) -> list[Release] | None:
    """The release list; None skips drift for this run (logged), and nothing else depends on it.
    A stable that cannot be compared says nothing about any box: it must not read as "caught up"."""
    if policy is None:
        return None
    try:
        releases = policy.fetch()
    except ReleaseFetchError as exc:
        logger.warning("check: the release list could not be read (%s) — no drift judged this run", exc)
        return None
    stable = choose(releases, moment, policy.soak_days).stable
    if stable is not None and version_key(stable.version) is None:
        logger.warning("check: stable %s is not comparable — no drift judged this run", stable.tag)
        return None
    return releases


def _known_drift(previous: object) -> dict | None:
    drift = previous.get("drift") if isinstance(previous, dict) else None
    if isinstance(drift, dict) and isinstance(drift.get("since"), str) and isinstance(drift.get("stable"), str):
        return {"since": drift["since"], "stable": drift["stable"], "notified": bool(drift.get("notified"))}
    return None


def _text(previous: object, key: str) -> str | None:
    value = previous.get(key) if isinstance(previous, dict) else None
    return value if isinstance(value, str) else None


def record_versions(record: dict, previous: object, version: str | None, stamp: str) -> None:
    """Keep the last seen version on the machine's record (none yet: no field)."""
    seen = version or _text(previous, "version")
    if seen is None:
        return
    record["version"] = seen
    record["version_seen"] = stamp if version else (_text(previous, "version_seen") or stamp)


def judge(
    record: dict,
    previous: object,
    version: str | None,
    releases: list[Release] | None,
    moment: _dt.datetime,
    soak_days: int,
) -> None:
    """Set or clear ``record["drift"]`` for one machine."""
    known = _known_drift(previous)
    if releases is None or version is None or version_key(version) is None:
        if version is not None and version_key(version) is None:
            logger.warning("check: version %r is not comparable — no drift judged for it", version)
        if known is not None:
            record["drift"] = known
        return
    since = behind_since(version, releases, moment, soak_days)
    if since is None or moment - since <= _dt.timedelta(days=GRACE_DAYS):
        return  # caught up, or within the grace: the episode (if any) is over
    stable = choose(releases, moment, soak_days).stable
    assert stable is not None  # behind_since found it
    record["drift"] = {
        "since": since.isoformat(timespec="seconds"),
        "stable": stable.tag,
        "notified": known["notified"] if known is not None else False,
    }


def _days(record: dict, moment: _dt.datetime) -> int:
    try:
        return (moment - _dt.datetime.fromisoformat(record["drift"]["since"])).days
    except (ValueError, TypeError):  # a hand-edited record: the report still goes out
        return 0


def _line(name: str, record: dict, moment: _dt.datetime) -> str:
    return f"• {name}: verzia {record.get('version', '?')}, za stable už {_days(record, moment)} dní"


def compose_messages(pending: dict[str, dict], host: str, moment: _dt.datetime) -> list[tuple[str, list[str]]]:
    """The owner's texts (Slovak, phone-readable), as ``(text, the names it announces)``, each at
    most ``CHUNK_LIMIT`` characters."""
    tags = sorted({record["drift"]["stable"] for record in pending.values()})
    title = f"MCP brána ({host}): stroje pozadu za stable {', '.join(tags)}"
    reserve = len(" (99/99)")

    def lines(names: list[str]) -> list[str]:
        return [_line(name, pending[name], moment) for name in names]

    groups = split_to_fit(pending, lambda names: len("\n".join([title, *lines(names)])) + reserve)
    texts = []
    for index, names in enumerate(groups, start=1):
        head = title + (f" ({index}/{len(groups)})" if len(groups) > 1 else "")
        texts.append(("\n".join([head, *lines(names)]), names))
    return texts


def announce(
    machines: dict[str, dict],
    host: str,
    moment: _dt.datetime,
    notifier: Callable[[str], bool] | None,
) -> bool | None:
    """Send what is owed. Returns None when nothing was, else whether all of it went out."""
    pending = {n: r for n, r in machines.items() if "drift" in r and not r["drift"]["notified"]}
    if not pending:
        return None
    sent = True
    for text, names in compose_messages(pending, host, moment):
        logger.warning("check: %s", text.replace("\n", " "))
        delivered = notifier(text) if notifier is not None else True
        sent = sent and delivered
        if delivered:
            for name in names:
                pending[name]["drift"]["notified"] = True
    return sent


def update(
    machines: dict[str, dict],
    previous: dict,
    versions: dict[str, str | None],
    policy: DriftPolicy | None,
    stamp: str,
    moment: _dt.datetime,
    host: str,
    notifier: Callable[[str], bool] | None,
) -> bool | None:
    """Record versions and report drift for one run. Nothing here may cost the run its status
    write: a failure only skips drift (logged), and every record keeps the drift it had."""
    for name, record in machines.items():
        record_versions(record, previous.get(name), versions.get(name), stamp)
    try:
        releases = read_releases(policy, moment)
        for name, record in machines.items():
            soak = policy.soak_days if policy is not None else 0
            judge(record, previous.get(name), versions.get(name), releases, moment, soak)
        return announce(machines, host, moment, notifier) if releases is not None else None
    except Exception:
        logger.exception("check: drift failed — no drift judged this run")
        for name, record in machines.items():
            record.pop("drift", None)
            known = _known_drift(previous.get(name))
            if known is not None:
                record["drift"] = known
        return None
