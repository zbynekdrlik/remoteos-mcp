"""List the live Claude sessions that read one MCP config (#66). Runs ON the config's account.

STDLIB ONLY, Python 3.8+, no package imports, like ``fileops.py``: ``targets.py`` runs this
module's SOURCE with ``python3 -c <source> <config>`` under ``sudo -n -u`` or over ssh. It prints
``{"sessions": [{"pid": 123, "tmux": true, "pane": "main:2"}, ...]}``; ``pane`` is
``session:window``, or null when unknown (``tmux`` says whether the session runs inside tmux at all).

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
        sessions.append({"pid": int(entry), "tmux": pane is not None, "pane": name})
    return sessions


if __name__ == "__main__":
    json.dump({"sessions": scan(sys.argv[1])}, sys.stdout)
