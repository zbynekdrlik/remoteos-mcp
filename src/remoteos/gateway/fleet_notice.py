"""Fleet owner notices (#102, owner ruling 2026-11-16): ONE footer-U item per condition KIND.

With an airuleset that has ``--owner-notice`` (``owner_notice.Notifier.per_condition``) the
machine conditions are not one U item per box (17 boxes down were 17 items) but one per kind,
each listing its boxes:

- ``fleet-down``     — every box that is not ``ok`` (``401`` / ``unreachable`` / ``error``), with its state,
                       grouped under its company (``org`` on its record, #105: Slovnormal apart from nlmedia)
- ``fleet-stale``    — every box behind the stable release (``drift`` on its record)
- ``fleet-no-admin`` — every box without admin rights (``no_admin`` on its record)
- ``enroll-pending`` — every registration waiting for ``enroll-accept`` (#101), from
                       ``status["enroll_pending"]`` (``[[id, hostname], ...]``, written by ``check``
                       from the queue), not from a machine record: the box is not in the key store yet

Each kind has a FINGERPRINT of what its text says (down: name + state + org; stale: name + version +
stable tag; no-admin: names), and its text holds nothing else that changes (no day counts). The
fingerprint last delivered is kept in the status file (``status["fleet_notices"] = {slug:
fingerprint}``), so it survives a run that had to send plain notices and a book that cannot be
written. Only a different BOX SET re-sends the SAME slug (airuleset replaces the U item): the same
boxes with another state / version / org, or a fingerprint in an older format after an upgrade, are
not news (#109, owner 2026-11-17: an acknowledged item must not come back for the same boxes); the
new fingerprint is then stored silently. An undelivered send keeps the old fingerprint, so it is
retried next run. A kind whose set is empty is not open: ``check._settle`` clears its slug, and any old
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
ENROLL = "enroll-pending"
STATUS_KEY = "fleet_notices"
PENDING_KEY = "enroll_pending"
ENROLL_HINT = ("Prijať: remoteos-gateway enroll-accept <id|hostname> --name win-… --org … --projects …"
               " · odmietnuť: remoteos-gateway enroll-reject <id>")

KINDS = (DOWN, STALE, NO_ADMIN, ENROLL)
_DETAIL_LIMIT = 80
ORG_TITLES = {"slovnormal": "Slovnormal", "nlmedia": "nlmedia"}
_NO_ORG = "bez organizácie"
_GROUP_ORDER = (ORG_TITLES["slovnormal"], ORG_TITLES["nlmedia"], _NO_ORG)


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


def _org_key(record: dict) -> str:
    """The record's org, ``""`` when none; an org this code does not know (a hand edit) counts as none."""
    org = record.get("org")
    return org if isinstance(org, str) and org in ORG_TITLES else ""


def _org(record: dict) -> str:
    """The record's company label, the header its line is grouped under."""
    return ORG_TITLES.get(_org_key(record), _NO_ORG)


def _down(machines: dict[str, dict], host: str) -> KindNotice | None:
    """ONE notice (the owner's one-item-per-kind ruling), its lines grouped under a company header
    (#105): Slovnormal first, then nlmedia, then boxes without an org."""
    names = sorted(
        (n for n, r in machines.items() if _is_down(r)), key=lambda n: (_GROUP_ORDER.index(_org(machines[n])), n)
    )
    if not names:
        return None
    tail = [KEY_HINT] if any(machines[n]["state"] == "401" for n in names) else []
    title = f"MCP brána ({host}): nefunkčné stroje ({len(names)})"
    fingerprint = [[n, machines[n]["state"], _org_key(machines[n])] for n in names]
    return KindNotice(DOWN, fingerprint, names, _fit_grouped(title, names, machines, tail))


def _down_line(name: str, record: dict) -> str:
    since = str(record.get("since") or "")[:16].replace("T", " ")
    line = f"• {name}: {STATE_LABELS.get(record['state'], record['state'])} (od {since})"
    if record["state"] != "unreachable":  # "All connection attempts failed" x17 says nothing
        line += " — " + str(record.get("detail") or "")[:_DETAIL_LIMIT]
    return line


def _fit_grouped(title: str, names: list[str], machines: dict[str, dict], tail: list[str],
                 limit: int = CHUNK_LIMIT) -> str:
    """Like ``fit``, for lines grouped under an org header: only BOX lines are counted and cut, a
    header is written only above a box it keeps, and the "… a ďalších N" line says which orgs the
    hidden boxes belong to."""
    for keep in range(len(names), -1, -1):
        lines: list[str] = []
        group = None
        for name in names[:keep]:
            if _org(machines[name]) != group:
                group = _org(machines[name])
                lines.append(f"{group}:")
            lines.append(_down_line(name, machines[name]))
        hidden = names[keep:]
        if hidden:
            counts = {label: sum(1 for n in hidden if _org(machines[n]) == label) for label in _GROUP_ORDER}
            per_org = ", ".join(f"{label} {count}" for label, count in counts.items() if count)
            lines.append(f"… a ďalších {len(hidden)} ({per_org})")
        text = "\n".join([title, *lines, *tail])
        if len(text) <= limit:
            return text
    return title[:limit]


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


def pending_of(status: dict) -> list[list[str]]:
    """The waiting registrations ``check`` wrote (``[[id, hostname], ...]``); anything else is none."""
    value = status.get(PENDING_KEY)
    if not isinstance(value, list):
        return []
    return [item for item in value
            if isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)]


def _enroll(pending: list[list[str]], host: str) -> KindNotice | None:
    """ONE item for every box waiting to be accepted (the owner says which machine it is)."""
    if not pending:
        return None
    lines = [f"• {hostname} (id {entry_id})" for entry_id, hostname in pending]
    title = f"Nový počítač čaká na prijatie do MCP brány ({host}): {len(pending)}"
    return KindNotice(ENROLL, [list(item) for item in pending], [entry_id for entry_id, _ in pending],
                      fit(title, lines, [ENROLL_HINT]))


def build_kind(slug: str, machines: dict[str, dict], host: str,
               pending: list[list[str]] | None = None) -> KindNotice | None:
    """The notice of kind *slug* in *machines* (a final status) and *pending* (the enrollment
    queue); None when nothing is in it."""
    if slug == ENROLL:
        return _enroll(pending or [], host)
    if slug == DOWN:
        return _down(machines, host)
    if slug == STALE:
        return _stale(machines, host)
    return _no_admin(machines, host)


def build(machines: dict[str, dict], host: str, pending: list[list[str]] | None = None) -> dict[str, KindNotice]:
    """The kind notices that are open in *machines* and *pending*, by slug."""
    made = (build_kind(slug, machines, host, pending) for slug in KINDS)
    return {notice.slug: notice for notice in made if notice is not None}


def open_slugs(machines: dict[str, dict], pending: list[list[str]] | None = None) -> set[str]:
    """The kind slugs open in *machines*: a kind with at least one box; ``enroll-pending`` when a
    registration waits."""
    open_now = {slug for slug, is_open in _OPEN.items() if any(is_open(r) for r in machines.values())}
    return open_now | ({ENROLL} if pending else set())


def delivered_before(previous: dict) -> dict[str, list]:
    """The fingerprints delivered by earlier runs; anything that is not what this code writes (a
    hand edit) is dropped, so that kind is simply sent again."""
    known = previous.get(STATUS_KEY)
    if not isinstance(known, dict):
        return {}
    return {slug: value for slug, value in known.items() if slug in KINDS and isinstance(value, list)}


def _box_set(fingerprint: object) -> frozenset[str] | None:
    """The box names of a delivered fingerprint, whatever its format (each item starts with the
    name since #102); None when it is not one (owed clear ``[]`` or a hand edit): sent again."""
    if not isinstance(fingerprint, list) or not fingerprint:
        return None
    names = [item[0] for item in fingerprint if isinstance(item, list) and item and isinstance(item[0], str)]
    return frozenset(names) if len(names) == len(fingerprint) else None


def _mark_notified(machines: dict[str, dict], notice: KindNotice) -> None:
    if notice.slug == ENROLL:  # registrations, not machine records
        return
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
    pending = pending_of(status)
    known = status.get(STATUS_KEY) if isinstance(status.get(STATUS_KEY), dict) else {}
    open_now = open_slugs(machines, pending)
    current: dict[str, list] = {slug: value for slug, value in known.items() if slug not in open_now}
    sent: bool | None = None
    for slug in KINDS:
        try:
            notice = build_kind(slug, machines, host, pending)
        except Exception:
            logger.exception("check: the %s notice could not be built — retried next run", slug)
            sent = False
            if slug in known:
                current[slug] = known[slug]
            continue
        if notice is None:
            continue
        if _box_set(known.get(slug)) == frozenset(notice.names):  # same boxes: not news (#109)
            if known.get(slug) != notice.fingerprint:
                logger.info("check: %s: same boxes, detail changed — not re-sent", slug)
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
    open_now = open_slugs(status["machines"], pending_of(status))
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
