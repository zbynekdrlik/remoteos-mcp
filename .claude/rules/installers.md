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

- **Install order (Linux and macOS, since 0.7.0.dev24, #12).** The order is:
  1. Record the installed version.
  2. Run the `pip install --dry-run` preflight through `pip_install_remoteos`, so it uses the real install's flags and `PIP_CONSTRAINT`. If it fails, the installer exits and nothing is touched.
  3. Snapshot the files `pip show -f remoteos-mcp fastmcp fastmcp-slim` lists (their RECORD).
  4. Run `uninstall_overlapping`.
  5. Run the real install. If it fails, `rollback_install` re-uninstalls the three packages, restores the snapshot byte for byte, and checks the old version is back.

  The installer exits non-zero after a rollback and never restarts the service. Keep this order. An uninstall before the preflight, or a failure path without `rollback_install`, can leave a box with no remoteos package. That happened on strih-pp: the service survived only from memory.
- **`--ignore-installed` is required on Linux.** Debian's pip (Ubuntu 26.04+, Debian 13) refuses to uninstall apt-owned `python3-*` packages ("installed by debian"). The pinned set must overlay them in `/usr/local`. This is safe only because `uninstall_overlapping` removes `remoteos-mcp`, `fastmcp` and `fastmcp-slim` first. The dev14 stale-package and monolithic-fastmcp-RECORD problems are why. Never drop the pre-uninstall, and never use `--force-reinstall` (static tests in `tests/test_installer_hardening.py`). macOS does not get the flag.
- **Never truncate or discard pip output** (`| tail -N || true`, `| Out-Null`). Capture it to a log, check pip's exit code, and print the whole log on failure. A swallowed error hid the 26.04 cause.
- **The rollback helper is a file, not `$(cat <<'PY' …)`.** It is written to `$WORK_DIR/rollback.py` with a plain heredoc. macOS runs `curl | bash` with bash 3.2, which mis-parses quotes inside a heredoc nested in `$(…)`.
- **Tests:** `tests/fake_pip.py` is a stateful Debian-pip stand-in: a fake site-packages with dist-info + RECORD, plus `debian_owned`, `dry_run_fail` and `install_fail` switches. `installer_harness.setup_fake_pip` routes the `python3 -m pip` shim to it. Assert on the real files (`FakePipBox.files()`), not only on which commands ran.
- **Static checks find the first code line** (`_find_first_code_line`). It skips `#` and `Write-Host` lines, but NOT bash `echo` lines. In the bash installers, keep `pip install … remoteos-mcp` and `pip uninstall … remoteos-mcp` out of `echo` text, or those tests silently match the message instead of the command.
