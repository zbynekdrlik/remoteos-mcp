"""SelfUpgrade (#71): start this OS's installer detached from the server, then return at once.

The installers own the proof and the rollback; a server cannot upgrade itself in-process (a failed
upgrade would leave nothing to roll back) and cannot run them as a child (the switch stops it). So
each OS starts the installer from outside the server:

- **Linux:** ``systemd-run`` as root, in its own cgroup, which passes install-linux.sh's guard
  against a run inside ``remoteos-mcp.service``. A non-root unit goes through ``sudo -n``: the
  installers' sudoers drop-in.
- **Windows:** ``schtasks /Run`` of the elevated task install.ps1 registers. The task's command is
  fixed at install time; the ref reaches it through a file next to ``config.json``.
- **macOS:** ``launchctl submit``. launchd keeps a submitted job alive, so without its last step,
  ``launchctl remove`` of its own label, the installer would run again forever.

``upgrade_plan`` is pure: the ref check and the argv of each OS. ``start`` does the I/O. The
key is in no argv and no log: the installers read it on the box, and the runners do not inherit
the server's environment (the server dropped the key from it at start anyway).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from remoteos.errors import failure
from remoteos.platform import is_macos, is_windows
from remoteos.platform.command import run_child

log = logging.getLogger("remoteos.selfupgrade")

REPO = "zbynekdrlik/remoteos-mcp"
DEFAULT_REF = "main"
WINDOWS_TASK = "RemoteOSMCP-Upgrade"
WINDOWS_REF_FILE = "upgrade-ref.txt"
WINDOWS_LOG_FILE = "upgrade.log"
START_TIMEOUT_S = 30
_REF = re.compile(r"[A-Za-z0-9._/-]{1,100}")


@dataclass(frozen=True)
class UpgradePlan:
    """What ``start`` does: run ``argv``, after writing ``ref`` to ``ref_file`` when there is one."""

    ref: str
    argv: tuple[str, ...]
    log_path: str
    ref_file: Optional[str] = None


def check_ref(ref: str) -> str:
    """The ref to install (empty = ``main``). It ends up in a URL and a shell line, so it is a plain
    git ref name: what install-linux.sh's ``ref_ok`` and install.ps1 accept, in at most 100 characters."""
    ref = ref or DEFAULT_REF
    if (
        not _REF.fullmatch(ref)
        or ref[0] in "-./"
        or ref.endswith("/")
        or any(bad in ref for bad in ("..", "//", "/."))
    ):
        raise ValueError(f"ref must be a commit sha, a tag or a branch name: {ref[:100]!r}")
    return ref


def _installer_url(ref: str, name: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{ref}/{name}"


def linux_plan(ref: str, stamp: str, euid: int) -> UpgradePlan:
    unit = f"remoteos-mcp-upgrade-{stamp}"
    log_path = f"/var/log/{unit}.log"
    # No `$` or `%` in the line: systemd expands them in a command. The ref was checked.
    script = f"set -o pipefail; curl -fsSL {_installer_url(ref, 'install-linux.sh')} | bash"
    argv = (
        *(() if euid == 0 else ("sudo", "-n")),
        "systemd-run",
        "--unit",
        unit,
        "--collect",
        f"--setenv=REMOTEOS_MCP_REF={ref}",
        f"--property=StandardOutput=file:{log_path}",
        "--property=StandardError=inherit",
        "/bin/bash",
        "-c",
        script,
    )
    return UpgradePlan(ref, argv, log_path)


def macos_plan(ref: str, stamp: str, home: str, python_dir: str) -> UpgradePlan:
    label = f"com.remoteos-mcp.upgrade-{stamp}"
    log_path = f"{home}/.remoteos-mcp/upgrade-{stamp}.log"
    # launchd's PATH has no Homebrew: this interpreter's directory first, so the installer finds
    # the Python the server runs on.
    script = "; ".join(
        (
            f"export REMOTEOS_MCP_REF={ref}",
            f"export HOME={shlex.quote(home)}",
            f"export PATH={shlex.quote(python_dir)}:/opt/homebrew/bin:/usr/local/bin:$PATH",
            "set -o pipefail",
            f"curl -fsSL {_installer_url(ref, 'install.sh')} | bash",
            f"launchctl remove {label}",
        )
    )
    argv = ("launchctl", "submit", "-l", label, "-o", log_path, "-e", log_path, "--", "/bin/bash", "-c", script)
    return UpgradePlan(ref, argv, log_path)


def windows_plan(ref: str, home: str) -> UpgradePlan:
    config_dir = os.path.join(home, ".remoteos-mcp")
    return UpgradePlan(
        ref,
        ("schtasks", "/Run", "/TN", WINDOWS_TASK),
        os.path.join(config_dir, WINDOWS_LOG_FILE),
        ref_file=os.path.join(config_dir, WINDOWS_REF_FILE),
    )


def upgrade_plan(ref: str = "") -> UpgradePlan:
    """The plan for this machine, for a checked ``ref``."""
    ref = check_ref(ref)
    home, stamp = str(Path.home()), time.strftime("%Y%m%dT%H%M%S")
    if is_windows():
        return windows_plan(ref, home)
    if is_macos():
        return macos_plan(ref, stamp, home, os.path.dirname(sys.executable))
    return linux_plan(ref, stamp, os.geteuid())


def start(plan: UpgradePlan) -> str:
    """Start the detached installer; returns the tool's reply. Raises when it did not start."""
    if plan.ref_file:
        Path(plan.ref_file).write_text(plan.ref + "\n", encoding="ascii")
    log.info("SelfUpgrade: starting ref=%s runner=%s log=%s", plan.ref, plan.argv[0], plan.log_path)
    done = run_child(list(plan.argv), timeout=START_TIMEOUT_S)
    if done.returncode != 0:
        log.warning("SelfUpgrade: the runner failed, exit=%s ref=%s", done.returncode, plan.ref)
        raise failure(
            f"SelfUpgrade did not start ({plan.argv[0]} exit {done.returncode}): {done.stderr.strip()}. "
            "A box installed before 0.7.0.dev39 has no sudoers drop-in or upgrade task: run its "
            "installer once over SSH."
        )
    log.info("SelfUpgrade: started ref=%s log=%s", plan.ref, plan.log_path)
    return (
        f"Upgrade to {plan.ref} started. Log: {plan.log_path}. The server restarts when the installer "
        "switches: poll /health for the new version."
    )
