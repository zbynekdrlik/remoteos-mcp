"""Start a console child and decode its output: the one owner of both (#32, #48).

- ``run_command`` is the shared runner of the Windows, Linux and macOS helpers (#32 review
  round 1). It replaces their copies of the same ``_run``.
- ``powershell_argv`` is how every PowerShell child is started: under a console trampoline that
  switches the child's OWN console to UTF-8 and then runs the script unchanged (#48 round 2).
- ``run_child`` starts a child, decodes it, and kills its whole tree on a timeout.
- ``decode_output`` turns a child's raw bytes into the text it wrote: strict UTF-8 first, then
  the OEM page, on Windows; the locale elsewhere.
- ``console_child`` starts a NATIVE program in text mode, read in its console's OEM page (ping).
"""

from __future__ import annotations

import locale
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import psutil

log = logging.getLogger(__name__)

# Windows: the child gets its own (hidden) console instead of sharing ours. subprocess has this
# constant only on Windows, hence the literal.
CREATE_NO_WINDOW = 0x08000000

# The console trampoline of every PowerShell child (#48 round 2): `python.exe <_TRAMPOLINE_FLAGS> <this> <cwd> <argv…>`.
# PowerShell 5.1 writes through [Console]::OutputEncoding, which it takes from its console's output
# page at start-up: the OEM page, where cp437 (resolume-pp) best-fitted "Žltý kôň" to "Zlty kôn"
# and cp852 has no "€" or "–". Started in a console that is ALREADY 65001, it writes UTF-8 with no
# BOM (live on resolume-pp: 'Ž€' -> C5 BD E2 82 AC 0D 0A), and so do the native programs it starts.
# - This program runs in the child's OWN hidden console (own_console): it switches that console
#   (output and input page) to UTF-8, runs the real argv in the same console with its own stdout
#   and stderr handed down, and exits with the child's exact exit code (all 32 bits; sys.exit takes
#   a C long, so a code >= 2**31 is passed as the same bits, signed).
# - It runs in the SERVER's directory and starts PowerShell in <cwd> ("" = the server's), because
#   CreateProcess looks for "powershell" in the PARENT's current directory first: a powershell.exe
#   in the user's Shell cwd is never found, as before #48 when the server was the parent (review
#   round 1).
# - No PowerShell code is ours: the script reaches PowerShell exactly as before #48, so its error
#   records, exit codes, param() and Constrained Language Mode are plain PowerShell. Round 1's
#   prefix was PowerShell code, and 5.1 printed it in every Write-Error record.
# - Accepted cost: PowerShell's $OutputEncoding, what it pipes INTO a native program's stdin, is
#   back to its default, ASCII (non-ASCII becomes "?"), as before #48.
# - A console that refuses a page (never seen) still runs the command and says which page on
#   stderr; refused output is read by decode_output's OEM fallback. PowerShell that cannot be
#   started (no powershell.exe, a vanished cwd) is one "remoteos: could not start" line and exit 1.
# - Fixed text: never any user data (that is the argv after it). -I -S: isolated, no site import.
#   -X utf8 (review round 2): its own messages reach the pipe as UTF-8, a localized OSError included
#   (the pipe would otherwise get the ANSI page, which the OEM fallback misreads).
UTF8_CONSOLE_TRAMPOLINE = "\n".join(
    [
        "import ctypes, subprocess, sys",
        "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)",
        "for name, switch in (('output', kernel32.SetConsoleOutputCP), ('input', kernel32.SetConsoleCP)):",
        "    if not switch(65001):",
        "        sys.stderr.write('remoteos: the console %s stays in the OEM code page (error %d)\\n'"
        " % (name, ctypes.get_last_error()))",
        "try:",
        "    code = subprocess.call(sys.argv[2:], cwd=sys.argv[1] or None, stdout=1, stderr=2)",
        "except OSError as e:",
        "    sys.stderr.write('remoteos: could not start %s: %s\\n' % (sys.argv[2], e))",
        "    code = 1",
        "sys.exit(code - 2**32 if code >= 2**31 else code)",
    ]
)


def _on_windows(windows: Optional[bool]) -> bool:
    """``windows=None`` means this machine."""
    return sys.platform == "win32" if windows is None else windows


def own_console(windows: Optional[bool] = None) -> dict:
    """``subprocess`` arguments that give a Windows child its OWN hidden console (#32 review round 5).

    - A child writes the CURRENT page of the console it is attached to. When every child
      shared the server's console, one ``chcp 65001`` in a Shell call garbled every later
      reply for every client (live on win-pz-server).
    - With its own console, a child can switch THAT console to UTF-8 (the console trampoline,
      ``powershell_argv``) and never touch the server's.
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
    ``decode_output`` (#48). Neither does tscon, which ``run_command`` reads the same way.
    """
    if _on_windows(windows):
        return {"text": True, "encoding": "oem", "errors": "replace", **own_console(True)}
    return {"text": True, "encoding": None, "errors": "replace"}


# The interpreter flags of the trampoline, up to its program: isolated, no site, UTF-8 stdio.
_TRAMPOLINE_FLAGS = ["-I", "-S", "-X", "utf8", "-c"]


def _console_python() -> str:
    """The console-subsystem ``python.exe`` next to the server's interpreter (#48 round 2).

    Never ``pythonw.exe``, which the server may itself run as: a GUI-subsystem program gets no
    console from ``CREATE_NO_WINDOW``, so it would have no console to switch. A missing
    ``python.exe`` is an error, never a silent fallback to the OEM page.
    """
    path = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.isfile(path):
        raise RuntimeError(f"no python.exe next to {sys.executable}: a PowerShell child needs it for its UTF-8 console")
    return path


def powershell_argv(script: str, cwd: Optional[str] = None) -> list[str]:
    """The argv of a PowerShell child running ``script`` in a UTF-8 console (#48 round 2).

    ``[python.exe, "-I", "-S", "-X", "utf8", "-c", UTF8_CONSOLE_TRAMPOLINE, cwd or "", "powershell",
    "-NoProfile", "-Command", script]``: the trampoline switches the child's own console to UTF-8,
    then runs the PowerShell argv, which is exactly the one a PowerShell child had before #48, the
    script unchanged. ``subprocess`` quotes it with ``list2cmdline`` and the trampoline's own
    ``subprocess.call`` quotes it the same way, so PowerShell gets the same command line as before.

    ``cwd`` is PowerShell's working directory. Pass it HERE, never to ``run_child``: the
    trampoline must run in the server's directory, which is where "powershell" is looked up.
    """
    trampoline = [_console_python(), *_TRAMPOLINE_FLAGS, UTF8_CONSOLE_TRAMPOLINE, cwd or ""]
    return [*trampoline, "powershell", "-NoProfile", "-Command", script]


def _shown(cmd: list[str]) -> list[str]:
    """``cmd`` as the user knows it: a trampolined PowerShell child is its PowerShell argv (review round 1)."""
    head = [*_TRAMPOLINE_FLAGS, UTF8_CONSOLE_TRAMPOLINE]
    if len(cmd) > len(head) + 2 and cmd[1 : len(head) + 1] == head:
        return cmd[len(head) + 2 :]
    return cmd


def decode_output(raw: Optional[bytes], windows: Optional[bool] = None, stream: str = "output") -> str:
    """The text of a child's raw ``stdout`` / ``stderr`` bytes (#48).

    - **Windows:** strict UTF-8, which every PowerShell child writes in its UTF-8 console. A
      leading UTF-8 BOM is dropped (never seen live; defensive). When the stream is not UTF-8,
      each LINE is read on its own: a UTF-8 line as UTF-8, any other line in the console's OEM
      page with ``errors="replace"``. Such lines come from:
      - a native program started directly (tscon), which writes the OEM page;
      - a native program started from PowerShell that writes its own page into the same pipe;
      - a console that refused the switch to UTF-8 (the trampoline says so on stderr).
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
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
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

    def reply(self) -> str:
        """The tool reply: stdout, then ``[STDERR] …`` and ``[Exit code: N]`` when there are any."""
        output = self.stdout
        if self.stderr:
            output += f"\n[STDERR] {self.stderr}"
        if self.returncode != 0:
            output += f"\n[Exit code: {self.returncode}]"
        return output.strip() or "(no output)"


# A POSIX process group gets this long between SIGTERM and SIGKILL (review round 1).
KILL_GRACE_S = 2.0


def _tree(pid: int) -> list[psutil.Process]:
    """The process ``pid`` and its descendants now (empty when unreadable); psutil detects pid reuse."""
    try:
        leader = psutil.Process(pid)
        return [leader, *leader.children(recursive=True)]
    except psutil.Error:
        return []


def _running(process: psutil.Process) -> bool:
    """Not exited: gone (psutil also reports a reused pid as gone) and a zombie both count as exited."""
    try:
        return process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _wait_exited(processes: list[psutil.Process], seconds: float) -> None:
    """Wait at most ``seconds`` until every one of ``processes`` exited. Reaps nothing (review round 2)."""
    deadline = time.monotonic() + seconds
    while any(_running(p) for p in processes) and time.monotonic() < deadline:
        time.sleep(0.05)


def _signal_group(pgid: int, sig: int) -> None:
    """``killpg``; ESRCH (no such group: the child leads none, or it is gone) is no error."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except Exception as e:
        log.debug("killpg of %s failed (%s)", pgid, type(e).__name__)


def _reap(proc: subprocess.Popen, seconds: float) -> bool:
    """Wait at most ``seconds`` for the child's pipes and exit; never raises."""
    try:
        proc.communicate(timeout=seconds)
        return True
    except Exception as e:
        # never the exception's text: TimeoutExpired's is the whole argv, the user's command (#31)
        log.debug("reaping %s failed: %s", proc.pid, type(e).__name__)
        return False


def kill_process_tree(proc: subprocess.Popen, windows: Optional[bool] = None) -> None:
    """Kill a timed-out child AND everything it started; best effort, never raises (#48 round 2).

    - **Windows:** ``taskkill /F /T``, in its own hidden console (no window flashes up when the
      server runs as ``pythonw.exe``). A PowerShell child runs under the console trampoline
      (``powershell_argv``), so the process the server started is not the one doing the work.
      Killing only it would leave PowerShell, and anything it started, running with our pipes
      open, and ``communicate()`` would never return (CPython's ``run()`` waits there with no
      timeout, gh-81605).
    - **POSIX:** the process group whose id is the child's pid: the child's own, when it leads
      one (``run_child`` starts it in a new session). The caller's group id is never the child's
      pid, so the server is never signalled.
      - SIGTERM to the group, then up to ``KILL_GRACE_S`` for the child and its descendants to
        exit, then SIGKILL to the group in any case: whatever ignored SIGTERM dies, whether or
        not it holds our pipes (review round 2).
      - Nothing is reaped before that SIGKILL: the child, a zombie by then, keeps the group id
        from being reused, so the SIGKILL can only reach the child's own group.

    Then the child itself is killed and reaped. The whole kill waits at most ``KILL_GRACE_S`` + 5 s
    on POSIX, and 10 s on Windows (taskkill's 5 s, then the reap's 5 s).
    """
    windows = _on_windows(windows)
    if windows:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5, **own_console(True)
            )
        except Exception as e:
            log.debug("taskkill of %s failed (%s)", proc.pid, type(e).__name__)
    else:
        started = _tree(proc.pid)  # before any signal: the tree is still intact
        _signal_group(proc.pid, signal.SIGTERM)
        _wait_exited(started, KILL_GRACE_S)
        _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.kill()  # Popen polls first: an exited child is reaped here, never signalled
    except Exception as e:
        log.debug("killing %s failed (%s)", proc.pid, type(e).__name__)
    _reap(proc, 5)


def run_child(cmd: list[str], timeout: int, windows: Optional[bool] = None, cwd: Optional[str] = None) -> ChildOutput:
    """Run ``cmd`` in binary mode and decode both streams with ``decode_output``.

    - The child gets its own hidden console on Windows (``own_console``) and its own session on
      POSIX, so a timeout can kill its whole process tree.
    - Its stdin is NUL, never the server's: under ``--transport stdio`` that is the MCP JSON-RPC
      stream, and a child that reads stdin would eat protocol frames (review round 1).
    - ``cwd``: the working directory (Shell on POSIX); ``None`` inherits the server's. A PowerShell
      child takes its cwd in ``powershell_argv`` instead.
    - A timeout kills the tree (``kill_process_tree``) and raises ``subprocess.TimeoutExpired``
      naming the command as the user knows it (``_shown``). Any other exception while waiting
      kills the tree too, as ``subprocess.run`` did.
    """
    windows = _on_windows(windows)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        start_new_session=not windows,
        **own_console(windows),
    )
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # never the argv: it carries the user's command (#31)
        log.warning("a child timed out after %ss: killing pid %s and the processes it started", timeout, proc.pid)
        kill_process_tree(proc, windows)
        raise subprocess.TimeoutExpired(_shown(cmd), timeout) from None
    except BaseException:
        kill_process_tree(proc, windows)
        raise
    return ChildOutput(
        proc.returncode,
        decode_output(raw_out, windows, "stdout"),
        decode_output(raw_err, windows, "stderr"),
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
        raise RuntimeError(f"{label or ' '.join(_shown(cmd)[:2])} failed (exit {result.returncode}): {reason}")
    return result.reply()
