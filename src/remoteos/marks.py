"""One numbered set of marks from accessibility elements and OCR words (#36). Pure: no OS, no I/O.

Custom-drawn apps (Ableton, Resolume, JUCE) expose almost nothing through accessibility, while
their labels are on the screen. So a word OCR read becomes a mark where no element covers it.
Everything here is in SCREEN coordinates; :mod:`remoteos.snapshot` maps a mark into the image.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, TypeVar

from remoteos.coords import Bbox

T = TypeVar("T")

MAX_MARKS = 300  # the bound of ElementTree too: a longer list is no use to read
ELEMENT, OCR = "element", "ocr"
OCR_ROLE = "text"

# A word belongs to an element when more than this part of its own area is inside the element.
_COVERED = 0.5
# An element this many times the word's area is a surface it is drawn on, not its control.
_SURFACE = 100
# Words join into a line when their middles are this many word heights apart at most...
_SAME_LINE = 0.5
# ...and the gap is at most this many heights: wider than a space, narrower than two buttons.
_WORD_GAP = 0.8
_ROW = 16  # tops this close to a row's first top are that row: OCR's 1 px jitter must not reorder it


@dataclass(frozen=True)
class Word:
    """One word OCR read, or a line of them, with its box in screen coordinates."""

    text: str
    rect: Bbox


@dataclass(frozen=True)
class Mark:
    """One numbered target. ``ref`` is the element's ElementTree ref, where the OS has refs."""

    number: int
    source: str
    role: str
    name: str
    rect: Bbox
    ref: Optional[str] = None


@dataclass(frozen=True)
class MarkSet:
    marks: tuple[Mark, ...]
    dropped: int  # marks beyond the limit


def _area(rect: Bbox) -> int:
    left, top, right, bottom = rect
    return max(0, right - left) * max(0, bottom - top)


def _overlap(a: Bbox, b: Bbox) -> int:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def covered(word: Word, rects: Iterable[Bbox]) -> bool:
    """Whether one element holds more than half of ``word``'s own area.

    A surface does not count: a custom-drawn app may show one focusable pane over all of its
    controls, and that element would otherwise swallow every label on it.
    """
    area = _area(word.rect)
    return any(
        _overlap(word.rect, rect) > _COVERED * area and _area(rect) <= _SURFACE * area
        for rect in rects
    )


def _height(rect: Bbox) -> int:
    return rect[3] - rect[1]


def _continues(line: Word, word: Word) -> bool:
    height = min(_height(line.rect), _height(word.rect))
    middle_gap = abs(line.rect[1] + line.rect[3] - word.rect[1] - word.rect[3]) / 2
    gap = word.rect[0] - line.rect[2]
    return middle_gap <= _SAME_LINE * height and -height <= gap <= _WORD_GAP * height


def _extended(line: Word, word: Word) -> Word:
    a, b = line.rect, word.rect
    return Word(f"{line.text} {word.text}", (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])))


def join_words(words: Iterable[Word]) -> list[Word]:
    """Words joined left to right into lines by proximity, whatever order the engine gave them in."""
    lines: list[Word] = []
    for word in sorted(words, key=lambda w: (w.rect[0], w.rect[1])):
        for index, line in enumerate(lines):
            if _continues(line, word):
                lines[index] = _extended(line, word)
                break
        else:
            lines.append(word)
    return lines


def _reading_order(items: Iterable[T], key: Callable[[T], tuple[Bbox, str]]) -> list[T]:
    """``items`` top-left to bottom-right, by rows; ``key`` gives an item's rectangle and name."""
    def _exact(item: T) -> tuple:
        rect, name = key(item)
        return rect[1], rect[0], rect[2], rect[3], name

    ordered, row_top, row = [], None, 0
    for item in sorted(items, key=_exact):
        top = key(item)[0][1]
        if row_top is None or top - row_top > _ROW:
            row, row_top = row + 1, top
        ordered.append((row, item))
    return [item for _, item in sorted(ordered, key=lambda pair: (pair[0], key(pair[1])[0][0], _exact(pair[1])))]


def _element_rect(element: dict) -> Bbox:
    r = element["rect"]
    return r["left"], r["top"], r["right"], r["bottom"]


def _unique(elements: Sequence[dict]) -> list[dict]:
    """The elements, each rectangle + name once: a control and its inner part can repeat each other."""
    seen, kept = set(), []
    for element in elements:
        key = (_element_rect(element), element["text"], element["class"])
        if key not in seen:
            seen.add(key)
            kept.append(element)
    return kept


def merge(elements: Sequence[dict], words: Sequence[Word], limit: int = MAX_MARKS) -> MarkSet:
    """The marks of ``elements`` (the providers' dicts) and ``words``, numbered in reading order.

    Elements come first in everything: a word an element covers adds nothing, and when the
    limit is reached the words are what goes. Numbers run top-left to bottom-right, so the
    same screen gets the same numbers.
    """
    limit = min(limit, MAX_MARKS)
    unique = _unique(elements)
    rects = [_element_rect(element) for element in unique]
    free = dict.fromkeys(word for word in words if _area(word.rect) and not covered(word, rects))
    lines = _reading_order(join_words(free), lambda w: (w.rect, w.text))
    kept_elements = unique[:limit]
    kept_lines = lines[: limit - len(kept_elements)]
    found = [(ELEMENT, e["class"], e["text"], _element_rect(e), e.get("ref")) for e in kept_elements]
    found += [(OCR, OCR_ROLE, w.text, w.rect, None) for w in kept_lines]
    ordered = _reading_order(found, lambda mark: (mark[3], mark[2]))
    marks = tuple(Mark(number, *fields) for number, fields in enumerate(ordered, start=1))
    return MarkSet(marks, len(unique) + len(lines) - len(marks))
