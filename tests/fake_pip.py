"""Fake pip for the installer tests — a stateful stand-in for Debian's pip.

The python3 shim routes `python3 -m pip <args>` to `fake_pip.py <args>`.  State
lives in a fake site-packages dir: each installed distribution is a
`<name>-<version>.dist-info/` with METADATA + INSTALLER + RECORD — the same
files real pip reads for `show` and `uninstall`, so the installer's
snapshot/rollback works on it exactly as on a real box.  Apt-owned packages are
dist-infos with INSTALLER "debian" and no RECORD, as Debian ships them.

Behaviour comes from the JSON file named by $FAKE_PIP_CONFIG:

  site            fake site-packages dir
  log             JSON-lines log of every call (argv, PIP_CONSTRAINT, TMPDIR)
  new_version     the remoteos-mcp version a successful install lands
  resolved        extra [name, version] pairs the resolver picks besides
                  fastmcp-slim + remoteos-mcp (reported by --dry-run --report)
  debian_owned    names apt owns: a real install WITHOUT --ignore-installed
                  fails like Debian's pip ("installed by debian")
  dry_run_fail    the --dry-run preflight fails with a multi-line error
  install_fail    the real install half-installs fastmcp-slim, then fails
  install_noop    the real install exits 0 but installs nothing
  install_broken  the real install exits 0 but fastmcp has no FastMCP
  block_restore   a failed install leaves a plain FILE where the remoteos/
                  package dir must be restored (the rollback cannot succeed)
  signal_parent   the real install half-installs, then sends this signal
                  (TERM / HUP / INT) to the installer and dies
  error_lines     how many numbered lines a failure prints
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from pathlib import Path

NEW_FASTMCP_SLIM = "4.0.3"
HALF_INSTALLED_FASTMCP_SLIM = "9.9.9"


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
    site: Path,
    name: str,
    version: str,
    files: dict[str, str],
    *,
    executable: tuple[str, ...] = (),
    installer: str = "pip",
    record: bool = True,
) -> Path:
    """Install a fake distribution: its files (paths relative to `site`, may
    start with `../`) plus a dist-info whose RECORD lists every one of them.
    `record=False` + `installer="debian"` is an apt-owned package."""
    info = site / f"{norm(name)}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "INSTALLER").write_text(f"{installer}\n")
    entries = []
    for rel, content in files.items():
        path = Path(os.path.normpath(site / rel))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if rel in executable:
            path.chmod(0o755)
        entries.append(f"{rel},,")
    if record:
        entries += [f"{info.name}/{f},," for f in ("METADATA", "INSTALLER", "RECORD")]
        (info / "RECORD").write_text("\n".join(entries) + "\n")
    return info


def dist_field(info: Path, key: str) -> str:
    for line in (info / "METADATA").read_text().splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1].strip()
    return ""


def find_dists(site: Path, name: str) -> list[Path]:
    return [d for d in sorted(site.glob("*.dist-info")) if norm(dist_field(d, "Name")) == norm(name)]


def _record_paths(info: Path) -> list[str] | None:
    record = info / "RECORD"
    if not record.exists():
        return None
    return [line.split(",", 1)[0] for line in record.read_text().splitlines() if line]


def _installer(info: Path) -> str:
    path = info / "INSTALLER"
    return path.read_text().strip() if path.exists() else ""


def _remove_dist(site: Path, info: Path) -> None:
    for rel in _record_paths(info) or []:
        path = Path(os.path.normpath(site / rel))
        if path.is_file() or path.is_symlink():
            path.unlink()
            parent = path.parent
            while parent != site and site in parent.parents and not any(parent.iterdir()):
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


def cmd_show(cfg: dict, site: Path, args: list[str]) -> int:
    with_files = "-f" in args or "--files" in args
    verbose = "-v" in args or "--verbose" in args
    names = [a for a in args if not a.startswith("-")]
    blocks, missing = [], []
    for name in names:
        dists = find_dists(site, name)
        if not dists:
            missing.append(name)
            continue
        info = dists[0]
        lines = [
            f"Name: {dist_field(info, 'Name')}",
            f"Version: {dist_field(info, 'Version')}",
            "Summary: fake",
            f"Location: {site}",
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


def cmd_uninstall(cfg: dict, site: Path, args: list[str]) -> int:
    for name in [a for a in args if not a.startswith("-")]:
        dists = find_dists(site, name)
        if not dists:
            print(f"WARNING: Skipping {name} as it is not installed.", file=sys.stderr)
            continue
        info = dists[0]
        version = dist_field(info, "Version")
        print(f"Found existing installation: {name} {version}")
        if _record_paths(info) is None:
            _cannot_uninstall(name, version, _installer(info))
            return 1
        _remove_dist(site, info)
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


def _half_install(site: Path) -> None:
    # pip installs dependencies one by one, so a mid-way failure leaves the
    # earlier ones in place: fastmcp-slim lands, then the install dies.
    version = HALF_INSTALLED_FASTMCP_SLIM
    write_dist(site, "fastmcp-slim", version, dist_files("fastmcp-slim", version))


def cmd_install(cfg: dict, site: Path, args: list[str]) -> int:
    ignore_installed = "--ignore-installed" in args or "-I" in args
    if "--dry-run" in args:
        if cfg.get("dry_run_fail"):
            _error(cfg, "dry-run")
            return 1
        _report(cfg, args)
        print("Would install " + " ".join(f"{n}-{v}" for n, v in _resolved(cfg)))
        return 0
    print("Collecting remoteos-mcp")
    if not ignore_installed:
        for pkg in cfg.get("debian_owned", []):
            print(f"Attempting uninstall: {pkg}", file=sys.stderr)
            _cannot_uninstall(pkg, "41.0.7", "debian")
            return 1
    if cfg.get("signal_parent"):
        _half_install(site)
        os.kill(os.getppid(), getattr(signal, f"SIG{cfg['signal_parent']}"))
        return 128 + int(getattr(signal, f"SIG{cfg['signal_parent']}"))
    if cfg.get("install_fail"):
        _half_install(site)
        if cfg.get("block_restore"):
            (site / "remoteos").write_text("a plain file where the remoteos package dir must go\n")
        _error(cfg, "install")
        return 1
    if cfg.get("install_noop"):
        print("Successfully installed nothing")
        return 0
    for name, version in _resolved(cfg):
        if not ignore_installed:
            # A plain upgrade uninstalls the old pip-owned copy first, as real pip does.
            for info in find_dists(site, name):
                if dist_field(info, "Version") != version:
                    _remove_dist(site, info)
        # With --ignore-installed real pip writes over the files and leaves any
        # other-version dist-info of the same name in place (a stale RECORD).
        write_dist(site, name, version, dist_files(name, version, importable=not cfg.get("install_broken")))
    print("Successfully installed " + " ".join(f"{n}-{v}" for n, v in _resolved(cfg)))
    return 0


def main(argv: list[str]) -> int:
    cfg = json.loads(Path(os.environ["FAKE_PIP_CONFIG"]).read_text())
    site = Path(cfg["site"])
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
    return handler(cfg, site, argv[1:]) if handler else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
