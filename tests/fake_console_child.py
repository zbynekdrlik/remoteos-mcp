"""A console child at the OS boundary (#48): what ``subprocess`` hands back for the bytes a child writes.

- ``returned(kwargs, value)`` / ``run_result(kwargs, …)``: what ``subprocess`` returns for a call
  made with ``kwargs``. It returns text only for a text-mode call (``text``, ``encoding``,
  ``errors`` or ``universal_newlines``), and bytes otherwise. A ``str`` reply to a binary-mode
  call is encoded as UTF-8, which is what a UTF-8 child writes (a Linux tool, or a PowerShell
  child after its prefix). So a faked ``subprocess.run`` / ``Popen.communicate`` keeps the real
  return type, whichever mode the code under test uses.
"""

from __future__ import annotations

import types

_TEXT_MODE = ("text", "encoding", "errors", "universal_newlines")


def returned(kwargs: dict, value: str | bytes) -> str | bytes:
    """``value`` as subprocess returns it for a call made with ``kwargs``: text in text mode, else bytes."""
    if isinstance(value, str) and not any(kwargs.get(key) for key in _TEXT_MODE):
        return value.encode("utf-8")
    return value


def run_result(kwargs: dict, returncode: int = 0, stdout: str | bytes = "", stderr: str | bytes = ""):
    """The ``CompletedProcess`` a faked ``subprocess.run(…, **kwargs)`` returns."""
    return types.SimpleNamespace(
        returncode=returncode, stdout=returned(kwargs, stdout), stderr=returned(kwargs, stderr)
    )
