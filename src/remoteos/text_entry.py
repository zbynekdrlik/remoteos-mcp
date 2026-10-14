"""The ``Type`` tool's text step (#30), shared by every provider's ``type_text``.

``method="keys"`` (the default) types Unicode keystrokes through the provider's
``type_unicode`` and replies with the number of characters delivered; a
shortfall is an error, never ``Typed N chars``. ``method="paste"`` is the opt-in
escape hatch for the few apps that ignore synthesized keystrokes (some games,
nested remote desktops): put the text on the clipboard, press the paste keys
(Ctrl+V, Command+V on macOS), then put the previous clipboard text back.

Paste caveats:

- Only TEXT is saved and restored, through the providers' existing
  ``get_clipboard`` / ``set_clipboard``. A clipboard that held no text (empty, an
  image, files) is left holding the pasted text, and the reply says so.
- Paste is not for secrets. Clipboard history, cloud sync and clipboard managers
  keep a copy even after the restore.
- Delivery is not confirmed: the reply means the paste keys were pressed.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from remoteos.typing_input import typed_reply

log = logging.getLogger("remoteos.text_entry")

# The target app reads the clipboard when it handles the paste keys, not when they are
# sent; restoring sooner would paste the OLD content. A short settle covers that.
SETTLE_S = 0.3
_SET_OK = "Clipboard set"  # what every provider's set_clipboard() returns on success
_FAILED = "Error: "  # how every provider's get/set_clipboard() reports a failure (it does not raise)


def enter_text(
    text: str,
    method: str,
    *,
    type_unicode: Callable[[str], int],
    get_clipboard: Callable[[], str],
    set_clipboard: Callable[[str], str],
    press_paste: Callable[[], object],
) -> str:
    """Type or paste ``text`` at the keyboard focus and return the tool reply.

    ``method`` is already validated (``typing_input.check_method``).
    """
    if method == "paste":
        return paste_text(text, get_clipboard=get_clipboard, set_clipboard=set_clipboard, press_paste=press_paste)
    delivered = type_unicode(text)
    log.debug("typed %d of %d chars", delivered, len(text))
    return typed_reply(delivered, len(text))


def paste_text(
    text: str,
    *,
    get_clipboard: Callable[[], str],
    set_clipboard: Callable[[str], str],
    press_paste: Callable[[], object],
) -> str:
    """Paste ``text`` through the clipboard and put the previous clipboard text back.

    The providers' clipboard functions report a failure as an ``Error: …`` string
    instead of raising. A failed or empty read means "no text to restore": Windows
    has no CF_UNICODETEXT, Linux no STRING target, and macOS ``pbpaste`` prints
    nothing for an empty clipboard and for an image alike. A failed write of
    ``text`` raises before the paste keys are pressed. The previous text is put
    back even when pressing the paste keys fails.
    """
    saved = get_clipboard()
    restorable = bool(saved) and not saved.startswith(_FAILED)
    if not restorable:  # never log what the clipboard holds
        log.info("paste: no clipboard text to restore (%s)", "empty" if not saved else "read failed")
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
