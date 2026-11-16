"""After ``sync-configs``: which live Claude sessions need a restart, and tell the owner (#66).

A session reads its ``.mcp.json`` only at start. For every project whose config was written, the
scan (``session_scan.py``, run where the config lives through the ``targets`` layer) finds its
live sessions and their tmux panes, and ONE owner message names them. No live session, no message.
A dry run prints what it would send. A failed scan or notify is only reported: the configs are
already in place. The message holds names only, never a token.

When the notifier sends per condition (#102), each project gets its own owner notice
``restart-<project>``, recorded with its sessions (pid + start time). ``check`` clears it once none
of those sessions runs any more (``still_running``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remoteos.gateway import owner_notice, session_scan
from remoteos.gateway.manifest import Project, is_valid_account, is_valid_host
from remoteos.gateway.messages import split_to_fit
from remoteos.gateway.owner_notice import Notify
from remoteos.gateway.targets import Target, TargetError, target_for

logger = logging.getLogger("remoteos.gateway.live_sessions")

_PANE_LIMIT = 80
TITLE = "MCP konfigurácia sa zmenila, tieto Claude sessions treba reštartovať:"
ACTION = "V každej spusti `/exit` a `claude -c`."
RESTART = "restart"  # the owner-notice slug kind: restart-<project> (#102)


@dataclass(frozen=True)
class Session:
    pid: int
    pane: str | None
    in_tmux: bool = False
    start: int | None = None  # the process start time: a reused pid is another process


def _parse(output: bytes) -> list[Session]:
    """The scan's JSON. Raises ``ValueError`` on anything else."""
    data = json.loads(output.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise ValueError("no session list")
    sessions = []
    for item in data["sessions"]:
        pid, pane = (item.get("pid"), item.get("pane")) if isinstance(item, dict) else (None, None)
        in_tmux = isinstance(item, dict) and item.get("tmux") is True
        start = item.get("start") if isinstance(item, dict) else None
        if not isinstance(pid, int) or not isinstance(pane, (str, type(None))):
            raise ValueError("a session is malformed")
        shown = "".join(ch if ch.isprintable() else "?" for ch in pane)[:_PANE_LIMIT] if pane else None
        sessions.append(Session(pid=pid, pane=shown, in_tmux=in_tmux, start=start if isinstance(start, int) else None))
    return sessions


def find_sessions(target: Target, config: str) -> list[Session]:
    """The live sessions of *config*, one per pane. Raises ``TargetError``."""
    source = Path(session_scan.__file__).read_text(encoding="utf-8")
    try:
        sessions = _parse(target.run_helper(source, [config]))
    except ValueError as exc:  # json and UTF-8 errors are ValueError
        raise TargetError(f"{target.label}: the session scan gave no result") from exc
    seen: set[str] = set()
    unique = []
    for session in sessions:
        if session.pane is None or session.pane not in seen:
            unique.append(session)
        if session.pane is not None:
            seen.add(session.pane)
    return unique


def _line(project: Project, session: Session) -> str:
    where = session.pane or f"proces {session.pid} " + ("v tmux, panel nezistený" if session.in_tmux else "mimo tmux")
    return f"• {project.name} ({project.account}@{project.host}): {where}"


def compose_messages(found: list[tuple[Project, list[Session]]]) -> list[str]:
    """ONE message; several only when it would pass ``CHUNK_LIMIT``."""
    lines = [_line(project, session) for project, sessions in found for session in sessions]
    if not lines:
        return []
    groups = split_to_fit(lines, lambda group: len("\n".join([TITLE, *group, ACTION])))
    return ["\n".join([TITLE, *group, ACTION]) for group in groups]


def _record(project: Project, sessions: list[Session]) -> dict:
    """What ``check`` needs to tell whether the restart is done (no token, no path)."""
    pairs = [{"pid": s.pid, "start": s.start} for s in sessions]
    return {"restart": {"project": project.name, "host": project.host, "account": project.account, "sessions": pairs}}


def notify_restarts(
    written: list[tuple[Project, Target]],
    *,
    notifier: Notify | None,
    echo: Callable[[str], None],
    dry_run: bool,
) -> None:
    """Scan the *written* projects and tell the owner. Never raises for a failed scan or notify."""
    found: list[tuple[Project, list[Session]]] = []
    for project, target in written:
        try:
            sessions = find_sessions(target, project.config)
        except (TargetError, OSError) as exc:
            echo(f"restart notice: cannot list the live sessions of {project.name}: {exc}")
            logger.warning("sync-configs: session scan of %s FAILED: %s", project.name, exc)
            continue
        if sessions:
            found.append((project, sessions))
    if not dry_run and owner_notice.per_condition(notifier):
        batches = [
            (text, owner_notice.slug(RESTART, project.name), _record(project, sessions))
            for project, sessions in found
            for text in compose_messages([(project, sessions)])
        ]
    else:
        batches = [(text, None, None) for text in compose_messages(found)]
    for text, notice, record in batches:
        if dry_run:
            echo(f"dry run — the owner would get:\n{text}")
        elif notifier is None:
            echo(f"restart notice NOT sent (no airuleset.py here):\n{text}")
        elif owner_notice.send(notifier, text, notice, record):
            where = f"owner notice {notice}" if notice else "notify"
            echo(f"restart notice handed to {where} (the owner's policy decides whether it posts):\n{text}")
        else:
            echo(f"restart notice NOT delivered:\n{text}")


def still_running(entry: dict, local_host: str) -> bool | None:
    """Does a session of the restart notice *entry* still run? None = cannot tell this run (the
    notice stays). A record that is not what ``notify_restarts`` writes is done (cleared)."""
    restart = entry.get("restart")
    sessions = restart.get("sessions") if isinstance(restart, dict) else None
    host = restart.get("host") if isinstance(restart, dict) else None
    account = restart.get("account") if isinstance(restart, dict) else None
    if not isinstance(sessions, list) or not isinstance(host, str) or not isinstance(account, str):
        return False
    if not is_valid_host(host) or not is_valid_account(account):  # never an ssh option from a hand edit
        return False
    pairs = [
        f"{s['pid']}:{s.get('start')}"
        for s in sessions
        if isinstance(s, dict) and isinstance(s.get("pid"), int) and isinstance(s.get("start"), (int, type(None)))
    ]
    if not pairs:
        return False
    if host == local_host:  # /proc/<pid>/stat is world-readable: no sudo (the unit has NoNewPrivileges)
        return bool(session_scan.alive(pairs))
    source = Path(session_scan.__file__).read_text(encoding="utf-8")
    try:
        data = json.loads(target_for(host, account, local_host=local_host).run_helper(source, ["--alive", *pairs]))
        running = data["alive"]
    except (TargetError, OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("check: cannot tell whether the restart on %s@%s is done (%s)", account, host, exc)
        return None
    return bool(running)
