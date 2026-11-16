"""Owner notices through ``airuleset.py notify`` (#51, #102): one footer-U item per open condition.

Every gateway notice (a box changed state, fell behind stable, has no admin rights, the check
cannot run, live sessions need a restart) goes out through airuleset's ``notify --body``. For owner
zbynek airuleset's owner policy posts nothing (it prints ``suppressed``), so since airuleset #1300 a
send can carry ``--owner-notice <slug> --cwd <project dir>``: a suppressed send then becomes ONE
item in the footer ``U`` of that project, the same slug replaces it, and
``notify --clear-owner-notice <slug> --cwd <dir>`` drops it. A send with the flag that exits 1 was
NOT recorded, so it is not delivered and is retried like any undelivered notice.

Whether the installed airuleset has the flag is read ONCE per run from ``notify --help``. Without
it (or without a usable project directory) every notice goes out as before: plain ``--body``,
grouped. With it, each open condition has its own slug: the machine conditions ONE per kind
(``fleet_notice``: ``fleet-down`` / ``fleet-stale`` / ``fleet-no-admin``, listing their boxes),
plus ``gateway-health``, ``check-failed`` and ``restart-<project>`` (``slug``).

The slugs recorded in U are kept in the notice BOOK (``owner-notices.json`` in the state dir),
edited under a lock because ``check`` and ``sync-configs`` both write it. ``settle`` clears every
recorded slug whose condition is gone; a clear that fails stays in the book and is retried on the
next run. A notice that is not recorded (no flag, an undelivered send) never needs a clear.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from functools import cached_property
from pathlib import Path

from remoteos.gateway import fileops
from remoteos.gateway.targets import last_line

logger = logging.getLogger("remoteos.gateway.owner_notice")

DEFAULT_AIRULESET_PATH = Path.home() / "devel" / "airuleset" / "airuleset.py"
DEFAULT_PROJECT_DIR = Path.home() / "devel" / "remoteos-mcp"
DEFAULT_NOTICES_PATH = Path.home() / ".local" / "state" / "remoteos-gateway" / "owner-notices.json"
NOTIFY_TIMEOUT_S = 60
OWNER_NOTICE_FLAGS = ("--owner-notice", "--clear-owner-notice")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# What `airuleset.py notify --body` prints (send()'s status). It EXITS 0 EVEN WHEN THE POST FAILED,
# so only these printed statuses count as delivered ("dedup" = already delivered under that key;
# "suppressed" = the owner's policy decided nothing is posted, which airuleset itself counts as
# delivered: retrying it every 10 minutes could never change the answer).
_DELIVERED = frozenset({"sent", "dedup", "suppressed"})


def slug(kind: str, name: str | None = None) -> str:
    """The notice slug of one condition: ``<kind>-<name>``. A name that does not fit airuleset's
    ``[a-z0-9][a-z0-9._-]{0,63}`` (upper case, too long) keeps a readable lowercased prefix plus a
    hash of the exact name, so two names never share a slug."""
    if name is None:
        return kind
    plain = f"{kind}-{name}"
    if SLUG_RE.fullmatch(plain):
        return plain
    readable = re.sub(r"[^a-z0-9._-]", "-", plain.lower())[:52]
    return f"{readable}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:10]}"


def project_dir_problem(path: Path) -> str | None:
    """Why *path* cannot be airuleset's ``--cwd`` (None = it can): it must be the root of a git work
    tree and not the home directory (airuleset refuses those: a notice at ``$HOME`` would land in
    every project footer of the account)."""
    try:
        real = path.expanduser().resolve()
        if not real.is_dir():
            return f"{path} is not a directory"
        if real == Path.home().resolve() or real == Path("/"):
            return f"{path} is the home directory (or /), not a project"
        if not (real / ".git").exists():
            return f"{path} is not the root of a git work tree"
    except OSError as exc:
        return f"{path} cannot be read ({exc.strerror or type(exc).__name__})"
    return None


class NoticeBook:
    """The slugs recorded in U (``{"notices": {slug: {"since": ..., ...}}}``), edited under a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with fileops.exclusive_lock(str(self.path) + ".lock"):
            yield

    def read(self) -> dict[str, dict]:
        """The recorded notices. Missing = none; unreadable = none (logged): a hand edit must not
        stop a run, and a notice it loses is only one U item nobody clears."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            logger.error("owner notices: %s unreadable (%s) — treated as empty", self.path, type(exc).__name__)
            return {}
        notices = data.get("notices") if isinstance(data, dict) else None
        if not isinstance(notices, dict):
            return {}
        return {k: v for k, v in notices.items() if isinstance(k, str) and SLUG_RE.fullmatch(k) and isinstance(v, dict)}

    def _write(self, notices: dict[str, dict]) -> None:
        text = json.dumps({"notices": notices}, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        fileops.atomic_write(str(self.path), text.encode("utf-8"))

    def add(self, name: str, record: dict | None) -> None:
        """Remember *name* as recorded in U. Call it holding ``locked()``."""
        notices = self.read()
        since = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")
        notices[name] = {**(record or {}), "since": since}
        self._write(notices)

    def remove(self, name: str) -> None:
        """Forget *name* (it was cleared). Call it holding ``locked()``."""
        notices = self.read()
        if notices.pop(name, None) is not None:
            self._write(notices)


class Notifier:
    """``notifier(body)`` sends a plain notice; ``notifier(body, slug, record)`` sends it as the
    owner notice *slug* when ``per_condition``. Both return whether it was delivered."""

    def __init__(self, script: Path, owner: str | None, project_dir: Path | None, book: NoticeBook | None) -> None:
        self.script = script
        self.owner = owner
        self.project_dir = project_dir
        self.book = book
        self.python = shutil.which("python3") or sys.executable

    @cached_property
    def per_condition(self) -> bool:
        """Does the installed airuleset take ``--owner-notice``? Read once per run (one notifier =
        one run) from ``notify --help``, and only when there is a usable project dir and book."""
        if self.project_dir is None or self.book is None:
            return False
        problem = project_dir_problem(self.project_dir)
        if problem is not None:
            logger.error("owner notices: --notify-project-dir unusable (%s) — sending plain notices", problem)
            return False
        try:
            proc = subprocess.run(
                [self.python, str(self.script), "notify", "--help"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=NOTIFY_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("owner notices: `notify --help` did not run (%s) — sending plain notices", type(exc).__name__)
            return False
        text = proc.stdout.decode("utf-8", "replace")
        supported = proc.returncode == 0 and all(
            re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) for flag in OWNER_NOTICE_FLAGS
        )
        if not supported:
            logger.info("owner notices: this airuleset has no --owner-notice — sending plain notices")
        return supported

    def __call__(self, body: str, notice: str | None = None, record: dict | None = None) -> bool:
        if notice is None or not self.per_condition or self.book is None or self.project_dir is None:
            return self._send(body, [])
        with contextlib.ExitStack() as stack:
            try:  # a concurrent settle must not clear what this send records
                stack.enter_context(self.book.locked())
            except OSError as exc:  # no book, no clear: a U item nobody could ever remove
                logger.error(
                    "owner notices: %s cannot be locked (%s) — sending a plain notice",
                    self.book.path,
                    exc.strerror or type(exc).__name__,
                )
                return self._send(body, [])
            delivered = self._send(body, ["--owner-notice", notice, "--cwd", str(self.project_dir)])
            if delivered:
                try:
                    self.book.add(notice, record)
                except OSError as exc:
                    logger.error(
                        "owner notices: %s is in U but %s cannot be written (%s) — it will not be cleared",
                        notice,
                        self.book.path,
                        exc.strerror or type(exc).__name__,
                    )
        return delivered

    def _send(self, body: str, extra: list[str]) -> bool:
        # one key per ATTEMPT: a retry of an undelivered message must never be deduped away
        attempt = f"remoteos-gateway-check#{time.time_ns()}#{secrets.token_hex(4)}"
        argv = [self.python, str(self.script), "notify", "--body", body, "--dedup-key", attempt, *extra]
        if self.owner:
            argv += ["--owner-name", self.owner]
        proc = self._run(argv, "notify")
        if proc is None:
            return False
        status = last_line(proc.stdout)
        if proc.returncode != 0 or status not in _DELIVERED:
            reason = (
                f"printed {status!r}" if proc.returncode == 0 else last_line(proc.stderr) or f"exit {proc.returncode}"
            )
            logger.error("notify NOT delivered (%s)", reason)
            return False
        if status == "suppressed":
            note = "in the footer U" if extra else "the text is only in this log"
            logger.warning("notify: the owner's policy posted nothing for this message (%s)", note)
        return True

    def clear(self, notice: str) -> bool:
        """``notify --clear-owner-notice <slug> --cwd <dir>``; True when airuleset accepted it."""
        if self.project_dir is None:
            return False
        argv = [self.python, str(self.script), "notify", "--clear-owner-notice", notice, "--cwd", str(self.project_dir)]
        proc = self._run(argv, "clear")
        if proc is None:
            return False
        if proc.returncode != 0:
            logger.error(
                "owner notice %s NOT cleared (%s) — retried next run",
                notice,
                last_line(proc.stderr) or f"exit {proc.returncode}",
            )
            return False
        logger.info("owner notice %s cleared", notice)
        return True

    def _run(self, argv: list[str], what: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=NOTIFY_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("%s did not run (%s)", what, type(exc).__name__)
            return None

    def settle(self, still_open: Callable[[str, dict], bool | None]) -> None:
        """Clear every recorded notice whose condition is gone. *still_open* answers per slug: True
        (keep), False (clear), None (cannot tell this run: keep). A failed clear stays recorded."""
        if self.book is None:
            return
        recorded = self.book.read()
        if not recorded:
            return
        if not self.per_condition:
            logger.warning("owner notices: %d recorded, but this airuleset cannot clear them", len(recorded))
            return
        for name, entry in sorted(recorded.items()):
            if still_open(name, entry) is not False:
                continue
            with self.book.locked():
                if self.book.read().get(name) != entry:  # re-sent meanwhile: that one is open again
                    continue
                if self.clear(name):
                    self.book.remove(name)


Notify = Callable[..., bool]


def per_condition(notifier: Notify | None) -> bool:
    """Does *notifier* send one owner notice per condition (else: plain grouped messages)?"""
    return isinstance(notifier, Notifier) and notifier.per_condition


def send(notifier: Notify | None, body: str, notice: str | None = None, record: dict | None = None) -> bool:
    """Send *body* (as the owner notice *notice* when the notifier can); no notifier = delivered
    (a log line was written instead, by the caller)."""
    if notifier is None:
        return True
    if notice is not None and per_condition(notifier):
        return notifier(body, notice, record)
    return notifier(body)


def announce(
    notifier: Notify | None,
    pending: dict[str, dict],
    compose: Callable[[dict[str, dict]], list[tuple[str, list[str]]]],
) -> tuple[bool, list[str]]:
    """Send the grouped plain messages *compose* makes of *pending* (an owner notice per condition
    KIND is ``fleet_notice``'s, never one per box). Returns (all delivered, the names delivered)."""
    sent, done = True, []
    for text, names in compose(pending):
        logger.warning("check: %s", text.replace("\n", " "))
        delivered = send(notifier, text)
        sent = sent and delivered
        if delivered:
            done.extend(names)
    return sent, done


def airuleset_notifier(
    script: Path, owner: str | None, *, project_dir: Path | None = None, notices: Path | None = None
) -> Notifier | None:
    """The notifier for *script*; None when the script is not there (the caller logs instead)."""
    if not script.is_file():
        return None
    return Notifier(script, owner, project_dir, NoticeBook(notices) if notices is not None else None)
