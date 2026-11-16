"""List the live Claude sessions that read one MCP config (#66). Runs ON the config's account.

STDLIB ONLY, Python 3.8+, no package imports, like ``fileops.py``: ``targets.py`` runs this
module's SOURCE with ``python3 -c <source> <config>`` under ``sudo -n -u`` or over ssh. It prints
``{"sessions": [{"pid": 123, "start": 4567, "tmux": true, "pane": "main:2"}, ...]}``; ``pane`` is
``session:window``, or null when unknown (``tmux`` says whether the session runs inside tmux at all);
``start`` is the process start time (``/proc/<pid>/stat`` field 22), so a reused pid is never the
same session (null when unreadable).

``python3 -c <source> --alive <pid>:<start> ...`` prints ``{"alive": [pid, ...]}``: which of those
sessions still run (#102: the restart notice stays open until none does). ``stat`` is world-readable,
so this needs no other account.

A session is a process named ``claude`` of this account (others are not readable) with a
``.mcp.json`` in its cwd or a parent directory that resolves to the config. Only ``TMUX`` and
``TMUX_PANE`` are taken from its environment: nothing else in there leaves this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

TMUX_TIMEOUT_S = 10
PANE_FORMAT = "#{pane_id} #{session_name}:#{window_index}"


def expand(path):
    """Like ``fileops.expand``: ``~`` is this account's passwd home, never ``$HOME``."""
    if path == "~" or path.startswith("~/"):
        import pwd

        path = pwd.getpwuid(os.getuid()).pw_dir + path[1:]
    return os.path.realpath(path)


def _read(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:  # the process ended, or it is not ours
        return None


def _stat(pid_dir):
    """``(comm, start time)`` from ``/proc/<pid>/stat``, or ``(None, None)``."""
    data = _read(os.path.join(pid_dir, "stat"))
    if not data or b")" not in data:
        return None, None
    head, _, rest = data.rpartition(b")")
    fields = rest.split()
    comm = head.partition(b"(")[2]
    try:
        return comm, int(fields[19])
    except (IndexError, ValueError):
        return comm, None


def alive(pairs, proc_root="/proc"):
    """The pids of *pairs* (``"<pid>:<start>"``, start ``None`` = unknown) still running as the same
    ``claude`` process: same pid, same start time."""
    running = []
    for pair in pairs:
        pid, _, start = pair.partition(":")
        if not pid.isdigit():
            continue
        comm, started = _stat(os.path.join(proc_root, pid))
        if comm != b"claude":
            continue
        if start not in ("", "None") and str(started) != start:
            continue
        running.append(int(pid))
    return running


def uses_config(cwd, wanted):
    """Does a ``.mcp.json`` in *cwd* or above it resolve to the file *wanted*?"""
    directory = cwd
    while True:
        if os.path.realpath(os.path.join(directory, ".mcp.json")) == wanted:
            return True
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent


def tmux_of(pid_dir):
    """``(socket, pane id)`` of a process, from its environment; ``(None, None)`` outside tmux."""
    found = {}
    for item in (_read(os.path.join(pid_dir, "environ")) or b"").split(b"\0"):
        name, _, value = item.partition(b"=")
        if name in (b"TMUX", b"TMUX_PANE"):
            found[name] = value.decode("utf-8", "replace")
    if b"TMUX_PANE" not in found:
        return None, None
    return found.get(b"TMUX", "").split(",")[0] or None, found[b"TMUX_PANE"]


def pane_names(socket, tmux="tmux"):
    """``{pane id: "session:window"}`` of one tmux server; empty when it does not answer."""
    argv = [tmux] + (["-S", socket] if socket else []) + ["list-panes", "-a", "-F", PANE_FORMAT]
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TMUX_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    names = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        pane, _, name = line.partition(" ")
        if name:
            names[pane] = name
    return names


def scan(config, proc_root="/proc", tmux="tmux"):
    wanted = expand(config)
    sessions = []
    servers = {}
    for entry in sorted(os.listdir(proc_root), key=lambda name: (len(name), name)):
        if not entry.isdigit():
            continue
        pid_dir = os.path.join(proc_root, entry)
        if (_read(os.path.join(pid_dir, "comm")) or b"").strip() != b"claude":
            continue
        try:
            cwd = os.readlink(os.path.join(pid_dir, "cwd"))
        except OSError:
            continue
        if not uses_config(cwd, wanted):
            continue
        socket, pane = tmux_of(pid_dir)
        if pane is not None and socket not in servers:
            servers[socket] = pane_names(socket, tmux)
        name = servers[socket].get(pane) if pane is not None else None
        sessions.append({"pid": int(entry), "start": _stat(pid_dir)[1], "tmux": pane is not None, "pane": name})
    return sessions


if __name__ == "__main__":
    if sys.argv[1] == "--alive":
        json.dump({"alive": alive(sys.argv[2:])}, sys.stdout)
    else:
        json.dump({"sessions": scan(sys.argv[1])}, sys.stdout)
