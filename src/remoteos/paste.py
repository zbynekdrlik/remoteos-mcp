"""``Type(method="paste")`` (#30): put the text on the clipboard, press the paste keys, restore.

The opt-in escape hatch for the few apps that ignore synthesized Unicode
keystrokes (some games, RDP inside RDP). The default ``method="keys"`` types
without touching the clipboard. Shared by every OS: the caller passes its
provider's existing ``get_clipboard`` / ``set_clipboard`` (text only) and a
callable that presses the paste shortcut (Ctrl+V, Command+V on macOS).

Only TEXT can be saved and restored: a clipboard that held no text (empty, an
image, files) is left holding the pasted text, and the reply says so.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("remoteos.paste")

# The target app reads the clipboard when it handles the paste keys, not when they are
# sent; restoring sooner would paste the OLD content. A short settle covers that.
SETTLE_S = 0.3
_SET_OK = "Clipboard set"  # what every provider's set_clipboard() returns on success
_FAILED = "Error: "  # how every provider's get/set_clipboard() reports a failure (it does not raise)


def paste_text(
    text: str,
    *,
    get_clipboard: Callable[[], str],
    set_clipboard: Callable[[str], str],
    press_paste: Callable[[], object],
) -> str:
    """Paste ``text`` through the clipboard and put the previous clipboard text back.

    The providers' clipboard functions report a failure as an ``Error: …`` string
    instead of raising. A failed read means "no text to restore" (Windows: no
    CF_UNICODETEXT on the clipboard; Linux: no STRING target); a failed write of
    ``text`` raises before the paste keys are pressed.
    """
    saved = get_clipboard()
    restorable = not saved.startswith(_FAILED)
    if not restorable:
        log.info("paste: the clipboard holds no readable text (%s) — it will not be restored", saved)
    restored = None
    try:
        result = set_clipboard(text)
        if result != _SET_OK:
            raise RuntimeError(f"could not put the text on the clipboard: {result}")
        press_paste()
        time.sleep(SETTLE_S)
    finally:
        if restorable:
            restored = set_clipboard(saved)
    log.debug("paste: %d chars, clipboard restore: %s", len(text), restored)
    if not restorable:
        return f"Pasted {len(text)} chars (the clipboard held no text, so it was not restored)"
    if restored != _SET_OK:
        return f"Pasted {len(text)} chars, but restoring the clipboard failed: {restored}"
    return f"Pasted {len(text)} chars (clipboard restored)"
