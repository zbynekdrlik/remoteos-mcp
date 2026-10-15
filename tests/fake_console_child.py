"""A console child at the OS boundary (#48): what ``subprocess`` hands back for the bytes a child writes.

- ``returned(kwargs, value)`` / ``run_result(kwargs, …)``: what ``subprocess`` returns for a call
  made with ``kwargs``. It returns text only for a text-mode call (``text``, ``encoding``,
  ``errors`` or ``universal_newlines``), and bytes otherwise. A ``str`` reply to a binary-mode
  call is encoded as UTF-8, which is what a UTF-8 child writes (a Linux tool, or a PowerShell
  child after its prefix). So a faked ``subprocess.run`` / ``Popen.communicate`` keeps the real
  return type, whichever mode the code under test uses.
- ``FakeChild``: stands in for ``subprocess.Popen``, through which ``subprocess.run`` starts its
  child too. It records each call's argv and keyword arguments, then starts a REAL Python child
  that writes the given bytes. So subprocess's own text-mode or binary-mode handling (decoding,
  newline translation) runs for real; only the program is replaced. ``creationflags`` is
  recorded and then dropped, because POSIX rejects it.
- ``oem_code_page(page)``: Python's ``oem`` codec exists only on Windows, where it is
  MultiByteToWideChar(CP_OEMCP). This registers ``oem`` as ``page`` for the duration:
  - cp437 = resolume-pp (en_150);
  - cp852 = the Slovak fleet;
  - cp932 = a double-byte page, where a byte sequence can be undecodable.
- ``POWERSHELL_UTF8_PREFIX``: the line every PowerShell child runs before its script, as a
  literal (never imported from the code under test).
- ``TEXT_MODE``: the ``subprocess`` keyword arguments that make a call text mode.
"""

from __future__ import annotations

import codecs
import contextlib
import subprocess
import sys
import types
from typing import Callable, Union

# the subprocess keyword arguments that make a call text mode
TEXT_MODE = ("text", "encoding", "errors", "universal_newlines")

# UTF-8 without a BOM, for the child's console output and for what PowerShell pipes to native programs.
# In try/catch (review round 1): where the switch is refused (e.g. Constrained Language Mode), the
# command still runs, with no error text on every reply, and is read through the OEM fallback.
POWERSHELL_UTF8_PREFIX = (
    "try { $OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
    " [Console]::OutputEncoding = $OutputEncoding } catch { }"
)

# The real Popen, taken when this module is imported (at test collection), before any test patches it.
_REAL_POPEN = subprocess.Popen


def returned(kwargs: dict, value: str | bytes) -> str | bytes:
    """``value`` as subprocess returns it for a call made with ``kwargs``: text in text mode, else bytes."""
    if isinstance(value, str) and not any(kwargs.get(key) for key in TEXT_MODE):
        return value.encode("utf-8")
    return value


def run_result(kwargs: dict, returncode: int = 0, stdout: str | bytes = "", stderr: str | bytes = ""):
    """The ``CompletedProcess`` a faked ``subprocess.run(…, **kwargs)`` returns."""
    return types.SimpleNamespace(
        returncode=returncode, stdout=returned(kwargs, stdout), stderr=returned(kwargs, stderr)
    )


class FakeChild:
    """``subprocess.Popen`` replaced by a real Python child that writes ``stdout`` / ``stderr`` (bytes).

    ``oem`` is the code page the fake ``oem`` codec stands for (see ``oem_code_page``). Set it
    before the code under test first decodes: the codec registry caches a lookup.
    """

    def __init__(self) -> None:
        self.stdout = b""
        self.stderr = b""
        self.returncode = 0
        self.oem = "cp437"
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        kwargs.pop("creationflags", None)  # Windows only: POSIX raises ValueError
        script = (
            "import sys; "
            f"sys.stdout.buffer.write({self.stdout!r}); sys.stdout.flush(); "
            f"sys.stderr.buffer.write({self.stderr!r}); sys.stderr.flush(); "
            f"sys.exit({self.returncode})"
        )
        return _REAL_POPEN([sys.executable, "-c", script], **kwargs)


@contextlib.contextmanager
def oem_code_page(page: Union[str, Callable[[], str]]):
    """Register Python's Windows-only ``oem`` codec as ``page`` (a name, or a callable read at lookup)."""

    def _search(name: str):
        if name != "oem":
            return None
        return codecs.lookup(page() if callable(page) else page)

    codecs.register(_search)
    try:
        yield
    finally:
        codecs.unregister(_search)  # this also empties the codec cache, so "oem" is unknown again
