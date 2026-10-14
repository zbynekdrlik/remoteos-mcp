"""Fake pip for the installer tests — a stateful stand-in for Debian's pip.

The python3 shim routes `python3 -m pip <args>` to `fake_pip.py <args>`.  A fake
box has TWO site dirs, like a Debian/Ubuntu box:

  site      pip's install target (/usr/local/lib/python3.X/dist-packages)
  apt_site  apt-owned packages (/usr/lib/python3/dist-packages), after `site`
            on sys.path, so a pip copy shadows an apt copy of the same name

Each installed distribution is a `<name>-<version>.dist-info/` with METADATA +
INSTALLER + RECORD — the same files real pip and importlib.metadata read, so the
installer's snapshot/rollback helper works on it exactly as on a real box.  Apt
dists carry INSTALLER "debian" and no RECORD, as Debian ships them.

Behaviour comes from the JSON file named by $FAKE_PIP_CONFIG:

  site, apt_site  the two site dirs above
  log             JSON-lines log of every call (argv, PIP_CONSTRAINT, TMPDIR)
  new_version     the remoteos-mcp version a successful install lands
  resolved        extra [name, version] pairs the resolver picks besides
                  fastmcp-slim + remoteos-mcp (reported by --dry-run --report)
  debian_owned    names apt owns: a real install WITHOUT --ignore-installed
                  fails like Debian's pip ("installed by debian")
  dry_run_fail    the --dry-run preflight fails with a multi-line error
  install_fail    the real install installs the dependencies, then fails
                  before remoteos-mcp
  fail_after      install_fail stops after this many packages of the resolution
  install_noop    the real install exits 0 but installs nothing
  install_broken  the real install exits 0 but fastmcp has no FastMCP
  wait_for_file   the real install waits until this file exists (the test
                  creates it once it has closed the installer's console)
  partial_signal  "HUP" / "INT" / "TERM": half-write the first dependency, then
                  send this signal to the whole process group (then, if still
                  alive, finish the install)
  block_restore   "file" / "dir" / "partial": a failed install leaves something
                  the rollback cannot undo (a file where a dir must go, a dir
                  where a file must go, a RECORD-less half-written dependency)
  uninstall_fail_multi  a multi-name `pip uninstall` (the pre-uninstall) fails
  corrupt_snapshot  "import" / "version": a failed install damages the
                  installer's rollback copy, so the restore "works" but the
                  old version is not back in a working state
  signal_parent   the real install installs the dependencies, then sends this
                  signal (TERM / HUP / INT) to the installer and dies
  error_lines     how many numbered lines a failure prints
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

NEW_FASTMCP_SLIM = "4.0.3"


def norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def dist_files(name: str, version: str, *, importable: bool = True) -> dict[str, str]:
    """The module files a fake `name` dist ships — importable by the installer's
    import check (remoteos.__main__ + fastmcp.FastMCP) unless `importable` is off."""
    if norm(name) == "remoteos_mcp":
        return {
            "remoteos/__init__.py": f"# remoteos {version}\n",
            "remoteos/__main__.py": "def cli():\n    return None\n",
        }
    if norm(name) == "fastmcp_slim":
        body = f"class FastMCP:\n    version = {version!r}\n" if importable else f"# broken fastmcp-slim {version}\n"
        return {"fastmcp/__init__.py": body}
    return {f"{norm(name)}/__init__.py": f"__version__ = {version!r}\n"}


def write_dist(
    base: Path,
    name: str,
    version: str,
    files: dict[str, str],
    *,
    executable: tuple[str, ...] = (),
    installer: str = "pip",
    record: bool = True,
    record_lists_dist_info: bool = True,
) -> Path:
    """Install a fake distribution into the site dir `base`: its files (paths
    relative to `base`, may start with `../`) plus a dist-info whose RECORD lists
    every one of them.  `record=False` + `installer="debian"` is an apt package;
    `record_lists_dist_info=False` is a truncated RECORD."""
    info = base / f"{norm(name)}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "INSTALLER").write_text(f"{installer}\n")
    entries = []
    for rel, content in files.items():
        path = Path(os.path.normpath(base / rel))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if rel in executable:
            path.chmod(0o755)
        entries.append(f"{rel},,")
    if record:
        if record_lists_dist_info:
            entries += [f"{info.name}/{f},," for f in ("METADATA", "INSTALLER", "RECORD")]
        (info / "RECORD").write_text("\n".join(entries) + "\n")
    return info


def dist_field(info: Path, key: str) -> str:
    for line in (info / "METADATA").read_text().splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1].strip()
    return ""


def find_dists(sites: list[Path], name: str) -> list[Path]:
    """The dists of `name` in sys.path order — the first one is what pip sees.
    Within one dir: os.listdir order, as importlib.metadata (and so the
    installer's helper) walks it."""
    return [
        base / entry
        for base in sites
        for entry in os.listdir(base)
        if entry.endswith(".dist-info") and norm(dist_field(base / entry, "Name")) == norm(name)
    ]


def _record_paths(info: Path) -> list[str] | None:
    record = info / "RECORD"
    if not record.exists():
        return None
    return [line.split(",", 1)[0] for line in record.read_text().splitlines() if line]


def _installer(info: Path) -> str:
    path = info / "INSTALLER"
    return path.read_text().strip() if path.exists() else ""


def _remove_dist(info: Path) -> None:
    base = info.parent
    for rel in _record_paths(info) or []:
        path = Path(os.path.normpath(base / rel))
        if path.is_file() or path.is_symlink():
            path.unlink()
            parent = path.parent
            while parent != base and base in parent.parents and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    shutil.rmtree(info, ignore_errors=True)


def _error(cfg: dict, what: str) -> None:
    for i in range(1, int(cfg.get("error_lines", 12)) + 1):
        print(f"{what}-error-line-{i:02d}", file=sys.stderr)


def _cannot_uninstall(pkg: str, version: str, installer: str) -> None:
    print("error: uninstall-no-record-file", file=sys.stderr)
    print(f"× Cannot uninstall {pkg} {version}", file=sys.stderr)
    print(f"╰─> The package's contents are unknown: no RECORD file was found for {pkg}.", file=sys.stderr)
    print(f"hint: The package was installed by {installer}. You should check if it can uninstall the package.",
          file=sys.stderr)


def _resolved(cfg: dict) -> list[tuple[str, str]]:
    extra = [(str(n), str(v)) for n, v in cfg.get("resolved", [])]
    return [("fastmcp-slim", NEW_FASTMCP_SLIM), *extra, ("remoteos-mcp", cfg["new_version"])]


def cmd_show(cfg: dict, sites: list[Path], args: list[str]) -> int:
    with_files = "-f" in args or "--files" in args
    verbose = "-v" in args or "--verbose" in args
    names = [a for a in args if not a.startswith("-")]
    blocks, missing = [], []
    for name in names:
        dists = find_dists(sites, name)
        if not dists:
            missing.append(name)
            continue
        info = dists[0]
        lines = [
            f"Name: {dist_field(info, 'Name')}",
            f"Version: {dist_field(info, 'Version')}",
            "Summary: fake",
            f"Location: {info.parent}",
            "Requires: ",
            "Required-by: ",
        ]
        if verbose:
            lines += ["Metadata-Version: 2.1", f"Installer: {_installer(info)}", "Classifiers:", "Entry-points:"]
        if with_files:
            lines.append("Files:")
            paths = _record_paths(info)
            if paths is None:
                lines.append("Cannot locate RECORD or installed-files.txt")
            else:
                lines += [f"  {rel}" for rel in paths]
        blocks.append("\n".join(lines))
    if blocks:
        print("\n---\n".join(blocks))
    if missing:
        print(f"WARNING: Package(s) not found: {', '.join(missing)}", file=sys.stderr)
    return 0 if blocks else 1


def cmd_uninstall(cfg: dict, sites: list[Path], args: list[str]) -> int:
    """Like real pip: the names in order, the visible dist of each; the FIRST
    one that cannot be uninstalled aborts the rest of the list."""
    names = [a for a in args if not a.startswith("-")]
    if cfg.get("uninstall_fail_multi") and len(names) > 1:
        print("ERROR: Exception: [Errno 5] Input/output error", file=sys.stderr)
        return 2
    for name in names:
        dists = find_dists(sites, name)
        if not dists:
            print(f"WARNING: Skipping {name} as it is not installed.", file=sys.stderr)
            continue
        info = dists[0]
        version = dist_field(info, "Version")
        print(f"Found existing installation: {name} {version}")
        if _record_paths(info) is None:
            _cannot_uninstall(name, version, _installer(info))
            return 1
        _remove_dist(info)
        print(f"  Successfully uninstalled {name}-{version}")
    return 0


def _report(cfg: dict, args: list[str]) -> None:
    if "--report" not in args:
        return
    path = Path(args[args.index("--report") + 1])
    path.write_text(json.dumps({
        "version": "1",
        "install": [{"metadata": {"name": n, "version": v}} for n, v in _resolved(cfg)],
    }))


def _install(cfg: dict, sites: list[Path], *, ignore_installed: bool, deps_only: bool) -> None:
    """Install the resolution into `site` in order, remoteos-mcp last — or,
    `deps_only`, the dependencies before it (only the first `fail_after` of the
    resolution when set): a mid-way failure leaves what pip had already
    installed in place."""
    target = sites[0]
    resolution = _resolved(cfg)
    if deps_only and cfg.get("fail_after") is not None:
        resolution = resolution[: int(cfg["fail_after"])]
    for name, version in resolution:
        if deps_only and norm(name) == "remoteos_mcp":
            return
        if not ignore_installed:
            # A plain upgrade uninstalls the old pip-owned copy first, as real pip does.
            for info in find_dists(sites, name):
                if dist_field(info, "Version") != version and _record_paths(info) is not None:
                    _remove_dist(info)
        # With --ignore-installed real pip writes over the files and leaves any
        # other-version dist-info of the same name in place (a stale RECORD).
        write_dist(target, name, version, dist_files(name, version, importable=not cfg.get("install_broken")))


def _block_restore(cfg: dict, sites: list[Path], mode: str) -> None:
    """Leave something behind that the rollback cannot undo."""
    if mode == "file":  # a plain file where the remoteos/ package dir must go
        (sites[0] / "remoteos").write_text("a plain file where the remoteos package dir must go\n")
    elif mode == "dir":  # a directory where the file remoteos/server.py must go
        (sites[0] / "remoteos" / "server.py").mkdir(parents=True)
    elif mode == "partial":  # the last dependency stays half-written: no RECORD
        name = [n for n, _ in _resolved(cfg) if norm(n) != "remoteos_mcp"][-1]
        for info in find_dists(sites[:1], name):
            (info / "RECORD").unlink(missing_ok=True)


def _corrupt_snapshot(mode: str) -> None:
    """Damage the installer's rollback copy (it lives next to pip's TMPDIR), so
    the restore 'succeeds' but the old version is not back in a working state."""
    files = Path(os.environ["TMPDIR"]).parent / "snapshot" / "files"
    if mode == "import":
        for path in files.rglob("remoteos/__main__.py"):
            path.write_text("raise ImportError('corrupted rollback copy')\n")
    elif mode == "version":
        for path in files.rglob("remoteos_mcp-*.dist-info/METADATA"):
            path.write_text("Metadata-Version: 2.1\nName: remoteos-mcp\nVersion: 0.0.0\n")


def _wait_for(path: str) -> None:
    """Block until the test has done its part (e.g. closed the console)."""
    deadline = time.monotonic() + 30
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise SystemExit(f"fake pip: {path} never appeared")
        time.sleep(0.02)


def _partial_wheel_then_signal(cfg: dict, sites: list[Path]) -> None:
    """Real pip writes a wheel's files first, then INSTALLER, then RECORD last.
    Signal the WHOLE process group (Ctrl-C, a tty hang-up, a killpg timeout)
    while the first dependency is half-written: an unshielded pip dies here and
    leaves RECORD-less files behind; a shielded one finishes the wheel."""
    name, version = _resolved(cfg)[0]
    write_dist(sites[0], name, version, dist_files(name, version), record=False)
    os.killpg(os.getpgrp(), getattr(signal, f"SIG{cfg['partial_signal']}"))
    time.sleep(0.2)  # a default-disposition signal kills us right here


def cmd_install(cfg: dict, sites: list[Path], args: list[str]) -> int:
    ignore_installed = "--ignore-installed" in args or "-I" in args
    if "--dry-run" in args:
        if cfg.get("dry_run_fail"):
            _error(cfg, "dry-run")
            return 1
        _report(cfg, args)
        print("Would install " + " ".join(f"{n}-{v}" for n, v in _resolved(cfg)))
        return 0
    print("Collecting remoteos-mcp")
    if cfg.get("wait_for_file"):
        _wait_for(str(cfg["wait_for_file"]))
    if cfg.get("partial_signal"):
        _partial_wheel_then_signal(cfg, sites)
    if not ignore_installed:
        for pkg in cfg.get("debian_owned", []):
            print(f"Attempting uninstall: {pkg}", file=sys.stderr)
            _cannot_uninstall(pkg, "41.0.7", "debian")
            return 1
    if cfg.get("signal_parent"):
        _install(cfg, sites, ignore_installed=ignore_installed, deps_only=True)
        sig = getattr(signal, f"SIG{cfg['signal_parent']}")
        os.kill(os.getppid(), sig)
        return 128 + int(sig)
    if cfg.get("install_fail"):
        _install(cfg, sites, ignore_installed=ignore_installed, deps_only=True)
        _block_restore(cfg, sites, str(cfg.get("block_restore") or ""))
        if cfg.get("corrupt_snapshot"):
            _corrupt_snapshot(str(cfg["corrupt_snapshot"]))
        _error(cfg, "install")
        return 1
    if cfg.get("install_noop"):
        print("Successfully installed nothing")
        return 0
    _install(cfg, sites, ignore_installed=ignore_installed, deps_only=False)
    print("Successfully installed " + " ".join(f"{n}-{v}" for n, v in _resolved(cfg)))
    return 0


def main(argv: list[str]) -> int:
    cfg = json.loads(Path(os.environ["FAKE_PIP_CONFIG"]).read_text())
    sites = [Path(cfg["site"]), Path(cfg["apt_site"])]
    with open(cfg["log"], "a") as log:
        log.write(json.dumps({
            "argv": argv,
            "PIP_CONSTRAINT": os.environ.get("PIP_CONSTRAINT"),
            "TMPDIR": os.environ.get("TMPDIR"),
        }) + "\n")
    if not argv:
        return 1
    if argv[0] == "--version":
        print("pip 25.1.1 from /usr/lib/python3/dist-packages/pip (python 3.14)")
        return 0
    handlers = {"show": cmd_show, "uninstall": cmd_uninstall, "install": cmd_install}
    handler = handlers.get(argv[0])
    return handler(cfg, sites, argv[1:]) if handler else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
