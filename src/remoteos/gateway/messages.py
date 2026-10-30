"""Owner messages: how much fits in one (#51, #63, #66).

airuleset cuts mention + body at 1900 characters and STILL prints "sent", so one message stays well
under that: a fleet-wide text goes out in several, each fully delivered or retried.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

CHUNK_LIMIT = 1700

Item = TypeVar("Item")


def split_to_fit(
    items: Iterable[Item], size: Callable[[list[Item]], int], limit: int = CHUNK_LIMIT
) -> list[list[Item]]:
    """Group *items* in order so ``size(group)`` stays within *limit*. Never returns an empty
    list: no items is one empty group."""
    groups: list[list[Item]] = [[]]
    for item in items:
        if groups[-1] and size(groups[-1] + [item]) > limit:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups
