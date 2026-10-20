"""Fake OS tools for the Linux installer tests (install-linux.sh, #12 venv layout).

`tests/linux_installer_harness.py` puts bash shims on the installer's PATH that log their argv
and exec this script with a role. The fakes stand in at the OS/tool boundary only. The venvs
are REAL (`python -m venv`), the python that runs in them is real, and the installer's import
and version checks run against real files.

Roles (`fake_linux_tools.py <config.json> <role> <args...>`):

  venv DIR       `python3 -m venv DIR`. It makes a REAL venv (`--without-pip`) whose `pip`
                 package is a stub. The stub runs `venv_pip` below, so `DIR/bin/python -m pip`
                 works even under the installer's `env -i`.
  systemctl ...  a stateful systemd stand-in. `daemon-reload` loads the unit file.
                 `start`/`restart` "run" the loaded unit: the remoteos version its ExecStart
                 python has installed, the key it hands the server (`--auth-key`, else
                 REMOTEOS_AUTH_KEY from its Environment/EnvironmentFile) and its User=.
                 `start` on an active service changes nothing, as in systemd.
  curl ...       the GitHub API tarball download, plus the service's /health and /mcp on
                 127.0.0.1:8092, answered from the systemctl state. `-H @-` reads headers
                 from stdin, as the installer sends the bearer key.
  id ...         `id -u <name>` for the test accounts; everything else is the real `id`.
  ss ...         `ss -Hltnp 'sport = :8092'`: the active service's own process (its MainPID,
                 which `systemctl show -p MainPID --value` answers) listening on :8092, plus a
                 `squatter_pid` (another local process holding the port) when configured.

Config keys (JSON):

  state, unit_path, system_site, records, curl_log, venv_pip_log, venv_pip_calls, tests_dir
  tarballs      {ref: path of a .tar.gz}
  fetch_fail    the tarball download fails (curl 22)
  venv_pip      fake_pip.py options for installs into a venv (install_fail, install_broken, ...)
  venv_fail     `python3 -m venv` fails until apt-get installed python3-venv
  service       {"start_fails": bool, "serve_version": str, "initialize_code": int,
                 "unauth_code": int,
                 "signal_on_restart": "TERM" | "HUP" | "INT"  (the first restart signals
                     the installer, as an ssh drop / timeout / Ctrl-C would),
                 "restart_waits_for": path  (the first restart waits for the file),
                 "health_waits_for": path  (/health waits for the file),
                 "enable_fails": bool  (the unit is masked: `systemctl enable` fails and
                     `is-enabled` answers `masked`),
                 "squatter_pid": int  (another process also listens on :8092)}
  uids          {"account": uid}

The systemctl state's `hung` (set by a test) makes the active service answer nothing until the
next start/restart.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

PORT = 8092
TARBALL_URL = "https://api.github.com/repos/zbynekdrlik/remoteos-mcp/tarball/"
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
MCP_URL = f"http://127.0.0.1:{PORT}/mcp"


def _load(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def _append(path: str, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def records(path: str | Path) -> list[list[str]]:
    """The argv of every shim call: `[tool, arg1, ...]`, in call order."""
    p = Path(path)
    if not p.exists():
        return []
    return [r.split("\x1f")[:-1] for r in p.read_text().split("\x1e") if r]


# ---------------------------------------------------------------------------
# venv + the venv's pip
# ---------------------------------------------------------------------------


def venv(cfg_path: str, cfg: dict, args: list[str]) -> int:
    target = [a for a in args if not a.startswith("-")][-1]
    if cfg.get("venv_fail") and not any(
        r[:1] == ["apt-get"] and "python3-venv" in r for r in records(cfg["records"])
    ):
        print(
            "The virtual environment was not created successfully because ensurepip is not\n"
            "available.  On Debian/Ubuntu systems, you need to install the python3-venv package",
            file=sys.stderr,
        )
        Path(target, "bin").mkdir(parents=True, exist_ok=True)  # Debian leaves a half venv behind
        return 1
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", target], check=True)
    site = Path(_venv_purelib(Path(target)))
    stub = site / "pip"
    stub.mkdir()
    (stub / "__init__.py").write_text("")
    (stub / "__main__.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {cfg['tests_dir']!r})\n"
        "import fake_linux_tools\n"
        f"sys.exit(fake_linux_tools.venv_pip({cfg_path!r}, sys.argv[1:]))\n"
    )
    return 0


def _venv_purelib(prefix: Path) -> str:
    found = sorted(prefix.glob("lib/python*/site-packages"))
    if not found:
        raise SystemExit(f"fake venv: no site-packages under {prefix}")
    return str(found[0])


def _source_version(src: str) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', Path(src, "pyproject.toml").read_text(), re.MULTILINE)
    if not match:
        raise SystemExit(f"fake pip: no version in {src}/pyproject.toml")
    return match.group(1)


def venv_pip(cfg_path: str, args: list[str]) -> int:
    """`<venv>/bin/python -m pip ...` inside a fake venv: fake_pip.py against the venv's own
    site-packages, installing the version of the source dir it is given."""
    cfg = _load(cfg_path)
    _append(cfg["venv_pip_log"], {"argv": args, "env": dict(os.environ), "prefix": sys.prefix})
    if args[:1] == ["--version"]:
        print(f"pip 24.0 from {sys.prefix}/lib/pip (python 3.12)")
        return 0
    site = Path(sysconfig.get_paths()["purelib"])
    empty = Path(sys.prefix) / ".fake-apt-site"
    empty.mkdir(exist_ok=True)
    options = {"site": str(site), "apt_site": str(empty), "log": cfg["venv_pip_calls"], **cfg.get("venv_pip", {})}
    if args[:1] == ["install"]:
        options["new_version"] = _source_version([a for a in args[1:] if not a.startswith("-")][-1])
    else:
        options["new_version"] = "0"
    pip_cfg = Path(sys.prefix) / ".fake-pip.json"
    pip_cfg.write_text(json.dumps(options))
    os.environ["FAKE_PIP_CONFIG"] = str(pip_cfg)
    sys.path.insert(0, cfg["tests_dir"])
    import fake_pip

    return fake_pip.main(args)


# ---------------------------------------------------------------------------
# systemctl
# ---------------------------------------------------------------------------


def _unit_fields(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith(("#", ";", "[")):
            key, value = line.split("=", 1)
            fields.setdefault(key.strip(), []).append(value.strip())
    return fields


def _installed_version(python: Path, cfg: dict) -> str | None:
    prefix = python.parent.parent
    if (prefix / "pyvenv.cfg").exists():
        sites = sorted(prefix.glob("lib/python*/site-packages"))
    else:
        sites = [Path(cfg["system_site"])]
    for site in sites:
        for info in sorted(site.glob("remoteos_mcp-*.dist-info")):
            for line in (info / "METADATA").read_text().splitlines():
                if line.startswith("Version: "):
                    return line.split(": ", 1)[1].strip()
    return None


def _run_unit(cfg: dict, text: str) -> tuple[dict | None, str]:
    """What the server started from `text` runs, or (None, why it does not start)."""
    fields = _unit_fields(text)
    if not fields.get("ExecStart"):
        return None, "no ExecStart"
    argv = fields["ExecStart"][-1].split()
    env: dict[str, str] = {}
    for item in fields.get("Environment", []):
        k, _, v = item.partition("=")
        env[k] = v
    for item in fields.get("EnvironmentFile", []):
        optional, path = item.startswith("-"), item.lstrip("-")
        if not os.path.exists(path):
            if optional:
                continue
            return None, f"Failed to load environment files: {path}"
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    key = argv[argv.index("--auth-key") + 1] if "--auth-key" in argv else env.get("REMOTEOS_AUTH_KEY")
    python = Path(argv[0])
    if not python.exists():
        return None, f"{python}: No such file or directory"
    version = _installed_version(python, cfg)
    if version is None:
        return None, "No module named remoteos"
    return {
        "python": str(python),
        "version": version,
        "key": key,
        "user": (fields.get("User") or [""])[-1],
        "environment": env,
    }, ""


def _wait_for(path: str) -> None:
    """Block until the test has done its part (e.g. closed the installer's console)."""
    deadline = time.monotonic() + 30
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise SystemExit(f"fake systemctl: {path} never appeared")
        time.sleep(0.02)


def systemctl(cfg: dict, args: list[str]) -> int:
    state = _load(cfg["state"]) or {"enabled": False, "active": False, "starts": 0, "restarts": 0, "reloads": 0}
    flags = [a for a in args if a.startswith("-")]
    words = [a for a in args if not a.startswith("-")]
    verb = words[0] if words else ""
    rc = 0
    if verb == "daemon-reload":
        state["reloads"] += 1
        unit = Path(cfg["unit_path"])
        state["loaded_unit"] = unit.read_text() if unit.exists() else None
    elif verb in ("start", "restart"):
        overrides = cfg.get("service", {})
        if verb == "restart" and not state.get("interrupted"):
            if overrides.get("restart_waits_for"):
                _wait_for(overrides["restart_waits_for"])
            if overrides.get("signal_on_restart"):
                state["interrupted"] = True
                Path(cfg["state"]).write_text(json.dumps(state, indent=1))
                os.kill(os.getppid(), getattr(signal, f"SIG{overrides['signal_on_restart']}"))
        if verb == "start" and state["active"]:
            pass  # systemd: starting an active unit does nothing; a changed unit is NOT loaded
        else:
            state["starts" if verb == "start" else "restarts"] += 1
            text = state.get("loaded_unit")
            if text is None:
                unit = Path(cfg["unit_path"])
                text = unit.read_text() if unit.exists() else ""
            service, why = _run_unit(cfg, text)
            if service is None:
                state.update(active=False, service=None, failure=why)
                print(f"Job for remoteos-mcp.service failed: {why}", file=sys.stderr)
                rc = 1
            elif cfg.get("service", {}).get("start_fails"):
                state.update(active=False, service=service, failure="the server exited")
            else:
                state.update(active=True, service=service, failure="", hung=False,
                             pid=4000 + state["starts"] + state["restarts"])
    elif verb == "show":  # `show -p MainPID --value remoteos-mcp`
        print(state.get("pid", 0) if state["active"] and words[1:2] == ["MainPID"] else 0)
    elif verb == "stop":
        state["active"] = False
    elif verb == "enable":
        if cfg.get("service", {}).get("enable_fails"):
            print("Failed to enable unit: Unit file remoteos-mcp.service is masked.", file=sys.stderr)
            rc = 1
        else:
            state["enabled"] = True
    elif verb == "disable":
        state["enabled"] = False
    elif verb == "is-active":
        if "--quiet" not in flags and "-q" not in flags:
            print("active" if state["active"] else "inactive")
        rc = 0 if state["active"] else 3
    elif verb == "is-enabled":
        if cfg.get("service", {}).get("enable_fails"):
            print("masked")  # what makes `systemctl enable` fail
            rc = 1
        else:
            print("enabled" if state["enabled"] else "disabled")
            rc = 0 if state["enabled"] else 1
    Path(cfg["state"]).write_text(json.dumps(state, indent=1))
    return rc


# ---------------------------------------------------------------------------
# curl
# ---------------------------------------------------------------------------

_CURL_VALUE_OPTS = {"-o", "--output", "-w", "--write-out", "-H", "--header", "-X", "--request",
                    "-d", "--data", "--data-raw", "-m", "--max-time", "--connect-timeout"}


def curl(cfg: dict, args: list[str]) -> int:
    opts: dict[str, list[str]] = {}
    url, flags, i = "", "", 0
    while i < len(args):
        a = args[i]
        if a in _CURL_VALUE_OPTS:
            opts.setdefault(a, []).append(args[i + 1])
            i += 2
            continue
        if a.startswith("-") and not a.startswith("--"):
            flags += a[1:]
        elif not a.startswith("-"):
            url = a
        i += 1
    headers: list[str] = []
    for h in opts.get("-H", []) + opts.get("--header", []):
        headers += [line for line in sys.stdin.read().splitlines() if line] if h == "@-" else [h]
    data = (opts.get("-d") or opts.get("--data") or opts.get("--data-raw") or [""])[-1]
    method = (opts.get("-X") or opts.get("--request") or ["POST" if data else "GET"])[-1]
    out = (opts.get("-o") or opts.get("--output") or [None])[-1]
    write_out = (opts.get("-w") or opts.get("--write-out") or [""])[-1]
    _append(cfg["curl_log"], {"url": url, "method": method, "headers": headers, "data": data})
    fail = "f" in flags

    def answer(code: int, body: str = "") -> int:
        if out and out != "/dev/null":
            Path(out).write_text(body)
        elif not out:
            sys.stdout.write(body)
        sys.stdout.write(write_out.replace("%{http_code}", f"{code:03d}"))
        if fail and code >= 400:
            print(f"curl: (22) The requested URL returned error: {code}", file=sys.stderr)
            return 22
        return 0

    if url.startswith(TARBALL_URL):
        ref = url[len(TARBALL_URL):]
        tarball = cfg.get("tarballs", {}).get(ref)
        if cfg.get("fetch_fail") or tarball is None:
            return answer(404, "")
        shutil.copyfile(tarball, out)
        return 0
    state = _load(cfg["state"])
    if not state.get("active"):
        sys.stdout.write(write_out.replace("%{http_code}", "000"))
        print(f"curl: (7) Failed to connect to 127.0.0.1 port {PORT}", file=sys.stderr)
        return 7
    service, overrides = state["service"], cfg.get("service", {})
    if state.get("hung"):  # active but answering nothing (until a (re)start runs it afresh)
        sys.stdout.write(write_out.replace("%{http_code}", "000"))
        print("curl: (28) Operation timed out after 3000 milliseconds", file=sys.stderr)
        return 28
    if url == HEALTH_URL and overrides.get("health_waits_for"):
        _wait_for(overrides["health_waits_for"])
    if url == HEALTH_URL:
        version = overrides.get("serve_version") or service["version"]
        return answer(200, json.dumps({"status": "ok", "version": version}))
    if url == MCP_URL:
        key = service.get("key")
        authed = bool(key) and f"Authorization: Bearer {key}" in headers
        if not key:
            code = 200  # the server without a key: authentication OFF
        elif authed:
            code = overrides.get("initialize_code", 200) if '"initialize"' in data else 400
        else:
            code = overrides.get("unauth_code", 401)
        return answer(code, '{"jsonrpc":"2.0","id":1,"result":{}}' if code == 200 else "")
    return answer(404)


# ---------------------------------------------------------------------------
# ss
# ---------------------------------------------------------------------------


def ss(cfg: dict, args: list[str]) -> int:
    """`ss -Hltnp 'sport = :8092'`: who listens on the service's port."""
    assert args == ["-Hltnp", f"sport = :{PORT}"], args
    state = _load(cfg["state"])
    if state.get("active"):
        print(f'LISTEN 0 2048 0.0.0.0:{PORT} 0.0.0.0:* users:(("python",pid={state["pid"]},fd=6))')
    squatter = cfg.get("service", {}).get("squatter_pid")
    if squatter:
        print(f'LISTEN 0 5 127.0.0.1:{PORT} 0.0.0.0:* users:(("python3",pid={squatter},fd=3))')
    return 0


# ---------------------------------------------------------------------------
# id
# ---------------------------------------------------------------------------


def fake_id(cfg: dict, args: list[str]) -> int:
    uids = cfg.get("uids", {})
    if len(args) == 2 and args[0] == "-u" and args[1] in uids:
        print(uids[args[1]])
        return 0
    if len(args) == 2 and args[0] == "-u":
        print(f"id: '{args[1]}': no such user", file=sys.stderr)
        return 1
    os.execv("/usr/bin/id", ["id", *args])
    return 1  # pragma: no cover


def main(argv: list[str]) -> int:
    cfg_path, role, args = argv[0], argv[1], argv[2:]
    cfg = _load(cfg_path)
    handlers = {
        "venv": lambda: venv(cfg_path, cfg, args),
        "systemctl": lambda: systemctl(cfg, args),
        "curl": lambda: curl(cfg, args),
        "id": lambda: fake_id(cfg, args),
        "ss": lambda: ss(cfg, args),
    }
    return handlers[role]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
