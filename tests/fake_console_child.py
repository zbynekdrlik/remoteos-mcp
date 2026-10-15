"""A console child at the OS boundary (#48): what ``subprocess`` hands back for the bytes a child writes.

- ``returned(kwargs, value)`` / ``run_result(kwargs, …)``: what ``subprocess`` returns for a call
  made with ``kwargs``. It returns text only for a text-mode call (``text``, ``encoding``,
  ``errors`` or ``universal_newlines``), and bytes otherwise. A ``str`` reply to a binary-mode
  call is encoded as UTF-8, which is what a UTF-8 child writes (a Linux tool, or a PowerShell
  child in its UTF-8 console). So a faked ``subprocess.run`` / ``Popen.communicate`` keeps the real
  return type, whichever mode the code under test uses.
- ``popen_answered_by(run)``: ``subprocess.Popen`` answered by such a ``run``-style fake. The
  boundary for every child started through ``platform.command.run_child`` (which uses ``Popen``)
  and through the real ``subprocess.run`` (which starts its child with ``Popen`` too).
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
- ``assert_runs_powershell(argv, python, script)``: ``argv`` runs ``script`` unchanged, as
  ``powershell -NoProfile -Command`` did before #48, under the console trampoline
  ``<python.exe> -I -S -c <program>`` (#48 round 2). The program is checked in its own tests.
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

# The real Popen and interpreter, taken when this module is imported (at test collection), before
# any test patches them (the windows_python fixture points sys.executable at a fake install).
_REAL_POPEN = subprocess.Popen
_REAL_PYTHON = sys.executable


def assert_runs_powershell(argv: list[str], python, script: str, cwd: str = "") -> None:
    """``argv`` runs ``script`` unchanged under the console trampoline of ``python`` (#48 round 2).

    ``[<python.exe>, "-I", "-S", "-X", "utf8", "-c", <program>, cwd, "powershell", "-NoProfile",
    "-Command", script]``: ``cwd`` is PowerShell's working directory ("" = the server's), and the
    PowerShell part is exactly the argv a PowerShell child had before #48, with nothing added.
    ``-X utf8`` (review round 2): the trampoline's own messages reach the pipe as UTF-8.
    """
    assert argv[:6] == [str(python), "-I", "-S", "-X", "utf8", "-c"], argv[:6]
    assert argv[7] == cwd, argv[7]
    assert argv[8:] == ["powershell", "-NoProfile", "-Command", script], argv[8:]


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


class AnsweredChild:
    """A finished child, as ``subprocess.Popen`` returns it, holding the answer of a ``run``-style fake."""

    def __init__(self, args, answer) -> None:
        self.args = args
        self.pid = 4242
        self.returncode = answer.returncode
        self._output = (answer.stdout, answer.stderr)

    def communicate(self, input=None, timeout=None):
        return self._output

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


def popen_answered_by(run: Callable) -> Callable:
    """``subprocess.Popen`` whose children are answered by ``run(argv, **popen_kwargs)``, a ``run``-style fake.

    ``platform.command.run_child`` starts its child through ``Popen`` (#48 round 2: a timeout kills
    the child's whole process tree), and the real ``subprocess.run`` starts its child through
    ``Popen`` too, so a fake at this boundary answers both. ``run`` returns a ``run_result`` or raises
    (e.g. ``OSError`` for a program that cannot be started).
    """

    def _popen(args, **kwargs):
        return AnsweredChild(list(args), run(args, **kwargs))

    return _popen


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
        return _REAL_POPEN([_REAL_PYTHON, "-c", script], **kwargs)


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
