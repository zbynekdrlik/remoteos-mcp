"""Fleet owner notices (#102, owner ruling 2026-11-16): ONE footer-U item per condition KIND.

With an airuleset that has ``--owner-notice`` (``owner_notice.Notifier.per_condition``) the
machine conditions are not one U item per box (17 boxes down were 17 items) but one per kind,
each listing its boxes:

- ``fleet-down``     — every box that is not ``ok`` (``401`` / ``unreachable`` / ``error``), with its state
- ``fleet-stale``    — every box behind the stable release (``drift`` on its record)
- ``fleet-no-admin`` — every box without admin rights (``no_admin`` on its record)

Each kind has a FINGERPRINT of what its text says (down: name + state; stale: name + stable tag;
no-admin: names). The fingerprint last delivered is kept in the status file
(``status["fleet_notices"] = {slug: fingerprint}``), so it survives a run that had to send plain
notices and a book that cannot be written. A different fingerprint re-sends the SAME slug
(airuleset replaces the U item); an undelivered send keeps the old fingerprint, so it is retried
next run. A kind whose set is empty is not open: ``check._settle`` clears its slug, and any old
per-box slug (``state-<box>``, ``stale-<box>``, ``no-admin-<box>``) the book still holds, once.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from remoteos.gateway import drift, owner_notice
from remoteos.gateway.messages import CHUNK_LIMIT
from remoteos.gateway.owner_notice import Notify

logger = logging.getLogger("remoteos.gateway.fleet_notice")

DOWN = "fleet-down"
STALE = "fleet-stale"
NO_ADMIN = "fleet-no-admin"
STATUS_KEY = "fleet_notices"

LABELS = {"ok": "funguje", "401": "odmieta kľúč (401)", "unreachable": "nedostupný", "error": "chyba"}
KEY_HINT = "Kľúč: `remoteos-gateway machine set-key <stroj>` (kľúč na stdin)."
ADMIN_HINT = "Oprava: spusti na stroji inštalátor (remoteos-install), server má bežať ako admin."
_DETAIL_LIMIT = 80


@dataclass(frozen=True)
class KindNotice:
    slug: str
    fingerprint: list[list[str]]
    names: list[str]
    text: str


def fit(title: str, lines: list[str], tail: list[str], limit: int = CHUNK_LIMIT) -> str:
    """ONE text of at most *limit* characters (a kind is one U item, never split): the lines that
    do not fit become "… a ďalších N"."""
    for keep in range(len(lines), -1, -1):
        more = [f"… a ďalších {len(lines) - keep}"] if keep < len(lines) else []
        text = "\n".join([title, *lines[:keep], *more, *tail])
        if len(text) <= limit:
            return text
    return title[:limit]


def _down(machines: dict[str, dict], host: str) -> KindNotice | None:
    names = sorted(n for n, r in machines.items() if r["state"] != "ok")
    if not names:
        return None
    lines = []
    for name in names:
        record = machines[name]
        since = str(record.get("since") or "")[:16].replace("T", " ")
        line = f"• {name}: {LABELS.get(record['state'], record['state'])} (od {since})"
        if record["state"] != "unreachable":  # "All connection attempts failed" x17 says nothing
            line += " — " + str(record.get("detail") or "")[:_DETAIL_LIMIT]
        lines.append(line)
    tail = [KEY_HINT] if any(machines[n]["state"] == "401" for n in names) else []
    title = f"MCP brána ({host}): nefunkčné stroje ({len(names)})"
    return KindNotice(DOWN, [[n, machines[n]["state"]] for n in names], names, fit(title, lines, tail))


def _stale(machines: dict[str, dict], host: str, moment: _dt.datetime) -> KindNotice | None:
    names = sorted(n for n, r in machines.items() if "drift" in r)
    if not names:
        return None
    tags = sorted({machines[n]["drift"]["stable"] for n in names})
    lines = [drift.line(n, machines[n], moment) for n in names]
    title = f"MCP brána ({host}): stroje pozadu za stable {', '.join(tags)} ({len(names)})"
    fingerprint = [[n, machines[n]["drift"]["stable"]] for n in names]
    return KindNotice(STALE, fingerprint, names, fit(title, lines, []))


def _no_admin(machines: dict[str, dict], host: str) -> KindNotice | None:
    names = sorted(n for n, r in machines.items() if "no_admin" in r)
    if not names:
        return None
    lines = [f"• {n}: server nemá admin práva" for n in names]
    title = f"MCP brána ({host}): stroje bez admin práv ({len(names)})"
    return KindNotice(NO_ADMIN, [[n] for n in names], names, fit(title, lines, [ADMIN_HINT]))


def build(machines: dict[str, dict], host: str, moment: _dt.datetime) -> dict[str, KindNotice]:
    """The kind notices that are open in *machines* (a final status), by slug."""
    made = (_down(machines, host), _stale(machines, host, moment), _no_admin(machines, host))
    return {notice.slug: notice for notice in made if notice is not None}


def open_slugs(machines: dict[str, dict]) -> set[str]:
    """The kind slugs open in *machines*: a kind with at least one box."""
    kinds = {
        DOWN: any(r["state"] != "ok" for r in machines.values()),
        STALE: any("drift" in r for r in machines.values()),
        NO_ADMIN: any("no_admin" in r for r in machines.values()),
    }
    return {slug for slug, is_open in kinds.items() if is_open}


def delivered_before(previous: dict) -> dict[str, list]:
    """The fingerprints delivered by earlier runs; anything that is not what this code writes (a
    hand edit) is dropped, so that kind is simply sent again."""
    known = previous.get(STATUS_KEY)
    if not isinstance(known, dict):
        return {}
    return {slug: value for slug, value in known.items() if slug in (DOWN, STALE, NO_ADMIN) and isinstance(value, list)}


def _mark_notified(machines: dict[str, dict], notice: KindNotice) -> None:
    for name in notice.names:
        record = machines[name]
        if notice.slug == DOWN:
            record["notified"] = True
        elif notice.slug == STALE:
            record["drift"]["notified"] = True
        else:
            record["no_admin"]["notified"] = True


def reconcile(status: dict, notifier: Notify | None, host: str, moment: _dt.datetime) -> bool | None:
    """Send each open kind whose fingerprint differs from the one delivered (the same slug: it
    replaces the U item). Updates ``status[STATUS_KEY]`` (an empty kind leaves it: ``_settle``
    clears its slug) and marks the listed boxes notified once their kind's notice is current.
    Returns None when nothing had to go out, else whether all of it did."""
    machines = status["machines"]
    known = status.get(STATUS_KEY) if isinstance(status.get(STATUS_KEY), dict) else {}
    current: dict[str, list] = {}
    sent: bool | None = None
    for slug, notice in build(machines, host, moment).items():
        if known.get(slug) == notice.fingerprint:
            current[slug] = notice.fingerprint
            _mark_notified(machines, notice)
            continue
        logger.warning("check: %s", notice.text.replace("\n", " "))
        delivered = owner_notice.send(notifier, notice.text, slug)
        sent = delivered if sent is None else (sent and delivered)
        if delivered:
            current[slug] = notice.fingerprint
            _mark_notified(machines, notice)
        elif slug in known:  # not delivered: keep the old one, so the change is retried next run
            current[slug] = known[slug]
    status[STATUS_KEY] = current
    return sent
