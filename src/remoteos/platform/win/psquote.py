"""Put a value into PowerShell source as a literal that PowerShell never expands (#39).

Every value a tool caller controls (a service name, an app path, a log name) that ends up in the
text of a PowerShell script goes through ``ps_literal``. A double-quoted PowerShell string is
EXPANDABLE: ``"$(Set-Content x pwned)"`` runs the command inside it. A single-quoted string is
taken verbatim; its only special character is the quote itself, written twice.

PowerShell's tokenizer treats the typographic single quotes U+2018, U+2019, U+201A and U+201B
exactly like ``'``: any of them ends a single-quoted string, and two in a row (any mix) stand for
one literal quote. So each of them is doubled too, or ``’; calc; ’`` would break out.

Free text that may span lines (the toast title and message) does not go into the script at all:
it travels in an environment variable that the script reads (``$env:NAME``), see
``platform/win/desktop.show_notification``.
"""

from __future__ import annotations

# ' and the typographic single quotes PowerShell treats as the same delimiter (#39)
_SINGLE_QUOTES = ("'", "‘", "’", "‚", "‛")


def ps_literal(value: str) -> str:
    """``value`` as a single-quoted PowerShell string literal: ``'...'`` with every quote doubled."""
    text = str(value)
    for quote in _SINGLE_QUOTES:
        text = text.replace(quote, quote * 2)
    return f"'{text}'"
