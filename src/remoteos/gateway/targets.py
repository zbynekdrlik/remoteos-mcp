"""Read and write a project's MCP config where it lives (#51): this account, another account on
this host (``sudo -n -u``), or another host (``ssh -o BatchMode=yes``).

All three run the SAME code, ``fileops.py``: in-process for this account, otherwise its source
via ``python3 -c``. The config's content (it holds tokens and keys) travels on stdin/stdout
only; an argv holds the helper's source, an operation and a path — never a secret.

When ssh or sudo access is missing, ``AccessError`` carries the exact one-time step to grant it.
``REMOTEOS_GATEWAY_SSH`` / ``REMOTEOS_GATEWAY_SUDO`` name the binaries (the tests use fakes).
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from remoteos.gateway import fileops

logger = logging.getLogger("remoteos.gateway.targets")

TARGET_TIMEOUT_S = 60
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")  # never a prompt, a bounded connect


class TargetError(Exception):
    """Reading or writing a config failed. The message never holds file content."""


class AccessError(TargetError):
    """ssh / sudo access is missing; the message names the one-time step."""


def current_account() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _helper_source() -> str:
    return Path(fileops.__file__).read_text(encoding="utf-8")


def last_line(data: bytes) -> str:
    lines = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    text = lines[-1].strip() if lines else ""
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:300]


@dataclass(frozen=True)
class Target:
    """Where one project's config lives. ``kind`` is ``local``, ``sudo`` or ``ssh``."""

    kind: str
    host: str
    account: str
    local_host: str
    ssh_bin: str = "ssh"
    sudo_bin: str = "sudo"

    @property
    def label(self) -> str:
        if self.kind == "ssh":
            return f"ssh {self.account}@{self.host}"
        return f"sudo -u {self.account}" if self.kind == "sudo" else "local"

    # -- operations ------------------------------------------------------------------------

    def read(self, path: str) -> str | None:
        """The config's text, or None when the file does not exist."""
        if self.kind == "local":
            try:
                real = fileops.expand(path)
                fileops.refuse_inside_git_work_tree(real)  # before any token is minted for it
                data = fileops.read_bytes(real)
            except OSError as exc:
                raise TargetError(f"cannot read {path}: {exc.strerror or exc}") from exc
            except ValueError as exc:
                raise TargetError(str(exc)) from exc
        else:
            proc = self._run(["read", path], b"")
            if proc.returncode == fileops.EXIT_MISSING:
                return None
            self._check(proc, "read")
            data = proc.stdout
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TargetError(f"{path} is not UTF-8 text") from exc

    def write(self, path: str, text: str, tag: str) -> dict:
        """Back up, then atomically replace the config (0600). Returns ``{"path", "backup"}``."""
        data = text.encode("utf-8")
        if self.kind == "local":
            try:
                return fileops.write_with_backup(fileops.expand(path), data, tag)
            except OSError as exc:
                raise TargetError(f"cannot write {path}: {exc.strerror or exc}") from exc
            except ValueError as exc:
                raise TargetError(str(exc)) from exc
        # the byte count (never a secret) lets the far side refuse a stdin cut short by a dropped link
        proc = self._run(["write", path, tag, str(len(data))], data)
        self._check(proc, "write")
        try:
            return json.loads(proc.stdout.decode("utf-8"))
        except ValueError as exc:
            raise TargetError(f"{self.label}: the write helper gave no result") from exc

    # -- plumbing --------------------------------------------------------------------------

    def _argv(self, op_args: list[str]) -> list[str]:
        helper = ["python3", "-c", _helper_source(), *op_args]
        if self.kind == "sudo":
            return [self.sudo_bin, "-n", "-u", self.account, "--", *helper]
        # ssh hands the rest of its argv to the remote shell as ONE command line: quote it
        return [self.ssh_bin, *SSH_OPTIONS, f"{self.account}@{self.host}", shlex.join(helper)]

    def _run(self, op_args: list[str], stdin: bytes) -> subprocess.CompletedProcess:
        argv = self._argv(op_args)
        logger.debug("config %s via %s", op_args[0], self.label)
        try:
            return subprocess.run(
                argv, input=stdin, capture_output=True, timeout=TARGET_TIMEOUT_S, cwd="/", check=False
            )
        except FileNotFoundError as exc:
            raise AccessError(f"{self.label}: {argv[0]} is not installed on {self.local_host}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TargetError(f"{self.label}: no answer within {TARGET_TIMEOUT_S} s") from exc

    def _check(self, proc: subprocess.CompletedProcess, op: str) -> None:
        if proc.returncode == 0:
            return
        reason = last_line(proc.stderr) or f"exit {proc.returncode}"
        me = current_account()
        if self.kind == "ssh" and proc.returncode == 255:
            raise AccessError(
                f"cannot reach {self.account}@{self.host} over ssh ({reason}). One-time step, as {me} on "
                f"{self.local_host}: `ssh-copy-id {self.account}@{self.host}` (a key login), then re-run sync-configs."
            )
        if self.kind == "sudo" and reason.startswith("sudo:"):
            raise AccessError(
                f"cannot act as {self.account} with `sudo -n` ({reason}). One-time step on {self.local_host}: "
                f"`echo '{me} ALL=({self.account}) NOPASSWD: /usr/bin/python3' | sudo tee "
                f"/etc/sudoers.d/remoteos-gateway-{self.account}` (lets {me} run python3 as {self.account}), "
                "then re-run sync-configs."
            )
        if proc.returncode == 127:
            raise AccessError(f"{self.label}: python3 is not installed for {self.account} on {self.host}")
        raise TargetError(f"{self.label}: config {op} failed: {reason}")


def target_for(
    host: str,
    account: str,
    *,
    local_host: str,
    ssh_bin: str | None = None,
    sudo_bin: str | None = None,
) -> Target:
    """The writer for a config of *account* on *host*, seen from *local_host*."""
    if host != local_host:
        kind = "ssh"
    elif account == current_account():
        kind = "local"
    else:
        kind = "sudo"
    return Target(
        kind=kind,
        host=host,
        account=account,
        local_host=local_host,
        ssh_bin=ssh_bin or os.environ.get("REMOTEOS_GATEWAY_SSH", "ssh"),
        sudo_bin=sudo_bin or os.environ.get("REMOTEOS_GATEWAY_SUDO", "sudo"),
    )
