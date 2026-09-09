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
