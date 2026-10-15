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
# "Žltý kôň" to "Zlty kôn", and cp852 has no "€" or "–".
# - Setting it to UTF-8 also switches the child's console (SetConsoleOutputCP), so a native
#   program that writes through the console's output page writes UTF-8 too. One that writes the
#   OEM page regardless: straight into our pipe, decode_output's per-line OEM fallback reads it;
#   CAPTURED by PowerShell (a pipeline, an assignment, | Out-String), PowerShell itself decodes it
#   as UTF-8 and its non-ASCII text is lost (review round 2; verified live only). The switched
#   page also governs grandchildren, e.g. how cmd.exe reads a .bat file.
# - $OutputEncoding is what PowerShell pipes INTO a native program.
# - UTF-8 without a BOM. New-Object works on every Windows PowerShell version ([…]::new needs 5.0).
# - try/catch (review round 1): where the switch is refused (e.g. Constrained Language Mode), the
#   command still runs, with no error text on every reply; its OEM output takes the fallback.
UTF8_PREFIX = (
    "try { $OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
    " [Console]::OutputEncoding = $OutputEncoding } catch { }"
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
      When the stream is not UTF-8, each LINE is read on its own: a UTF-8 line as UTF-8, any
      other line in the console's OEM page with ``errors="replace"``. Such lines come from:
      - output written before the prefix ran, e.g. PowerShell's own PARSE error on a localized
        Windows, which the host prints before any statement executes;
      - a native program started directly (tscon), which writes the OEM page;
      - a native program started from PowerShell that writes its own page into the same pipe.
      Line by line (review round 1), so one such line never turns PowerShell's UTF-8 lines into
      mojibake. The fallback is logged with ``stream``, the count of fallback lines and of
      replaced characters, never the content: WARNING when a character was lost, INFO when not.
      A known limit: a short OEM line that happens to be valid UTF-8 (cp852 ``Ďá`` = D2 A0)
      reads as UTF-8; real localized text practically never is.
    - **Elsewhere:** the locale's encoding, ``errors="replace"``. That is what a text-mode run
      decoded, so macOS/Linux output is unchanged.
    - **Newlines** as a text-mode run gave them: ``\\r\\n`` and a lone ``\\r`` become ``\\n``.
    """
    if not raw:
        return ""
    if not _on_windows(windows):
        text = raw.decode(locale.getpreferredencoding(False), "replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = _decode_lines_utf8_or_oem(raw, stream)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_lines_utf8_or_oem(raw: bytes, stream: str) -> str:
    """Each line as UTF-8, or else in the OEM page; logs the counts, never the text.

    A line ends at ``\\r\\n``, ``\\n`` or a lone ``\\r``, the breaks ``decode_output`` turns into
    ``\\n`` (review round 2: splitting at ``\\n`` only read a UTF-8 line and an OEM line separated
    by a lone ``\\r`` together as OEM). 0x0A and 0x0D are never part of a multi-byte character,
    neither in UTF-8 nor in a double-byte OEM page (cp932/936/949/950 trail bytes are >= 0x40), so
    splitting the bytes there never cuts one. Two encodings on the SAME line (``Write-Host
    -NoNewline`` and then a native program) are read together in the OEM page.
    """
    lines: list[str] = []
    fallback = replaced = 0
    for chunk in raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
        try:
            lines.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            line = chunk.decode("oem", "replace")
            fallback += 1
            replaced += line.count("\ufffd")
            lines.append(line)
    log.log(
        logging.WARNING if replaced else logging.INFO,
        "%s: %d line(s) not UTF-8, read in the OEM code page; %d undecodable character(s) replaced",
        stream,
        fallback,
        replaced,
    )
    return "\n".join(lines)


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
