"""Which window a title means: the one matching rule of FocusWindow and ElementTree on every OS."""

from __future__ import annotations

from typing import Iterable, TypeVar

from thefuzz import fuzz

W = TypeVar("W")  # a provider's WindowInfo: anything with a ``title``

_MIN_SCORE = 50


def best_window(title: str, windows: Iterable[W]) -> W:
    """The window whose title best matches part of ``title``; raises when none does."""
    best_score, best = 0, None
    for window in windows:
        score = fuzz.partial_ratio(title.lower(), window.title.lower())
        if score > best_score:
            best_score, best = score, window
    if best is None or best_score < _MIN_SCORE:
        raise LookupError(f"No window matching '{title}' (best score {best_score})")
    return best
