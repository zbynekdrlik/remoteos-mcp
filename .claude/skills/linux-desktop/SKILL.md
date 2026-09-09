---
name: linux-desktop
description: Linux X11 desktop provider — session detection, xdotool/scrot/xclip/wmctrl backends, and how to deploy + live-test desktop tools on an X11 box vs a headless box. Load before touching src/remoteos/platform/linux/desktop.py, the Linux tool gating in tiers.py/__main__.py, or the OCR Linux path.
---

# Linux desktop provider (X11)

Landed in #6. #7 (AnnotatedSnapshot via AT-SPI) + #8 (ScreenRecord via ffmpeg x11grab) landed in 0.7.0.dev7. On an X11 box the full set is `[tools: 40/44]`; headless stays `24/44`.

## Architecture (read before editing)

- **Input tools (Click/Type/Scroll/Move/Shortcut) are dispatched in `__main__.py` via `pyautogui`, NOT the provider.** pyautogui is deliberately NOT imported on Linux (`if _sys.platform != "linux"`). So each input handler has an `if is_linux(): return get_desktop().<fn>(...)` branch that routes to xdotool in the provider. Win/Mac keep the pyautogui path.
- **Tool gating is runtime-session-aware.** `tiers.py` splits the old `LINUX_EXCLUDED_TOOLS` into `LINUX_ALWAYS_EXCLUDED_TOOLS` (Reg*, ReconnectSession, ScreenRecord, Scrape, AnnotatedSnapshot) + `LINUX_DISPLAY_TOOLS` (enabled only when X11 is detected). `__main__.py` calls `linux.desktop.get_session()` at startup; headless → exclude display tools (byte-identical to before, `[tools: 24/44]`), X11 → keep them (`[tools: 38/44]`). Startup log prints `Linux session: <mode>`.
- **OCR on Linux** = provider `capture_png` (scrot) → `tesseract` CLI (no PIL/pytesseract). Pillow is a Linux dep only for the screenshot resize/JPEG in `take_screenshot`.

## Session detection gotcha (the load-bearing one)

`loginctl show-session <id>` returns an **empty `Display=`** for X11 sessions. Do NOT rely on it. The reliable source of `DISPLAY` + `XAUTHORITY` is `/proc/<pid>/environ` of a process owned by the session user (e.g. gnome-shell): `DISPLAY=:0`, `XAUTHORITY=/run/user/<uid>/gdm/Xauthority`. Pick the session with `Type=x11 Class=user Active=yes` (skip `tty` and `greeter`; SSH logins create transient tty sessions).

The service may run as the **session user** (imag-nb: `User=newlevel`) OR as **root** (cam1: `User=root`). `_build_invocation` injects the session env when non-root, wraps `sudo -n -u <user> env …` when root.

## Two live-only subprocess hangs (both fixed; do not reintroduce)

- **xclip SET forks to stay resident** holding the selection, so `capture_output=True` keeps the inherited pipe open and `subprocess.run` blocks until timeout. Fix: run the clipboard-set with output to `DEVNULL` (`_run_x(..., discard_output=True)`). Read (`xclip -o`) is fine captured.
- **Launching a GUI app blocks forever** — a GUI process never exits, so `_run_x` (which waits) times out. Fix: `launch_app` spawns detached via `_spawn_x` (Popen `start_new_session=True`, `wait(settle)` → `TimeoutExpired` means still-running = success).

These won't show in unit tests (no real xclip/GUI) — they only appear on the live box. Always live-test any new forking/long-running X command.

## Deploy + live-test a desktop-tool box (imag-nb, X11)

Managed X11 box: **imag-nb**, `newlevel@10.77.9.182` (creds in memory `reference_ssh_credentials.md`), Ubuntu 24.04, service on :8092. There is NO CI in this repo — live functional verification IS the gate.

1. Install X tools once: `sudo apt-get install -y xdotool scrot xclip wmctrl tesseract-ocr` (the installer does this automatically only when a graphical session is detected).
2. Fast pre-merge iteration: `tar -C src -czf /tmp/r.tgz remoteos`, scp, `sudo tar -C /usr/local/lib/python3.12/dist-packages -xzf /tmp/r.tgz`, clear `__pycache__`, `sudo systemctl restart remoteos-mcp`.
3. Test as the SERVICE USER (so session resolution matches). The `linux-<host>` MCP server isn't wired into the working session — verify with `python3 -c "from remoteos.platform.linux import desktop as d; …"` over SSH (same uid as the service) or curl the endpoint. Real checks: screenshot decodes to a valid JPEG, clipboard round-trips an exact token, `enumerate_windows` non-empty, launch+Type+OCR reads the typed text back.
4. Headless regression: deploy the same code to a headless box (cam1, `root@cam1.lan`) and confirm `Linux session: headless`, `[tools: 24/44]`, every desktop fn returns the exact stub `Not available on headless Linux (no display)`, no crash.

## ScreenRecord — ffmpeg x11grab (#8)

`provider.record_screen()` returns a base64 **animated GIF** (same return shape as `recording.record_screen`, so the shared `ScreenRecord` tool treats Linux like Windows/Mac). It builds the ffmpeg argv in `_ffmpeg_x11grab_argv()` (pure, unit-tested) and runs it through `_run_x` (session env-inject / `sudo -u` wrap). Flags that matter: full-screen omits `-video_size` (x11grab auto-detects the root size from the display); a region uses `-video_size WxH -i :0+X,Y`; `-vf "fps=N,scale=W:-2:flags=lanczos"` (`-2` = keep aspect, even dimension). fps clamped ≤10, duration ≤10, timeout `dur+30`. Headless → raises the stub (like `take_screenshot`), never shells out. Needs `ffmpeg` (installer adds it in the graphical branch).

## AnnotatedSnapshot — AT-SPI2 accessibility tree (#7)

`provider.get_interactive_elements()` walks the **AT-SPI2** tree in-process via GI (`gi.repository.Atspi`) — needs `gir1.2-atspi-2.0` + `python3-gi` (+ `python3-pyatspi`; installer adds them). Two load-bearing gotchas, both hit live on imag-nb:

- **The a11y bus needs the session env IN THIS PROCESS.** `_load_atspi()` sets `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus` (+ DISPLAY/XAUTHORITY/XDG_RUNTIME_DIR) from the resolved session before `Atspi.get_desktop(0)`. A systemd service starts with none of these; without them the walk finds 0 apps. (`frame.get_process_id()` works and matches wmctrl's pid, useful for window matching.)
- **GTK4 reports `CoordType.SCREEN` extents as `(0,0)`** (gtk-at-spi limitation) — every element lands at the origin, useless. The fix (`_atspi_coord_strategy` + `_content_origin`): read **`CoordType.WINDOW`** extents (reliable) and add the focused window's **content origin** = xdotool `getactivewindow getwindowgeometry` `(X,Y)` **+ the CSD shadow margin** `(outer_size − atspi_frame_size)//2` (≈61px for GTK4 CSD, ≈0 for server-decorated). `screen_rect = WINDOW extent + content_origin`. Verified: boxes land exactly on gnome-calculator buttons. Base image comes from the provider (`capture_png`/scrot), NOT `PIL.ImageGrab.grab()` (which fails for the service). Degrades gracefully: Qt/Electron/legacy apps expose little → returns the plain screenshot + whatever AT-SPI gives, never fabricates.

Both provider fns are display-gated in `tiers.py` (`LINUX_DISPLAY_TOOLS`); the pure logic (`_ffmpeg_x11grab_argv`, `_collect_actionable`, `_content_origin`) is unit-tested with mocks/synthetic trees.

## Hardening invariants (#10 — don't regress these)

Review of #6/#7/#8 fixed 5 defects; keep them fixed:

- **`_load_atspi()` must NOT leak env into global `os.environ`.** It injects the session's `DBUS_SESSION_BUS_ADDRESS`/`DISPLAY`/`XAUTHORITY`/`XDG_RUNTIME_DIR`, forces `Atspi.get_desktop(0)` **while they're set** (Atspi/libdbus caches the a11y-bus connection process-wide, so the later tree walk still works), then RESTORES the originals in a `finally`. Leaking them (a) poisons every later `Shell` tool call (it inherits the process env) and (b) makes a root-service force-redetect misclassify itself as a uid-0 env session. If AnnotatedSnapshot ever returns 0 elements after this, the connection isn't being cached before restore — wrap the whole walk in the env window instead.
- **`_detect()` trusts its own env only when `os.geteuid() != 0`.** A root service's own env is never the real seat; a root process always resolves the session via loginctl (otherwise a stray/leaked `DISPLAY` drops the `sudo -n -u <user>` wrap → D-Bus EXTERNAL auth rejects uid 0 → clipboard/notifications break).
- **Screen captures go to the session user's `XDG_RUNTIME_DIR` (`/run/user/<uid>`, 0700), never `/tmp`.** `_capture_tmp(suffix)` returns the private path used by both `capture_png` (scrot/import) and `record_screen` (ffmpeg). `/tmp` is world-readable (0644 under a sticky dir) → a co-located local user could read screenshots/recordings. The runtime dir is also writable by the session user under `sudo -u` (a root-owned 0700 /tmp dir would not be).
- **`_run_x` converts `subprocess.TimeoutExpired` → `RuntimeError(f"{cmd[0]} timed out after {timeout}s")`** at BOTH the first-attempt and retry call sites. TimeoutExpired is not a RuntimeError, so it otherwise escapes every caller's `except RuntimeError`.
- **The ImageMagick `import` fallback in `capture_png` guards empty output** (`raise RuntimeError("ImageMagick import produced empty output")`), like the scrot path — never return zero-length PNG bytes.

## Post-merge deploy gotcha

`pip install "git+…@main"` right after a merge can clone a **stale** `main` (pre-merge) → installs the OLD version. Pin to the merge commit SHA: `sudo python3 -m pip install --no-cache-dir --break-system-packages --force-reinstall --no-deps "git+https://github.com/zbynekdrlik/remoteos-mcp.git@<merge-sha>"` (both imag-nb and cam1 install into system `dist-packages`, so `sudo` + `--break-system-packages` on Ubuntu 24.04). Version surface for this MCP server is `/health` (`{"version":"…"}`), not a web DOM.

**The fast src-copy (tar→scp→extract) does NOT bump `/health`.** `__version__` comes from `importlib.metadata.version("remoteos-mcp")` = the installed `.dist-info`, which a src-copy leaves untouched — so `/health` keeps reporting the OLD version even though the CODE is new. Use src-copy only for PRE-merge FUNCTIONAL testing (behavior); to make `/health` show the new version you must `pip install @<sha>` (updates metadata). Verify the fixes live (session self-resolves via loginctl — pass `python3 … </dev/null` so ffmpeg's stdin read doesn't hang the heredoc).

## Testing gotcha

`python3 -m pytest tests/` collection CRASHES here (`ModuleNotFoundError: jinja2`) because a stray global `pytest-html` plugin auto-loads. Run with `python3 -m pytest tests/ -q -p no:html -p no:cacheprovider`. Current suite: 71 tests (61 original + 10 #10 hardening).
