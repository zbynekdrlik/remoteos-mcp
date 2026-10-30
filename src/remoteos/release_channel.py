"""Which release is ``stable`` and which is ``canary`` (#63, #60 part a). Stdlib only.

GitHub Releases is the single source of a version's age and its hold state. CI creates
``v<version>`` on every merge to main (``scripts/create-release.sh``); its ``published_at`` is the
release's age, and marking it ``prerelease`` in the GitHub UI HOLDS it.

- ``canary`` = the newest release that is not held.
- ``stable`` = the newest release that is not held and is at least ``REMOTEOS_SOAK_DAYS`` old
  (default 14).

"Newest" is by publish date, never by version. ``choose`` is pure. ``fetch_releases`` is the thin
network part: ``ReleaseFetchError`` is its only failure, so a caller just skips that run.
"""

from __future__ import annotations

import datetime as _dt
import http.client
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

logger = logging.getLogger("remoteos.release_channel")

RELEASES_URL = "https://api.github.com/repos/zbynekdrlik/remoteos-mcp/releases?per_page=100"
DEFAULT_SOAK_DAYS = 14
MAX_SOAK_DAYS = 3650
FETCH_TIMEOUT_S = 10
_MAX_BODY = 5_000_000  # 100 releases are far less; a bigger answer is not GitHub's

_VERSION = re.compile(r"(\d+(?:\.\d+)*)(?:\.dev(\d+))?")


class ReleaseFetchError(Exception):
    """The release list could not be read. The message is safe to log."""


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: _dt.datetime
    held: bool = False

    @property
    def version(self) -> str:
        return self.tag[1:] if self.tag.startswith("v") else self.tag


@dataclass(frozen=True)
class Channels:
    canary: Release | None
    stable: Release | None


def version_key(version: str) -> tuple | None:
    """A comparable key for ``1.2.3`` / ``1.2.3.dev4`` (a dev release sorts before its final
    release); None for anything else. ``packaging`` is not a dependency of a box's updater."""
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while parts and parts[-1] == 0:  # 1.2 == 1.2.0
        parts.pop()
    dev = match.group(2)
    return (tuple(parts), (0, int(dev)) if dev is not None else (1, 0))


def choose(releases: Iterable[Release], now: _dt.datetime, soak_days: int) -> Channels:
    usable = sorted((r for r in releases if not r.held), key=lambda r: r.published_at, reverse=True)
    soak = _dt.timedelta(days=soak_days)
    return Channels(
        canary=usable[0] if usable else None,
        stable=next((r for r in usable if now - r.published_at >= soak), None),
    )


def behind_since(
    version: str, releases: Iterable[Release], now: _dt.datetime, soak_days: int
) -> _dt.datetime | None:
    """When a box on *version* fell behind stable, or None when it is not older than stable (or
    either version cannot be compared).

    That moment is when the OLDEST release newer than the box finished its soak: a new stable
    while the box is still behind does not restart the count, and no history is needed.
    """
    releases = list(releases)
    stable = choose(releases, now, soak_days).stable
    key = version_key(version)
    if stable is None or key is None:
        return None
    stable_key = version_key(stable.version)
    if stable_key is None or stable_key <= key:
        return None
    soak = _dt.timedelta(days=soak_days)
    crossed = []
    for release in releases:
        release_key = version_key(release.version)
        if release.held or release_key is None or release_key <= key:
            continue
        if release.published_at + soak <= now:
            crossed.append(release.published_at + soak)
    return min(crossed)  # stable itself is in there


def soak_days(env: Mapping[str, str] | None = None) -> int:
    """``REMOTEOS_SOAK_DAYS``; an invalid value is logged and the default applies."""
    raw = (os.environ if env is None else env).get("REMOTEOS_SOAK_DAYS")
    if raw is None or raw == "":
        return DEFAULT_SOAK_DAYS
    try:
        days = int(raw)
        if not 0 <= days <= MAX_SOAK_DAYS:  # a huge value would overflow the date arithmetic
            raise ValueError
    except ValueError:
        logger.error("REMOTEOS_SOAK_DAYS is not a day count (%r) — using %d", raw, DEFAULT_SOAK_DAYS)
        return DEFAULT_SOAK_DAYS
    return days


def is_newest(version: str, tags: Iterable[str]) -> bool:
    """Is *version* above every comparable tag? Raises ``ValueError`` when it is not comparable."""
    key = version_key(version)
    if key is None:
        raise ValueError(f"not a release version: {version!r}")
    keys = [version_key(tag[1:] if tag.startswith("v") else tag) for tag in tags]
    return all(other is None or other < key for other in keys)


def parse_releases(payload: object) -> list[Release]:
    """The GitHub list as ``Release``s, drafts left out. Raises ``ValueError`` on another shape:
    a partly understood list could name the wrong stable."""
    if not isinstance(payload, list):
        raise ValueError("not a list")
    releases = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("a release is not an object")
        if item.get("draft"):
            continue
        tag, published, prerelease = item.get("tag_name"), item.get("published_at"), item.get("prerelease")
        if not isinstance(tag, str) or not tag or not isinstance(published, str) or not isinstance(prerelease, bool):
            raise ValueError("a release lacks tag_name, published_at or prerelease")
        moment = _dt.datetime.fromisoformat(published)
        if moment.tzinfo is None:
            raise ValueError("published_at has no time zone")
        releases.append(Release(tag=tag, published_at=moment, held=prerelease))
    return releases


def fetch_releases(url: str = RELEASES_URL, *, timeout: float = FETCH_TIMEOUT_S) -> list[Release]:
    """``GET`` the public release list (no credential). Raises ``ReleaseFetchError``."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "remoteos-mcp"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        raise ReleaseFetchError(f"HTTP {exc.code}") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:  # URLError and timeouts are OSError
        raise ReleaseFetchError(type(exc).__name__) from exc
    if len(body) > _MAX_BODY:
        raise ReleaseFetchError("the answer is too big")
    try:
        return parse_releases(json.loads(body.decode("utf-8")))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueError
        raise ReleaseFetchError(f"unexpected answer ({exc.__class__.__name__})") from exc


if __name__ == "__main__":  # scripts/create-release.sh: is-newest <version>, the tags on stdin
    try:
        sys.exit(0 if is_newest(sys.argv[2], sys.stdin.read().split()) else 1)
    except ValueError as exc:  # exit 1 means "a newer release exists": this must fail the job
        print(exc, file=sys.stderr)
        sys.exit(2)
