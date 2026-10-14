"""Unicode typing (#30): text → keyboard strokes, the same plan on every OS.

The ``Type`` tool types any text — Slovak diacritics, symbols, emoji — as
Unicode keystrokes, independent of the keyboard layout. ``plan`` splits the text
into strokes: a Unicode character (its UTF-16 code units; a character outside the
BMP is a surrogate pair) or a special key — ``\\n``, ``\\r\\n`` and a lone ``\\r``
are one Enter each, ``\\t`` is Tab. Windows sends one stroke per character
(``SendInput`` + ``KEYEVENTF_UNICODE``, one down/up per code unit); macOS merges
consecutive characters into runs of at most 20 UTF-16 units (Quartz keyboard
events carrying a Unicode string). Every stroke keeps the source text it stands
for, so the plan never drops a character and the reply can count what was really
delivered (``typed_reply``).

Pure: no I/O, no OS imports.
"""

from __future__ import annotations

from dataclasses import dataclass

ENTER, TAB = "enter", "tab"
METHODS = ("keys", "paste")
_SPECIAL_KEYS = {"\n": ENTER, "\r": ENTER, "\t": TAB}


@dataclass(frozen=True)
class Stroke:
    """One step of typing: ``source`` is the text it types.

    ``key`` is ``"enter"`` / ``"tab"`` for a special key (``source`` is then its
    line break or tab), ``None`` for Unicode text.
    """

    source: str
    key: str | None = None

    @property
    def units(self) -> tuple[int, ...]:
        """The UTF-16 code units of a Unicode stroke (empty for a special key)."""
        return () if self.key is not None else utf16_units(self.source)

    @property
    def chars(self) -> int:
        """How many characters of the typed text this stroke delivers."""
        return len(self.source)


def utf16_units(text: str) -> tuple[int, ...]:
    """``text`` as UTF-16 code units (a character outside the BMP → a surrogate pair)."""
    data = text.encode("utf-16-le")
    return tuple(int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2))


def plan(text: str, max_units: int = 1) -> list[Stroke]:
    """Split ``text`` into strokes.

    Consecutive Unicode characters merge into one stroke while it stays within
    ``max_units`` UTF-16 units. A character is never split, so a surrogate pair is
    always whole, and the default (1) gives one stroke per character. ``\\r\\n`` is a
    single Enter stroke.
    """
    strokes: list[Stroke] = []
    run, run_units = "", 0
    i = 0
    while i < len(text):
        char = text[i]
        key = _SPECIAL_KEYS.get(char)
        if key is not None:
            if run:
                strokes.append(Stroke(run))
                run, run_units = "", 0
            source = "\r\n" if text.startswith("\r\n", i) else char
            strokes.append(Stroke(source, key))
            i += len(source)
            continue
        units = len(utf16_units(char))
        if run and run_units + units > max_units:
            strokes.append(Stroke(run))
            run, run_units = "", 0
        run += char
        run_units += units
        i += 1
    if run:
        strokes.append(Stroke(run))
    return strokes


def check_method(method: str) -> str:
    """The ``Type`` tool's ``method``: ``keys`` (the default, also for ``""``) or ``paste``."""
    name = str(method).strip().lower() or "keys"
    if name not in METHODS:
        raise ValueError(f"method must be 'keys' or 'paste' (got {method!r})")
    return name


def typed_reply(delivered: int, total: int) -> str:
    """The ``Type`` reply for ``delivered`` of ``total`` characters; anything short is an error."""
    if delivered != total:
        raise RuntimeError(f"delivered {delivered} of {total} chars")
    return f"Typed {delivered} chars"
