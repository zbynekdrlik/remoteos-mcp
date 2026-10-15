"""Start a console child and decode its output: the one owner of both (#32, #48).

- ``run_command`` is the shared runner of the Windows, Linux and macOS helpers (#32 review
  round 1). It replaces their copies of the same ``_run``.
- ``powershell_argv`` is how every PowerShell child is started. Its prefix switches the child's
  OWN console to UTF-8 (#48).
- ``decode_output`` turns a child's raw bytes into the text it wrote: strict UTF-8 first, then
  the OEM page, on Windows; the locale elsewhere.
- ``console_child`` starts a NATIVE program in text mode, read in its console's OEM page (ping).
"""

from __future__ import annotations

import locale
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Windows: the child gets its own (hidden) console instead of sharing ours. subprocess has this
# constant only on Windows, hence the literal.
CREATE_NO_WINDOW = 0x08000000

# The first line of every PowerShell child (#48). PowerShell writes its output through
# [Console]::OutputEncoding, which is the console's OEM page: cp437 on resolume-pp best-fitted
# "Žltý kôň" to "Zlty kôn", and cp852 has no "€" or "–". Setting it to UTF-8 also switches the
# child's console, so native programs started from that PowerShell write UTF-8 too, and
# $OutputEncoding is what PowerShell pipes INTO a native program. UTF-8 without a BOM.
# New-Object works on every Windows PowerShell version ([…]::new needs 5.0).
UTF8_PREFIX = (
    "$OutputEncoding = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $OutputEncoding"
)


def _on_windows(windows: Optional[bool]) -> bool:
    """``windows=None`` means this machine."""
    return sys.platform == "win32" if windows is None else windows


def own_console(windows: Optional[bool] = None) -> dict:
    """``subprocess`` arguments that give a Windows child its OWN hidden console (#32 review round 5).

    - A child writes the CURRENT page of the console it is attached to. When every child
      shared the server's console, one ``chcp 65001`` in a Shell call garbled every later
      reply for every client (live on win-pz-server).
    - With its own console, a child can switch THAT console to UTF-8 (``UTF8_PREFIX``) and
      never touch the server's.
    - Elsewhere nothing: POSIX rejects ``creationflags``.

    No text-mode arguments: the child's bytes come back raw, for ``decode_output``.
    """
    return {"creationflags": CREATE_NO_WINDOW} if _on_windows(windows) else {}


def console_child(windows: Optional[bool] = None) -> dict:
    """``subprocess`` arguments for a NATIVE console program started directly, read in its console's page.

    Used for ping: its own localized text fits the system OEM page.
    - **Windows:** its own hidden console (``own_console``), at the system OEM page that
      ``encoding="oem"`` decodes (cp852 on the Slovak fleet). Python's default for a pipe,
      the ANSI page (cp1250), garbled "č" (#32 review round 4).
    - **Elsewhere:** the locale, and no creationflags.
    - **Always** ``errors="replace"``: an undecodable byte becomes U+FFFD and never loses the
      output. On Windows a decode error in subprocess's reader thread leaves stdout ``None``.

    A PowerShell child does not use this: it runs ``powershell_argv`` in binary mode, decoded by
    ``decode_output`` (#48).
    """
    if _on_windows(windows):
        return {"text": True, "encoding": "oem", "errors": "replace", **own_console(True)}
    return {"text": True, "encoding": None, "errors": "replace"}


def powershell_argv(script: str) -> list[str]:
    """The argv of a PowerShell child running ``script``, after ``UTF8_PREFIX`` on its own line (#48).

    The script follows verbatim on the next line. So a script that must begin with ``param(...)``
    or a ``using`` statement cannot run this way: PowerShell requires those first.
    """
    return ["powershell", "-NoProfile", "-Command", UTF8_PREFIX + "\n" + script]


def decode_output(raw: Optional[bytes], windows: Optional[bool] = None, stream: str = "output") -> str:
    """The text of a child's raw ``stdout`` / ``stderr`` bytes (#48).

    - **Windows:** strict UTF-8, which every PowerShell child writes after ``UTF8_PREFIX``.
      Bytes that are not UTF-8 are read in the console's OEM page, ``errors="replace"``:
      - output written before the prefix ran, e.g. PowerShell's own PARSE error on a localized
        Windows, which the host prints before any statement executes;
      - a native program started directly (tscon), which writes the OEM page.
      That fallback is logged with ``stream`` and the count of replaced characters, never the
      content: WARNING when a character was lost, INFO when nothing was.
    - **Elsewhere:** the locale's encoding, ``errors="replace"``. That is what a text-mode run
      decoded, so macOS/Linux output is unchanged.
    - **Newlines** as a text-mode run gave them: ``\\r\\n`` and a lone ``\\r`` become ``\\n``.
    """
    if not raw:
        return ""
    if _on_windows(windows):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("oem", "replace")
            replaced = text.count("�")
            log.log(
                logging.WARNING if replaced else logging.INFO,
                "%s is not UTF-8: read in the OEM code page, %d undecodable character(s) replaced",
                stream,
                replaced,
            )
    else:
        text = raw.decode(locale.getpreferredencoding(False), "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class ChildOutput:
    """A finished child: its exit code and its decoded ``stdout`` / ``stderr``."""

    returncode: int
    stdout: str
    stderr: str


def run_child(cmd: list[str], timeout: int, windows: Optional[bool] = None) -> ChildOutput:
    """Run ``cmd`` in binary mode in its own console and decode both streams with ``decode_output``."""
    windows = _on_windows(windows)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, **own_console(windows))
    return ChildOutput(
        result.returncode,
        decode_output(result.stdout, windows, "stdout"),
        decode_output(result.stderr, windows, "stderr"),
    )


def run_command(cmd: list[str], timeout: int = 30, check: bool = False, label: Optional[str] = None) -> str:
    """Run ``cmd`` and return stdout, then ``[STDERR] …`` and ``[Exit code: N]`` when there are any.

    ``check=True`` is for a command that CHANGES state (start, stop, create,
    delete, write): a non-zero exit means it failed, so it raises
    ``RuntimeError("<label> failed (exit N): <stderr>")``, which the tool reports
    as an MCP error. Without it, the exit code is part of the answer, the way a
    listing reports it (e.g. Get-WinEvent's "No events were found").

    A PowerShell command is ``powershell_argv(script)``; ``run_child`` decodes the output.
    """
    result = run_child(cmd, timeout)
    if check and result.returncode != 0:
        reason = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise RuntimeError(f"{label or ' '.join(cmd[:2])} failed (exit {result.returncode}): {reason}")
    output = result.stdout
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"
