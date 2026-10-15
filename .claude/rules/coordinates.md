---
paths:
  - "src/remoteos/__main__.py"
  - "src/remoteos/coords.py"
  - "src/remoteos/snapshot.py"
  - "src/remoteos/platform/*/desktop.py"
  - "src/remoteos/platform/win/dpi.py"
  - "src/remoteos/platform/linux/display.py"
  - "tests/test_coords.py"
  - "tests/test_coordinate_contract_tools.py"
  - "tests/test_snapshot_render.py"
  - "tests/test_provider_geometry.py"
  - "tests/test_win_dpi.py"
---

# Coordinate contract (Snapshot image ↔ screen, #28) — gotchas

- **One frame per `(monitor, max_width)` plus the live geometry.** Each provider has
  `capture_geometry(monitor)`, which returns a `CaptureGeometry`, and `capture_frame()`.
  `coords.frame_for` turns the geometry into a `Frame`. Input tools recompute the frame at call
  time. Nothing is cached between Snapshot and Click, because the transport is stateless.
- **The image size comes from the geometry, never from the capture.** `snapshot.fit_to_frame`
  always resizes to `frame.image_size`. If you size the image from the raw capture instead, the
  click-time frame stops matching what the model saw. This bites on macOS Retina, where the
  capture is in pixels and the bounds are in points.
  - Providers pass the `geometry` along, so when a capture's size does not match the
    geometry's pixel size, `fit_to_frame` logs a WARNING.
  - Such a mismatch means the geometry query and the grabber disagree about the screen. It
    is never fitted silently: the silent fit is what hid the Xinerama bug.
- **Screen space differs per OS:**
  - Windows (per-monitor-v2) and X11: physical pixels.
  - macOS: points (`CGDisplayBounds`, the same space as pyautogui). The capture pixel size
    comes from `CGDisplayModeGetPixelWidth`.
- **Rounding is `floor(v + 0.5)` everywhere.** Never use the built-in `round()`. It does
  banker's rounding (4.5 → 4), and then the resize height and the two mapping directions
  disagree.
- **Clamp only in `image_to_screen`.** `screen_to_image` is unclamped on purpose: listings mark
  points outside the frame `(off-frame)` instead of moving them.
- **Windows DPI:** pyautogui's import calls `SetProcessDPIAware()`. After that, per-monitor
  awareness is refused. So `__main__.py` and `platform/win/desktop.py` call
  `enable_per_monitor_dpi_awareness()` BEFORE `import pyautogui`. `tests/test_win_dpi.py`
  locks that order on the AST.
- **Linux `monitor=0` = the X ROOT, sized by `xrandr --current` (`current W x H`).** NEVER size
  it with `xdotool getdisplaygeometry`. libxdo answers that from `XineramaQueryScreens()[0]`,
  which is only the FIRST head. On Xorg, Xinerama is always on through RandR emulation, so on a
  multi-head box the frame would be head 1 while scrot captures the whole root. xrandr
  (`x11-xserver-utils`) is therefore required for every Linux frame. A missing xrandr fails
  loudly; it never falls back to a wrong frame.
- **Windows `monitor=N`** must grab with `ImageGrab.grab(bbox=…, all_screens=True)`. Without
  `all_screens`, PIL crops the bbox out of a primary-only grab, and a non-primary monitor comes
  back black.
- **pyautogui does not clamp to the screen**: the clamp is commented out in 0.9.54. Negative
  virtual-screen coordinates reach `SetCursorPos` as they are. Our clamp is the only guard.
- **Optional points (Type, Scroll, drag start) go through `_optional_point(x, y)` (#31).**
  - The rule is `x or y`: a point is given unless BOTH are 0, the tools' default.
  - x=0 alone and y=0 alone are a real column and row. The point (0,0) itself cannot be
    targeted by these three; that is the documented trade-off.
  - Never write `if x and y`. Below the tool, a provider gets `None` for "no point" and
    checks `is not None`.
- **Tests:**
  - Tool-level tests run the real MCP tool functions on a fake X11 display, stubbing only
    `_run_x` and `_load_atspi`.
  - Windows/macOS providers are imported against fake `ctypes.windll` / pywin32 / `Quartz`
    modules. See `tests/test_provider_geometry.py`: `tool_test_support.purge_module`
    re-imports the provider fresh, and `conftest.win_session` is the shared Windows fixture.
