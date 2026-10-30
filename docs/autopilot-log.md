# Autopilot Log

Terse per-ticket record of autonomous cycles (decisions, commits, tests, PR).

## 2026-09-09 — #13 Add GitHub Actions CI

- **Issue:** #13 Add GitHub Actions CI: ruff lint + pytest + dev>main version-check (repo has no workflows).
- **Commits:** `6f6ef37` version bump 0.7.0.dev9; `a18df21` ci.yml with lint/test/version-check jobs (#13); `3921304` fix pre-existing ruff I001 import sorting.
- **Files added:** `.github/workflows/ci.yml` (69 lines) — 3 jobs: lint (ruff check), test (pytest), version-check (PR-only, packaging.version comparison).
- **Key decisions:** Single workflow file (vs separate per-job — unnecessary at this scale). Concurrency group added to auto-cancel superseded runs. Python 3.12 on ubuntu-latest. Version-check uses `packaging.version.Version` for PEP 440 comparison.
- **Local verification:** ruff check: 0 errors (after fixing pre-existing I001). pytest: 71 passed in 4.45s.
- **CI fix (post-merge):** First CI run failed: `test_detect_non_root_still_uses_env` raised `PermissionError: /home/packer/.Xauthority` on GitHub runner. Root cause: `_guess_xauthority` calls `Path(c).exists()` which does NOT ignore EACCES (errno 13); on the runner uid 1000 = `packer` whose home is mode 0700. Fix: `3939e6a` [red] regression test + autouse env-cleaning fixture; `591e581` [green] catch OSError in `_guess_xauthority`. 72 tests pass.

## 2026-07-03 — #6 Linux X11 desktop provider

- **Issue:** #6 Implement Linux desktop provider (X11) — replace headless stub with real X11 backends, gated on runtime session detection.
- **Commits (dev):** `1e4f75d` version bump 0.7.0.dev6; `d5a8f3b` X11 provider + session detection + tiers/main/ocr wiring + installer + tests (Closes #6); `d8aab14` fix set_clipboard xclip hang (discard_output); `e739644` fix launch_app hang (spawn detached).
- **PR:** #5 (dev→main), merge commit `8395d4e`. (PR #5 pre-existed for dev→main — repurposed to carry the X11 work + the already-on-dev playbook/SIGPIPE commits; body carries `Closes #6`.)
- **Tests:** `tests/test_linux_desktop.py` (48). Session-detection logic, headless-regression contract (every fn returns stub, never shells out), X11 argv construction, + guards for the two live-found bugs.
- **Key decisions:**
  - Input (Click/Type/Scroll/Move/Shortcut) is dispatched from `__main__.py` via pyautogui, which is NOT imported on Linux. Route Linux input to the provider (xdotool) with an `if is_linux()` branch per handler; Win/Mac pyautogui path untouched.
  - `loginctl show-session` returns an EMPTY `Display=` for X11 sessions → resolve DISPLAY/XAUTHORITY from `/proc/<pid>/environ` of a session-owned process (gnome-shell). This is the reliable source.
  - Service may run as the session user (imag-nb: `User=newlevel`) OR root (cam1: `User=root`). `_build_invocation` injects env when non-root, wraps `sudo -u` when root.
  - `LINUX_EXCLUDED_TOOLS` split into `LINUX_ALWAYS_EXCLUDED_TOOLS` (Reg*, ReconnectSession, ScreenRecord, Scrape, AnnotatedSnapshot) + `LINUX_DISPLAY_TOOLS` (enabled only when X11 detected). Headless union is byte-identical to before (24/44 tools).
  - Two live-only gotchas: xclip (set) forks to stay resident → captured pipe blocks `subprocess.run` → send output to DEVNULL. Launching a GUI app blocks `subprocess.run` forever → spawn detached (`_spawn_x`, Popen start_new_session, wait(settle) → None=still-running=success).
  - OCR on Linux: screenshot via provider (scrot) → `tesseract` CLI (no PIL/pytesseract needed). Pillow added as a Linux dep (screenshot resize/JPEG).
- **Live verification:** imag-nb (X11, 10.77.9.182) — screenshot 1920x1080/1280x720, clipboard round-trip exact token, windows listed, launch_app→gnome-text-editor, Type wrote text (OCR read it back), notify shown; service `[tools: 38/44]`, `/health`=0.7.0.dev6. cam1 (headless, root) — `[tools: 24/44]`, all desktop fns return exact stub, no crash. Zero regression.
- **Follow-ups (NOT dropped):** Wayland backend is tier-3, tracked as a non-goal in #6. AnnotatedSnapshot (AT-SPI accessibility tree) filed as #7; ScreenRecord (ffmpeg x11grab) filed as #8 — both left excluded on Linux for now.

## 2026-07-03 — #7 AnnotatedSnapshot (AT-SPI) + #8 ScreenRecord (ffmpeg x11grab)

- **Issues:** #8 Linux ScreenRecord via ffmpeg x11grab; #7 Linux AnnotatedSnapshot via AT-SPI accessibility tree. Both follow-ups to #6, bundled in ONE PR.
- **Commits (dev):** `766c6ef` bump 0.7.0.dev7; `16155b1` ScreenRecord (Closes #8); `8b65d1a` AnnotatedSnapshot AT-SPI (Closes #7); `4e79a8d` AT-SPI screen-coord fix (window content offset, part of #7).
- **Tests:** `tests/test_linux_desktop.py` now 61 (was 48). #8: ffmpeg argv (full/region/clamp), headless-stub-no-shell, x11 run+b64, empty-output guard. #7: `_collect_actionable` tree→element mapping (actionable/visible/valid filtering + max_elements), AT-SPI unavailable/no-frame/collect-from-frame, `_content_origin` CSD-margin + no-decoration.
- **Key decisions / gotchas (all live-found on imag-nb):**
  - **ScreenRecord** = `provider.record_screen()` → base64 animated GIF (same return shape as `recording.record_screen`, so the shared tool is platform-agnostic). ffmpeg x11grab via `_run_x` (session wrap). Full-screen omits `-video_size` (auto-detect); region = `-video_size WxH -i :0+X,Y`; `-vf fps=N,scale=W:-2:flags=lanczos`. Headless → raises stub, never shells out.
  - **AnnotatedSnapshot** = `provider.get_interactive_elements()` walks AT-SPI2 in-process via GI. `_load_atspi()` MUST inject `DBUS_SESSION_BUS_ADDRESS=/run/user/<uid>/bus` (+DISPLAY/XAUTHORITY) — a systemd service has none, without it the tree is empty.
  - **GTK4 reports `CoordType.SCREEN` extents as (0,0)** (gtk-at-spi limitation) → all elements at origin. FIX: read `CoordType.WINDOW` extents + the focused window's content origin = xdotool `getactivewindow` (X,Y) + CSD shadow margin `(outer−frame)//2` (~61px GTK4, ~0 SSD). `screen_rect = WINDOW + content_origin`. Verified boxes land exactly on gnome-calculator buttons.
  - Base image via provider `capture_png` (scrot), NOT `ImageGrab.grab()` (fails for the service). Shared `_draw_annotations` helper for Win + Linux (no duplication). Degrades gracefully (Qt/Electron expose little → plain screenshot + whatever AT-SPI gives, never fabricates).
  - `tiers`: moved ScreenRecord + AnnotatedSnapshot ALWAYS→DISPLAY. X11 now 40/44; headless union still 24/44 (unchanged).
  - Installer graphical branch adds `ffmpeg python3-pyatspi gir1.2-atspi-2.0 python3-gi`.
- **Live verification:** imag-nb (X11) — dev7, `[tools: 40/44]`, `Linux session: x11`. ScreenRecord: 3s@5fps → valid GIF89a 640x360 **15 frames** 3000ms. AnnotatedSnapshot (real `__main__` tool path): gnome-calculator → **34 elements**, boxes pixel-aligned on every button (C ( ) mod π, 7-9, 4-6, 1-3, 0 . = + − ×, etc.). cam1 (headless, root) — dev7, `[tools: 24/44]` unchanged, both tools return the exact stub, `/health`=0.7.0.dev7, service healthy. Zero regression.
- **Follow-ups:** none dropped. Wayland still a non-goal.

## 2026-09-09 — #12 Pin transitive dependencies

- **Issue:** #12 Pin transitive dependencies in pyproject.toml (currently >= floors) for reproducible fleet installs.
- **Commits:** `675e664` version bump 0.7.0.dev10; `f870e0d` pin deps + constraints.txt + installer PIP_CONSTRAINT + master→main fix + tests (#12).
- **Tests:** `tests/test_dependency_pins.py` (6 new): exact-pin format, no-floor-pins, constraints file existence/format/direct-dep coverage. Total: 77 pass, ruff clean.
- **Key decisions:**
  - Two-layer pinning: direct deps exact `==` in pyproject.toml + committed `constraints.txt` (uv-generated, universal cross-platform) for transitive graph.
  - All 3 installers set `PIP_CONSTRAINT` env var to raw GitHub URL of constraints.txt before pip install.
  - install.ps1 `archive/master.zip` returns HTTP 302→main; corrected all `master` refs to `main` across all installers, uninstallers, and README.
  - Clean-venv install verified: `PIP_CONSTRAINT=constraints.txt pip install . && pip check` → clean.

## 2026-09-09 — Post-PR#14 deploy verification + skill fix

- **Task:** Deploy v0.7.0.dev10 to 3 platforms (cam1, resolume, mac-propresenter) and verify PIP_CONSTRAINT + pinned deps. Fix install skill `master` → `main` URLs.
- **Deploy verification:** BLOCKED — all 3 LAN targets (10.77.9.61, 10.77.9.201, 10.77.9.203) unreachable from dev1 (no ping, no SSH, no HTTP). macOS tailscale offline 4d.
- **Skill fix:** `.claude/skills/install/SKILL.md` had `master` in all 3 one-liner URLs; fixed to `main`. Also un-gitignored `.claude/skills/` so skills are version-controlled (previously gitignored, unavailable in clones/worktrees).
- **Commits:** `3cabcb3` version bump 0.7.0.dev11; `ea25b9b` fix master→main + track skills in git.
- **Tests:** 78/78 pass, ruff clean.

## 2026-09-09 — #12 Installer hardening (ro-rootfs + version self-check)

- **Task:** Harden install-linux.sh and install.sh (macOS) after live deploy on cam1 surfaced two gaps: no ro-rootfs detection and no post-install version verification.
- **Commits:** `fdbdf1e` version bump 0.7.0.dev13; `870dcdf` [red] 3 regression tests; `c7e5bff` [green] ro pre-flight + version self-check + pip TMPDIR.
- **Tests:** `tests/test_installer_hardening.py` (3 new): ro-rootfs abort, Linux version mismatch, macOS version mismatch. Total: 81 pass, ruff clean.
- **Changes:**
  - `install-linux.sh`: PRE-FLIGHT ro-rootfs check via `findmnt -no OPTIONS /`; pip install uses fresh `mktemp -d` TMPDIR; POST-INSTALL polls `/health` and compares version to `pip show`.
  - `install.sh`: POST-INSTALL version self-check (same as Linux).
  - `.claude/skills/install/SKILL.md`: gotcha about cam boxes booting with read-only rootfs.
- **Skipped:** `install.ps1` version self-check (not trivially portable to PowerShell — listed as follow-up candidate).

## 2026-09-09 — #12 Installer --force-reinstall fix + deploy dev13

- **Task:** Deploy 0.7.0.dev13 to imag.lan, cam1.lan, cam2.lan. Fix installer bug discovered during deploy.
- **Bug found:** Installer's `--ignore-installed` flag does not force pip to re-clone from git URL when an older version is already installed. On imag.lan (had dev8) and cam2.lan (had dev12), the canonical installer installed the OLD version instead of current main (dev13).
- **Fix:** Replaced `--ignore-installed` with `--force-reinstall` in all 3 installers; pinned `@main` in git URL for Linux/macOS.
- **Commits:** `7387132` version bump 0.7.0.dev14; `9507976` [red] 4 static analysis tests; `5c39537` [green] fix: --force-reinstall in all installers.
- **Tests:** 85/85 pass, ruff clean.
- **Deploy results:** All 3 boxes at 0.7.0.dev13, health OK, MCP handshake 200, systemctl active, tokens preserved.

### 2026-09-10 — #12 install.ps1 self-check + fastmcp repair + Windows fleet deploy (0.7.0.dev15)
- **Part A (deploy):** Deployed 0.7.0.dev14 (current main) to resolume.lan and iem.lan via canonical one-liner. Found and worked around fastmcp corruption bug on both boxes (manual `pip install --force-reinstall fastmcp==4.0.3`). Both boxes verified: /health = dev14, MCP 200, auth keys preserved.
- **Part B (code):** Added post-install health/version self-check to install.ps1 (Invoke-RestMethod, 6x5s poll, exit 1 on mismatch/timeout). Added fastmcp import repair step after pip install. Version reads via importlib.metadata.
- **Bug found:** `pip install --force-reinstall` from archive URL (`main.zip`) on Windows corrupts fastmcp's `__init__.py` — leaves a namespace package. Repair: targeted reinstall of fastmcp after main install.
- **Commits:** `d0fe5a0` version bump 0.7.0.dev15; `ba6482b` [red] 4 structural tests; `9bc4fad` [green] fix: self-check + fastmcp repair.
- **Tests:** 89/89 pass, ruff clean. pwsh not available locally — no hermetic behavioral test for install.ps1.

### 2026-10-14 — #25 MCP gateway on dev1 (remoteos-gateway, 0.7.0.dev21)
- **What:** new `src/remoteos/gateway/` (registry, accesslog, app, cli) + console script `remoteos-gateway` + `deploy/remoteos-gateway.service` (user unit) + README "MCP gateway (dev1)". Transparent streaming reverse proxy, per-consumer sha256 tokens, machine key read from `~/.claude/mcp-configs/remoteos-mcp.mcp.json` (never copied), 401→403→404 order, 503 JSON-RPC `-32000` when a machine is down, JSONL access log without token/args.
- **Decisions (on #25):** HTTP client = `httpx2==2.12.0` (fastmcp/mcp's own runtime client; plain httpx is test-only); `starlette==1.6.0` + `uvicorn==0.52.4` pinned directly; constraints.txt regenerated (only `# via` comments changed).
- **Commits:** `4cb648b` version bump 0.7.0.dev21; `840e39e` feat + 32 hermetic tests (in-process fake machine; chunk-by-chunk SSE proof; disconnect cleanup; SIGHUP forced reload). Mutation probe: 8/8 mutants killed.
- **Not done here:** install on dev1 + `expose win-pjsnvs-pokladna` + consumer hand-off (airuleset#1243) — supervisor rollout; #25 stays open until then.

### 2026-10-14 — #28 coordinate contract on all platforms (0.7.0.dev22)
- **What:** one stateless frame contract: `src/remoteos/coords.py` (pure Frame math, round-half-up, clamp), `src/remoteos/snapshot.py` (fit/encode/listings/annotations in image px), providers' `capture_geometry` + `capture_frame` (Win virtual screen + all_screens bbox, Linux X root + xrandr, macOS points vs mode pixels), Snapshot/AnnotatedSnapshot contract line, Click/Move/drag/Scroll/Type take image px (`monitor`/`max_width`/`coords`), Windows per-monitor-v2 DPI before pyautogui (`platform/win/dpi.py`), installer adds `x11-xserver-utils`.
- **Decisions (on #28):** AnnotatedSnapshot shares Snapshot's frame + gains `monitor`; macOS monitor=0 = main display; image size from geometry, not capture; replies carry `[image → screen; scale]`; off-frame entries marked.
- **Commits:** `cd51b7d` bump 0.7.0.dev22; `9af95dd` [red] tool-level fake-X11 + pure tables; `c2b024e` [green] fix; `f116623` provider geometry tests on fake Win32/Quartz; review round 1 `f198355` [red] / `c7b348f` [green] (Linux monitor=0 = X root via `xrandr --current`: `xdotool getdisplaygeometry` is only the first Xinerama head); round 2 `b5991bd` [red] / `062b9cd` [green] (DPI read-back tells preset v2 from v1, error-path tests).
- **Tests:** 246 pass, ruff clean. Live read-only on stream-pp / strih-snv / strih-pp: xrandr present, `current WxH` == scrot capture size. Live check (Move + GetCursorPos on a >1280 px Windows box) = supervisor after canonical install.

## #25 reopened — gateway 503 RemoteProtocolError on stale keep-alive connections (0.7.0.dev23)

- **Root cause:** the upstream httpx2 pool kept idle connections for 5 s, the same as the machines' uvicorn `timeout_keep_alive`. A call after ~4.9 s idle reused a connection the machine was closing.
- **Fix:** `UPSTREAM_LIMITS.keepalive_expiry=2.0` (caps kept at 100/20). Plus ONE retry in `Gateway._send_upstream` for RemoteProtocolError/ReadError/WriteError before headers, only on a REUSED pooled connection (httpcore `trace`), sent through a no-keep-alive `retry_client` so it always dials.
- **Commits:** `61bc08b` bump; `2e1ab54` [red]; `f01a11c` [green]; `c84faa8` test split (test_gateway.py was 1179 lines); review fixes (fresh-connection retry client, invariants documented, rule paths) after one review pass (0🔴 2🟡 3🔵).
- **Tests (RED→GREEN):** `test_machine_closing_a_pooled_connection_as_the_request_arrives_is_retried_not_503`, `test_one_stale_connection_failure_before_the_response_is_retried_once[*]`, `test_two_consecutive_stale_connection_failures_are_503_after_exactly_one_retry`, `test_idle_pooled_connections_expire_well_before_the_machines_keep_alive`, `test_the_production_client_drops_a_pooled_connection_after_2s_idle`.
- **Loopback sweep:** real uvicorn + 60 ms latency proxy. Pre-fix: 503 at 4.88 s / 4.92 s idle. Retry alone: 4 retries, all 200. Production fix: 0 retries, all 200.

## #12 — installers on Ubuntu 26.04 / Debian-owned deps: preflight, rollback, full pip error (0.7.0.dev24)

- **Root cause (strih-pp, Python 3.14, Debian pip 25.1.1):** the pinned set must replace apt `python3-*` packages (cryptography, PyJWT, psutil, …). Debian's pip refuses ("installed by debian"). `| tail -3 || true` hid the error, and the pre-uninstall had already removed remoteos, so the box was left without a package.
- **Fix:**
  - Linux: `--ignore-installed`, safe because the pre-uninstall of remoteos-mcp, fastmcp and fastmcp-slim stays.
  - Linux and macOS: a `pip install --dry-run` preflight with the same flags and `PIP_CONSTRAINT`, run before any uninstall. A RECORD-file snapshot of the three dists. On failure, `rollback_install` restores the old version byte for byte, offline, and the installer still exits non-zero. Full pip output on failure.
  - `install.ps1`: the install output is captured and printed on failure (error-output parity only).
- **Decision (on #12, ROZHODNUTÉ):** the rollback restores local files. A networked reinstall of the old commit or tag would re-resolve and rebuild everything, so it would fail the same way the install just did.
- **Commits:** `e999e4c` bump; `e147d68` retire the dev14 no-`--ignore-installed` static test (the design reverses it); `71fd468` move shims to `tests/installer_harness.py`; `306674b` [red] 9 tests + `tests/fake_pip.py` (stateful Debian-pip fake); `7cc8be3` [green].
- **Tests (RED→GREEN):** `test_upgrade_over_debian_owned_dependencies_succeeds_with_ignore_installed`, `test_failed_dry_run_aborts_before_any_uninstall_and_prints_the_full_error` (Linux + macOS), `test_failed_install_after_pre_uninstall_restores_the_recorded_version`, `test_failed_install_prints_the_full_pip_output`, `test_failed_install_on_a_fresh_box_exits_nonzero_with_the_full_error`, `test_upgrade_runs_preflight_then_pre_uninstall_then_install`, `test_failed_install_restores_the_recorded_version_and_prints_the_full_error`, `test_main_pip_install_output_is_kept_and_printed_on_failure`. Result: 269 pass, ruff clean, shellcheck clean at `-S warning`. Three SC2004 style notes on main were outside the diff; round 2 cleared them.
- **Real-pip check:** in a pip 24.0 venv, snapshot → uninstall → restore of tabulate gave byte-identical files. The console script stayed +x, and pip show, import and `pip check` were all OK.
- **Review round 1:** 0🔴 3🟡 9🔵, all fixed in this branch except Windows rollback (out of scope; follow-up candidate).
  - Commits: `75a051d` [red] (16 fail), `a9f08ba` + `5b3a6e7` harness fixes (no `__pycache__` in the fake site; stateless shims answer the import check), `0c85cca` [green].
  - One shared, byte-identical install block in install-linux.sh and install.sh (drift-guarded).
  - One commit of main from `git ls-remote` for the preflight, the install `@<sha>` and the constraints.
  - Changed pip-owned deps join the pre-uninstall and the snapshot. `--ignore-installed` otherwise leaves stale dist-info.
  - An installed remoteos without RECORD aborts the run.
  - A HUP/INT/TERM trap rolls back.
  - A post-install import check, which the rollback also runs.
  - A failed rollback keeps the snapshot.
  - `install.ps1` checks `$LASTEXITCODE`.
  - Result: 285 pass, shellcheck clean at default severity. All 42 installer tests also pass under bash 3.2.57 (`bash:3.2` container, uid 1000). Real pip 24.0 check of `names`/`plan`/`snapshot`/`restore` OK.
- **Review round 2:** 0🔴 2🟡 8🔵, all fixed in this branch except the Windows rollback (follow-up candidate).
  - Commits: `d053f84` [red] (9 fail), `54fdbd8` [green].
  - The rollback is armed by state (EXIT trap + `ROLLBACK_ARMED`, SIGPIPE ignored while armed). A lost console (no-tty ssh drop) or a truncated download now rolls back. Before, bash died on SIGPIPE and the snapshot was deleted.
  - The helper reads `importlib.metadata` (RECORD + INSTALLER) instead of parsing `pip show`. Real pip prints a multi-line License with a raw `---` and a `Name:` line.
  - The rollback removes every visible pip-owned copy of the three, the changed and the added deps, one per pip call, so an apt refusal cannot skip the rest.
  - plan and snapshot abort when a RECORD is missing or does not list its own METADATA.
  - The fake box got two site dirs (pip over apt).
  - Result: 297 pass. 52/52 shell-dependent installer tests pass under bash 3.2.57. Real pip 24.0 check of plan/pipowned/snapshot/restore OK.
- **Review round 3:** 0🔴 1🟡 10🔵.
  - Commits: `4500cab` [red] (10 fail), `238f760` [green].
  - pip is shielded from group signals while armed (it no longer dies mid-wheel).
  - A `leftovers` check runs after the rollback.
  - A pre-uninstall failure is fatal.
  - restore refuses a directory in the way.
  - Deps owned by another installer abort the upgrade.
  - Both installers run inside `main()`, so a truncated download fails to parse.
  - macOS auth key fixed: `tr | head` died of SIGPIPE, so a fresh mac aborted at [3/5]. The harness had hidden it.
  - Result: 304 pass, 59/59 shell tests pass under bash 3.2.57.
  - Left for follow-up: a CI job for the bash-3.2/shellcheck checks, the skill recovery section, the Windows rollback, venv migration, `PYTHONNOUSERSITE` in the unit.
- **Not done here:** running the installer on strih-pp and stream-pp (26.04). The supervisor deploys and verifies.

## #29 — Scroll = wheel clicks + direction on every OS; native wheel events, no pyautogui (0.7.0.dev25)

- **Root cause:** Windows and macOS `Scroll` called `pyautogui.scroll`/`hscroll`.
  - On Windows, 0.9.54 sends the raw count as the wheel delta (`_pyautogui_win.py:539`): 3 = 3/120 of a notch.
  - Its `_hscroll` calls the vertical `_scroll` (`:544-557`).
  - Each OS used a different unit.
- **Fix (Approach 1 of the main session's design):** a pure `src/remoteos/scroll.py` (`wheel_steps`, `normalize_request`, `chunks`). Every provider has `scroll(direction, clicks, x=None, y=None)`.
  - Windows: one `SendInput`, `MOUSEEVENTF_WHEEL`/`HWHEEL`, ±clicks×120, `SetCursorPos` first.
  - macOS: Quartz line events, ≤10 per event. wheel2 positive = LEFT, so `right` is negative.
  - Linux: xdotool 4/5/6/7, unchanged.
  - The tool keeps the #28 mapping and the legacy signed `amount` + `horizontal`. An explicit `direction` wins. Clamped to 1..100.
- **Decisions (on #29):** the three Anchors-confirmed ROZHODNUTÉ items: macOS wheel2 sign (Hammerspoon/SFML evidence), ≤10 lines per Quartz event (Apple doc), the provider `is not None` point check.
- **Commits:** `b28bb97` bump; `ba8b96a` [red] (30 fail); `618e589` [green].
  - Review round 1 (0🔴 1🟡 7🔵): `be93035` [red] (4 fail), `d5e5437` [green]. No GetLastError/UIPI claims in errors; macOS builds all events before posting; unused dy/dx dropped; headless Scroll test.
  - Not changed, deliberately: one ±clicks×120 event (the design + the live `WH_MOUSE_LL` check expect −360 as one event), and the tool-level `0 = no point` convention (shared with Type).
- **Tests (RED→GREEN):** `test_windows_legacy_horizontal_scroll_is_horizontal`, `test_windows_scroll_sends_one_wheel_event[*]`, `test_windows_scroll_tool_maps_the_point_and_sends_the_wheel`, `test_mac_scroll_posts_line_events[*]`, `test_mac_legacy_horizontal_scroll_is_horizontal`, `test_x11_scroll_direction_picks_the_wheel_button[*]`, `test_scroll_legacy_horizontal_routes_to_the_horizontal_buttons`, `test_wheel_steps_table[*]`.
  - Result: 363 pass, ruff clean.
- **Not done here:** the live Windows check (a `WH_MOUSE_LL` hook expects −360 for down 3 and +240 for right 2). The supervisor deploys and verifies. No macOS box was reachable; the mac sign is source-evidenced.

## #30 — Unicode / Slovak diacritics typing on Windows and macOS; no false Typed-N-chars success (0.7.0.dev26)

- **Root cause:** Windows/macOS `Type` used pyautogui 0.9.54 `typewrite`/`write` (the same function). Its key map covers only `chr(32..127)` and SILENTLY skips the rest (`_pyautogui_win.py:246-264`, `_pyautogui_osx.py:219-221`), while the tool replied `Typed {len(text)} chars`.
- **Fix (Approach 1 of the main session's design):**
  - Pure `src/remoteos/typing_input.py`: `plan()` gives UTF-16 code units or Enter/Tab, `check_method`, `typed_reply`.
  - Windows `type_unicode`: one `SendInput` per character, `KEYEVENTF_UNICODE` with wScan = code unit, `VK_RETURN`/`VK_TAB`; a short return raises.
  - macOS `type_unicode`: Quartz keyboard events with `CGEventKeyboardSetUnicodeString`, ≤20 units per event, `kVK_Return`/`kVK_Tab`; `CGPreflightPostEventAccess` is checked first.
  - Linux `type_unicode`: `xdotool type`.
  - Shared `src/remoteos/text_entry.py`: `enter_text` (keys) and `paste_text` (save → set → paste keys → 0.3 s → restore).
  - Every provider has `type_text(text, x, y, clear, press_enter, method)`. The tool is one provider call.
- **Decisions (ROZHODNUTÉ on #30):**
  - `\r\n`/`\r` = one Enter.
  - paste works on every OS, and an empty clipboard counts as "nothing to restore".
  - macOS select-all/paste use Command: Ctrl+A only moved the caret, so `clear` deleted one character.
  - the macOS preflight runs before the click/clear/paste.
  - points are used when they are `is not None`.
- **Commits:** `917cde5` bump; `ee00184` [red] (24 fail + 1 collection error); `6f38e81` [green]; `bc52547` docs.
  - Review round 1 (0🔴 3🟡 7🔵): `09c0440` [red] (10 fail), `9a62359` [green].
  - Review round 2 (0🔴 1🟡 3🔵): `d355753` [red] (2 fail), `297fc3b` [green].
- **Tests (RED→GREEN):**
  - `test_windows_type_tool_sends_every_slovak_character`, `test_windows_type_tool_reports_blocked_input_as_an_error_not_typed`, `test_windows_type_unicode_*`
  - `test_mac_type_tool_types_slovak_text_through_quartz`, `test_mac_clear_selects_all_with_command_not_ctrl`, `test_mac_*_without_the_posting_permission_*`
  - `test_*paste*`, `tests/test_typing_input.py`
  - Result: 437 pass, ruff clean.
- **Not done here:**
  - The live Windows check (Notepad + `WM_GETTEXT` read-back). The supervisor deploys and verifies.
  - No macOS box was reachable, so the macOS path is fake-tested and source-evidenced (pynput passes the UTF-16 length the same way).
  - Round-2 🔵 follow-up candidates: Linux `xdotool type` argv exposure, `Error: `-prefixed clipboard text, and a split ticket for the over-budget `__main__.py`/`linux/desktop.py`.

## #31 — Upstream quick fixes: x/y=0, Shell cwd, dead duplicate, FileUpload, cancel_task, uvicorn_config, --debug (0.7.0.dev27)

- **Re-derived after #28/#29/#30.** Every item was still live except AnnotatedSnapshot's triple grab: #28 (c2b024e) left one capture, and a fake-X11 probe shows 1 `scrot` call.
- **Fixes (Approach 1 of the main's design, upstream winremote c3a675174c / pull 71 / pull 65):**
  - `_optional_point` (`x or y`, 0/0 = no point) for Type/Scroll/drag start; the Linux `move()` start is `is not None`.
  - Shell → `Popen(cwd=expandvars(expanduser(cwd)))`, and a path that is not a directory is refused.
  - The dead first `_ensure_session_connected` is deleted.
  - FileUpload: a 100 MB base64 cap + `validate=True` before any write.
  - TaskManager: `_transition` puts every status write under `_lock`, together with `cancel_task`.
  - `uvicorn_config` for TLS; `--debug` = the `remoteos` logger + uvicorn; `--reload` warns; a lone TLS flag is a usage error; stdio status lines go to stderr.
- **Decisions:**
  - `x or y`, not `is not None`: the 0 defaults would otherwise click the origin on every call.
  - `--reload` cannot work in-process: uvicorn reloads only an import-string app.
  - `--debug` keeps FastMCP at INFO, because at DEBUG FastMCP logs tool arguments and replies.
- **Commits:**
  - `b5eb85d` bump.
  - Item 1: `41f328e` [red] → `2bbcdc1` [green].
  - Item 2: `0f27b57` → `0cf4098`.
  - Item 4: `8ff5c90` → `3af9dc0`.
  - Item 5: `f01883f` → `8a9c8db`, and `4dc4dd3` → `bde9677`.
  - Item 6: `9d2c06f` → `bc31d29`, and the TLS pair `400e5ee` → `3d3e4cb`.
  - Review round 1 (0🔴 2🟡 6🔵): `ad9ff07` → `d1a0234`.
  - Review round 2 (0🔴 0🟡 4🔵): `8d0d949` → `fd8dc00`.
- **Tests:** `tests/test_shell_tool.py`, `tests/test_file_upload.py`, `tests/test_taskmanager.py`, `tests/test_startup_flags.py` (the real server in a subprocess, hermetic env), plus additions to `test_coordinate_contract_tools.py`, `test_provider_geometry.py` and `test_linux_desktop.py`. Result: 474 pass, ruff clean.

## #32 — Tool failures as real MCP errors (isError) + desktop diagnoses (0.7.0.dev28)

- **Validated live first:** through a real in-process FastMCP 4.0.3 client, a missing Shell cwd, bad FileUpload base64, an unknown KillProcess pid, a missing FileRead file and a headless Click all came back `isError: false`.
- **FastMCP 4.0.3 finding:** `call_tool` re-raises a `FastMCPError` before its masking branch, so a `ToolError` message is never masked. `_on_call_tool` returns it as `CallToolResult(isError=True)`.
- **Design (the main's Approach 1):**
  - `errors.py`: `ToolError`, `DesktopUnavailable` (verbatim text), `@reports_errors` on all 44 tools, `failure()` at WARNING, payload-free `stack_frames`, and `RedactInvalidArguments` for FastMCP's own argument-value warning.
  - `wrap_sync_tool` raises with `[task:<id>]` on a failure, the lock timeout and a cancel. The stale sweep respects a call's declared runtime (`_RUNTIME_ARGS`: Shell's `timeout`, Wait's `seconds`), never below 300 s.
  - The helpers and providers raise instead of returning error text. `platform/command.run_command(check=)` is for commands that change state.
  - `console_child()`: on Windows each child gets its own hidden console (CREATE_NO_WINDOW) at the OEM page, decoded with errors='replace'.
  - `platform/<os>/availability.diagnose(need)` with the needs capture / input / windows.
- **Decisions:**
  - The preflight never moves a session. A disconnected session → "run ReconnectSession", which reattaches the server's OWN session (ProcessIdToSessionId + WTSConnectState, never `query session`: a disconnected session's SESSIONNAME is blank) and re-checks it.
  - macOS input uses `CGPreflightPostEventAccess`, not `AXIsProcessTrusted`.
  - No Linux lock check: the locker is visible in the screenshot and reachable by input.
  - `Snapshot(use_vision=False)` needs only the user's desktop.
  - Logs carry no paths, names or argument values (#31).
  - A non-zero exit of Shell, a listing or EventLog is the answer.
- **Commits:**
  - `555f42a` bump.
  - Contract: `aee8087`/`ff5a9fd`/`a7e3d23`/`c1fd204` [red] → `59791ed` [green].
  - Diagnoses: `a251b6d`/`c15eacb` [red] → `73aa0cb` [green].
  - Review round 1 (1🔴 7🟡 8🔵): `7dde22d` refactor, `5b0f810`/`ab0776d` → `cec6908`.
  - Round 2 (0🔴 1🟡 6🔵): `b925145` → `4749122`.
  - Round 3 (0🔴 1🟡 3🔵): `0b9c1f0` refactor, `9e16ab2` → `d4fa15f`.
  - Round 4 (0🔴 0🟡 2🔵): `f85770c` → `c5bd63a`.
  - Round 5 (0🔴 1🟡 2🔵): `672a4c5` → `ad440f8`.
  - Round 6 (0🔴 0🟡 1🔵): `1318664` test move.
  - Round 7 (0🔴 1🟡 2🔵): `ad21f74`.
  - Round 8 (0🔴 2🟡 5🔵): `2d1c3d4` → `a37e496` (the stale sweep honours Wait's `seconds`), `06e8dfd` + `3e19e52` tests.
  - Round 9 (0🔴 0🟡 6🔵): tests for the 300 s floor, the client-path `seconds` and a missing registry value; doc drifts.
- **Tests:**
  - New: `tests/test_tool_errors.py` (real client), `test_desktop_diagnose.py` (per-OS fakes plus the preflight contract), `test_helper_failures.py`, `test_windows_tool_errors.py`, and the shared `fake_windows_session.py` / `tool_test_support.py` / `conftest.win_session`.
  - Existing error-text tests now expect the raised ToolError with the same texts. Result: 631 pass, ruff clean.
- **Out of scope, recorded on other tickets:**
  - #39: PowerShell `$()` injection in the Windows toast (Notification is tier 1).
  - #37: the AX tree needs a new `need`.

## #48 — Shell output on Windows as lossless UTF-8 (0.7.0.dev29)

- **Validated first:** Shell still ran `powershell -NoProfile -Command` decoded in the OEM page (live in #32: `Zlty kôn` on resolume-pp, cp437). The RED test reproduced the OEM decode of UTF-8 bytes as `┼╜lt├╜ k├┤┼ê …`.
- **Design (the main's Approach 1):**
  - `platform/command.py` owns starting and decoding a child: `powershell_argv` (a UTF-8 prefix line in `try/catch`), `own_console`, `decode_output`, `run_child`.
  - Every PowerShell child (Shell, `_ps`, launch_app, show_notification, OCR) runs binary in its own console. `console_child` (text mode, OEM) is left only for ping.
  - `decode_output` on Windows: strict UTF-8; otherwise per LINE UTF-8 or the OEM page (`errors="replace"`, one payload-free log line with counts). Off Windows: the locale, unchanged. Newlines as in text mode.
  - `_get_system_language`: GetUserDefaultLocaleName → `locale.getlocale()` → `en_US`; the deprecated getter is gone.
- **Decisions:**
  - The fallback is per line (round 1), and a lone CR also breaks a line (round 2).
  - The prefix uses `New-Object` (any PowerShell version) inside `try/catch` (a CLM box).
  - The Shell docstring states the `param(...)`/`using` limit. Native output captured BY PowerShell is decoded by PowerShell as UTF-8 (documented limit; on the live list).
  - System Language is now `sk-SK` (design).
- **Commits:**
  - `8569c41` bump.
  - `1c3dbe8`: fakes keep subprocess's return type.
  - `f07fc1d`/`31f2ef5` [red] → `3422348` [green].
  - Locale: `187b4e9` → `8067ab0`.
  - Round 1 (0🔴 2🟡 9🔵): `4a571fa`, `ef13ff5` → `071a3f8`, `14e4f69`.
  - Round 2 (0🔴 1🟡 8🔵): `9b1cc6d`, `cc0de75` → `8185910`, `fefbccd`.
  - Round 3 (0🔴 0🟡 5🔵): `af5a8ef`, `ba429a1` (new `.claude/rules/child-output.md`).
- **Tests:**
  - New: `tests/test_output_encoding.py`, `tests/test_win_system_language.py`, `tests/fake_console_child.py` (a REAL Python child through the real subprocess; a fake `oem` codec; `run_result`), and `conftest.windows_child`.
  - Result: 671 pass, ruff clean.
- **Live verification (supervisor, after install):** the design's list plus the extra items on #48:
  - `Write-Error` and `cmd /c echo` on resolume-pp;
  - PowerShell-captured native output and a cp852 `.bat` on win-pz-server.

## #48 round 2 — Windows PowerShell children in a UTF-8 console, no injected code (0.7.0.dev30)

- **Why:** round 1's prefix was PowerShell code, and 5.1 printed it in every `Write-Error` record
  (live on resolume-pp); a leading `param()` failed. A `[ScriptBlock]::Create` bootstrap only moved
  the leak to the record's position line and lost the `$?` exit code (pwsh probes, Design-question
  on #48). The main decided option E (ROZHODNUTÉ issuecomment-6187272301).
- **Design:** every PowerShell child is `python.exe -I -S -X utf8 -c <fixed program> <cwd>
  powershell -NoProfile -Command <script>`. The program switches its own hidden console to 65001,
  runs PowerShell unchanged (the dev28 command line) in the same console, and exits with the exact
  32-bit code. No PowerShell code is ours; `UTF8_PREFIX` is gone. Accepted cost: `$OutputEncoding`
  (stdin into native programs) is ASCII again.
- **Runner:** `run_child` (Popen, NUL stdin) is the one runner of Shell, `run_command` and OCR. A
  timeout kills the tree: `taskkill /F /T` (hidden console) on Windows; on POSIX the group's
  members are snapshotted, the group gets SIGTERM, up to 2 s grace without reaping, then SIGKILL
  in any case. A never-started PowerShell raises.
- **Decisions on the way:** the cwd is an argv element (the trampoline runs in the server's
  directory, so `powershell` is never looked up in the user's cwd); fakes of a child answer at
  `subprocess.Popen` (`popen_answered_by`); the trampoline stays a `-c` string (the decision).
- **Commits:** `a114fd5` bump; `c9b60c0`/`496b872` round-1 🔵 leftovers; `e8368dc` fakes at Popen;
  `f7c1e36` → `77097a8` tree kill; `47f248a` → `d0c92f1` the trampoline; review rounds 1-5 as
  RED → GREEN: `5503558` → `e020efd`, `feda73a` → `dfbbff9` (+ `2c8349f` ci cmd shell),
  `db3951d` → `c64a775`, `428f721` → `65f0c33`, `3fdc3e7` → `bd2fe78`; docs in between.
  Reviews: 0🔴3🟡8🔵, 0🔴3🟡5🔵, 0🔴1🟡4🔵, 0🔴1🟡5🔵, 0🔴1🟡8🔵, 0🔴0🟡6🔵 (folded).
- **Tests:** `tests/test_child_timeout.py` (real POSIX process trees), the trampoline program exec'd
  against its kernel32/`subprocess.call` boundary, and `tests_windows/` in the new
  `windows-powershell` CI job (real PowerShell 5.1, en-US cp437, no fakes). Result: 711 pass on
  Linux, ruff clean.
- **Follow-up candidate (supervisor):** split the pre-existing `__main__.py` (1883 lines).

## #52 — Linux Shell children get the user session environment (0.7.0.dev31)

- **Why:** the Linux server is a system unit with `User=`, which has no login-session
  environment, so a Shell child had no `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` / `DISPLAY`
  and `systemctl --user` failed (live on stream-pp) although `/run/user/1000/bus` existed.
- **Design (main session, Approach 1):** new `platform/linux/shell_env.shell_env()`: the server's
  environment plus, for its OWN effective uid only and only where a variable is unset or empty,
  `XDG_RUNTIME_DIR` (an owned dir), `DBUS_SESSION_BUS_ADDRESS` (its bus socket), and from the
  provider's cached session (same uid) `DISPLAY` (a local display whose X socket is owned by root
  or the euid, lstat), `XAUTHORITY` (only with that checked display), `WAYLAND_DISPLAY=wayland-0`
  (a Wayland session, socket in the child's runtime dir). Nothing as root. `run_child(env=)`;
  Linux Shell passes it, Windows/macOS unchanged. `desktop.py` untouched.
- **Decisions on the way (all on #52):** the design's re-detect-on-gone-socket was REMOVED in
  review round 1 (`get_session(force=True)` at a logout wrote `headless` into the shared cache and
  the desktop tools refused until a restart); the environment is copied key by key (a key popped
  by `_load_atspi` in another thread raised KeyError, round 4); one lock for the log de-dups;
  conftest gives every test a headless start-up session cache; GUI programs must be started
  detached, prompting commands non-interactive (docs + Shell description).
- **Commits:** `55ac948` bump; RED `3cfbc00`/`e9554c9`/`b98730b` → GREEN `d8fe0a6`; review rounds
  as RED → GREEN: `5c3dc2a` → `16a35c7`, `8660de5`/`268575e` → `6024b59`, `cb100a8` (test gaps),
  `03ce11c` → `d1179f4`, `b1d3e20` (round-5 pins); docs `9985678`, `8e4ae88`, `fc08c37`, `969bc59`, `d872ff7`.
  Reviews: 1🔴3🟡4🔵, 0🔴2🟡6🔵, 0🔴2🟡6🔵, 0🔴1🟡5🔵, 0🔴0🟡7🔵 (folded or dispositioned).
- **Tests:** `tests/test_shell_env.py` (real AF_UNIX sockets in a short temp tree, real bash,
  loginctl faked at `subprocess.run`, euid / stat+lstat owner faked only where needed). Result:
  771 pass, ruff clean; the lane's 36-mutant pass over `shell_env.py` caught all.
- **Follow-up candidates (supervisor):** the provider-wide start-up-only session cache (a
  graphical login after a boot-time start reaches neither Shell's DISPLAY nor the display-tool
  gating until a restart); `desktop._load_atspi`'s temporary `os.environ` window, which a
  concurrent Shell snapshot can copy (needs a lock shared with `desktop.py`); the provider's
  `/proc` scan can pick a detached Shell-started program's stale `DISPLAY`/`XAUTHORITY` after a
  re-login on another display (match `/proc/<pid>/sessionid`).

## #12 — one installer per OS: camera-box's venv layout, pinned ref, the key never in a unit or an argv (0.7.0.dev32)

- **Why:** two installers managed `remoteos-mcp.service`. Ours installed into the system python
  from `main` and wrote `--auth-key <key>` into the unit's ExecStart (world-readable, in `ps`);
  camera-box's `scripts/lib/remoteos-mcp.sh` used a venv, a pinned ref and 0600 key files, and on
  16.10 its run reverted strih-pp/stream-pp from dev31 to its pin (dev20). macOS had the key in
  the plist's ProgramArguments, Windows on the `.bat`'s server command line.
- **Design (main session, Approach 1, issuecomment-6293340521):** adopt camera-box's lib as the
  base of `install-linux.sh` (resolve_key, fetch_source, build_venv side by side + swap,
  camera-box's desktop/headless unit, 0600 EnvironmentFile, prove_health, then
  remove_legacy_system_copy), and the same key rule on macOS and Windows.
- **Implementation details decided on the way (all on #12, issuecomment-6293797059):** the swap
  is a symlink (`/opt/remoteos-mcp-venv` → a build in `/opt/remoteos-mcp-venvs/`; a venv cannot
  be renamed); a failed fetch/build exits 1 changing nothing; a failed proof or an interrupted
  switch goes back to the previous unit/key files/venv only when that state was PROVEN (a unit
  existed, no `.switch-pending`, and the running process answers with its files' version and
  refuses a request without the key), else the service is stopped and disabled (an unproven
  agent may have authentication off); enable-only never starts or stops anything, and a no-op
  run on a proven box keeps it proven; desktop venvs get one `.pth` to the
  system gi (AT-SPI); the mode and User= follow the existing unit; the installers never print
  the key; Windows keys come from a CSPRNG; an empty `REMOTEOS_MCP_REF` means main; the import
  checks never write bytecode (an adopted venv stays byte-identical); `uninstall-linux.sh`
  knows the layout; the install skill moved to `.claude/skills/remoteos-install/` (a hook
  blocks a skill that shadows `/install`).
- **Commits:** `3edbc91` bump; RED `e1942ac`, `327bc4a`, `8a8c922`, `ca49f09`, `e57935c`,
  `46ccae2`; the system-python block's tests moved to `install.sh` and the Linux ones the venv
  replaces retired in `12aaf35`; GREEN `5f5dcf3` (Linux), `afe8ed0` (macOS), `115ec17`
  (Windows), `87de551` (uninstall-linux.sh).
- **Review rounds (each RED → GREEN, recorded on #12):** round 1 `7a105f4` → `74f1dd5`,
  `282acd4`, `ea177ab`/`b51a9e8` (the server drops the key names from its environment, refuse
  a key the installer cannot carry); round 2 `2a9812e` → `229480a`, `dc15f27`; round 3 `6b7d74e`
  → `ebfecd5` (only a proven state is restarted); round 4 `c57d6a3` → `90d9323` (enable-only
  never stops the running agent); round 5 `13c4e14` → `aa798fa` (a no-op enable-only run keeps
  a proven box proven; the lock is 0600); round 6 `bc736d9` → `6dec457` (PROVEN = the running
  install answers with its files' version and refuses a request without the key; nothing
  switched → left as it ran; the gi link undone; the key only to the unit's own listener, `ss`);
  round 7 `443ce91` → `6c53a05` (the rollback re-proves what it restarts, else stop + disable;
  no gi flag without a system gi; no key temp copy left on a signal); round 8 `796006f` →
  `2f365c7` (the pending mark holds the unit's InvocationID: a switch the unit has started since
  is checked as it runs; the proof's and the re-proof's polling pinned); round 9 `91b0cb6` →
  `57e1093` (macOS and Windows drop the explicit key before their first child; a stopped
  switch's kept mark names the stopped unit); round 10 `7c4ca82` → `a380342` (the start
  script sets the key only around the server line; the read of what runs gives a
  not-yet-answering unit the proof's polls); round 11 `e4f0c1e` → `d5a38b1` (every loopback
  check goes direct, `-q --noproxy '*'`; macOS removes write_private's temp key copy); round 12
  `60fe348` → `bc4fcf5` (the read-only-root refusal never advises editing fstab; the key-file
  mode rewrite and the direct loopback curls pinned); round 13 `1ce9562` → `9fec760` (the
  installer and the uninstaller refuse a run from inside the unit, the agent's own Shell; the
  build's readable modes pinned); round 14 `d0b3554` → `e52c4d6` (a proven, no-op enable-only
  run removes the old system-python copy; two messages corrected) — round 14 had no 🔴/🟡.
- **Tests:** `tests/test_installer_linux_venv.py` (42), `tests/test_installer_linux_edges.py`
  (57, the review rounds' edges) and `tests/test_installer_linux_rollback.py` (19, what a failed
  run leaves running), on `tests/linux_installer_harness.py` +
  `tests/fake_linux_tools.py` (real venvs, a stateful fake systemd/curl/ss, the argv and the
  environment of every process); `tests/test_installer_macos_key.py` (13, also under GNU bash
  3.2.57); `tests/test_installer_windows_key.py` (5 static) and
  `tests_windows/test_install_ps1_key.py` (real PowerShell 5.1: install.ps1's own functions, real
  cmd.exe runs of the .bat, the ACL). Result: 906 pass, ruff + shellcheck clean;
  `install-linux.sh` 1000 lines.

## #51 Phase 1 — every project through the gateway: one key store, generated configs, a drift watchdog (0.7.0.dev33)

- **Why:** every project's `.mcp.json` held its own copy of each machine key; a key change made
  every copy stale and every running session kept the old key until the owner reconnected it.
- **Design (main session, Approach 1, issuecomment-6306364162):** the #25 gateway becomes the
  route for EVERY project. ONE key store (`machines.json`) is the gateway's machine source;
  `projects.toml` lists each project's machines; `sync-configs` writes each project's fleet
  entries (same server names, one project token, allow-list = the manifest); `check` (a 10-minute
  user timer) watches for key drift. remoteos-mcp keeps DIRECT entries.
- **Implementation details decided on the way (all on #51):** `fileops` (stdlib-only, the same
  code runs locally / under `sudo -n -u` / over `ssh -o BatchMode=yes`, contents on stdin only);
  a format-preserving `mcpServers` renderer (kept members byte-equal); sync owns only entries it
  wrote; managed consumers (`managed_by`); exposure is a kill switch sync never undoes; import
  candidates `^(win|linux|mac)-`; the manifest before the import in the migration.
- **Commits:** bump `24df568`; RED `d0e2099` + `5ecd240` → GREEN `fb8c63b` (key store, set-key,
  import); RED `0c89427` → GREEN `d213d29` (sync-configs); RED `9f872c9` → GREEN `7dc10c2`
  (check + units); `c74cd75` (feature tests), `25fc0b1` (docs).
- **Review: 8 full rounds + 1 focused round, every 🟡 RED→GREEN:** r1 `86e2c70` → `8bdfa53`
  (5 🟡: a landed-but-failed write, unverified import downgrade, re-exposure, mass revoke on an
  empty manifest, untested failure paths); r2 `16df35c` → `3654c18` (3 🟡: airuleset exits 0 on a
  failed post, import url switch, token-rule mutants); r3 `99bca7a` → `3403c7b` (3 🟡: urlsplit
  ValueError, dry-run choke point, repaired-after-ok); r4 `98ff852` → `0a99e9a` (5 🟡: bad ports,
  airuleset cuts at 1900 → chunks, retried repair notice, direct removal, unreadable --from);
  r5 `6196a0e` → `6fc2854` (2 🟡: a repair failure blinded the run, two untested branches); r6
  `09fb901` + `63f863d` → `a8b0ddd` (1 🟡 doc; short writes, git work trees, silent watchdog);
  r7 `75ea49c` → `0d93912` (1 🟡: round 6's hash cap kept the wrong hash); r8 0 🔴 0 🟡, its
  three 🔵 `5eecf1f` → `2363ea1`; focused review of that fix 0 🔴 0 🟡, its three 🔵 `7d6ad40` →
  `b426dd5`.
- **Tests:** `tests/test_gateway_keystore.py`, `test_gateway_import.py`, `test_gateway_sync.py`,
  `test_gateway_check.py`, `test_gateway_files.py` on `tests/fleet_harness.py` (real loopback
  machines/gateway, fake ssh/sudo/airuleset at the OS boundary); conftest isolates every gateway
  default path. Result: 1034 pass (908 before), ruff clean.

## #59 — server announces its real hostname, OS and shell (PR #61, merge `030d65b`, 0.7.0.dev34)

- **Design:** main's Approach 1 (`Design-by: main`): pure `src/remoteos/identity.py`
  (`server_instructions` + `machine_identity`), FastMCP `instructions` built once at import,
  `/health` adds `os` + `hostname` (status/version unchanged). Rejected: a `WhoAmI` tool, gateway-side rename.
- **Commits:** `628a34d` bump, `82d7a7f` feature, `509fecd` review round 1 (Windows Server named by
  build — CPython 3.12 says "2022Server" for builds ≥ 20348; non-UTF-8 os-release falls back
  instead of stopping the server; `/health` is keyless so its fields are public).
- **Tests:** `tests/test_identity.py` (OS variants, fallbacks, Shell runs what is announced),
  `test_startup_flags.py::test_initialize_and_health_name_this_machine_its_os_and_its_shell` (real
  initialize over HTTP), `tests_windows/test_identity_windows.py`. 1053 pass, ruff clean, CI all green.

## #57 + #12 (rescoped) — install.ps1: an elevated run or nothing changes; one resolved commit; shell CI job (0.7.0.dev35)

- **#57 design:** main's Approach 1 (pure `Get-RemoteOSElevationAction` gate before the first
  change; relaunch elevated for a person, fail for SSH / session 0 / no console input / an explicit
  key / a relaunch still not elevated). Session 0 starts the server only through
  `schtasks /Run` and proves the listener is in a desktop session (amendment); so does an elevated
  run as ANOTHER account, whose per-user Python is refused before any change.
- **#12 design:** `REMOTEOS_MCP_REF` (default main) → ONE sha via the GitHub commits API before any
  change; constraints, archive and the relaunch's installer all use it; `installed-ref.txt` after
  the health check. `shell` CI job: shellcheck (clean, nothing to fix) + macOS installer tests
  under bash:3.2.
- **Commits:** `576603f` bump, `4ecd85e` [red] #57 → `d8fa4e6` [green], `0460569` [red] #12 →
  `5d34a9f` [green], locator commits `62ad16d` / `e9be09c` / `5a0d456`, review rounds `0a83d30`,
  `df927d1`, `9a48f99` (relaunch loop guard, console-input probe, Exit-RemoteOSInstaller pause for
  piped console runs, pip/Python failures exit 1, another-account task start + Python check).
- **Tests:** `tests/test_installer_windows_key.py` (gate order, exits through the helper, pinned
  call-site args, one sha for both URLs) and `tests_windows/test_install_ps1_key.py` (decision
  table, probe, relaunch command run for real, exit helper, listener session, ref names, Python
  reachability). Local: 1063 pass, ruff clean, shellcheck clean, bash:3.2 53 pass.

## #39 — upstream security sync: PowerShell/AppleScript quoting, OAuth, Scrape cap, bind guard, TOML booleans, pip-audit (PR #68, merge `2053ff4`, 0.7.0.dev36)

- **Design:** main's Approach 1. `ps_literal` (typographic quotes doubled too) at every PowerShell
  splice site, toast text via `REMOTEOS_TOAST_*` env, `applescript_literal`; OAuth S256 + https/
  loopback redirects + required configured secret (never returned by register); `scrape.py` 5 MiB
  cap, http(s) only, opt-in private block, redirect bodies never read; `check_bind_exposure`;
  `config._boolean`; pip-audit matrix (py3.12 Linux/Windows/macOS + py3.11 Linux), pins upgraded
  (fastmcp 4.1.2, PyJWT 2.15.1, opentelemetry-api 1.45.1, Pillow 13.0.0 on py3.11+).
- **Commits:** RED 2b081ea → GREEN 0fc2693 (PowerShell), 1ca1dae (AppleScript), 862b3c2 (OAuth),
  ccd237e (Scrape/bind/TOML), 60037c4 (pip-audit + pins); review RED b48df8d → GREEN 2779320,
  RED 7f30e6c → GREEN fd6f7a0.
- **Tests:** `tests/test_injection_quoting.py`, `tests/test_server_hardening.py`, bind guard + Scrape
  policy in `tests/test_startup_flags.py`, real PS 5.1 `tests_windows/test_injection_powershell.py`
  (toast `Show()` works on windows-latest). Local 1188 pass; CI all green on PR and main.
- **Open for the owner:** Python 3.10 keeps Pillow 12.3.0 (CVEs; Pillow 13 needs 3.11) — drop 3.10,
  ignore entry, or accept. Follow-up candidate: OAuth without a secret (authorize approves all).

## #64 — Windows installer: download first, install offline, roll back; macOS ref input; Python 3.11 (PR #69, merge `173cfe1`, 0.7.0.dev37)

- **Design:** main's Approach 1. install.ps1: preflight into `<config>\wheelhouse\<sha>` (constraints
  via urllib, `pip download --timeout 60 --retries 10`, `pip wheel --no-deps` over every sdist,
  `<sha>.partial` renamed when whole, reused when this Python installs it), dry-run of the new and
  the rollback wheelhouse before the stop, offline install, every failure after the stop rolls back
  (`Exit-RemoteOSAfterFailure @rollback`), prune to current+previous after the proven install.
  The wheelhouse root is Administrators-owned (elevated pip); no elevated `Remove-Item -Recurse`
  (junctions). install.sh: `REMOTEOS_MCP_REF`. Python >= 3.11 everywhere, constraints for 3.11.
- **Commits:** bump 9900d68; RED 2610972 → GREEN ed8bbea (+ 8496ff4 older static checks moved);
  review RED eba93dd → GREEN 197b38b, RED 7a6d5ed → GREEN eef4936, RED cd0bc79 → GREEN 12e8902,
  bf5e4d8 → 09a3df6 (tree walk: .NET Framework's recursive delete throws on a junction).
- **Tests:** `tests_windows/test_install_ps1_wheelhouse.py` (real pip in a venv, a local index with
  in-tree-backend sdists, no network; ACL/junction cases Windows-only), static checks in
  `tests/test_installer_windows_key.py`, `tests/test_installer_python_gate.py` (also bash 3.2),
  `TestMacosSourceRef`. CI all green on PR and main; local 1225 pass.
- **Follow-up candidates (listed on the ticket):** wheelhouse in %ProgramData% (profile swap
  residual), a Windows trap for a run killed between stop and install, uninstall.ps1 elevation +
  desktop profile, generator split of install.ps1 (999 lines) before the updater ticket.

## #70 — `remoteos-call`: scripts reach a machine over MCP, not SSH (part b of #67, 0.7.0.dev38)

- **What:** console script `remoteos-call` (`src/remoteos/cli_call.py`): `MACHINE Shell CMD`
  (stdout/stderr split, remote exit code), `put`/`get` (FileUpload/FileDownload, size-checked),
  `TOOL --json`, `--list`; config from `--config` / `--project` / `./.mcp.json`; exits 125 tool
  error, 126 no result, 130, 141, 2. Design by the main session on #70 (Approach 1).
- **Decisions:** fastmcp `Client` with `mode="legacy"`; an httpx response hook recovers the HTTP
  status the SDK hides; `trust_env=False`; the SDK's 1 MiB SSE event limit lifted by a
  process-local `EventSource` swap (loud if the SDK changes); config read with
  `gateway/registry.py`'s helpers (`is_http_url` and `taskmanager.declared_runtime` made public).
- **Commits:** bump 021e4d3 → feature 4375cfd → review rounds 69f9458, dbe5071, 4cdacb2 (three
  fresh-context reviews: 2🔴8🟡7🔵 → 0🔴2🟡8🔵 → 0🔴1🟡6🔵, all fixed or dispositioned).
- **Tests:** `tests/test_cli_call.py` against a REAL server + REAL gateway (subprocesses on
  loopback): exit-code mapping, stderr split, put/get sha256 round trips (5 MB get), 2.5 MB Shell
  output, 125/126 paths, the token never in output/logs/access log/argv; console script run as a
  process (closed pipe → 141, `FASTMCP_LOG_LEVEL=DEBUG` silent). Local 1334 pass.
- **Follow-up candidates (for the supervisor):** `put` is capped at about 3 MB by the machine's
  4 MiB mcp request limit (raise it server-side or add chunked/append FileUpload) — restreamer's
  `.deb` installs will hit it; structured Shell output (exit code / stderr as fields) to remove
  the plain-text round-trip limits; the gateway's 600 s upstream read timeout cuts Shell calls
  over ~540 s (SSE keep-alive pings).

## #71 — installers keep admin + SelfUpgrade (67 a+c), 0.7.0.dev39

- **Design:** main's Approach 1 on #71. The installers own a sudoers drop-in; `remoteos.selfupgrade`
  starts the installer detached (systemd-run, the `RemoteOSMCP-Upgrade` task, `launchctl submit`).
- **Decisions in the lane:** both generated installers (`installers/`, `scripts/build_installers.py`);
  the drop-in goes in AFTER the proof, visudo BEFORE the download; macOS with no sudo warns (only a
  refused line fails); the macOS job removes its own label because launchd keeps a submitted job alive.
- **Tests:** `tests/test_installer_generated.py`, `tests/test_installer_linux_sudoers.py`,
  `tests/test_installer_macos_sudoers.py` (bash 3.2 too), `tests/test_selfupgrade.py`,
  `tests_windows/test_install_ps1_upgrade_task.py`.
