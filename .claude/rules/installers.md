---
paths:
  - "install-linux.sh"
  - "install.sh"
  - "install.ps1"
  - "tests/test_installer_*.py"
  - "tests/installer_harness.py"
  - "tests/fake_pip.py"
---

# One-liner installers (install-linux.sh / install.sh / install.ps1) — gotchas

The live-deploy procedure lives in the `/remoteos-install` skill. This file covers the code.

- **One shared install block.** `install-linux.sh` and `install.sh` carry the same `# >>> shared install block` … `# <<< shared install block`, byte for byte (`TestSharedInstallBlock` guards it). Edit it in one file and copy it to the other. Only `PIP_FLAGS` (Linux adds `--ignore-installed`) and `SERVICE_DESC` sit outside it.
- **Order inside the block (since 0.7.0.dev24, #12). Keep it.**
  1. Pin ONE commit of main (`git ls-remote`). Code `@<sha>` and `PIP_CONSTRAINT` `raw…/<sha>/constraints.txt` come from it. `raw.githubusercontent.com` caches `…/main/` for minutes.
  2. Record the installed version.
  3. Run the `pip install --dry-run --report` preflight through `pip_install_remoteos`, so it uses the same flags as the real install.
  4. Plan: decide which deps are *changed* (pip-owned, pinned version differs) and which are *added* (not pip-owned yet). Abort if remoteos-mcp, fastmcp or fastmcp-slim has no RECORD.
  5. Snapshot the RECORD files of the three plus the changed deps. Abort if a RECORD does not list its own METADATA.
  6. **ARM** (`ROLLBACK_ARMED=1`, HUP/INT/TERM → `exit`, SIGPIPE ignored).
  7. Pre-uninstall.
  8. Install.
  9. Verify the version AND `import remoteos.__main__, fastmcp; from fastmcp import FastMCP`.
  10. **DISARM**.

  Before ARM nothing has changed. While armed, **any** exit goes through the EXIT trap (`on_exit` → `rollback_install`): a failed step, a signal, a lost console (the write fails, set -e exits), or a truncated download. The rollback removes every visible pip-owned copy of the three, the changed and the added deps, one name per pip call, then restores the snapshot and re-checks version + import. The installer exits non-zero and never touches the service. A failed rollback keeps the work dir and prints the restore command + log.
  - Never arm on a list of signals alone. A no-tty `ssh … | sudo bash` drop sends no SIGHUP; bash just dies on SIGPIPE.
  - Never let pip write to the console: pip writes to logs, and the logs are printed afterwards.
  - Strih-pp was once left with no package, the service alive only from memory. That is what this order prevents.
- **The rollback helper reads dists through `importlib.metadata`** (RECORD + INSTALLER, the visible dist first on sys.path, which is what pip sees). It never parses `pip show` text: real pip prints a multi-line License raw, including a literal `---` and a `Name:` line. "pip-owned" = INSTALLER `pip` + a RECORD. Apt dists (`debian`, no RECORD) and other installers (`uv`) are never uninstalled.
- **`--ignore-installed` is required on Linux.** Debian's pip (Ubuntu 26.04+, Debian 13) refuses to uninstall apt-owned `python3-*` packages ("installed by debian"), so the pinned set must overlay them in `/usr/local`. This is safe only because the block pre-uninstalls every pip-owned copy it is about to replace. Otherwise the old dist-info + RECORD stays next to the new files: the dev14 stale-package problem, and the monolithic-fastmcp `__init__.py` deletion. Never drop the pre-uninstall, and never use `--force-reinstall` (static tests in `tests/test_installer_hardening.py`). macOS does not get the flag.
- **Never truncate or discard pip output** (`| tail -N || true`, `| Out-Null`). Capture it to a log, check pip's exit code (Windows: `$LASTEXITCODE`), and print the whole log on failure.
- **macOS runs this under bash 3.2** (`curl | bash` uses `/bin/bash`):
  - No heredoc inside `$(…)`: the helper is written to `$WORK_DIR/rollback.py` with a plain heredoc.
  - Expand a maybe-empty array as `${arr[@]+"${arr[@]}"}` (`set -u`).
  - No `mapfile`.
  - Check it with the `bash:3.2` container. `RealMetadata` needs a pip-installed pytest, and apk's has no INSTALLER:

    ```
    docker run --rm -v <worktree>:/w:ro bash:3.2 sh -c 'ln -sf /usr/local/bin/bash /bin/bash; apk add -q python3 py3-pytest; adduser -D -u 1000 t; su t -c "cd /w && INSTALLER_TEST_BASH=/bin/bash python3 -m pytest -q --noconftest -k \"not RealMetadata\" tests/test_installer_*.py"'
    ```
- **Tests.**
  - `tests/fake_pip.py` is a stateful Debian-pip stand-in with TWO site dirs: pip's (`/usr/local`) shadows the apt one behind it. It supports `--report`, `show -v -f`, an uninstall that aborts the list at the first refusal like real pip, and modes for a failing dry-run, a failing, delayed, no-op or broken install, `fail_after`, a blocked restore, a damaged snapshot, and a signal to the installer.
  - `installer_harness.setup_fake_pip` routes `-m pip` to it. It runs the import check and `…/rollback.py` with only the box's site dirs on sys.path (`-S -B`).
  - `start_installer` lets a test close the installer's console.
  - Assert on the real files (`FakePipBox.files()`) and on `work_dirs()`, not only on which commands ran. `TestRollbackHelperOnRealMetadata` checks the helper against the test interpreter's own real dists (read-only).
  - The git shims answer `ls-remote`. A test must never reach the real git or the network.
- **Static checks find the first code line** (`_find_first_code_line`). It skips `#` and `Write-Host` lines, but NOT bash `echo` lines. In the bash installers, keep `pip install … remoteos-mcp` and `pip uninstall … remoteos-mcp` out of `echo` text, or those tests silently match the message instead of the command.
