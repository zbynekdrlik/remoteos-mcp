"""Fleet owner notices (#102, owner ruling 2026-11-16): ONE footer-U item per condition KIND.

With an airuleset that has ``--owner-notice`` (``owner_notice.Notifier.per_condition``) the
machine conditions are not one U item per box (17 boxes down were 17 items) but one per kind,
each listing its boxes:

- ``fleet-down``     — every box that is not ``ok`` (``401`` / ``unreachable`` / ``error``), with its state
- ``fleet-stale``    — every box behind the stable release (``drift`` on its record)
- ``fleet-no-admin`` — every box without admin rights (``no_admin`` on its record)

Each kind has a FINGERPRINT of what its text says (down: name + state; stale: name + version +
stable tag; no-admin: names), and its text holds nothing else that changes (no day counts), so the
item stays true until the fingerprint changes. The fingerprint last delivered is kept in the status file
(``status["fleet_notices"] = {slug: fingerprint}``), so it survives a run that had to send plain
notices and a book that cannot be written. A different fingerprint re-sends the SAME slug
(airuleset replaces the U item); an undelivered send keeps the old fingerprint, so it is retried
next run. A kind whose set is empty is not open: ``check._settle`` clears its slug, and any old
per-box slug (``state-<box>``, ``stale-<box>``, ``no-admin-<box>``) the book still holds, once. A
kind delivered but missing from the book (its book write failed) is cleared by ``clear_unbooked``
instead; a clear that fails there stays owed (fingerprint ``[]``) and is retried next run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from remoteos.gateway import owner_notice
from remoteos.gateway.messages import CHUNK_LIMIT, KEY_HINT, STATE_LABELS
from remoteos.gateway.no_admin import ADMIN_HINT
from remoteos.gateway.owner_notice import Notifier, Notify

logger = logging.getLogger("remoteos.gateway.fleet_notice")

DOWN = "fleet-down"
STALE = "fleet-stale"
NO_ADMIN = "fleet-no-admin"
STATUS_KEY = "fleet_notices"

KINDS = (DOWN, STALE, NO_ADMIN)
_DETAIL_LIMIT = 80


def _is_down(record: dict) -> bool:
    return record["state"] != "ok"


def _is_stale(record: dict) -> bool:
    return "drift" in record


def _is_no_admin(record: dict) -> bool:
    return "no_admin" in record


_OPEN: dict[str, Callable[[dict], bool]] = {DOWN: _is_down, STALE: _is_stale, NO_ADMIN: _is_no_admin}


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
    names = sorted(n for n, r in machines.items() if _is_down(r))
    if not names:
        return None
    lines = []
    for name in names:
        record = machines[name]
        since = str(record.get("since") or "")[:16].replace("T", " ")
        line = f"• {name}: {STATE_LABELS.get(record['state'], record['state'])} (od {since})"
        if record["state"] != "unreachable":  # "All connection attempts failed" x17 says nothing
            line += " — " + str(record.get("detail") or "")[:_DETAIL_LIMIT]
        lines.append(line)
    tail = [KEY_HINT] if any(machines[n]["state"] == "401" for n in names) else []
    title = f"MCP brána ({host}): nefunkčné stroje ({len(names)})"
    return KindNotice(DOWN, [[n, machines[n]["state"]] for n in names], names, fit(title, lines, tail))


def _stale(machines: dict[str, dict], host: str) -> KindNotice | None:
    names = sorted(n for n, r in machines.items() if _is_stale(r))
    if not names:
        return None
    tags = sorted({machines[n]["drift"]["stable"] for n in names})
    # "pozadu od <date>", never a day count: the text must stay true while the fingerprint holds
    lines = [
        f"• {n}: verzia {machines[n].get('version', '?')}, pozadu od {str(machines[n]['drift']['since'])[:10]}"
        for n in names
    ]
    title = f"MCP brána ({host}): stroje pozadu za stable {', '.join(tags)} ({len(names)})"
    fingerprint = [[n, str(machines[n].get("version", "?")), machines[n]["drift"]["stable"]] for n in names]
    return KindNotice(STALE, fingerprint, names, fit(title, lines, []))


def _no_admin(machines: dict[str, dict], host: str) -> KindNotice | None:
    names = sorted(n for n, r in machines.items() if _is_no_admin(r))
    if not names:
        return None
    lines = [f"• {n}: server nemá admin práva" for n in names]
    title = f"MCP brána ({host}): stroje bez admin práv ({len(names)})"
    return KindNotice(NO_ADMIN, [[n] for n in names], names, fit(title, lines, [ADMIN_HINT]))


def build_kind(slug: str, machines: dict[str, dict], host: str) -> KindNotice | None:
    """The notice of kind *slug* in *machines* (a final status); None when no box is in it."""
    if slug == DOWN:
        return _down(machines, host)
    if slug == STALE:
        return _stale(machines, host)
    return _no_admin(machines, host)


def build(machines: dict[str, dict], host: str) -> dict[str, KindNotice]:
    """The kind notices that are open in *machines*, by slug."""
    made = (build_kind(slug, machines, host) for slug in KINDS)
    return {notice.slug: notice for notice in made if notice is not None}


def open_slugs(machines: dict[str, dict]) -> set[str]:
    """The kind slugs open in *machines*: a kind with at least one box."""
    return {slug for slug, is_open in _OPEN.items() if any(is_open(r) for r in machines.values())}


def delivered_before(previous: dict) -> dict[str, list]:
    """The fingerprints delivered by earlier runs; anything that is not what this code writes (a
    hand edit) is dropped, so that kind is simply sent again."""
    known = previous.get(STATUS_KEY)
    if not isinstance(known, dict):
        return {}
    return {slug: value for slug, value in known.items() if slug in KINDS and isinstance(value, list)}


def _mark_notified(machines: dict[str, dict], notice: KindNotice) -> None:
    for name in notice.names:
        record = machines[name]
        if notice.slug == DOWN:
            record["notified"] = True
        elif notice.slug == STALE:
            record["drift"]["notified"] = True
        else:
            record["no_admin"]["notified"] = True


def reconcile(status: dict, notifier: Notify | None, host: str) -> bool | None:
    """Send each open kind whose fingerprint differs from the one delivered (the same slug: it
    replaces the U item). Updates ``status[STATUS_KEY]`` and marks the listed boxes notified once
    their kind's notice is current. A kind that closed keeps its entry for ``clear_unbooked``.
    Each kind on its own: one that fails is logged and retried, the others still go out.
    Returns None when nothing had to go out, else whether all of it did."""
    machines = status["machines"]
    known = status.get(STATUS_KEY) if isinstance(status.get(STATUS_KEY), dict) else {}
    current: dict[str, list] = {slug: value for slug, value in known.items() if slug not in open_slugs(machines)}
    sent: bool | None = None
    for slug in KINDS:
        try:
            notice = build_kind(slug, machines, host)
        except Exception:
            logger.exception("check: the %s notice could not be built — retried next run", slug)
            sent = False
            if slug in known:
                current[slug] = known[slug]
            continue
        if notice is None:
            continue
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
        elif slug in known:  # not delivered: the old one is still in U (a later close must clear it)
            current[slug] = known[slug]
    status[STATUS_KEY] = current
    return sent


def clear_unbooked(status: dict, notifier: Notify | None) -> None:
    """Clear each CLOSED kind still in ``status[STATUS_KEY]`` that the book does not hold: it went
    into U but its book write failed, so ``settle`` cannot see it. A kind the book holds is left
    to ``settle``. Dropped once cleared; a failed clear stays owed (``[]``) and is retried next run."""
    notices = status.get(STATUS_KEY)
    if not isinstance(notices, dict):
        return
    open_now = open_slugs(status["machines"])
    closed = [slug for slug in KINDS if slug in notices and slug not in open_now]
    if not closed:
        return
    if not isinstance(notifier, Notifier) or not notifier.per_condition or notifier.book is None:
        return  # cannot clear this run: kept, retried when the notifier can
    booked = notifier.book.read()
    for slug in closed:
        if slug in booked or notifier.clear(slug):
            del notices[slug]
        else:
            notices[slug] = []
