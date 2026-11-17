"""Boxes that sleep after hours (#118, owner 2026-11-17): reported only when they did not come up.

The pokladne close at 17-18:30 and are switched off overnight. A key-store machine with
``may_sleep`` (``machine set-sleep NAME on``) that is not ok is EXCUSED from the owner notice
unless it has not been ok at any moment since local midnight (Europe/Bratislava) AND the local time
is 10:00 or later; then it is reported like any other box. A box without the flag is never excused.

``check`` keeps, per machine record of the status file:

- ``last_ok``   — the stamp of the last run that saw the box ok (every box, flagged or not);
- ``may_sleep`` — ``true`` while the key store says so;
- ``asleep``    — ``true`` while the box is excused: ``fleet_notice`` leaves it out of ``fleet-down``
                  and ``check`` sends no per-box message for it. Its ``state`` stays the real one.

Waking up after an excused night is not news (the record is marked notified); a box that WAS
reported (not ok today at 10:00) announces its recovery like any other.

Local midnight is built from the LOCAL date (``datetime.combine`` with the zone), never as "now
minus N hours" or the UTC date, so it is right on the days the clocks change (Bratislava changes
at 02:00/03:00, so midnight itself always exists and is unambiguous).
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

LOCAL_ZONE = ZoneInfo("Europe/Bratislava")
REPORT_FROM = _dt.time(10, 0)  # a sleeping box not up today is reported from this local time on
LAST_OK = "last_ok"
MAY_SLEEP = "may_sleep"
ASLEEP = "asleep"


def local_midnight(moment: _dt.datetime) -> _dt.datetime:
    """The start of *moment*'s local day (Europe/Bratislava), as an aware datetime."""
    return _dt.datetime.combine(moment.astimezone(LOCAL_ZONE).date(), _dt.time(0), tzinfo=LOCAL_ZONE)


def _stamp(value: object) -> _dt.datetime | None:
    """A stamp ``check`` wrote (ISO 8601 with an offset); anything else (a hand edit) is None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def excused(record: dict, moment: _dt.datetime) -> bool:
    """Whether a may_sleep box's not-ok *record* stays out of the owner notice at *moment*: before
    10:00 local always, from 10:00 only if it was ok at some moment since local midnight."""
    if record.get("state") == "ok":
        return False
    if moment.astimezone(LOCAL_ZONE).time() < REPORT_FROM:
        return True
    last_ok = _stamp(record.get(LAST_OK))
    return last_ok is not None and last_ok >= local_midnight(moment)


def _carried_last_ok(previous: object, previous_checked_at: object) -> str | None:
    """The last-ok stamp a record keeps while the box is not ok. A record from before #118 has
    none: if it was ok, the run that wrote it (``checked_at``) is when it was last seen ok. The
    caller passes None for a previous run that FAILED (``_report_failure`` rewrote ``checked_at``
    over the older records)."""
    if not isinstance(previous, dict):
        return None
    if _stamp(previous.get(LAST_OK)) is not None:
        return previous[LAST_OK]
    if previous.get("state") == "ok" and _stamp(previous_checked_at) is not None:
        return str(previous_checked_at)
    return None


def apply(
    machines: dict[str, dict],
    old_machines: dict,
    may_sleep: dict[str, bool],
    moment: _dt.datetime,
    stamp: str,
    previous_checked_at: object,
) -> None:
    """Write ``last_ok`` / ``may_sleep`` / ``asleep`` into this run's *machines* records (built by
    ``check._advance`` from *old_machines*), and mark a wake-up after an excused night notified."""
    for name, record in machines.items():
        previous = old_machines.get(name)
        last_ok = stamp if record["state"] == "ok" else _carried_last_ok(previous, previous_checked_at)
        if last_ok is not None:
            record[LAST_OK] = last_ok
        # back after a night nobody was told about: not news. Keyed on the PREVIOUS record, so it
        # holds even when the flag was turned off meanwhile.
        was_silent = isinstance(previous, dict) and previous.get(ASLEEP) is True
        if record["state"] == "ok" and not record["notified"] and was_silent and previous.get("notified") is not True:
            record["notified"] = True
        if not may_sleep.get(name, False):
            continue
        record[MAY_SLEEP] = True
        if excused(record, moment):
            record[ASLEEP] = True
