"""Fake pip for the installer tests — a stateful stand-in for Debian's pip.

The python3 shim routes `python3 -m pip <args>` to `fake_pip.py <args>`.  State
lives in a fake site-packages dir: each installed distribution is a
`<name>-<version>.dist-info/` with METADATA + RECORD — the same files real pip
reads for `show` and `uninstall`, so the installer's snapshot/rollback works on
it exactly as on a real box.

Behaviour comes from the JSON file named by $FAKE_PIP_CONFIG:

  site          fake site-packages dir
  log           JSON-lines log of every call (argv, PIP_CONSTRAINT, TMPDIR)
  new_version   the remoteos-mcp version a successful install lands
  debian_owned  dependencies apt owns: a real install over them WITHOUT
                --ignore-installed fails like Debian's pip ("installed by debian")
  dry_run_fail  the --dry-run preflight fails with a multi-line error
  install_fail  the real install half-installs fastmcp-slim, then fails
  error_lines   how many numbered lines a failure prints
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

NEW_FASTMCP_SLIM = "4.0.3"
HALF_INSTALLED_FASTMCP_SLIM = "9.9.9"


def norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def write_dist(
    site: Path,
    name: str,
    version: str,
    files: dict[str, str],
    *,
    executable: tuple[str, ...] = (),
) -> Path:
    """Install a fake distribution: its files (paths relative to `site`, may
    start with `../`) plus a dist-info whose RECORD lists every one of them."""
    info = site / f"{norm(name)}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "INSTALLER").write_text("pip\n")
    record = []
    for rel, content in files.items():
        path = Path(os.path.normpath(site / rel))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if rel in executable:
            path.chmod(0o755)
        record.append(f"{rel},,")
    record += [f"{info.name}/{f},," for f in ("METADATA", "INSTALLER", "RECORD")]
    (info / "RECORD").write_text("\n".join(record) + "\n")
    return info


def dist_field(info: Path, key: str) -> str:
    for line in (info / "METADATA").read_text().splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1].strip()
    return ""


def find_dists(site: Path, name: str) -> list[Path]:
    return [d for d in sorted(site.glob("*.dist-info")) if norm(dist_field(d, "Name")) == norm(name)]


def _record_paths(info: Path) -> list[str]:
    return [line.split(",", 1)[0] for line in (info / "RECORD").read_text().splitlines() if line]


def _remove_dist(site: Path, info: Path) -> None:
    for rel in _record_paths(info):
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


def _debian_refusal(pkg: str) -> None:
    print(f"Attempting uninstall: {pkg}", file=sys.stderr)
    print("error: uninstall-no-record-file", file=sys.stderr)
    print(f"× Cannot uninstall {pkg} 41.0.7", file=sys.stderr)
    print(f"╰─> The package's contents are unknown: no RECORD file was found for {pkg}.", file=sys.stderr)
    print("hint: The package was installed by debian. You should check if it can uninstall the package.",
          file=sys.stderr)


def cmd_show(cfg: dict, site: Path, args: list[str]) -> int:
    with_files = "-f" in args or "--files" in args
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
        if with_files:
            lines.append("Files:")
            lines += [f"  {rel}" for rel in _record_paths(info)]
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
        if norm(name) in {norm(p) for p in cfg.get("debian_owned", [])}:
            _debian_refusal(name)
            return 1
        for info in dists:
            version = dist_field(info, "Version")
            print(f"Found existing installation: {name} {version}")
            _remove_dist(site, info)
            print(f"  Successfully uninstalled {name}-{version}")
    return 0


def cmd_install(cfg: dict, site: Path, args: list[str]) -> int:
    new_version = cfg["new_version"]
    ignore_installed = "--ignore-installed" in args or "-I" in args
    if "--dry-run" in args:
        if cfg.get("dry_run_fail"):
            _error(cfg, "dry-run")
            return 1
        print(f"Would install fastmcp-slim-{NEW_FASTMCP_SLIM} remoteos-mcp-{new_version}")
        return 0
    print("Collecting remoteos-mcp")
    if not ignore_installed:
        for pkg in cfg.get("debian_owned", []):
            _debian_refusal(pkg)
            return 1
    if cfg.get("install_fail"):
        # pip installs dependencies one by one, so a mid-way failure leaves the
        # earlier ones in place: fastmcp-slim lands, then the install dies.
        write_dist(site, "fastmcp-slim", HALF_INSTALLED_FASTMCP_SLIM,
                   {"fastmcp/__init__.py": f"# fastmcp-slim {HALF_INSTALLED_FASTMCP_SLIM}\n"})
        _error(cfg, "install")
        return 1
    write_dist(site, "fastmcp-slim", NEW_FASTMCP_SLIM, {"fastmcp/__init__.py": f"# fastmcp-slim {NEW_FASTMCP_SLIM}\n"})
    write_dist(site, "remoteos-mcp", new_version, {"remoteos/__init__.py": f"# remoteos {new_version}\n"})
    print(f"Successfully installed fastmcp-slim-{NEW_FASTMCP_SLIM} remoteos-mcp-{new_version}")
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
