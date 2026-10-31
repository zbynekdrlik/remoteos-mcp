"""The UI element tree behind ElementTree / ClickElement / TypeElement (#35): the OS-free part.

A provider gives :func:`walk` a root and two callables that read the real accessibility
API; this module decides what is listed, bounds the walk and hands out refs. No OS calls,
so all of it runs on fake elements on any machine. ``snapshot.tree_lines`` renders the result.
"""

from __future__ import annotations

import itertools
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

log = logging.getLogger("remoteos.element_tree")

MAX_ELEMENTS = 300
MAX_DEPTH = 40
MAX_VISITED = 5000
MAX_NAME = 80
TREE_WALK_S = 10.0  # a big tree ends as a partial list, not as a timeout
SNAPSHOT_WALK_S = 3.0  # Snapshot's element list is a side dish: it must not hold up the image

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
    hint: str = ""  # what would make an app that shows nothing show its controls


@dataclass(frozen=True)
class Walk:
    found: list[tuple[Any, ElementFacts]]
    truncated: str = ""


def one_line(name: str) -> str:
    """An element's name on one short line: apps put whole documents in it, and a reply line is ours."""
    text = " ".join(name.split())
    return text[: MAX_NAME - 1] + "…" if len(text) > MAX_NAME else text


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


def expires_in(seconds: float) -> Callable[[], bool]:
    """A walk's ``expired``: true from ``seconds`` from now."""
    deadline = time.monotonic() + seconds
    return lambda: time.monotonic() >= deadline


def element_dict(index: int, facts: ElementFacts) -> dict:
    """``facts`` as the element dict every provider's Snapshot list uses."""
    return {
        "index": index,
        "class": facts.role,
        "text": facts.name,
        "rect": dict(zip(("left", "top", "right", "bottom"), facts.rect)),
    }


def listed_elements(result: Walk) -> list[dict]:
    """Snapshot's list: publishes no refs, so reading it ends none."""
    return [element_dict(index, facts) for index, (_, facts) in enumerate(result.found, start=1)]


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


# ``entry_of(node, facts)`` makes what a ref keeps, or ``None`` when the element cannot have a ref.
EntryOf = Callable[[Any, ElementFacts], Any]


def _published(refs: RefTable, result: Walk, entry_of: EntryOf) -> list[tuple[str, ElementFacts]]:
    """Start a new ref tree of the found elements that can have a ref: ``(ref, facts)``."""
    entries, kept = [], []
    for node, facts in result.found:
        entry = entry_of(node, facts)
        if entry is not None:
            entries.append(entry)
            kept.append(facts)
    return list(zip(refs.publish(entries), kept))


def tree_from(refs: RefTable, title: str, result: Walk, entry_of: EntryOf) -> ElementTree:
    """ElementTree's reply data, with new refs."""
    items = tuple(TreeItem(r, f.role, f.name, f.enabled, f.rect) for r, f in _published(refs, result, entry_of))
    return ElementTree(title=title, items=items, truncated=result.truncated)


def marked_from(refs: RefTable, result: Walk, entry_of: EntryOf) -> tuple[list[dict], str]:
    """AnnotatedSnapshot's list (#36) and why it is cut: refs in a new tree, so a mark can be acted on."""
    ref_of = {id(facts): ref for ref, facts in _published(refs, result, entry_of)}
    found = enumerate((facts for _, facts in result.found), start=1)
    return [{**element_dict(index, facts), "ref": ref_of.get(id(facts))} for index, facts in found], result.truncated


def gone(ref: str, name: str) -> StaleRef:
    return StaleRef(f"Element {ref} ({name!r}) is gone: call ElementTree again")


def may_have_happened(ref: str, name: str) -> RuntimeError:
    """An action that neither worked nor was plainly refused: repeating it blindly could do it twice."""
    return RuntimeError(
        f"The app did not answer the action on element {ref} ({name!r}). It may still have happened (a dialog it"
        " opened can block the call, a button can close its own window): look before repeating it, then call"
        " ElementTree again"
    )
