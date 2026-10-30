"""The per-box updater (#65, #60 part c): ``python -m remoteos.updater``, run by the OS's scheduler.

A pull model, so a box that was off catches up by itself: the scheduler starts this process, it
reads the box's ring (``stable`` default, ``canary``) from ``updater.json``, asks GitHub Releases
for that ring's target (``release_channel``), and when the target is newer than the installed
version starts this OS's installer through ``selfupgrade``. The installers own the proof and the
rollback; a held release is never a target, and nothing is ever downgraded.

``decide`` is pure. Every decision is logged, and no secret passes through here: the installers
read the key on the box. A release list that cannot be read skips the run (exit 0: the next run
tries again); an upgrade that did not start exits 1.

Never inside the server's own service: the switch stops the server (see ``selfupgrade``).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import logging.handlers
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remoteos import __version__, release_channel
from remoteos.platform import is_macos, is_windows
from remoteos.release_channel import Channels, Release, ReleaseFetchError, choose, version_key

log = logging.getLogger("remoteos.updater")

DEFAULT_RING = "stable"
RINGS = ("stable", "canary")
CONFIG_NAME = "updater.json"
LINUX_CONFIG_DIR = "/etc/remoteos-mcp"
CGROUP_FILE = "/proc/self/cgroup"
SERVER_CGROUP = "/remoteos-mcp.service"
LOG_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class Decision:
    target: Release | None  # None = nothing to do
    reason: str


def config_path() -> Path:
    """Where this OS's installer writes the ring."""
    if is_windows() or is_macos():
        return Path.home() / ".remoteos-mcp" / CONFIG_NAME
    return Path(LINUX_CONFIG_DIR) / CONFIG_NAME


def read_ring(path: Path) -> str:
    """The box's ring. No file = the default; a broken one is logged and the safer default applies."""
    try:
        ring = json.loads(path.read_text(encoding="utf-8")).get("ring")
    except FileNotFoundError:
        return DEFAULT_RING
    except (OSError, ValueError, AttributeError, RecursionError) as exc:
        log.error("updater: %s is unreadable (%s) — using ring %s", path, type(exc).__name__, DEFAULT_RING)
        return DEFAULT_RING
    if ring not in RINGS:
        log.error("updater: %s names no known ring (%r) — using ring %s", path, ring, DEFAULT_RING)
        return DEFAULT_RING
    return ring


def decide(ring: str, installed: str, channels: Channels) -> Decision:
    """Which release this box moves to, if any. Only ever forward: a box ahead of its ring stays."""
    target = channels.canary if ring == "canary" else channels.stable
    if target is None:
        return Decision(None, f"ring {ring} has no release yet")
    installed_key, target_key = version_key(installed), version_key(target.version)
    if installed_key is None or target_key is None:
        return Decision(None, f"versions not comparable (installed {installed}, {ring} {target.tag})")
    if target_key <= installed_key:
        return Decision(None, f"up to date (installed {installed}, {ring} {target.tag})")
    return Decision(target, f"newer release (installed {installed}, {ring} {target.tag})")


def inside_server_service(cgroup_file: str = CGROUP_FILE) -> bool:
    """Is this process in the server's own unit (a Shell call)? install-linux.sh's guard, here."""
    try:
        return SERVER_CGROUP in Path(cgroup_file).read_text(encoding="utf-8", errors="replace")
    except OSError:  # no such file: not Linux
        return False


def _start_upgrade(tag: str) -> str:
    # Here, not at the top: selfupgrade reaches fastmcp through errors.py, and every decision above
    # must be logged even on a box whose packages are broken.
    from remoteos import selfupgrade

    return selfupgrade.start(selfupgrade.upgrade_plan(tag))


def run(
    config: Path,
    *,
    installed: str = __version__,
    now: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc),
    fetch: Callable[[], list[Release]] = release_channel.fetch_releases,
    start: Callable[[str], str] = _start_upgrade,
    cgroup_file: str = CGROUP_FILE,
) -> int:
    """One scheduled run; the process's exit code."""
    if inside_server_service(cgroup_file):
        log.error("updater: this run is inside remoteos-mcp.service, its upgrade would stop it — nothing done")
        return 1
    ring = read_ring(config)
    try:
        releases = fetch()
    except ReleaseFetchError as exc:
        log.warning("updater: the release list could not be read (%s) — the next run tries again", exc)
        return 0
    decision = decide(ring, installed, choose(releases, now(), release_channel.soak_days()))
    log.info("updater: %s", decision.reason)
    if decision.target is None:
        return 0
    try:
        log.info("updater: %s", start(decision.target.tag))
    except Exception as exc:  # an unattended run: the log is the only reader
        log.error("updater: the upgrade to %s did not start: %s: %s", decision.target.tag, type(exc).__name__, exc)
        return 1
    return 0


def _set_up_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # None under pythonw.exe, the Windows task
        handlers.append(logging.StreamHandler())
    file_error = None
    if log_file:
        try:
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
                )
            )
        except OSError as exc:
            file_error = exc
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if file_error is not None:
        log.error("updater: cannot write the log %s: %s", log_file, file_error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="remoteos.updater", description="Move this box to its ring's release.")
    parser.add_argument("--config", type=Path, default=None, help="the ring file (default: this OS's)")
    parser.add_argument("--log-file", default=None, help="also log there, rotated (systemd's journal needs none)")
    args = parser.parse_args(argv)
    _set_up_logging(args.log_file)
    return run(args.config or config_path())


if __name__ == "__main__":
    sys.exit(main())
