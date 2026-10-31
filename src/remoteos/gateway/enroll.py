"""One-command onboarding (#26): dev1's side of ``remoteos-gateway enroll``.

The receiver is airuleset's one-shot credential endpoint (``filedrop/vault_server.py``), the same
hardened code ``secret request`` runs, so this repo holds no credential endpoint of its own. It
cannot sit on the public drop lane: ``drop-dev1`` is behind Cloudflare Access, and a box with no
Access login gets a 302 from the edge (measured, comment on #26). So ``--private`` binds it to
tailscale and an ephemeral Cloudflare quick tunnel gives it a public TLS name for as long as the
``enroll`` run lives. The gateway itself stays tailnet-only.

The body the installer posts stays in the vault (0600) and reaches ``enroll-finish`` on stdin
through ``secret exec --stdin``. Whoever holds the printed URL could post their own body, so
nothing in it is trusted: the machine name and the projects come from the dev1 command line, and
the key goes only to private addresses.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from remoteos.gateway.registry import MachineSourceError, validate_key

logger = logging.getLogger("remoteos.gateway.enroll")

CLOUDFLARED = "cloudflared"
SECRET_TTL_S = 3600  # airuleset's maximum: the receiver's life, and how long the owner has
SECRET_KEEP_S = 3600  # a failed enroll-finish can be retried for this long
USER_AGENT = "remoteos-enroll"  # Cloudflare answers 403 to python's default one
# A name resolves about 10 s after cloudflared logs it, and a lookup before that is cached negative.
DNS_SETTLE_S = 12.0
TUNNEL_TIMEOUT_S = 60.0
POLL_S = 3.0
MAX_ADDRESSES = 16
_QUICK_HOST = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_ENDPOINT = re.compile(r"(http://[0-9.]+:[0-9]+)/([A-Za-z0-9_-]+)/")
_SSH_KEY = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # tailscale; is_private does not cover it
_HOSTNAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
RAW_URL = "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp"


class EnrollError(Exception):
    """Enrollment cannot go on. The message never holds the key, the token or a posted value."""


@dataclass(frozen=True)
class Registration:
    """What a box posted, validated. The field names say what it claims, not that it is true."""

    hostname: str
    addresses: tuple[str, ...]
    port: int
    version: str
    os: str
    key: str = field(repr=False)


def secret_name(machine: str) -> str:
    """The vault name of an enrollment: letters, digits and underscores only."""
    return "REMOTEOS_ENROLL_" + re.sub(r"[^A-Za-z0-9]", "_", machine)[:48]


def _private_ipv4(text: object) -> bool:
    """A private or tailscale IPv4 literal: never a public, loopback or link-local one."""
    try:
        address = ipaddress.IPv4Address(text)  # type: ignore[arg-type]
    except ValueError:
        return False
    if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
        return False
    return address in _CGNAT or address.is_private


def parse_registration(raw: bytes) -> Registration:
    """Validate the posted body. Errors name the field, never its value."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise EnrollError("the registration is not JSON") from exc
    if not isinstance(data, dict):
        raise EnrollError("the registration is not a JSON object")
    hostname, version, port = data.get("hostname"), data.get("version"), data.get("port")
    if not isinstance(hostname, str) or not _HOSTNAME.fullmatch(hostname):
        raise EnrollError("the registration has no valid hostname")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise EnrollError("the registration has no valid version")
    if type(port) is not int or not 1 <= port <= 65535:
        raise EnrollError("the registration has no valid port")
    if data.get("os") not in ("windows", "linux", "macos"):
        raise EnrollError("the registration has no valid os")
    key = data.get("auth_key")
    try:
        validate_key(key)
    except (MachineSourceError, TypeError) as exc:
        raise EnrollError("the registration has no valid auth_key") from exc
    ips = data.get("ips")
    if not isinstance(ips, list) or len(ips) > MAX_ADDRESSES:
        raise EnrollError("the registration has no valid ips")
    addresses = tuple(ip for ip in ips if isinstance(ip, str) and _private_ipv4(ip))
    return Registration(hostname, addresses, port, version, data["os"], key)


def candidate_urls(registration: Registration) -> list[str]:
    """The box's possible /mcp urls: tailscale first (it survives a LAN change), the rest in the
    order the box gave them."""
    tailnet = [ip for ip in registration.addresses if ipaddress.IPv4Address(ip) in _CGNAT]
    others = [ip for ip in registration.addresses if ip not in tailnet]
    return [f"http://{ip}:{registration.port}/mcp" for ip in dict.fromkeys(tailnet + others)]


def read_ssh_key(path: Path | None) -> str:
    """dev1's public key: *path*, else the account's ed25519, else its rsa one. Type and key only:
    a comment may hold anything (dev1's has spaces), and it goes into a shell command."""
    home = Path.home() / ".ssh"
    candidates = [path] if path else [home / "id_ed25519.pub", home / "id_rsa.pub"]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        fields = text.split(" ")
        if "\n" not in text and _SSH_KEY.fullmatch(" ".join(fields[:2])):
            return " ".join(fields[:2])
        raise EnrollError(f"{candidate} is not one ssh public key line")
    raise EnrollError("no ssh public key found: pass --ssh-key")


def install_commands(url: str, ssh_key: str, ref: str = "main") -> dict[str, str]:
    """The command to run on the new box, per OS. On Linux the values reach sudo's environment
    only: on sudo's command line they would show in ``ps``."""
    raw = f"{RAW_URL}/{ref}"
    ps_ref = "" if ref == "main" else f'$env:REMOTEOS_MCP_REF="{ref}"; '
    sh_ref = "" if ref == "main" else f"REMOTEOS_MCP_REF='{ref}' "
    sh_env = f"{sh_ref}REMOTEOS_ENROLL='{url}' REMOTEOS_ENROLL_SSH_KEY='{ssh_key}'"
    keep = "REMOTEOS_ENROLL,REMOTEOS_ENROLL_SSH_KEY" + ("" if ref == "main" else ",REMOTEOS_MCP_REF")
    return {
        "Windows (PowerShell as Administrator)": (
            f'{ps_ref}$env:REMOTEOS_ENROLL="{url}"; $env:REMOTEOS_ENROLL_SSH_KEY="{ssh_key}"; '
            f"irm {raw}/install.ps1 | iex"
        ),
        "Linux": f"curl -fsSL {raw}/install-linux.sh | {sh_env} sudo --preserve-env={keep} bash",
        "macOS": f"curl -fsSL {raw}/install.sh | {sh_env} bash",
    }


def public_health(url: str) -> int | None:
    """The status of the public ``/healthz``, None when there is no answer."""
    try:
        return httpx2.get(url, headers={"user-agent": USER_AGENT}, timeout=5.0, trust_env=False).status_code
    except httpx2.HTTPError:
        return None


class Airuleset:
    """The ``secret`` actions of airuleset.py, the owner of the vault."""

    def __init__(self, path: Path):
        self.path = path

    def _run(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(self.path), "secret", *args], capture_output=True, text=True, check=False, **kwargs
        )

    def request(self, name: str) -> str:
        """A private one-shot endpoint for *name*; its stdout holds the token and is never shown."""
        result = self._run("request", name, "--private", "--ttl", str(SECRET_TTL_S), "--keep", str(SECRET_KEEP_S))
        if result.returncode != 0:
            raise EnrollError(f"secret request failed (exit {result.returncode}): {result.stderr.strip()[-300:]}")
        return result.stdout

    def state(self, name: str) -> str:
        """``ready`` | ``pending`` | ``absent``."""
        words = self._run("status", name).stdout.split()  # "<name> ready expires=<time>"
        return words[1] if len(words) > 1 else "absent"

    def forget(self, name: str) -> None:
        result = self._run("forget", name)
        if result.returncode != 0:
            logger.warning("secret forget %s failed (exit %s)", name, result.returncode)

    def exec_stdin(self, name: str, command: list[str]) -> subprocess.CompletedProcess:
        """Run *command* with the stored value on its stdin; airuleset filters its output."""
        return self._run("exec", name, "--stdin", "--", *command)


def endpoint_from_request(stdout: str) -> tuple[str, str]:
    """``(origin, token)`` of the tailscale line of ``secret request --private``."""
    lines = stdout.splitlines()
    for line in [line for line in lines if "tailscale" in line] or lines:
        match = _ENDPOINT.search(line)
        if match:
            return match.group(1), match.group(2)
    raise EnrollError("secret request printed no endpoint: is tailscale up on this box?")


class Tunnel:
    """An ephemeral quick tunnel to *origin*. ``--config`` names an empty file: cloudflared would
    read ``~/.cloudflared/config.yml`` otherwise, and a catch-all there answers 404 to everything."""

    def __init__(self, origin: str):
        if shutil.which(CLOUDFLARED) is None:
            raise EnrollError("cloudflared is not installed on this box")
        self._dir = tempfile.TemporaryDirectory(prefix="remoteos-enroll-")
        base = Path(self._dir.name)
        (base / "empty.yml").write_text("")
        self._log = base / "cloudflared.log"
        with self._log.open("wb") as log:
            self._process = subprocess.Popen(
                [CLOUDFLARED, "tunnel", "--config", str(base / "empty.yml"), "--no-autoupdate", "--url", origin],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            )

    def alive(self) -> bool:
        return self._process.poll() is None

    def wait_for_host(self) -> str:
        """The public https base, once ``/healthz`` answers through it. Never returns a dead one."""
        deadline = time.monotonic() + TUNNEL_TIMEOUT_S
        host = None
        while host is None:
            found = _QUICK_HOST.search(self._log.read_text(errors="replace"))
            host = found.group(0) if found else None
            if host is None:
                self._check(deadline)
                time.sleep(0.5)
        time.sleep(DNS_SETTLE_S)
        while public_health(f"{host}/healthz") != 204:
            self._check(deadline)
            time.sleep(2.0)
        return host

    def _check(self, deadline: float) -> None:
        if not self.alive():
            raise EnrollError(f"cloudflared exited with {self._process.returncode}")
        if time.monotonic() > deadline:
            raise EnrollError(f"the tunnel did not answer within {TUNNEL_TIMEOUT_S:.0f} s")

    def close(self) -> None:
        if self.alive():
            self._process.terminate()
            try:
                self._process.wait(10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(5)
        self._dir.cleanup()


def wait_for_registration(airuleset: Airuleset, name: str, tunnel: Tunnel, timeout_s: float) -> None:
    """Block until a box posted, or fail: the tunnel died, the time ran out."""
    deadline = time.monotonic() + timeout_s
    while True:
        state = airuleset.state(name)
        if state == "ready":
            return
        if state != "pending":
            raise EnrollError(f"the enrollment request is gone (state {state})")
        if not tunnel.alive():
            raise EnrollError("the tunnel died while waiting for the box")
        if time.monotonic() > deadline:
            raise EnrollError(f"no box enrolled within {timeout_s:.0f} s")
        time.sleep(POLL_S)
