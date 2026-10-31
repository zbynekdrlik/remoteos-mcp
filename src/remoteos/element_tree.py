"""The UI element tree behind ElementTree / ClickElement / TypeElement (#35): the OS-free part.

A provider gives :func:`walk` a root and two callables that read the real accessibility
API; this module decides what is listed, bounds the walk and hands out refs. No OS calls,
so all of it runs on fake elements on any machine. ``snapshot.tree_lines`` renders the result.
"""

from __future__ import annotations

import itertools
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

log = logging.getLogger("remoteos.element_tree")

MAX_ELEMENTS = 300
MAX_DEPTH = 40
MAX_VISITED = 5000

# What a user can operate. Anything else counts only when it takes the keyboard focus.
INTERACTIVE_ROLES = frozenset(
    {
        "Button",
        "CheckBox",
        "ComboBox",
        "DataItem",
        "Edit",
        "HeaderItem",
        "Hyperlink",
        "ListItem",
        "MenuItem",
        "RadioButton",
        "Slider",
        "Spinner",
        "SplitButton",
        "TabItem",
        "TreeItem",
    }
)


@dataclass(frozen=True)
class ElementFacts:
    """What the walk needs to know about one element. ``rect`` = screen left, top, right, bottom."""

    role: str
    name: str
    enabled: bool
    rect: tuple[int, int, int, int]
    focusable: bool = False
    offscreen: bool = False


@dataclass(frozen=True)
class TreeItem:
    """One line of a tree: ``ref`` is what ClickElement / TypeElement take."""

    ref: str
    role: str
    name: str
    enabled: bool
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class ElementTree:
    title: str
    items: tuple[TreeItem, ...]
    truncated: str = ""  # why the list is incomplete, "" when it is complete


@dataclass(frozen=True)
class Walk:
    found: list[tuple[Any, ElementFacts]]
    truncated: str = ""


def listed(facts: ElementFacts) -> bool:
    """Interactive and visible; enabled or not, because a disabled button is worth seeing."""
    left, top, right, bottom = facts.rect
    if facts.offscreen or right <= left or bottom <= top:
        return False
    return facts.role in INTERACTIVE_ROLES or facts.focusable


def walk(
    root: Any,
    read: Callable[[Any], Optional[ElementFacts]],
    children: Callable[[Any], Iterable[Any]],
    *,
    limit: int = MAX_ELEMENTS,
    max_depth: int = MAX_DEPTH,
    max_visited: int = MAX_VISITED,
    expired: Callable[[], bool] = lambda: False,
) -> Walk:
    """Depth-first over ``root``'s descendants, stopping at every bound and saying which one.

    ``read`` returns ``None`` for an element that vanished under the walk: it is skipped.
    A page can hold far more elements than are worth reading, so the walk counts what it
    visits and asks ``expired`` too: a partial list with its reason beats a timeout.
    """
    found: list[tuple[Any, ElementFacts]] = []
    visited = 0
    clipped = False
    stack = [iter(children(root))]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        if visited >= max_visited:
            return Walk(found, f"the walk stopped after {max_visited} elements")
        if expired():
            return Walk(found, f"the walk ran out of time after {visited} elements")
        visited += 1
        facts = read(node)
        if facts is None:
            continue
        if listed(facts):
            if len(found) >= limit:
                return Walk(found, f"only the first {limit} elements are listed")
            found.append((node, facts))
        if len(stack) < max_depth:
            stack.append(iter(children(node)))
        elif next(iter(children(node)), None) is not None:
            clipped = True
    log.debug("element walk: %d visited, %d listed", visited, len(found))
    return Walk(found, f"elements deeper than {max_depth} levels are left out" if clipped else "")


_tree_numbers = itertools.count(1000 + secrets.randbelow(9000))


class StaleRef(LookupError):
    """A ref of an older tree, or of an element that is gone."""


class RefTable:
    """The refs of the LATEST tree. Publishing a tree ends the previous one.

    A ref is ``<tree>.<n>``, never a bare ``n``: numbering every tree from 1 would make a
    ref of an old tree name some other element of the new one. Tree numbers are unique in
    a process, also across restarted workers. The first one is random, so a ref from before
    a server restart almost never matches (a 1 in 9000 chance per restart).
    """

    def __init__(self) -> None:
        self._tree = 0
        self._entries: dict[str, Any] = {}

    def publish(self, payloads: Sequence[Any]) -> list[str]:
        """Start a new tree holding ``payloads``; returns their refs, in order."""
        self._tree = next(_tree_numbers)
        refs = [f"{self._tree}.{n}" for n in range(1, len(payloads) + 1)]
        self._entries = dict(zip(refs, payloads))
        return refs

    def lookup(self, ref: str) -> Any:
        try:
            return self._entries[ref]
        except KeyError:
            raise StaleRef(f"Element {ref} is not in the latest tree: call ElementTree again") from None
