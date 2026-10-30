"""Boxes without admin rights, for ``remoteos-gateway check`` (#57).

A server that runs without admin looks healthy until a project trips on it (fohabl, twice). While a
box's ``/health`` says ``admin: false``, its status record holds::

    "no_admin": {"since": "<when it was first seen>", "notified": true}

It is announced ONCE, by name, like version drift: an undelivered announcement stays
``notified: false`` and is retried, and the episode ends only when the box reports ``admin: true``.
Only a value read in THIS run is judged: a box that does not answer, or a server older than #57
(no ``admin`` key), keeps what it had.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from remoteos.gateway.messages import split_to_fit

logger = logging.getLogger("remoteos.gateway.no_admin")


def _known(previous: object) -> dict | None:
    record = previous.get("no_admin") if isinstance(previous, dict) else None
    if isinstance(record, dict) and isinstance(record.get("since"), str):
        return {"since": record["since"], "notified": bool(record.get("notified"))}
    return None


def judge(record: dict, previous: object, admin: bool | None, stamp: str) -> None:
    """Set ``record["no_admin"]`` for one machine, or leave it out (it has admin)."""
    known = _known(previous)
    if admin is True:
        return
    if admin is False:
        record["no_admin"] = known or {"since": stamp, "notified": False}
    elif known is not None:
        record["no_admin"] = known


def compose_messages(pending: dict[str, dict], host: str) -> list[tuple[str, list[str]]]:
    """The owner's texts (Slovak, phone-readable), as ``(text, the names it announces)``."""
    title = f"MCP brána ({host}): stroje bez admin práv"
    hint = "Oprava: spusti na stroji inštalátor (remoteos-install), server má bežať ako admin."
    reserve = len(" (99/99)")

    def lines(names: list[str]) -> list[str]:
        return [f"• {name}: server nemá admin práva" for name in names]

    groups = split_to_fit(pending, lambda names: len("\n".join([title, *lines(names), hint])) + reserve)
    texts = []
    for index, names in enumerate(groups, start=1):
        head = title + (f" ({index}/{len(groups)})" if len(groups) > 1 else "")
        texts.append(("\n".join([head, *lines(names), hint]), names))
    return texts


def announce(machines: dict[str, dict], host: str, notifier: Callable[[str], bool] | None) -> bool | None:
    """Send what is owed. Returns None when nothing was, else whether all of it went out."""
    pending = {n: r for n, r in machines.items() if "no_admin" in r and not r["no_admin"]["notified"]}
    if not pending:
        return None
    sent = True
    for text, names in compose_messages(pending, host):
        logger.warning("check: %s", text.replace("\n", " "))
        delivered = notifier(text) if notifier is not None else True
        sent = sent and delivered
        if delivered:
            for name in names:
                pending[name]["no_admin"]["notified"] = True
    return sent


def update(
    machines: dict[str, dict],
    previous: dict,
    admins: dict[str, bool | None],
    stamp: str,
    host: str,
    notifier: Callable[[str], bool] | None,
) -> bool | None:
    """Judge and report one run. Like drift, nothing here may cost the run its status write."""
    for name, record in machines.items():
        judge(record, previous.get(name), admins.get(name), stamp)
    try:
        return announce(machines, host, notifier)
    except Exception:
        logger.exception("check: the no-admin report failed — it stays owed")
        return None
