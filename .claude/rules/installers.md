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
  4. Plan and snapshot the RECORD files of the packages to be replaced: `remoteos-mcp`, `fastmcp`, `fastmcp-slim`, plus every `Installer: pip` dep whose pinned version changes.
  5. Arm the HUP/INT/TERM trap, then run `uninstall_overlapping`.
  6. Run the real install.
  7. Verify the version AND `import remoteos.__main__, fastmcp; from fastmcp import FastMCP`.

  A failure before step 5 changes nothing. A failure or interrupt after it runs `rollback_install`, which restores the snapshot and checks version + import, and the installer exits non-zero without touching the service. A failed rollback keeps the work dir and prints the restore command. Strih-pp was once left with no package, the service alive only from memory. That is what this order prevents.
- **`--ignore-installed` is required on Linux.** Debian's pip (Ubuntu 26.04+, Debian 13) refuses to uninstall apt-owned `python3-*` packages ("installed by debian"), so the pinned set must overlay them in `/usr/local`. This is safe only because everything pip owns and is about to replace is pre-uninstalled first. Otherwise its old dist-info + RECORD stays next to the new files: the dev14 stale-package problem, and the monolithic-fastmcp `__init__.py` deletion. Never drop the pre-uninstall, and never use `--force-reinstall` (static tests in `tests/test_installer_hardening.py`). Apt-owned deps (`Installer: debian`, no RECORD) are never uninstalled. macOS does not get the flag.
- **Never truncate or discard pip output** (`| tail -N || true`, `| Out-Null`). Capture it to a log, check pip's exit code (Windows: `$LASTEXITCODE`), and print the whole log on failure. A swallowed error hid the 26.04 cause.
- **macOS runs this under bash 3.2** (`curl | bash` uses `/bin/bash`):
  - No heredoc inside `$(…)`: bash 3.2 mis-parses quotes there. The helper is written to `$WORK_DIR/rollback.py` with a plain heredoc.
  - Expand a maybe-empty array as `${arr[@]+"${arr[@]}"}` (`set -u`).
  - No `mapfile`.
  - Check it with the `bash:3.2` container:

    ```
    docker run --rm -v <worktree>:/w:ro bash:3.2 sh -c 'ln -sf /usr/local/bin/bash /bin/bash; apk add -q python3 py3-pytest; adduser -D -u 1000 t; su t -c "cd /w && INSTALLER_TEST_BASH=/bin/bash python3 -m pytest -q --noconftest tests/test_installer_*.py"'
    ```
- **The interrupt handler can fire inside a redirected command**, e.g. `pip_install_remoteos >install.log`. So it re-points stdout/stderr to the console fds 3/4 that the block saved, and it ignores SIGPIPE, because the ssh pipe may be gone.
- **Tests.**
  - `tests/fake_pip.py` is a stateful Debian-pip stand-in: a fake site-packages with dist-info + INSTALLER + RECORD, and apt dists without a RECORD. It supports `--report`, `show -v -f`, and modes for a failing dry-run, a failing, no-op or broken install, a blocked restore, and a signal to the installer.
  - `installer_harness.setup_fake_pip` routes the `python3 -m pip` shim to it, and runs the import check against the fake site only (`-S -B`).
  - Assert on the real files (`FakePipBox.files()`) and on `work_dirs()`, not only on which commands ran.
  - The git shims answer `ls-remote`. A test must never reach the real git or the network.
- **Static checks find the first code line** (`_find_first_code_line`). It skips `#` and `Write-Host` lines, but NOT bash `echo` lines. In the bash installers, keep `pip install … remoteos-mcp` and `pip uninstall … remoteos-mcp` out of `echo` text, or those tests silently match the message instead of the command.
