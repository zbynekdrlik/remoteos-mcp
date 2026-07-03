# Autopilot Log

Terse per-ticket record of autonomous cycles (decisions, commits, tests, PR).

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
